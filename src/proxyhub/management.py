"""Typed management operations shared by the panel and namespaced RPC CLI."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import re
import secrets

from . import deployment
from . import management_web
from .core import OperationError
from .jobs import Jobs
from .management_errors import ManagementError, public_error
from .management_read import overview, ssh_details
from .management_read import web_resources
from .state import StateStore
from .websites import normalize_web, get_site, get_certificate, sync_default

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
    def __init__(self, root=ROOT, *, jobs=None, synchronous=False):
        self.root = Path(root)
        self.jobs = None if synchronous else (jobs or Jobs(self.root/'plans'))

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
        if operation == 'website.content.versions':
            shape(payload,('site_id',),())
            with self.state() as store:
                _, config, _, _ = self.snapshot(store,reject_pending=True)
                return deployment.content_versions(store,config,payload.get('site_id','default'))
        if operation == 'web.resources':
            shape(payload,())
            with self.state() as store:
                _, config, revision, _ = self.snapshot(store,reject_pending=True)
                return {'revision':revision,**web_resources(config)}
        if operation == 'website.content.preview':
            shape(payload,('plan_id',))
            with self.state() as store:
                with store.exclusive():
                    kind, candidate = store.get(identifier(payload['plan_id']))
                    if kind != 'deployment' or candidate.get('website_change') is not True:
                        raise ManagementError('VALIDATION','此计划不是网站内容变更。')
                    directory = deployment._managed_revision(deployment.ROOT/'revisions'/candidate['revision'])
                    site_id = candidate.get('website_id','default')
                    files = deployment._read_site(directory,deployment._site_manifest(candidate,site_id),site_id)
            from .site_content import preview_html
            try:
                html = preview_html(files)
            except ValueError:
                raise ManagementError('PREVIEW_UNAVAILABLE','静态预览超过大小或结构限制，无法显示；候选未改变，仍可确认应用后通过公网网站检查。') from None
            return {'html':html,'file_count':len(files),'total_bytes':sum(map(len,files.values()))}
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
            if payload['kind'] not in management_web.KINDS | {'ssh.append','website.update','website.content.upload','website.content.restore','proxy.update','subscription.update','certificate.issue','certificate.renew'}:
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
            if kind in management_web.KINDS:
                result = management_web.change(config,kind,values,self.root)
                prepared = deployment.prepare(store,result['config'],expected_revision=base_revision,web_only=True)
                return self.preview(prepared['plan_id'],kind,base_revision,result['changes'],
                                    ['重新加载网站和订阅发布器，保留现有代理进程。'],result['warnings'])
            if kind == 'certificate.issue' or (kind == 'certificate.renew' and ('web' in config or values)):
                return self._acme_plan(store,directory,config,kind,base_revision,values)
            before = deepcopy(config)
            site_files = None
            site_id = 'default'
            warnings, changes, impact = [], [], ['将重新加载网站、代理与订阅服务，现有连接可能短暂中断。']
            if kind == 'website.update':
                shape(values,('site_title','site_id'),('site_title',))
                site_id = values.get('site_id','default')
                config['web'] = normalize_web(config)
                selected = get_site(config['web'],site_id)
                if selected['content_version'] is not None:
                    raise ManagementError('VALIDATION','上传网站的标题由 HTML 管理；如需默认页面，请先恢复默认模板。')
                changes = [{'label':'网站标题','before':selected['title'],'after':values['site_title']}]
                selected['title'] = values['site_title']
                config = sync_default(config)
            elif kind == 'website.content.upload':
                shape(values,('archive_base64','site_id'),('archive_base64',))
                site_id = values.get('site_id','default')
                selected = get_site(normalize_web(config),site_id)
                from .site_content import decode_archive
                site_files = decode_archive(values['archive_base64'])
                changes = [{'label':'网站内容','before':selected['content_version'] or '默认模板','after':'新上传的静态网站'},
                           {'label':'文件数','before':None,'after':len(site_files)},
                           {'label':'内容字节数','before':None,'after':sum(map(len,site_files.values()))}]
                warnings = ['仅更换静态内容，沿用当前域名、证书、REALITY 与订阅配置。','隔离预览禁用脚本和外部资源；应用后请验证公网网站和真实代理请求。']
            elif kind == 'website.content.restore':
                shape(values,('version_id','site_id'),('version_id',))
                site_id = values.get('site_id','default')
                config['web'] = normalize_web(config)
                selected = get_site(config['web'],site_id)
                version = values['version_id']
                if version == 'template':
                    selected['content_version'] = None
                else:
                    deployment.content_version(store,version,site_id)
                    selected['content_version'] = version
                changes = [{'label':'网站内容版本','before':get_site(normalize_web(before),site_id)['content_version'] or '默认模板','after':version}]
                config = sync_default(config)
                warnings = ['恢复仅改变内容，沿用当前域名、证书、REALITY 与订阅配置。','恢复默认模板时使用当前网站标题，不恢复历史基础配置。']
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
            options = {'expected_revision':base_revision,'publish_only':kind=='subscription.update'}
            if kind.startswith('website.'):
                options.update(site_id=site_id,web_only=True)
                impact = ['重新加载网站和订阅发布器，保留现有代理进程。']
            if kind.startswith('website.content.'):
                options.update(site_files=site_files,website_change=True)
            result = deployment.prepare(store,config,**options)
            return self.preview(result['plan_id'],kind,base_revision,changes,impact,warnings)

    def _acme_plan(self, store, directory, config, kind, base_revision, values):
        from .certificates import acme_source
        from .proxy import _text, validate_domain
        web = normalize_web(config)
        if kind == 'certificate.issue':
            shape(values,('name','domains'))
            if len(web['certificates']) >= 32:
                raise ManagementError('VALIDATION','证书绑定数量已达到上限。')
            if not isinstance(values['domains'],list) or not 1 <= len(values['domains']) <= 10:
                raise ManagementError('VALIDATION','请输入 1–10 个有效域名。')
            domains = [validate_domain(domain) for domain in values['domains']]
            if len(set(domains)) != len(domains):
                raise ManagementError('VALIDATION','域名不得重复。')
            identity, name = secrets.token_hex(16), _text(values['name'],'certificate name')
            lineage = 'proxyhub-'+identity
            action = 'issue'
        else:
            shape(values,('certificate_id',),())
            identity = values.get('certificate_id',get_site(web,'default')['certificate_id'])
            binding = get_certificate(web,identity)
            source = acme_source(binding)
            if source is None:
                raise ManagementError('UNSUPPORTED','此绑定由外部流程维护，请导入更新后的证书。')
            name, domains, lineage = binding['name'], source['domains'], source['lineage']
            action = 'renew'
        for domain in domains:
            management_web.check_domain(config['server'],domain)
        payload = {'current':directory.name,'action':action,'certificate_id':identity,
                   'name':name,'domains':domains,'lineage':lineage}
        plan = store.plan('panel-renew',payload,expected_revision=base_revision)
        return self.preview(plan,kind,base_revision,
                            [{'label':'证书绑定','before':None if action=='issue' else identity,'after':identity},
                             {'label':'域名','before':None,'after':domains}],
                            ['申请成功后才保存绑定；证书变化时重载其消费者并核对实际指纹，保留代理进程。'],
                            ['HTTP-01 要求所有 A/AAAA 正确指向本机且公网 TCP 80 可达。','签发失败不会替换当前有效配置；手工证书不会自动接管续期。'])

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
                           'website.create':'创建网站','website.enable':'启用网站','website.disable':'停用网站','website.delete':'删除网站',
                           'website.domain.bind':'绑定网站域名','website.domain.unbind':'解绑网站域名','website.certificate.bind':'更换网站证书绑定',
                           'certificate.add':'新增证书绑定','certificate.issue':'申请新证书绑定','certificate.delete':'删除证书绑定',
                           'website.content.upload':'发布网站内容','website.content.restore':'恢复网站内容',
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
                if 'action' in payload:
                    return self._apply_acme(store,identity)
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

    def _apply_acme(self, store, identity):
        from .certificates import issue, acme_source
        with store.exclusive():
            _, payload = store.get(identity)
            child = store.renewal_child(identity)
            base = store.revision
            directory, config = self.active()
        if child is not None:
            return deployment.apply(store,child)
        if directory.name != payload['current']:
            raise ManagementError('STALE_REVISION','证书或绑定关系已变化，请重新预览。')
        web = normalize_web(config)
        binding = None
        if payload['action']=='renew':
            binding = get_certificate(web,payload['certificate_id'])
            source = acme_source(binding)
            if source is None or source['lineage'] != payload['lineage'] or source['domains'] != payload['domains']:
                raise ManagementError('STALE_REVISION','证书的续期归属已变化，请重新预览。')
        elif payload['action']!='issue':
            raise ManagementError('UNSUPPORTED','不支持此证书任务。')
        for domain in payload['domains']:
            management_web.check_domain(config['server'],domain)
        certificate = issue(payload['domains'],standalone=False,lineage=payload['lineage'])
        if store.revision != base or self.active()[0] != directory:
            raise ManagementError('STALE_REVISION','签发期间配置已变化，已保留当前部署，请重新预览。')
        if binding is not None and certificate['certificate_path']==binding['certificate_path']:
            with store.exclusive():
                store.get(identity)
                if self.active()[0] != directory:
                    raise ManagementError('STALE_REVISION','证书绑定已变化，请重新预览。')
                store.commit(identity)
            return {'status':'unchanged','certificate_id':binding['id'],'message':'证书未到更新期，当前服务保持运行。'}
        if binding is None:
            binding = {'id':payload['certificate_id'],'name':payload['name'],'domains':payload['domains']}
            web['certificates'].append(binding)
        binding.update(certificate_path=certificate['certificate_path'],key_path=certificate['key_path'])
        config['web'] = web
        config = sync_default(config)
        prepared = deployment.prepare(store,config,expected_revision=base,web_only=True)
        store.link_renewal(identity,prepared['plan_id'])
        return deployment.apply(store,prepared['plan_id'])

    def recover(self):
        with self.state() as store:
            return deployment.recover(store)

    def close(self):
        if self.jobs is not None:
            self.jobs.close()
