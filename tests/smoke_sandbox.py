#!/usr/bin/env python3
"""Root-only real Docker/gVisor/SDK smoke test; never calls a real model.

Run on the installed server:
  sudo python3 /opt/agent-cloud/tests/smoke_sandbox.py

Only uniquely named temporary containers and /tmp bind directories are used.
The SDK tests use --network=none and a loopback Chat Completions fixture.
Network probes use separate temporary containers on the existing agent-net;
this script never changes firewall rules or application containers/config.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time


UID = 10001
PYTHON = '/opt/harness/bin/python'
RUNNER = '/opt/task_runner.py'
MODEL = 'deepseek-v4-flash'
MODEL_OUTPUT_TOKENS = 49_152

# Executes inside the isolated test container, not on the host. The exposed
# tool schema is verified before emitting an actual Bash tool call. This is
# the fixed SDK 0.1.5rc1 Chat Completions protocol, including streaming.
MODEL_FIXTURE = r'''
import json, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

commands = {
 'AC_SMOKE_1': "if env | grep -q '^SDK_SHOULD_NOT_INHERIT='; then exit 77; fi; printf 'actual SDK bash tool\\n' > /workspace/sdk-tool-ran.txt; sleep 900 >/workspace/background.log 2>&1 & p=$!; printf '%s\\n' \"$p\" > /workspace/background.pid; printf 'BACKGROUND_STARTED\\n'",
 'AC_SMOKE_2': "p=$(cat /workspace/background.pid); kill -0 \"$p\" && printf 'BACKGROUND_ALIVE\\n' && printf 'second real tool task\\n' > /workspace/second.txt",
 'AC_SMOKE_LIMIT': "p=$(cat /workspace/background.pid); kill -0 \"$p\" && printf 'one committed effect\\n' >> /workspace/limit-effects.txt",
 'AC_SMOKE_CONTINUE': "test \"$(wc -l < /workspace/limit-effects.txt)\" -eq 1 && p=$(cat /workspace/background.pid) && kill -0 \"$p\" && printf 'CONTINUED_WITHOUT_REPLAY\\n'",
 'AC_SMOKE_CANCEL': "printf '%s\\n' \"$$\" > /workspace/cancel-shell.pid; sleep 900",
}

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *_): pass
    def do_GET(self):
        self.send_response(200); self.send_header('Content-Length','2'); self.end_headers(); self.wfile.write(b'OK')
    def do_POST(self):
        try:
            assert self.path == '/v1/chat/completions'
            size = int(self.headers.get('Content-Length','0')); assert 0 < size <= 12*1024*1024
            body = json.loads(self.rfile.read(size))
            assert body.get('max_tokens') == 49152, ('Wrong per-request output cap', body.get('max_tokens'))
            tools = body.get('tools', [])
            names = [tool.get('function',{}).get('name') for tool in tools]
            assert names == ['bash'], ('Unexpected sdk-minimal tools', names)
            schema = tools[0]['function']['parameters']
            assert schema['properties']['command']['type'] == 'string'
            assert 'command' in schema.get('required', [])
            messages = body['messages']
            user = next(message for message in reversed(messages) if message['role'] == 'user')
            phase = next(marker for marker in commands if marker in json.dumps(user))
            # A returned tool result is followed by one final assistant reply.
            tool_reply = messages[-1]['role'] == 'tool'
            call = {'id':'call_smoke_'+phase, 'type':'function', 'function':{'name':'bash','arguments':json.dumps({'command':commands[phase]})}}
            with Path('/home/agent/mock-requests.jsonl').open('a') as log:
                log.write(json.dumps({'phase':phase,'tool_reply':tool_reply,'model':body['model'],'stream':body.get('stream'),'max_tokens':body.get('max_tokens')})+'\n')
            envelope = {'id':'chatcmpl-smoke', 'object':'chat.completion', 'created':int(time.time()), 'model':body['model'], 'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}}
            limited = phase == 'AC_SMOKE_LIMIT' and tool_reply
            reason = 'length' if limited else ('stop' if tool_reply else 'tool_calls')
            response = None if limited or not tool_reply else 'SMOKE_COMPLETED'
            if body.get('stream'):
                self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.send_header('Connection','close'); self.end_headers()
                delta = {'role':'assistant', 'content':response, 'reasoning_content':'Fixture reasoning reached the output ceiling.' if limited else ''}
                if not tool_reply: delta['tool_calls'] = [{'index':0, **call}]
                for payload in (
                    {**envelope, 'object':'chat.completion.chunk','choices':[{'index':0,'delta':delta,'finish_reason':None}]},
                    {**envelope, 'object':'chat.completion.chunk','choices':[{'index':0,'delta':{},'finish_reason':reason}]},
                ):
                    self.wfile.write(b'data: '+json.dumps(payload).encode()+b'\n\n'); self.wfile.flush()
                self.wfile.write(b'data: [DONE]\n\n'); self.wfile.flush(); self.close_connection = True
            else:
                message = {'role':'assistant','content':response, 'reasoning_content':'Fixture reasoning reached the output ceiling.' if limited else ''}
                if not tool_reply: message['tool_calls'] = [call]
                encoded = json.dumps({**envelope,'choices':[{'index':0,'message':message,'finish_reason':reason}]}).encode()
                self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
        except Exception as exc:
            Path('/home/agent/mock-error.txt').write_text(type(exc).__name__+': '+str(exc))
            self.send_error(500, 'Smoke fixture rejected request')

server = ThreadingHTTPServer(('127.0.0.1',18765),Handler)
server.daemon_threads = True
server.serve_forever()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', default='agent-cloud-sandbox:0.1')
    parser.add_argument('--docker-config', default='/etc/agent-cloud/docker-client')
    parser.add_argument('--keep-on-failure', action='store_true', help='Preserve only this test\'s containers/bind directories for diagnostics')
    args = parser.parse_args()
    if sys.platform != 'linux' or os.geteuid() != 0:
        parser.error('Run as root on the Linux server with the installed Docker/gVisor setup')
    prefix = 'ac-smoke-' + secrets.token_hex(6)
    containers = []
    temporary = Path(tempfile.mkdtemp(prefix=prefix+'-', dir='/tmp'))
    home, workspace = temporary/'home', temporary/'workspace'
    for path in (home, workspace):
        path.mkdir(mode=0o700); os.chown(path, UID, UID)
    fixture = home/'model_fixture.py'
    fixture.write_text(MODEL_FIXTURE); os.chown(fixture, UID, UID); fixture.chmod(0o400)
    base_command = ['docker', '--config', args.docker_config]
    succeeded = False
    attached = None
    host_listener = None

    def docker(*arguments, timeout=45, check=True, input=None):
        result = subprocess.run([*base_command, *map(str,arguments)], input=input, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError('docker '+str(arguments[0])+': '+result.stderr.strip())
        return result

    def execute(name, code, *, timeout=30):
        return docker('exec',name,PYTHON,'-I','-c',code,timeout=timeout)

    def background_alive(name, pid):
        execute(name,f"import os; from pathlib import Path; os.kill({pid},0); stat=Path('/proc/{pid}/stat').read_text(); assert stat[stat.rfind(')')+2:].split()[0] not in ('Z','X'); assert Path('/proc/{pid}/cmdline').read_bytes().split(bytes([0]))[:2]==[b'sleep',b'900']")

    def wait_for(check, label, timeout=45):
        deadline = time.monotonic()+timeout
        last_error = None
        while time.monotonic()<deadline:
            try:
                if check(): return
            except (RuntimeError, ValueError, AssertionError) as exc:
                last_error = str(exc)[-1000:]
            time.sleep(.5)
        raise RuntimeError(label+' timed out'+(': '+last_error if last_error else ''))

    def create(name, network, memory='768m', mount_user=False, command=None):
        containers.append(name)
        options = ['create','--name',name,'--runtime=runsc-systrap','--network='+network,
            '--user=10001:10001','--cap-drop=ALL','--security-opt=no-new-privileges=true',
            '--read-only','--cpus=1','--memory='+memory,'--memory-swap='+memory,
            '--pids-limit=256','--ulimit=nofile=4096:4096',
            '--tmpfs=/tmp:rw,nosuid,nodev,size=96m','--shm-size=64m',
            '--workdir=/workspace','--env=HOME=/home/agent',
            '--env=SDK_SHOULD_NOT_INHERIT=nonsecret-test-marker',
            '--dns=223.5.5.5','--dns=1.1.1.1']
        if mount_user:
            options += ['--mount',f'type=bind,src={home},dst=/home/agent',
                        '--mount',f'type=bind,src={workspace},dst=/workspace']
        docker(*options,args.image,*(command or [])); docker('start',name)
        record = json.loads(docker('inspect',name).stdout)[0]
        assert record['HostConfig']['ReadonlyRootfs'] and record['HostConfig']['Runtime']=='runsc-systrap'

    def start_daemon(name):
        docker('exec','-d',name,'/bin/sh','-c',
            'exec /opt/harness/bin/python -I /opt/task_runner.py --daemon > /home/agent/daemon-test.log 2>&1')
        def healthy():
            result=docker('exec',name,PYTHON,'-I',RUNNER,'--health',check=False)
            if result.returncode: return False
            data=json.loads(result.stdout)
            return data['type']=='health' and data['data']['ready'] and data['data']['protocol']==2
        wait_for(healthy,'daemon startup / --health')

    def task_request(task_id, marker, session_id):
        return {'task_id':task_id,'prompt':marker,'session_id':session_id,
                'proxy_url':'http://127.0.0.1:18765/v1','proxy_token':'smoke-only-'+secrets.token_urlsafe(20),
                'model':MODEL}

    def run_task(name, request, *, expected_success=True):
        result=docker('exec','-i',name,PYTHON,'-I',RUNNER,'--client',
                      input=json.dumps(request)+'\n',timeout=180,check=False)
        events=[json.loads(line) for line in result.stdout.splitlines() if line]
        final=next((event['data'] for event in reversed(events) if event['type']=='result'),None)
        expected_exit = 0 if expected_success else 1
        if result.returncode != expected_exit or not final or final.get('success') is not expected_success or not final.get('journal_terminal'):
            raise RuntimeError('Real SDK task failed: '+result.stdout[-12000:]+'\n'+result.stderr[-4000:])
        assert request['proxy_token'] not in result.stdout
        return final, events

    try:
        docker('image','inspect',args.image)
        main_name = prefix+'-sdk'
        # No external network is available to the SDK or its model fixture.
        create(main_name,'none',mount_user=True)
        execute(main_name,"import os; from pathlib import Path; from importlib.metadata import version; from deepseek_harness import DeepSeekHarness; assert os.getuid()==10001; assert version('deepseek-harness-sdk')=='0.1.5rc1'; Path('/home/agent/write-check').write_text('persistent home'); Path('/workspace/write-check').write_text('persistent workspace'); Path('/tmp/write-check').write_text('tmpfs'); failures=0\nfor target in ('/etc/ac-smoke-write','/opt/ac-smoke-write'):\n try: Path(target).write_text('bad')\n except OSError: failures+=1\nassert failures==2; assert not Path('/var/run/docker.sock').exists(); print('SDK import and rootfs checks passed')")
        print('PASS: real SDK 0.1.5rc1 import, UID10001, readonly rootfs, writable home/workspace/tmpfs, no Docker socket',flush=True)
        docker('exec','-d',main_name,'/bin/sh','-c',
            'exec /opt/harness/bin/python -I /home/agent/model_fixture.py > /home/agent/model-fixture.log 2>&1')
        wait_for(lambda: execute(main_name,"import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:18765/').read()==b'OK'").returncode==0,
                 'loopback model fixture')
        start_daemon(main_name)
        print('PASS: daemon --health protocol2 under runsc-systrap',flush=True)
        first, events1=run_task(main_name,task_request('smoke1','AC_SMOKE_1','smoke-session'))
        assert (workspace/'sdk-tool-ran.txt').read_text().strip()=='actual SDK bash tool'
        pid=int((workspace/'background.pid').read_text())
        background_alive(main_name,pid)
        time.sleep(2)
        background_alive(main_name,pid)
        second,events2=run_task(main_name,task_request('smoke2','AC_SMOKE_2',first['session_id']))
        assert (workspace/'second.txt').exists()
        background_alive(main_name,pid)
        assert next(e['data']['pid'] for e in events1 if e['type']=='session') == next(e['data']['pid'] for e in events2 if e['type']=='session')
        assert any(path.is_file() for path in (home/'.dsh'/'sessions').rglob('*'))
        records=[json.loads(line) for line in (home/'mock-requests.jsonl').read_text().splitlines()]
        assert sum(not record['tool_reply'] for record in records)==2
        assert all(record['max_tokens'] == MODEL_OUTPUT_TOKENS for record in records)
        print('PASS: actual bundled SDK runtime + streaming mock API + real persistent Bash tool; two tasks retain background sleep/session and write durable session data',flush=True)

        limited, events_limit=run_task(main_name,task_request('smoke-limit','AC_SMOKE_LIMIT',second['session_id']),expected_success=False)
        assert limited['finish_reason']=='max-tokens' and not limited['response']
        assert isinstance(limited.get('error'),str) and limited['error'].strip()
        assert (workspace/'limit-effects.txt').read_text().splitlines()==['one committed effect']
        background_alive(main_name,pid)
        continued, events_continue=run_task(main_name,task_request('smoke-continue','AC_SMOKE_CONTINUE',limited['session_id']))
        assert continued['session_id']==limited['session_id']==second['session_id']
        assert (workspace/'limit-effects.txt').read_text().splitlines()==['one committed effect']
        background_alive(main_name,pid)
        daemon_pids={next(event['data']['pid'] for event in events if event['type']=='session')
                     for events in (events1,events2,events_limit,events_continue)}
        assert len(daemon_pids)==1
        records=[json.loads(line) for line in (home/'mock-requests.jsonl').read_text().splitlines()]
        limit_requests=[record for record in records if record['phase']=='AC_SMOKE_LIMIT']
        assert len(limit_requests)==2 and sum(not record['tool_reply'] for record in limit_requests)==1
        assert all(record['max_tokens']==MODEL_OUTPUT_TOKENS for record in records)
        print('PASS: real SDK length finish reports max-tokens and explicit error; same-session continuation preserves committed tool effects/background without automatic replay; every request uses 49152 output tokens',flush=True)

        request=task_request('smoke-cancel','AC_SMOKE_CANCEL',continued['session_id'])
        attached=subprocess.Popen([*base_command,'exec','-i',main_name,PYTHON,'-I',RUNNER,'--client'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        attached.stdin.write(json.dumps(request)+'\n'); attached.stdin.close()
        wait_for(lambda:(workspace/'cancel-shell.pid').exists(),'running cancellation tool',timeout=60)
        shell_pid=int((workspace/'cancel-shell.pid').read_text())
        snapshot=json.loads(execute(main_name,f"import json; from pathlib import Path; records={{}}\nfor pid in ({pid},{shell_pid}):\n stat=Path(f'/proc/{{pid}}/stat').read_text(); records[str(pid)]={{'started':stat[stat.rfind(')')+2:].split()[19],'command':Path(f'/proc/{{pid}}/cmdline').read_bytes().hex()}}\nprint(json.dumps(records))").stdout)
        docker('stop','--time=8',main_name,timeout=30)
        attached.wait(timeout=15)
        docker('start',main_name)
        execute(main_name,"from pathlib import Path\nold="+repr(snapshot)+"\nfor pid,record in old.items():\n path=Path(f'/proc/{pid}')\n if path.exists():\n  stat=(path/'stat').read_text(); current={'started':stat[stat.rfind(')')+2:].split()[19],'command':(path/'cmdline').read_bytes().hex()}\n  assert current!=record, 'Old task process survived cancellation'\nassert Path('/workspace/sdk-tool-ran.txt').exists(); assert Path('/home/agent/write-check').exists(); assert not Path('/tmp/write-check').exists()")
        start_daemon(main_name)
        print('PASS: explicit cancellation stop/start kills running tool and background process; home/workspace survive and daemon restarts',flush=True)
        docker('rm','-f',main_name)

        network=json.loads(docker('network','inspect','agent-net').stdout)[0]
        gateway=network['IPAM']['Config'][0]['Gateway']
        assert gateway=='172.29.0.1' and not network.get('EnableIPv6')
        for destination in ('100.100.100.200/32','169.254.0.0/16','10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'):
            subprocess.run(['iptables','-w','10','-C','AGENT-CLOUD-FWD','-d',destination,'-j','DROP'],check=True)
        for chain in ('INPUT','FORWARD'):
            subprocess.run(['ip6tables','-w','10','-C',chain,'-i','agent-br','-m','comment','--comment','agent-cloud-no-ipv6','-j','DROP'],check=True)
        peer_name,probe_name=prefix+'-peer',prefix+'-probe'
        create(peer_name,'agent-net','128m',command=[PYTHON,'-I','-c',
            "from http.server import HTTPServer,BaseHTTPRequestHandler; HTTPServer(('0.0.0.0',443),BaseHTTPRequestHandler).serve_forever()"])
        wait_for(lambda:execute(peer_name,"import socket; socket.create_connection(('127.0.0.1',443),timeout=2).close()").returncode==0,'known-open peer listener')
        peer_ip=json.loads(docker('inspect',peer_name).stdout)[0]['NetworkSettings']['Networks']['agent-net']['IPAddress']
        create(probe_name,'agent-net','128m')
        host_listener=socket.socket(); host_listener.bind((gateway,0)); host_listener.listen(4)
        host_port=host_listener.getsockname()[1]
        def drain():
            while True:
                try: connection,_=host_listener.accept(); connection.close()
                except OSError: return
        threading.Thread(target=drain,daemon=True).start()
        socket.create_connection((gateway,host_port),timeout=2).close()
        targets=[(peer_ip,443,'known-open tenant peer'),(gateway,host_port,'known-open forbidden host port'),
                 ('100.100.100.200',80,'Alibaba metadata'),('100.100.100.200',443,'Alibaba metadata HTTPS'),
                 ('169.254.169.254',80,'link-local metadata')]
        probe="import socket\ntargets="+repr(targets)+"\nfor address,port,label in targets:\n try:\n  connection=socket.create_connection((address,port),timeout=2); connection.close()\n except OSError:\n  print('BLOCKED: '+label)\n else:\n  raise AssertionError('Isolation failure: '+label)"
        print(execute(probe_name,probe,timeout=20).stdout.strip(),flush=True)
        print('PASS: known-open peer/host destinations unreachable; IMDS/private-range DROP rules and IPv6 guards installed',flush=True)
        succeeded=True
        print('ALL SANDBOX SMOKE CHECKS PASSED; no real model/API key was used',flush=True)
    except Exception as exc:
        print('FAIL: '+type(exc).__name__+': '+str(exc),file=sys.stderr,flush=True)
        for name in containers:
            result=docker('inspect',name,check=False)
            if result.returncode==0:
                record=json.loads(result.stdout)[0]
                print(json.dumps({'container':name,'state':record['State']},ensure_ascii=False),file=sys.stderr)
                logs=docker('logs','--tail','80',name,check=False)
                if logs.stdout or logs.stderr:
                    print(name+' container log:\n'+(logs.stdout+logs.stderr)[-12000:],file=sys.stderr)
        for name in ('daemon-test.log','mock-error.txt','model-fixture.log'):
            path=home/name
            if path.exists(): print(name+':\n'+path.read_text(errors='replace')[-12000:],file=sys.stderr)
        return 1
    finally:
        if host_listener is not None: host_listener.close()
        if attached is not None:
            if attached.poll() is None:
                attached.terminate()
                try: attached.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    attached.kill(); attached.wait(timeout=5)
            for stream in (attached.stdout,attached.stderr):
                if stream: stream.close()
        if succeeded or not args.keep_on_failure:
            for name in reversed(containers): docker('rm','-f',name,check=False)
            shutil.rmtree(temporary)
        else:
            print('Preserved test-only diagnostics: '+str(temporary)+'; containers: '+', '.join(containers),file=sys.stderr)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
