#!/usr/bin/env python3
"""Apply a source release while leaving Docker and tenant containers alone.

Run as root: python3 update-web.py --stage /path/to/unpacked/agent-cloud
Add --include-worker to also publish Store, worker and sandbox build sources.
This flag does not build an image or reload/recreate a tenant container.
Exit 75 means a running task was found; retry after it finishes.
"""
import argparse
import ast
import datetime
import http.client
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time


WEB = 'agent-cloud.service'
WORKER = 'agent-cloud-worker.service'
BACKUP_ROOT = Path('/root')
REQUIRED = ('server.py', 'server_stats.py', 'publisher.py', 'backup.py',
            'static/app.js', 'static/style.css', 'static/index.html', 'static/favicon.svg')
OPTIONAL = ('README.md', 'DEPLOYMENT.txt', 'deploy/install.sh', 'deploy/update-web.py',
            'deploy/agent-cloud-sites.nginx.conf', 'deploy/migrate-conversation-files.py')
WORKER_SOURCES = ('store.py', 'worker.py', 'sandbox/task_runner.py', 'sandbox/Dockerfile')


class UpdateError(RuntimeError):
    pass


class TaskRunning(UpdateError):
    pass


def systemctl(*arguments, timeout=65, check=True):
    result = subprocess.run(['systemctl', *arguments], timeout=timeout,
                            capture_output=True, text=True)
    if check and result.returncode:
        raise UpdateError('systemctl ' + ' '.join(arguments) + ': ' + result.stderr.strip()[:600])
    return result


def service_state(unit):
    result = systemctl('show', unit, '--property=LoadState,ActiveState,MainPID')
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if values.get('LoadState') != 'loaded':
        raise UpdateError(f'{unit} is not loaded')
    return {'active': values.get('ActiveState'), 'pid': int(values.get('MainPID', '0'))}


def regular_file(path):
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def destination(app, relative):
    target = app / relative
    current = target
    while current != app:
        if current.is_symlink():
            raise UpdateError(f'Refusing symlink destination: {relative}')
        current = current.parent
    if target.exists() and not regular_file(target):
        raise UpdateError(f'Destination is not a regular file: {relative}')
    return target


def release_files(stage, app, include_worker=False):
    required = REQUIRED + (WORKER_SOURCES if include_worker else ())
    names = list(required)
    for name in required:
        if not regular_file(stage / name):
            raise UpdateError(f'Stage is missing regular file {name}')
    names.extend(name for name in OPTIONAL if (stage / name).exists())
    tests = stage / 'tests'
    if tests.exists():
        if tests.is_symlink() or not tests.is_dir():
            raise UpdateError('Stage tests must be a directory, not a symlink')
        names.extend(str(path.relative_to(stage)) for path in tests.rglob('*')
                     if path.suffix in {'.py', '.js'} and '__pycache__' not in path.parts
                     and not any(part.startswith('._') for part in path.relative_to(tests).parts))
    for name in names:
        source = stage / name
        if not regular_file(source):
            raise UpdateError(f'Stage file is not a regular file: {name}')
        destination(app, name)
        if source.suffix == '.py':
            ast.parse(source.read_text(encoding='utf-8'), filename=name)
    # Publish the document only after its JS/CSS and supporting code exist.
    return sorted(set(names), key=lambda name: (name == 'static/index.html', name))


def atomic_copy(source, target, mode):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    fd, temporary = tempfile.mkstemp(prefix='.agent-cloud-update-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as output, source.open('rb') as incoming:
            shutil.copyfileobj(incoming, output, 1024 * 1024)
            output.flush()
            os.fchmod(output.fileno(), mode)
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def wait_health():
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection('127.0.0.1', 8080, timeout=2)
        try:
            connection.request('GET', '/health')
            response = connection.getresponse()
            if response.status == 200:
                with socket.create_connection(('172.29.0.1', 8090), timeout=2):
                    return
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(0.3)
    raise UpdateError('Updated web API/model proxy did not become healthy')


def pause_idle_worker(data, initial_worker, flags):
    # mode=rw refuses to create an empty database at a mistyped path.
    database = data / 'data' / 'agent-cloud.sqlite3'
    connection = sqlite3.connect(database.as_uri() + '?mode=rw', uri=True,
                                 timeout=5, isolation_level=None)
    try:
        connection.execute('BEGIN IMMEDIATE')
        if connection.execute("SELECT COUNT(*) FROM tasks WHERE status='running'").fetchone()[0]:
            raise TaskRunning('Running task exists; no service or source file changed')
        if initial_worker['active'] == 'active':
            flags['stop_attempted'] = True
            # Hold only the claim fence during stop, never during source copying.
            # A worker blocked in SQLite may take its busy timeout to exit.
            result = systemctl('stop', WORKER, timeout=35, check=False)
            state = service_state(WORKER)
            if result.returncode or state['pid'] != 0 or state['active'] not in {'inactive', 'failed'}:
                raise UpdateError('Cannot confirm worker stopped; source files were not changed')
        elif initial_worker['pid'] != 0:
            raise UpdateError('Inactive worker unexpectedly has a MainPID')
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def rollback(app, backup, changed, initial_worker, flags):
    errors = []
    for name in reversed(changed):
        try:
            target = destination(app, name)
            saved = backup / name
            if saved.exists():
                atomic_copy(saved, target, stat.S_IMODE(saved.stat().st_mode))
            else:
                target.unlink(missing_ok=True)
        except Exception as error:
            errors.append(f'Restore {name}: {type(error).__name__}')
    web_healthy = not flags['web_touched']
    if flags['web_touched'] and not errors:
        try:
            systemctl('restart', WEB)
            wait_health()
            web_healthy = True
        except Exception as error:
            errors.append('Restore old web service: ' + type(error).__name__)
    if flags['stop_attempted'] and initial_worker['active'] == 'active' and web_healthy and not errors:
        try:
            systemctl('start', WORKER)
            state = service_state(WORKER)
            if state['active'] != 'active' or state['pid'] == 0:
                raise UpdateError('Old worker did not return to active state')
        except Exception as error:
            errors.append('Restore worker: ' + type(error).__name__)
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', required=True, type=Path)
    parser.add_argument('--app-dir', default=Path('/opt/agent-cloud'), type=Path)
    parser.add_argument('--data-dir', default=Path('/srv/agent-cloud'), type=Path)
    parser.add_argument('--include-worker', action='store_true',
                        help='Also copy Store, worker and sandbox build sources; no image/container changes')
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        raise UpdateError('Run this update script as root')
    os.umask(0o077)
    stage = arguments.stage.resolve(strict=True)
    app = arguments.app_dir.resolve(strict=True)
    data = arguments.data_dir.resolve(strict=True)
    if stage == app or not stage.is_dir() or not app.is_dir() or not data.is_dir():
        raise UpdateError('Stage must be a separate complete release directory')
    names = release_files(stage, app, include_worker=arguments.include_worker)
    initial_web = service_state(WEB)
    initial_worker = service_state(WORKER)
    if initial_web['active'] != 'active' or initial_web['pid'] == 0:
        raise UpdateError('This online update requires an initially active web service')
    if initial_worker['active'] not in {'active', 'inactive', 'failed'}:
        raise UpdateError('Worker is changing state; retry when stable')
    flags = {'stop_attempted': False, 'web_touched': False}
    changed = []
    backup = None
    try:
        pause_idle_worker(data, initial_worker, flags)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        backup = BACKUP_ROOT / ('agent-cloud-code-backup-' + stamp)
        backup.mkdir(mode=0o700)
        old_modes = {}
        for name in names:
            target = destination(app, name)
            if target.exists():
                old_modes[name] = stat.S_IMODE(target.stat().st_mode)
                saved = backup / name
                saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copy2(target, saved)
        (backup / 'update-manifest.json').write_text(json.dumps({
            'created_at': stamp, 'files': names, 'previously_present': sorted(old_modes),
            'initial_web': initial_web, 'initial_worker': initial_worker,
            'include_worker': arguments.include_worker,
            'scope': 'source code only; no Docker/container/config/database changes'
        }, ensure_ascii=False, indent=2) + '\n')
        for name in names:
            default_mode = 0o755 if name in {'deploy/install.sh', 'deploy/update-web.py'} else 0o644
            # Track before replace so an interruption just after os.replace
            # cannot leave an updated file outside the rollback list.
            changed.append(name)
            atomic_copy(stage / name, destination(app, name), old_modes.get(name, default_mode))
        flags['web_touched'] = True
        systemctl('restart', WEB)
        wait_health()
        if initial_worker['active'] == 'active':
            systemctl('start', WORKER)
            state = service_state(WORKER)
            if state['active'] != 'active' or state['pid'] == 0:
                raise UpdateError('Worker did not return to active state')
    except TaskRunning as error:
        print(str(error), file=sys.stderr)
        return 75
    except BaseException as error:
        restoration_errors = rollback(app, backup, changed, initial_worker, flags)
        print('Update failed: ' + type(error).__name__ + ': ' + str(error), file=sys.stderr)
        for restoration_error in restoration_errors:
            print('Recovery required: ' + restoration_error, file=sys.stderr)
        if backup is not None:
            print('Code backup: ' + str(backup), file=sys.stderr)
        return 1
    # Console disconnection after a successful restart must not trigger rollback
    # after the worker was already allowed to claim a new task.
    print(json.dumps({'updated': names, 'code_backup': str(backup),
                      'worker_restored': initial_worker['active'] == 'active'}))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except UpdateError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
