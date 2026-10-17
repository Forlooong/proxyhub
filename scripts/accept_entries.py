"""Opt-in public-entry acceptance using the original subscription and loopback echoes.

Run on the personal computer while the controlled VPS TCP/UDP echoes are active.
No system proxy is changed. Subscription credentials exist only in a temporary
client directory; output contains only roles, protocols, timings and error types.
The VPS --serve-mode starts only loopback echoes and retains two REALITY streams.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import uuid


def receive(stream, size):
    data = b''
    while len(data) < size:
        part = stream.recv(size - len(data))
        if not part:
            raise ConnectionError('closed')
        data += part
    return data


def address(host, port):
    try:
        parsed = ipaddress.ip_address(host)
        value = bytes([1 if parsed.version == 4 else 4]) + parsed.packed
    except ValueError:
        encoded = host.encode('ascii')
        if not 1 <= len(encoded) <= 255:
            raise ValueError('address') from None
        value = bytes([3, len(encoded)]) + encoded
    return value + struct.pack('!H', port)


def read_address(stream, kind):
    if kind == 1:
        host = socket.inet_ntop(socket.AF_INET, receive(stream, 4))
    elif kind == 4:
        host = socket.inet_ntop(socket.AF_INET6, receive(stream, 16))
    elif kind == 3:
        host = receive(stream, receive(stream, 1)[0]).decode('ascii')
    else:
        raise ValueError('address kind')
    return host, struct.unpack('!H', receive(stream, 2))[0]


def association(port, command, target):
    stream = socket.create_connection(('127.0.0.1', port), timeout=10)
    try:
        stream.sendall(b'\x05\x01\x00')
        if receive(stream, 2) != b'\x05\x00':
            raise ConnectionError('authentication')
        stream.sendall(bytes([5, command, 0]) + address(*target))
        head = receive(stream, 4)
        if head[:3] != b'\x05\x00\x00':
            raise ConnectionError('association')
        bound = read_address(stream, head[3])
        return stream, bound
    except BaseException:
        stream.close()
        raise


def tcp_echo(port, target):
    stream, _ = association(port, 1, target)
    with stream:
        payload = secrets.token_bytes(32)
        stream.sendall(payload)
        if receive(stream, len(payload)) != payload:
            raise ValueError('echo')


def udp_echo(port, target, supported, diagnostic=None):
    stream, relay = association(port, 3, ('0.0.0.0', 0))
    with stream, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
        datagram.settimeout(10 if supported else 2)
        payload = secrets.token_bytes(32)
        datagram.sendto(b'\x00\x00\x00' + address(*target) + payload, relay)
        if diagnostic is not None:
            diagnostic('udp_sent', sequence_sha256=hashlib.sha256(payload).hexdigest(),
                       bytes=len(payload), association_port=datagram.getsockname()[1])
        try:
            response, _ = datagram.recvfrom(4096)
        except TimeoutError:
            if diagnostic is not None:
                diagnostic('udp_timeout')
            if supported:
                raise
            return
        if diagnostic is not None:
            diagnostic('udp_received', bytes=len(response))
        if not supported:
            raise ValueError('unsupported UDP forwarded')
        if response[:3] != b'\x00\x00\x00':
            raise ValueError('datagram header')
        kind = response[3]
        offset = 8 if kind == 1 else 20 if kind == 4 else 5 + response[4]
        if kind not in (1, 3, 4) or response[offset + 2:] != payload:
            raise ValueError('datagram echo')


def target(value):
    host, port = value.rsplit(':', 1)
    return host.strip('[]'), int(port)


def stop_client(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def client_configuration(outbound, port):
    return {'mixed-port': port, 'allow-lan': False, 'bind-address': '127.0.0.1',
            'mode': 'rule', 'log-level': 'silent',
            'proxies': [{**deepcopy(outbound), 'name': 'ENTRY'}],
            'rules': ['MATCH,ENTRY', 'MATCH,REJECT']}


def serve(args):
    """VPS-only loopback echoes and two uninterrupted REALITY TCP streams."""
    from proxyhub import deployment, reality

    class EchoTCP(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                while data := self.request.recv(4096):
                    self.request.sendall(data)
            except OSError:
                pass

    class EchoUDP(socketserver.BaseRequestHandler):
        def handle(self):
            data, listener = self.request
            listener.sendto(data, self.client_address)

    class TCPServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    args.work.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.work, 0o700)
    events = args.work / 'retention-events.jsonl'
    ready_path = args.work / 'retention-ready.json'
    stop_path = args.work / 'retention-stop'
    summary_path = args.work / 'retention-summary.json'
    if ready_path.exists() or stop_path.exists() or summary_path.exists():
        raise ValueError('retention work directory already used')
    run = args.run or uuid.uuid4().hex
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    stopped = threading.Event()
    lock = threading.Lock()
    clients, workers, listeners, states = [], [], [], []
    temporary = None
    summary = {'schema_version': 1, 'run': run, 'boot_id': boot, 'helper_pid': os.getpid(),
               'phase': 'retention', 'completed': False, 'entries': states}
    started = time.monotonic()

    def emit(phase, **details):
        event = {'utc': datetime.now(timezone.utc).isoformat(), 'monotonic_ns': time.monotonic_ns(),
                 'run': run, 'boot_id': boot, 'helper_pid': os.getpid(), 'phase': phase, **details}
        with lock, events.open('a', encoding='utf-8') as output:
            output.write(json.dumps(event) + '\n')

    def save(path, value):
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
        temporary.replace(path)

    def retain(state, port, first_success):
        stream = None
        try:
            stream, _ = association(port, 1, ('127.0.0.1', 18992))
            stream.settimeout(3)
            state['connections'] = 1
            emit('stream_connected', **state)
            sequence = 0
            while not stopped.is_set():
                sequence += 1
                before = time.monotonic()
                payload = struct.pack('!Q', sequence) + secrets.token_bytes(24)
                stream.sendall(payload)
                if receive(stream, len(payload)) != payload:
                    raise ValueError('echo mismatch')
                state['success'] += 1
                emit('retention_ping', **state, sequence=sequence, success_result=True,
                     elapsed_ms=round((time.monotonic()-before)*1000))
                first_success.set()
                stopped.wait(.5)
        except Exception as exc:
            state['failures'] += 1
            state['interruptions'] += int(state['connections'] > 0)
            emit('retention_failure', **state, success_result=False, error_class=type(exc).__name__)
        finally:
            if stream is not None:
                stream.close()

    old_signals = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT):
            old_signals[signum] = signal.signal(signum, lambda *_: stopped.set())
        config_path = args.config or deployment.ROOT / 'current/config.json'
        config = json.loads(config_path.read_text(encoding='utf-8'))
        entries = reality.public_resources(config)['endpoints']
        if len(entries) != 2:
            raise ValueError('expected two REALITY entries')
        primary = deployment._generated_client(config)['proxies'][0]
        if primary['type'] != 'vless' or not primary.get('reality-opts'):
            raise ValueError('unexpected primary entry')
        executable = str((args.mihomo or deployment.BIN / 'mihomo').resolve())
        version = subprocess.run([executable, '-v'], capture_output=True, check=True,
                                 timeout=10).stdout.decode('utf-8')
        if 'v1.19.32 linux amd64' not in version:
            raise ValueError('unexpected retention client version')
        summary['client'] = {'version': '1.19.32', 'platform': 'linux-amd64',
                             'sha256': hashlib.sha256(Path(executable).read_bytes()).hexdigest()}
        units = deployment._reality_units(config)
        emit('starting', client_version='1.19.32', echo_bind='loopback_only')
        for server_class, bind, handler in (
            (TCPServer, ('127.0.0.1', 18992), EchoTCP),
            (socketserver.ThreadingUDPServer, ('127.0.0.1', 18993), EchoUDP),
        ):
            listener = server_class(bind, handler)
            listeners.append(listener)
            threading.Thread(target=listener.serve_forever, daemon=True).start()
        temporary = tempfile.TemporaryDirectory(prefix='proxyhub-reality-retention-')
        try:
            prepared = []
            for index, endpoint in enumerate(entries):
                role = 'reality-primary' if endpoint['primary'] else 'reality-retained'
                service_pid = int(subprocess.run(
                    ['systemctl', 'show', units[endpoint['id']], '--property=MainPID', '--value'],
                    capture_output=True, text=True, check=True, timeout=5).stdout.strip())
                with socket.socket() as available:
                    available.bind(('127.0.0.1', 0))
                    port = available.getsockname()[1]
                directory = Path(temporary.name) / str(index)
                directory.mkdir(mode=0o700)
                path = directory / 'client.json'
                outbound = {**deepcopy(primary), 'server': '127.0.0.1', 'servername': endpoint['sni']}
                path.write_text(json.dumps(client_configuration(outbound, port)), encoding='utf-8')
                process = subprocess.Popen([executable, '-d', str(directory), '-f', str(path)],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                clients.append(process)
                for _ in range(50):
                    if process.poll() is not None:
                        raise ChildProcessError('retention client exited')
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.2):
                            break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise TimeoutError('retention client unavailable')
                state = {'role': role, 'protocol': 'vless', 'client_pid': process.pid,
                         'service_pid_at_start': service_pid, 'success': 0, 'failures': 0,
                         'connections': 0, 'interruptions': 0, 'reconnections': 0}
                states.append(state)
                first_success = threading.Event()
                worker = threading.Thread(target=retain, args=(state, port, first_success), daemon=True)
                workers.append(worker)
                prepared.append(first_success)
                worker.start()
            deadline = time.monotonic() + 20
            while not all(event.is_set() for event in prepared):
                if any(state['failures'] for state in states) or time.monotonic() >= deadline or stopped.is_set():
                    raise RuntimeError('retention readiness failed')
                time.sleep(.1)
            summary['started_at'] = datetime.now(timezone.utc).isoformat()
            save(ready_path, summary)
            emit('ready', client_pids=[process.pid for process in clients], entries=states)
            while not stopped.wait(.2):
                if stop_path.exists():
                    break
                if any(process.poll() is not None for process in clients):
                    raise ChildProcessError('retention client exited')
            summary['completed'] = all(state['success'] > 0 and state['failures'] == 0 for state in states)
        finally:
            stopped.set()
    except Exception as exc:
        summary['error_class'] = type(exc).__name__
        emit('helper_failure', error_class=type(exc).__name__)
    finally:
        stopped.set()
        for worker in workers:
            worker.join(timeout=5)
        for process in clients:
            stop_client(process)
        if temporary is not None:
            temporary.cleanup()
        for listener in listeners:
            listener.shutdown()
            listener.server_close()
        for signum, handler in old_signals.items():
            signal.signal(signum, handler)
        summary['elapsed_ms'] = round((time.monotonic()-started)*1000)
        summary['finished_at'] = datetime.now(timezone.utc).isoformat()
        summary['completed'] = summary['completed'] and all(state['failures'] == 0 for state in states)
        summary['clients_stopped'] = all(process.poll() is not None for process in clients)
        save(summary_path, summary)
        emit('stopped', **{key: value for key, value in summary.items() if key not in ('run', 'boot_id', 'helper_pid', 'phase')})
        print(json.dumps(summary))
    return 0 if summary['completed'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--mihomo', type=Path)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--tcp-target', type=target)
    parser.add_argument('--udp-target', type=target)
    parser.add_argument('--serve-mode', action='store_true', help='VPS loopback echoes and REALITY retention only')
    parser.add_argument('--run', help='Private correlation identifier for the VPS retention run')
    parser.add_argument('--only-role', action='append', help='Run only this entry role; may be repeated')
    parser.add_argument('--diagnostic', action='store_true', help='Include bounded UDP event and client-log counts')
    args = parser.parse_args()
    if args.serve_mode:
        return serve(args)
    if any(value is None for value in (args.config, args.mihomo, args.report, args.tcp_target, args.udp_target)):
        parser.error('public-entry mode requires --config, --mihomo, --report, --tcp-target and --udp-target')
    import requests
    import yaml

    report = {'schema_version': 1, 'phase': 'public_entries',
              'started_at': datetime.now(timezone.utc).isoformat(),
              'subscription_https': False, 'fallback_rejected': True,
              'entries': [], 'completed': False}
    started = time.monotonic()
    try:
        config = json.loads(args.config.read_text(encoding='utf-8'))
        server, profile = config['server'], config['subscription']
        executable = str(args.mihomo.resolve())
        version = subprocess.run([executable, '-v'], capture_output=True, check=True,
                                 timeout=10).stdout.decode('utf-8')
        if 'v1.19.32 windows amd64' not in version:
            raise ValueError('unexpected client version')
        report['client'] = {'version': '1.19.32', 'platform': 'windows-amd64',
                            'sha256': hashlib.sha256(args.mihomo.read_bytes()).hexdigest()}
        session = requests.Session()
        session.trust_env = False
        suffix = '' if server['public_port'] == 443 else ':' + str(server['public_port'])
        url = 'https://' + server['subscription_domain'] + suffix + '/sub/' + profile['token'] + '/mihomo.yaml'
        response = session.get(url, timeout=25)
        response.raise_for_status()
        parsed = yaml.safe_load(response.text)
        proxies = parsed['proxies']
        expected = Counter({'vless': 2, 'hysteria2': 1, 'vmess': 1, 'trojan': 1,
                            'ss': 1, 'socks5': 1, 'tuic': 1, 'anytls': 1})
        if Counter(proxy['type'] for proxy in proxies) != expected:
            raise ValueError('unexpected subscription members')
        primary = proxies[0]
        if primary['type'] != 'vless' or not primary.get('reality-opts'):
            raise ValueError('unexpected primary entry')
        if primary['uuid'] != server['uuid']:
            raise ValueError('primary identity changed')
        reality = config['reality']
        snis = [entry.get('sni', entry.get('server_name')) for entry in reality['endpoints']]
        if len(snis) != 2 or any(not isinstance(sni, str) for sni in snis):
            raise ValueError('expected two REALITY entries')
        secondary = [sni for sni in snis if sni != primary['servername']]
        if len(secondary) != 1:
            raise ValueError('retained SNI missing')
        retained = deepcopy(primary)
        retained['servername'] = secondary[0]
        selected = [('reality-primary', primary), ('reality-retained', retained)]
        selected.extend(('shared-' + proxy['type'], proxy) for proxy in proxies[1:])
        if args.only_role:
            if not set(args.only_role) <= {role for role, _ in selected}:
                raise ValueError('unknown selected role')
            selected = [(role, outbound) for role, outbound in selected if role in args.only_role]
            report['scope'] = 'targeted'
            report['selected_roles'] = args.only_role
        report['subscription_https'] = True
        report['subscription_proxy_count'] = len(proxies)
        args.work.mkdir(parents=True, exist_ok=True)
        for role, outbound in selected:
            entry = {'role': role, 'protocol': outbound['type'], 'checks': []}
            if args.diagnostic:
                entry['diagnostic_events'] = []

            def diagnostic(phase, **details):
                entry['diagnostic_events'].append({
                    'phase': phase, 'utc': datetime.now(timezone.utc).isoformat(),
                    'monotonic_ns': time.monotonic_ns(), **details})

            report['entries'].append(entry)
            process = None
            client_log = None
            with tempfile.TemporaryDirectory(prefix='entry-', dir=args.work) as temporary:
                with socket.socket() as available:
                    available.bind(('127.0.0.1', 0))
                    port = available.getsockname()[1]
                path = Path(temporary) / 'client.json'
                client = client_configuration(outbound, port)
                if args.diagnostic:
                    client['log-level'] = 'debug'
                path.write_text(json.dumps(client), encoding='utf-8')
                phase = 'client_start'
                phase_started = time.monotonic()
                try:
                    subprocess.run([executable, '-t', '-d', temporary, '-f', str(path)],
                                   capture_output=True, check=True, timeout=15)
                    if args.diagnostic:
                        client_log = (Path(temporary) / 'client.log').open('wb')
                    process = subprocess.Popen([executable, '-d', temporary, '-f', str(path)],
                                               stdout=client_log or subprocess.DEVNULL,
                                               stderr=client_log or subprocess.DEVNULL)
                    for _ in range(40):
                        if process.poll() is not None:
                            raise ChildProcessError('client exited')
                        try:
                            with socket.create_connection(('127.0.0.1', port), timeout=.2):
                                break
                        except OSError:
                            time.sleep(.1)
                    else:
                        raise TimeoutError('client unavailable')
                    entry['checks'].append({'phase': phase, 'passed': True,
                                            'elapsed_ms': round((time.monotonic()-phase_started)*1000)})
                    for phase, check in (
                        ('tcp_echo', lambda: tcp_echo(port, args.tcp_target)),
                        ('udp_echo' if outbound.get('udp', True) else 'udp_unsupported_rejected',
                         lambda: udp_echo(port, args.udp_target, outbound.get('udp', True),
                                          diagnostic if args.diagnostic else None)),
                        ('https_exit', lambda: https_exit(session, port, server['public_address'])),
                    ):
                        phase_started = time.monotonic()
                        check()
                        entry['checks'].append({'phase': phase, 'passed': True,
                                                'elapsed_ms': round((time.monotonic()-phase_started)*1000)})
                except Exception as exc:
                    entry['checks'].append({'phase': phase, 'passed': False,
                                            'error_class': type(exc).__name__,
                                            'elapsed_ms': round((time.monotonic()-phase_started)*1000)})
                finally:
                    stop_client(process)
                    if client_log is not None:
                        client_log.close()
                        lines = (Path(temporary) / 'client.log').read_text(encoding='utf-8', errors='replace').splitlines()
                        entry['client_log_counts'] = {
                            key: sum(key.lower() in line.lower() for line in lines)
                            for key in ('UDP', 'TCP', 'level=error', 'level=warning', 'timeout', 'EOF',
                                        'deadline exceeded', 'handshake', 'certificate',
                                        'network is unreachable', 'connection refused')}
                        entry['client_log_line_count'] = len(lines)
            entry['passed'] = len(entry['checks']) == 4 and all(check['passed'] for check in entry['checks'])
        report['completed'] = len(report['entries']) == len(selected) and all(entry['passed'] for entry in report['entries'])
    except Exception as exc:
        report['error_class'] = type(exc).__name__
    finally:
        report['elapsed_ms'] = round((time.monotonic()-started)*1000)
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report))
    return 0 if report['completed'] else 1


def https_exit(session, port, expected):
    proxy = 'http://127.0.0.1:' + str(port)
    response = session.get('https://api.ipify.org?format=json',
                           proxies={'http': proxy, 'https': proxy}, timeout=45)
    response.raise_for_status()
    if response.json()['ip'] != expected:
        raise ValueError('unexpected exit')


if __name__ == '__main__':
    raise SystemExit(main())
