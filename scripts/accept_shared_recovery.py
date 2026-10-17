"""Opt-in VPS health diagnostics; private correlated evidence, no secret stdout."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--diagnose', type=int, default=100)
    parser.add_argument('--same-client-control', action='store_true')
    args = parser.parse_args()
    from proxyhub import deployment as d
    root = args.evidence_dir
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise SystemExit('Evidence directory must not be a symlink')
    root.chmod(0o700)
    config = json.loads((d._current_revision()/'config.json').read_text())
    run_id = secrets.token_hex(8)
    context = {'run_id':run_id,'transaction':None,'stage':'stable-direct-hy2',
               'member':'local-hy2','protocol':'hysteria2','attempt':0}
    output = root/('probes-'+run_id+'.jsonl')
    def event(kind, **fields):
        record = {'utc':datetime.now(timezone.utc).isoformat(),'monotonic_ns':time.monotonic_ns(),
                  **context,'event':kind,**fields}
        with output.open('a', encoding='utf-8') as stream:
            os.chmod(output,0o600)
            stream.write(json.dumps(record,ensure_ascii=False)+'\n')
    original_probe, original_popen = d._proxy_probe, subprocess.Popen
    original_begin = d.http.client.HTTPResponse.begin
    original_connect = d.socket.create_connection
    log_path = None
    process = None
    proxy_address = None
    request_bytes = None
    request_attempt = 0
    def popen(command, **kwargs):
        nonlocal log_path, process
        if Path(command[0]).name!='mihomo':
            return original_popen(command,**kwargs)
        path = Path(command[command.index('-f')+1])
        client = json.loads(path.read_text())
        client['log-level'] = 'debug'
        path.write_text(json.dumps(client))
        log_path = root/('client-'+context['probe_id']+'.log')
        with log_path.open('wb') as log:
            os.chmod(log_path,0o600)
            kwargs.update(stdout=log,stderr=subprocess.STDOUT)
            process = original_popen(command,**kwargs)
        event('client_spawned',pid=process.pid,phase='client-start')
        return process
    def begin(response, *values, **options):
        try:
            result = original_begin(response,*values,**options)
        except Exception as error:
            event('proxy_response_failed',error_class=type(error).__name__,error=redact(str(error)))
            raise
        body = redact(response.read(4096).decode('utf-8','replace')) if response.status!=404 else None
        event('proxy_http_response',status=response.status,error_body=body,
              request_attempt=request_attempt,phase='authenticated-request')
        if response.status==502 and args.same_client_control:
            for index in range(1,6):
                with original_connect(proxy_address,timeout=5) as stream:
                    stream.sendall(request_bytes)
                    control=d.http.client.HTTPResponse(stream)
                    original_begin(control)
                    event('same_client_control',request=index,status=control.status,pid=process.pid)
                    control.close()
                    if control.status==404:break
        return result
    def connect(address, *values, **options):
        nonlocal proxy_address, request_attempt
        proxy_address=address
        request_attempt+=1
        try:
            stream=original_connect(address,*values,**options)
        except OSError as error:
            event('client_connect_failed',error_class=type(error).__name__,errno=error.errno,
                  request_attempt=request_attempt,phase='client-listener')
            raise
        event('client_connected',self_connected=stream.getsockname()==stream.getpeername())
        class TracedSocket:
            def __getattr__(self,key):return getattr(stream,key)
            def __enter__(self):return self
            def __exit__(self,kind,error,tb):
                if error:event('socket_context_failed',error_class=kind.__name__,error=redact(str(error)),errno=getattr(error,'errno',None))
                return stream.__exit__(kind,error,tb)
            def settimeout(self,value):
                stream.settimeout(value);event('client_timeout_set')
            def sendall(self,data):
                nonlocal request_bytes
                request_bytes=data
                stream.sendall(data);event('request_sent',bytes=len(data))
            def makefile(self,*args,**kwargs):
                result=stream.makefile(*args,**kwargs);event('response_reader_created');return result
        return TracedSocket()
    secrets_to_hide = []
    def visit(value, key=''):
        if isinstance(value,dict):
            for child,item in value.items():visit(item,child)
        elif isinstance(value,list):
            for item in value:visit(item,key)
        elif isinstance(value,str) and key in {'uuid','password','private_key','public_key','short_id','token','public_address','site_domain','subscription_domain','server_name','sni'}:
            secrets_to_hide.append(value)
    visit(config)
    def redact(text):
        for value in sorted(set(secrets_to_hide),key=len,reverse=True):
            if value:text=text.replace(value,'<REDACTED>')
        text=re.sub(r'\b\d{1,3}(?:\.\d{1,3}){3}\b','<IP>',text)
        text=re.sub(r'\b[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,}\b','<HOST>',text)
        return text[-6000:]
    def probe(c, outbound, fingerprint=None):
        nonlocal request_attempt, process, log_path
        request_attempt=0;process=None;log_path=None
        context.update(probe_id=secrets.token_hex(6),verification='pin' if fingerprint else 'pki')
        start=time.monotonic();event('probe_started')
        try:
            original_probe(c,outbound,fingerprint)
        except Exception as error:
            event('probe_failed',error_class=type(error).__name__,elapsed_ms=round((time.monotonic()-start)*1000,3),
                  error=redact(str(error)),client_returncode=process.returncode if process else None,
                  client_log=log_path.name if log_path else None)
            if log_path and log_path.exists():
                clean=redact(log_path.read_text(errors='replace'))
                log_path.write_text(clean,encoding='utf-8')
                event('client_failure_diagnostics',text=clean)
            raise
        else:
            event('probe_passed',elapsed_ms=round((time.monotonic()-start)*1000,3))
            if log_path:log_path.unlink(missing_ok=True)
    d._proxy_probe=probe;d.subprocess.Popen=popen;d.http.client.HTTPResponse.begin=begin;d.socket.create_connection=connect
    failures=0
    try:
        for attempt in range(1,args.diagnose+1):
            context['attempt']=attempt
            try:d._hy2_health(config,d._current_revision())
            except Exception:
                failures+=1
                if failures>=3:break
    finally:
        d._proxy_probe=original_probe;d.subprocess.Popen=original_popen;d.http.client.HTTPResponse.begin=original_begin;d.socket.create_connection=original_connect
    result={'run_id':run_id,'completed_pairs':context['attempt'],'failures':failures,'evidence':output.name,
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'probe_source_sha256':hashlib.sha256(Path(d.__file__).read_bytes()).hexdigest()}
    (root/('diagnosis-'+run_id+'.json')).write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))
    return int(bool(failures))


if __name__=='__main__':
    raise SystemExit(main())
