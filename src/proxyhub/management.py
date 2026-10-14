"""Typed management operations shared by the panel and namespaced RPC CLI."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import re
import tempfile

from . import deployment
from .core import OperationError, atomic_write
from .jobs import Jobs
from .management_errors import ManagementError, public_error
from .management_read import overview, ssh_details
from .state import StateStore

ROOT = Path('/var/lib/proxyhub')


def shape(value, fields, required=None):
    required = fields if required is None else required
    if not isinstance(value,dict) or set(value)-set(fields) or not set(required)<=set(value):
        raise ManagementError('VALIDATION','请求字段缺失或包含不支持的字段。')


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{32}',value):
        raise ManagementError('VALIDATION','计划或任务编号无效。')
    return value


class OperationService:
    def __init__(self, root=ROOT, *, jobs=None):
        self.root = Path(root)
        self.jobs = jobs or Jobs(self.root/'plans')

    @contextmanager
    def state(self):
        store = StateStore(self.root/'plans')
        try:
            yield store
        finally:
            store.db.close()

    def active(self):
        current = deployment._current_revision()
        if current is None:
            raise ManagementError('SETUP_REQUIRED','尚无已部署配置。请先通过 CLI 完成最小部署，再使用面板。')
        return current, json.loads((current/'config.json').read_text(encoding='utf-8'))

    def snapshot(self, store, *, reject_pending=False):
        """Copy config and revision under the activation lock; probe after release."""
        with store.exclusive():
            pending = store.pending()
            if pending and reject_pending:
                raise ManagementError('BUSY','存在尚未提交的部署，请先检查任务并恢复。')
            if pending and len(pending) == 1 and pending[0]['details'].get('previous'):
                # During recovery show the last committed state, never promote
                # a switched-but-uncommitted current link to an active resource.
                directory = deployment._managed_revision(pending[0]['details']['previous'])
                config = json.loads((directory/'config.json').read_text(encoding='utf-8'))
            else:
                directory, config = self.active()
            return directory, config, store.revision, bool(pending)

    def handle(self, operation, payload):
        try:
            return self._handle(operation,deepcopy(payload))
        except Exception as exc:
            error = public_error(exc)
            raise ManagementError(error['code'],error['message']) from None

    def _handle(self, operation, payload):
        if operation == 'overview':
            shape(payload,())
            with self.state() as store:
                _, config, revision, pending = self.snapshot(store)
                result = overview(config,revision,self.jobs,self.root)
                result['operation_pending'] = pending
                return result
        if operation == 'jobs.list':
            shape(payload,())
            return self.jobs.list()
        if operation == 'job.get':
            shape(payload,('job_id',))
            return self.jobs.get(identifier(payload['job_id']))
        if operation == 'ssh.inspect':
            shape(payload,('account','context'))
            return ssh_details(payload['account'],payload['context'])
        if operation == 'subscription.reveal':
            shape(payload,('format',))
            with self.state() as store:
                directory, config, _, _ = self.snapshot(store,reject_pending=True)
            if payload['format'] == 'yaml':
                return {'yaml':(directory/'client.yaml').read_text(encoding='utf-8')}
            if payload['format'] != 'links':
                raise ManagementError('VALIDATION','仅支持链接或 YAML 的显式查看。')
            s, sub = config['server'], config['subscription']
            suffix = '' if s['public_port'] == 443 else ':'+str(s['public_port'])
            base = 'https://'+s['subscription_domain']+suffix+'/sub/'+sub['token']
            return {'links':{'mihomo':base+'/mihomo.yaml','raw':base+'/raw'}}
        if operation == 'plan.create':
            shape(payload,('kind','base_revision','values'))
            if type(payload['base_revision']) is not int or payload['base_revision'] < 0:
                raise ManagementError('VALIDATION','配置版本必须是非负整数。')
            if payload['kind'] not in ('ssh.append','website.update','proxy.update','subscription.update','certificate.import','certificate.renew'):
                raise ManagementError('UNSUPPORTED','当前不支持此变更。')
            return self.jobs.submit('plan.create:'+payload['kind'],lambda:self.create_plan(**payload))
        if operation == 'plan.apply':
            shape(payload,('plan_id',))
            identity = identifier(payload['plan_id'])
            return self.jobs.submit('plan.apply',lambda:self.apply_plan(identity))
        if operation == 'deployment.recover':
            shape(payload,())
            return self.jobs.submit('deployment.recover',self.recover)
        raise ManagementError('UNSUPPORTED','操作未列入允许的管理接口。')

    def create_plan(self, kind, base_revision, values):
        with self.state() as store:
            with store.exclusive():
                if store.revision != base_revision or store.pending():
                    raise ManagementError('STALE_REVISION','配置已变化或正在恢复，请刷新后重新预览。')
                if kind != 'ssh.append':
                    directory, config = self.active()
            if kind == 'ssh.append':
                return self._ssh_plan(store,base_revision,values)
            before = deepcopy(config)
            warnings, changes, impact = [], [], ['将重新加载网站、代理与订阅服务，现有连接可能短暂中断。']
            if kind == 'website.update':
                shape(values,('site_title',))
                config['server']['site_title'] = values['site_title']
                changes = [{'label':'网站标题','before':before['server']['site_title'],'after':values['site_title']}]
            elif kind == 'proxy.update':
                shape(values,('node_name','public_address'))
                for key,label in (('node_name','节点名称'),('public_address','客户端连接地址')):
                    config['server'][key] = values[key]
                    changes.append({'label':label,'before':before['server'][key],'after':values[key]})
                warnings = ['节点凭据、监听端口、SNI 与握手目标保持原值；客户端需要刷新订阅。']
            elif kind == 'subscription.update':
                shape(values,('name','update_interval_hours','userinfo','reset_day','reset_at','overrides'))
                if not isinstance(values['overrides'],dict):
                    raise ManagementError('VALIDATION','客户端覆盖必须是 JSON 对象。')
                for key in ('name','update_interval_hours','userinfo','reset_day','reset_at'):
                    config['subscription'][key] = values[key]
                    changes.append({'label':{'name':'订阅名称','update_interval_hours':'刷新间隔（小时）','userinfo':'手工流量 / 到期信息','reset_day':'重置日记录','reset_at':'重置时间记录'}[key],
                                    'before':before['subscription'].get(key),'after':values[key]})
                config['overrides'] = values['overrides']
                changes.append({'label':'客户端配置覆盖','before':before['overrides'],'after':values['overrides']})
                impact = ['仅重新加载订阅发布内容，保持网站和代理进程运行。']
                warnings = ['流量为手工填写；重置字段仅作记录，不会执行计量归零。','空流量对象表示未知；刷新间隔、到期和重置时间分别保存。']
            elif kind == 'certificate.renew':
                shape(values,())
                from .certificates import managed_acme
                if not managed_acme(config['server']):
                    raise ManagementError('UNSUPPORTED','此证书由外部签发流程维护，请导入更新后的证书。')
                identity = store.plan('panel-renew',{'current':directory.name},expected_revision=base_revision)
                return self.preview(identity,kind,base_revision,[],['检查 HTTP-01 续期；仅证书变化时加载新证书。'],['必须保持公网 TCP 80 可达；失败保留有效旧配置。'])
            elif kind == 'certificate.import':
                shape(values,('certificate_pem','private_key_pem'))
                for key in values:
                    if not isinstance(values[key],str) or not 1<=len(values[key].encode())<=100000:
                        raise ManagementError('VALIDATION','证书和私钥需要有效 PEM，单个输入不得超过 100KB。')
                from .certificates import import_pair
                with tempfile.TemporaryDirectory(prefix='.panel-import-',dir=self.root/'certificates') as temporary:
                    cert, key = Path(temporary)/'cert.pem', Path(temporary)/'key.pem'
                    atomic_write(cert,values['certificate_pem'].encode(),0o600)
                    atomic_write(key,values['private_key_pem'].encode(),0o600)
                    result = import_pair(cert,key,[config['server']['site_domain'],config['server']['subscription_domain']])
                config['server'].update(certificate_path=result['certificate_path'],key_path=result['key_path'])
                changes = [{'label':'证书 SHA256','before':'当前绑定证书','after':result['sha256']},
                           {'label':'有效期至','before':None,'after':result['not_after']}]
                from .certificates import managed_acme
                ownership = ('此证书已有受管 HTTP-01 签发记录，保留已验证的自动续期归属。'
                             if managed_acme(config['server']) else '导入证书由原签发流程维护，不接管自动续期。')
                warnings = [ownership,'私钥不进入预览或任务结果。']
            result = deployment.prepare(store,config,expected_revision=base_revision,publish_only=kind=='subscription.update')
            return self.preview(result['plan_id'],kind,base_revision,changes,impact,warnings)

    def _ssh_plan(self, store, base_revision, values):
        from .host.ssh import plan_append
        shape(values,('account','public_key','path_id','context'))
        inspected = ssh_details(values['account'],values['context'])
        target = next((entry['label'] for entry in inspected['paths'] if entry['id']==values['path_id']),None)
        if target is None:
            raise ManagementError('STALE_REVISION','授权路径无效或已变化，请重新检查 SSH 连接条件。')
        key = values['public_key']
        if not isinstance(key,str) or len(key.encode()) > 16384:
            raise ManagementError('VALIDATION','请提供不超过 16KB 的一行 SSH 公钥。')
        key = key.removesuffix('\n').removesuffix('\r')
        plan = plan_append(values['account'],key,values['context'],selected_path=target)
        identity = store.plan('ssh-append',plan,expected_revision=base_revision)
        return self.preview(identity,'ssh.append',base_revision,
                            [{'label':'账号','before':values['account'],'after':values['account']},
                             {'label':'授权文件','before':target,'after':target},
                             {'label':'公钥指纹','before':None,'after':plan['fingerprint']},
                             {'label':'处理方式','before':None,'after':'已存在，无需追加' if plan['status']=='noop' else '保留原内容并追加'}],
                            ['不修改 SSH 端口、密码登录、root 登录或防火墙。'],
                            ['写入后仍标记为未验证；请用第二个 SSH 会话验证该公钥。'])

    @staticmethod
    def preview(identity,kind,revision,changes,impact,warnings):
        return {'plan_id':identity,'kind':kind,'base_revision':revision,
                'summary':{'ssh.append':'追加 SSH 公钥','website.update':'更新网站标题','proxy.update':'更新 REALITY 节点',
                           'subscription.update':'发布订阅变更','certificate.import':'替换绑定证书','certificate.renew':'检查并更新证书'}[kind],
                'changes':changes,'impact':impact,'warnings':warnings}

    def apply_plan(self, identity):
        with self.state() as store:
            kind,payload = store.get(identity)
            if kind == 'deployment':
                result = deployment.apply(store,identity)
                return result
            if kind == 'ssh-append':
                from .host.ssh import apply_append
                with store.exclusive():
                    _,payload = store.get(identity)
                    result = apply_append(payload)
                    store.commit(identity)
                return {key:result[key] for key in ('status','verification','fingerprint')}
            if kind == 'panel-renew':
                from .certificates import issue, managed_acme
                with store.exclusive():
                    _,payload = store.get(identity)
                    child = store.renewal_child(identity)
                    base = store.revision
                    directory, config = self.active()
                if child is not None:
                    return deployment.apply(store,child)
                server = config['server']
                if directory.name != payload['current'] or not managed_acme(server):
                    raise ManagementError('STALE_REVISION','证书或绑定关系已变化，请重新预览。')
                certificate = issue([server['site_domain'],server['subscription_domain']],standalone=False)
                if store.revision != base or self.active()[0] != directory:
                    raise ManagementError('STALE_REVISION','续期期间配置已变化，已保留当前部署，请重新预览。')
                if certificate['certificate_path'] == server['certificate_path']:
                    with store.exclusive():
                        store.get(identity)
                        if self.active()[0] != directory:
                            raise ManagementError('STALE_REVISION','证书绑定已变化，请重新预览。')
                        store.commit(identity)
                    return {'status':'unchanged','message':'证书未到更新期，当前服务保持运行。'}
                server.update(certificate_path=certificate['certificate_path'],key_path=certificate['key_path'])
                prepared = deployment.prepare(store,config,expected_revision=base)
                store.link_renewal(identity,prepared['plan_id'])
                return deployment.apply(store,prepared['plan_id'])
            raise ManagementError('UNSUPPORTED','此计划不属于已接入的操作。')

    def recover(self):
        with self.state() as store:
            return deployment.recover(store)

    def close(self):
        self.jobs.close()
