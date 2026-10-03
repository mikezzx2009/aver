#!/usr/bin/env python3
"""Durable task supervisor; each tenant's SDK daemon outlives its tasks."""
import fcntl
import json
import os
from pathlib import Path
import queue
import secrets
import shutil
import signal
import subprocess
import threading
import time

from common import DATA, ProxyTokens, config
from store import Store, Conflict, prepare_agent_directory
from publisher import publish_requested, list_sites, PublicationError

STOP = threading.Event()
STORE = None
TOKENS = None
WORKER_ID = 'worker-' + secrets.token_hex(8)
MAX_EVENT_BYTES = 12 * 1024 * 1024 + 1
MAX_PERSISTED_EVENT_BYTES = 1024 * 1024
MAX_RESULT_CHARS = 128 * 1024


def result_failure_message(result):
    message = result.get('message')
    if isinstance(message, str) and message.strip():
        return message.strip()[:4000]
    reason = str(result.get('finish_reason') or '')[:160]
    if reason == 'max-tokens':
        return '本轮模型输出达到长度上限，任务尚未完成；已生成的文件仍保留，可发送新消息让 Agent 继续'
    if reason:
        return 'Agent 未完成任务（原因：' + reason + '）；已保存的文件仍保留，请查看过程记录'
    return 'Agent 运行失败，请查看过程记录'


def bounded_event(kind, payload):
    """Read a complete protocol line, but bound both DB and host log records.

    Preserve committed outcome flags even when the result text is shortened;
    a large response must never turn successful shell effects into a retry.
    The tenant daemon's journal retains the original event.
    """
    encoded = json.dumps({'type': kind, 'data': payload}, ensure_ascii=False).encode() + b'\n'
    if len(encoded) <= MAX_PERSISTED_EVENT_BYTES:
        return payload, encoded
    limited = {'truncated': True, 'message': '单条输出过大；完整内容保留在用户环境的 .agent-cloud 任务日志中，大型成果请保存为工作空间文件'}
    if isinstance(payload, dict):
        for key in ('task_id', 'session_id', 'finish_reason'):
            if key in payload:
                limited[key] = str(payload[key])[:512]
        for key in ('success', 'journal_terminal', 'requires_restart'):
            if key in payload:
                limited[key] = bool(payload[key])
        if kind == 'result':
            limited['response'] = str(payload.get('response', ''))[:MAX_RESULT_CHARS]
            limited['response_truncated'] = True
    encoded = json.dumps({'type': kind, 'data': limited}, ensure_ascii=False).encode() + b'\n'
    return limited, encoded

def docker(*arguments, timeout=120, check=True):
    return subprocess.run(['docker', *map(str, arguments)], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout, check=check)

def container_name(user_id):
    if len(user_id) != 32 or any(c not in '0123456789abcdef' for c in user_id):
        raise ValueError('Invalid server-generated user ID')
    return 'agent-pc-' + user_id

def ensure_sandbox(user):
    name = container_name(user['id'])
    home = DATA / 'sandboxes' / user['id'] / 'home'
    prepare_agent_directory(home)
    # Also repair the root-owned home/files directories of existing accounts.
    # Host uploads already set file ownership on their opened descriptors.
    workspace = STORE.prepare_workspace(user['id'])
    existing = docker('inspect', name, check=False)
    if existing.returncode:
        docker('create', '--name', name, '--runtime=runsc-systrap', '--restart=unless-stopped',
            '--user=10001:10001', '--cap-drop=ALL', '--security-opt=no-new-privileges=true',
            '--read-only', '--cpus=1', '--memory=768m', '--memory-swap=768m', '--pids-limit=256',
            '--ulimit=nofile=4096:4096', '--tmpfs=/tmp:rw,nosuid,nodev,size=96m', '--shm-size=64m',
            '--network=agent-net', '--dns=223.5.5.5', '--dns=1.1.1.1',
            '--mount', f'type=bind,src={home},dst=/home/agent',
            '--mount', f'type=bind,src={workspace},dst=/workspace',
            '--workdir=/workspace', '--env=HOME=/home/agent',
            '--log-driver=local', '--log-opt=max-size=10m', '--log-opt=max-file=3', config()['sandbox_image'])
    else:
        details = json.loads(existing.stdout)[0]
        if details['HostConfig'].get('Runtime') != 'runsc-systrap':
            raise RuntimeError('Sandbox isolation does not match required runtime')
    docker('start', name)
    return name

def stop_execution(user_id):
    """Restart the tenant cgroup to guarantee all SDK/PTy child groups stop."""
    name = container_name(user_id)
    result = docker('stop', '--time=8', name, timeout=30, check=False)
    if result.returncode:
        inspect = docker('inspect', name, check=False)
        if inspect.returncode:
            return
        if json.loads(inspect.stdout)[0]['State']['Running']:
            raise RuntimeError('Cannot confirm task process has stopped')
    docker('start', name, timeout=30)

def ensure_daemon(name):
    """Launch independently of this worker's exec/subscription lifetime."""
    command = (name, '/opt/harness/bin/python', '-I', '/opt/task_runner.py')
    def healthy():
        result = docker('exec', *command, '--health', timeout=15, check=False)
        if result.returncode:
            return False
        try:
            event = json.loads(result.stdout)
            return event['type'] == 'health' and event['data'].get('ready') and event['data'].get('protocol') == 2
        except (ValueError, KeyError, TypeError):
            return False
    if healthy():
        return
    # --daemon acquires a tenant-local flock; concurrent starts never create
    # separate SDK runtimes or silently replace an existing daemon.
    docker('exec', '-d', *command, '--daemon', timeout=15)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not STOP.is_set():
        if healthy():
            return
        STOP.wait(0.5)
    raise RuntimeError('Tenant agent daemon did not become ready')

def append(task, kind, payload):
    STORE.append_event(task['user_id'], task['id'], kind, payload, worker_id=WORKER_ID)

def conversation_file_instructions(task):
    """Route each task without replacing its resident SDK or shared tools."""
    directory = '/workspace/conversations/' + task['conversation_id']
    return ('\n\n[Aver 对话文件规则]\n'
            '当前对话的工作目录是 ' + directory + '。本次任务新增或修改的项目文件、成果、附件和临时项目资源都保存在此目录内。'
            '新上传的附件位于该工作目录或用户在文件面板选择的子目录中；请先检查当前对话目录定位附件。'
            '每次执行相对路径的 shell 命令前先 cd 到此工作目录；'
            '文件读取、写入和其他工具一律使用指向此目录的明确绝对路径，不能依赖上一个任务留下的 shell 当前目录。\n'
            '用户的持久工具、依赖缓存和个人设置仍可放在 /home/agent 中共享使用。'
            '除非用户明确要求引用其他对话或历史共享文件，不要把成果写入 /workspace 根目录或其他对话目录。'
            '已有项目如在历史共享目录，先在当前对话目录内建立副本后继续工作，并保留原文件。')

def website_instructions(task, sites=None):
    """The host appends the URL only after a requested static snapshot is live."""
    directory = 'conversations/' + task['conversation_id'] + '/public/my-site'
    manifest = {'version': 1, 'task_id': task['id'], 'directory': directory, 'title': '网站标题'}
    return ('\n\n[Aver 网站交付规则]\n'
            '如果本次任务要制作或更新一个可在浏览器打开的静态网站，请把仅用于公开访问的页面和资源放到 '
            '/workspace/conversations/' + task['conversation_id'] + '/public/<项目名>/，其中必须有 index.html。'
            '不要放入密码、配置凭证、用户私有文件或后台源代码。\n'
            '发布后的页面位于 /<固定网站ID>/ 下，请使用相对路径引用页面和资源；例如 ./assets/app.js，不要依赖网站根目录 /assets。\n'
            '测试完成后创建 /workspace/.ever-publish/' + task['id'] + '.json，格式为：'
            + json.dumps(manifest, ensure_ascii=False) + '。directory 替换为实际发布目录。\n'
            '同一对话中同一项目目录的网站会沿用固定的短地址，每次发布只更新页面内容。'
            '更新已有网站时请沿用它的项目目录；如果必须更换目录，在发布说明中额外加入 site_id 字段，值为下方已有网站的 ID。'
            '新建不同的网站请使用不同的项目目录；不要自行猜测、指定或更改网站 ID。\n'
            '当前对话已发布的网站（directory 相对于 /workspace；历史共享项目请先复制到当前对话内）：'
            + json.dumps([{'site_id': site['site_id'], 'url': site['url'],
                           'directory': site['directory'], 'title': site['title']}
                          for site in (sites or [])], ensure_ascii=False) + '\n'
            '服务器将读取此请求并发布网站，再在任务回复中追加实际固定公网链接。'
            '容器内的 127.0.0.1、localhost 和私网 IP 不能供用户直接访问，不要把它们当作用户的访问地址。'
            '不要猜测尚未生成的公网 URL。非网站任务不需要创建发布请求。')

def run_task(task):
    if not config()['api_key']:
        STORE.mark_waiting_config(task['id'], WORKER_ID, '请管理员在设置中接入 DeepSeek API Key')
        return
    proc = None
    token = None
    final = None
    terminal = False
    restart_required = False
    stopped = False
    reader_stop = threading.Event()
    status = 'failed'
    error = None
    try:
        user = STORE.get_user(task['user_id'])
        name = ensure_sandbox(user)
        STORE.prepare_conversation_workspace(task['user_id'], task['conversation_id'])
        ensure_daemon(name)
        token = TOKENS.issue(task['id'], task['user_id'])
        session = STORE.get_setting('session:' + task['conversation_id'], user_id=task['user_id']) or task['conversation_id']
        sites = list_sites(DATA, task['user_id'], task['conversation_id'])
        request = {'task_id': task['id'], 'prompt': task['prompt'] + conversation_file_instructions(task) + website_instructions(task, sites), 'session_id': session,
                   'proxy_url': config()['proxy_url'], 'proxy_token': token, 'model': config()['model']}
        proc = subprocess.Popen(['docker', 'exec', '-i', name, '/opt/harness/bin/python', '-I', '/opt/task_runner.py', '--client'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        proc.stdin.write((json.dumps(request, ensure_ascii=False)+'\n').encode())
        proc.stdin.close()
        lines = queue.Queue(maxsize=4)
        def read():
            try:
                while True:
                    line = proc.stdout.readline(MAX_EVENT_BYTES)
                    if not line:
                        break
                    while not reader_stop.is_set():
                        try:
                            lines.put(line, timeout=1)
                            break
                        except queue.Full:
                            continue
            finally:
                while not reader_stop.is_set():
                    try:
                        lines.put(None, timeout=1)
                        break
                    except queue.Full:
                        continue
        threading.Thread(target=read, daemon=True).start()
        started = time.monotonic()
        heartbeat = started
        runs = DATA / 'runs'
        runs.mkdir(mode=0o700, exist_ok=True)
        with open(runs / (task['id']+'.jsonl'), 'ab', buffering=0) as log:
            while True:
                current = STORE.get_task(task['user_id'], task['id'])
                timeout = config()['task_timeout_seconds']
                if current['cancel_requested'] or STOP.is_set() or (timeout and time.monotonic()-started > timeout):
                    TOKENS.revoke(task['id'])
                    stop_execution(task['user_id'])
                    stopped = True
                    proc.wait(timeout=15)
                    status = 'cancelled' if current['cancel_requested'] else 'interrupted'
                    error = '用户停止了任务' if current['cancel_requested'] else '执行服务重启或超过配置的时间限制；请检查已保存的结果再继续'
                    break
                if time.monotonic()-heartbeat > 15:
                    if not STORE.heartbeat(task['id'], WORKER_ID):
                        TOKENS.revoke(task['id'])
                        stop_execution(task['user_id'])
                        stopped = True
                        proc.wait(timeout=15)
                        raise Conflict('Task lease no longer owned')
                    TOKENS.extend(task['id'])
                    heartbeat = time.monotonic()
                try:
                    line = lines.get(timeout=1)
                except queue.Empty:
                    continue
                if line is None:
                    return_code = proc.wait(timeout=10)
                    # Lost output does not stop the daemon. If no committed
                    # terminal event arrived, kill the cgroup before freeing
                    # the lease; otherwise shell effects could continue unseen.
                    if not terminal or restart_required:
                        TOKENS.revoke(task['id'])
                        stop_execution(task['user_id'])
                        stopped = True
                    if return_code == 0 and final and final.get('success'):
                        status = 'succeeded'
                    else:
                        error = error or 'Agent 运行失败，请查看过程记录'
                    break
                # Raw subprocess stderr may contain the tenant proxy token.
                decoded = line.decode('utf-8', errors='replace').replace(token, '[redacted]')
                try:
                    event = json.loads(decoded)
                    kind = str(event.get('type', 'log'))[:80]
                    payload = event.get('data', {})
                except (ValueError, AttributeError):
                    kind, payload = 'log', {'message': decoded[:8192]}
                payload, persisted = bounded_event(kind, payload)
                if kind == 'session' and isinstance(payload, dict) and payload.get('session_id'):
                    STORE.set_setting('session:'+task['conversation_id'], payload['session_id'], user_id=task['user_id'])
                if kind == 'result':
                    final = payload if isinstance(payload, dict) else None
                    terminal = bool(final and final.get('journal_terminal'))
                    if final and not final.get('success'):
                        error = result_failure_message(final)
                    if final and final.get('session_id'):
                        STORE.set_setting('session:'+task['conversation_id'], final['session_id'], user_id=task['user_id'])
                if kind == 'error' and isinstance(payload, dict):
                    terminal = terminal or bool(payload.get('journal_terminal'))
                    restart_required = restart_required or bool(payload.get('requires_restart'))
                    error = str(payload.get('message', payload))[:4000]
                if kind == 'cancelled':
                    terminal = terminal or bool(isinstance(payload, dict) and payload.get('journal_terminal'))
                log.write(persisted)
                os.fsync(log.fileno())
                append(task, kind, payload)
        response = str(final.get('response', '')) if final else ''
        if status == 'succeeded':
            try:
                site = publish_requested(STORE, task, public_base=config().get('site_base_url', ''))
                if site:
                    append(task, 'publication', {'site_id': site.get('site_id', site['id']),
                        'snapshot_id': site['id'], 'title': site['title'], 'url': site['url']})
                    response += '\n\n公网访问地址：' + site['url'] + '\n这个地址可以直接在你的浏览器中打开。'
            except PublicationError as exc:
                status = 'failed'
                error = '网站成果已保留，但公网发布未完成：' + str(exc)[:500]
                append(task, 'publication_error', {'message': error})
                response += '\n\n' + error
        if response:
            STORE.add_message(task['user_id'], task['conversation_id'], 'assistant', response, task_id=task['id'])
        elif error:
            STORE.add_message(task['user_id'], task['conversation_id'], 'system', error, task_id=task['id'])
        STORE.finish_task(task['id'], WORKER_ID, status, result=response or None, error=error)
    except Conflict:
        # Another owner/recovery event already decided task state; never overwrite it.
        if proc is not None and not stopped and not terminal:
            try:
                TOKENS.revoke(task['id'])
                stop_execution(task['user_id'])
                stopped = True
                proc.wait(timeout=15)
            except Exception:
                STOP.set()
    except Exception as e:
        if proc is not None and not stopped and (not terminal or restart_required or proc.poll() is None):
            try:
                TOKENS.revoke(task['id'])
                stop_execution(task['user_id'])
                stopped = True
                proc.wait(timeout=15)
            except Exception:
                # Leave occupied lease until recovery: do not claim a successful stop.
                STOP.set()
                return
        message = '执行环境暂时不可用：' + type(e).__name__
        try:
            STORE.add_message(task['user_id'], task['conversation_id'], 'system', message, task_id=task['id'])
            STORE.finish_task(task['id'], WORKER_ID, 'failed', error=message)
        except Conflict:
            pass
        print(json.dumps({'task':task['id'],'error':type(e).__name__}), flush=True)
    finally:
        reader_stop.set()
        TOKENS.revoke(task['id'])
        if proc is not None and proc.stdout:
            proc.stdout.close()

def main():
    global STORE, TOKENS
    os.umask(0o077)
    DATA.mkdir(parents=True, exist_ok=True)
    lock = open(DATA/'worker.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    STORE = Store(DATA/'data')
    TOKENS = ProxyTokens(DATA/'data')
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    # A crashed host worker can leave an exec alive. Stop it before freeing leases.
    for task in STORE.list_running_tasks():
        TOKENS.revoke(task['id'])
        stop_execution(task['user_id'])
        STORE.finish_task(task['id'], task['worker_id'], 'interrupted', error='执行服务重启，请检查已保存的结果再继续') if task['lease_expires_at']>time.time() else None
    STORE.recover_stale_tasks()
    TOKENS.revoke_all()
    print(json.dumps({'service':'worker','concurrency':1}), flush=True)
    while not STOP.is_set():
        # No submission-count cap. Disk/RAM exhaustion pauses new execution.
        if shutil.disk_usage(DATA).free < 2*1024**3:
            STOP.wait(10)
            continue
        if config()['api_key']:
            STORE.requeue_waiting_tasks()
        task = STORE.claim_task(WORKER_ID, max_concurrency=1)
        if task:
            run_task(task)
        else:
            STOP.wait(1)

if __name__ == '__main__':
    main()
