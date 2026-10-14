"""Explicit public projections of privileged state for the management panel."""
import hashlib
import ipaddress
import json
from pathlib import Path
import platform
import re
import socket

from .core import canonical, run
from .management_errors import ManagementError


def accounts():
    import pwd
    return sorted(p.pw_name for p in pwd.getpwall() if (p.pw_uid == 0 or 1000 <= p.pw_uid < 65534)
                  and p.pw_shell not in ('/usr/sbin/nologin','/sbin/nologin','/bin/false',''))[:50]


def ssh_details(account, context):
    from .host.ssh import _account, _context, _effective, _paths
    if account not in accounts():
        raise ManagementError('VALIDATION','请选择当前已有且可登录的账号。')
    if not isinstance(context,dict) or set(context) != {'addr','host','laddr','lport'}:
        raise ManagementError('VALIDATION','SSH 连接条件不完整。')
    try:
        ipaddress.ip_address(context['addr'])
        ipaddress.ip_address(context['laddr'])
        if not isinstance(context['host'],str) or not re.fullmatch(r'[A-Za-z0-9.:_-]{1,253}',context['host']):
            raise ValueError()
        if str(int(context['lport'])) != str(context['lport']) or not 1 <= int(context['lport']) <= 65535:
            raise ValueError()
    except (ValueError,TypeError):
        raise ManagementError('VALIDATION','请输入有效的来源 IP、目标 IP、来源主机名及 SSH 端口。') from None
    identity = _account(account,None)
    effective_context = _context(account,context)
    captured = {}
    def capture(command):
        captured['output'] = run(command,timeout=30)
        return captured['output']
    config, digest = _effective(effective_context,capture,'/usr/sbin/sshd')
    # Keep host.ssh's established plan projection unchanged. Derive display-only
    # policy from exactly the same sshd output used for its full-output digest.
    forwarding = {}
    for line in captured['output'].splitlines():
        parts = line.split(None,1)
        if len(parts) == 2 and parts[0] in ('disableforwarding','allowtcpforwarding'):
            if parts[0] in forwarding:
                raise ManagementError('VALIDATION','SSH 转发策略存在重复字段，请检查生效配置。')
            forwarding[parts[0]] = parts[1]
    allowed = None
    if set(forwarding) == {'disableforwarding','allowtcpforwarding'}:
        allowed = forwarding['disableforwarding'] == 'no' and forwarding['allowtcpforwarding'] in ('yes','local','all')
    paths = _paths(config,identity)
    return {'account':account,'paths':[{'id':hashlib.sha256(canonical([account,effective_context,digest,path])).hexdigest(),
                                     'label':path} for path in paths],
            'public_key_authentication':config.get('pubkeyauthentication') == 'yes',
            'tunnel_policy_allowed':allowed,'tunnel_verification':'unverified',
            'tunnel_note':'仅显示 sshd 转发策略；公钥的 no-port-forwarding / permitopen 限制与实际隧道仍需分别验证。',
            'verification':'unverified'}


def overview(config, revision, jobs, root):
    from .certificates import managed_acme
    from .site import validate_certificate
    server, sub = config['server'], config['subscription']
    domains = [server['site_domain'],server['subscription_domain']]
    try:
        certificate = {**validate_certificate(server['certificate_path'],server['key_path'],domains),
                       'status':'valid','error':None}
    except Exception:
        # The management surface must remain usable to repair expired/missing
        # certificates. Runtime validation still fails closed before activation.
        certificate = {'domains':domains,'not_after':None,'sha256':None,'status':'invalid',
                       'error':'证书不可用或校验失败，请检查有效期、域名与私钥匹配，并通过续期或导入修复。'}
    status = []
    for unit in ('proxyhub-proxy','proxyhub-nginx','proxyhub-publisher','proxyhub-manager','proxyhub-panel','proxyhub-renew.timer'):
        try:
            value = run(['systemctl','show',unit,'-p','ActiveState','--value'],timeout=5).strip()
        except Exception:
            value = 'unknown'
        status.append({'name':unit,'status':value})
    os_name = platform.system()
    try:
        values = dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
        os_name = values.get('PRETTY_NAME',os_name).strip('"')
    except OSError:
        pass
    total = None
    try:
        total = int(re.search(r'MemTotal:\s+(\d+)',Path('/proc/meminfo').read_text()).group(1))*1024
    except (OSError,AttributeError):
        pass
    defaults = {'addr':'','host':'','laddr':'','lport':'22'}
    source = root/'panel/ssh-context.json'
    if source.is_file():
        try:
            saved = json.loads(source.read_text(encoding='utf-8'))
            if isinstance(saved,dict) and set(saved) == set(defaults):
                defaults = saved
        except (OSError,ValueError):
            pass
    port = '' if server['public_port'] == 443 else ':'+str(server['public_port'])
    return {'revision':revision,
            'server':{'hostname':socket.gethostname(),'os':os_name,'architecture':platform.machine(),
                      'memory_total_bytes':total,'accounts':accounts(),'ssh_context':defaults},
            'website':{'id':'default','domain':server['site_domain'],'title':server['site_title'],'url':'https://'+server['site_domain']+port},
            'certificate':{'id':Path(server['certificate_path']).parent.name,'domains':certificate['domains'],
                           'not_after':certificate['not_after'],'sha256':certificate['sha256'],
                           'status':certificate['status'],'error':certificate['error'],
                           'automatic_renewal':managed_acme(server),'consumers':['网站 HTTPS','订阅 HTTPS','REALITY 本机握手目标（间接）']},
            'proxy':{'id':'local-reality','name':server['node_name'],'protocol':'VLESS REALITY / Vision',
                     'public_address':server['public_address'],'port':server['public_port'],'sni':server['site_domain'],
                     'target':'127.0.0.1:'+str(server['site_tls_port']),'verification':'请使用客户端验证当前连接；服务运行不等于真实代理请求通过'},
            'subscription':{'id':sub['id'],'name':sub['name'],'domain':server['subscription_domain'],
                            'update_interval_hours':sub['update_interval_hours'],'userinfo':sub['userinfo'],
                            'reset_day':sub.get('reset_day'),'reset_at':sub.get('reset_at'),
                            'overrides':config['overrides'],'traffic_source':'manual'},
            'services':status,'capabilities':[
                {'name':'单网站 / REALITY / 单订阅','available':True,'reason':'当前已接入的管理范围'},
                {'name':'自动流量计量与重置','available':False,'reason':'当前仅手工元数据，重置字段只作记录'},
                {'name':'多站点、内容上传、HY2、外部目标、规则管理','available':False,'reason':'按后续阶段实施，不影响现有节点'}],
            'recent_jobs':jobs.list()['jobs'][:5]}
