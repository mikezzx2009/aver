"""Publish immutable static snapshots behind a permanent short site address.

Only normal visible files beneath the task conversation's public directory
(or a legacy workspace/public directory) are eligible. Hidden entries
are skipped; links and special files fail the entire publication. Private
records live outside the web-served directory and bind a site to its owner/task.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import time
from urllib.parse import urlsplit
import uuid

from common import DATA, config

MAX_FILES = 1000
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 8192
MAX_ENTRIES = 4000
MAX_DEPTH = 32
MAX_RECORDS = 10000
_ID = re.compile(r'^[0-9a-f]{32}$')
_SLUG = re.compile(r'^[a-z0-9]{4,10}$')
RESERVED_SLUGS = frozenset(('api', 'sites', 'static', 'assets', 'private', 'admin',
    'settings', 'workspace', 'workspaces', 'data', 'backups', 'runs', 'sandboxes',
    'health', 'login', 'logout', 'favicon', 'signup', 'status', 'config', 'published'))
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_PUBLIC_FIELDS = ('id', 'url', 'title', 'directory', 'file_count', 'total_bytes')


class PublicationError(Exception):
    """A bounded, user-visible failure that does not reveal private paths."""


def _identifier(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _slug(value):
    return isinstance(value, str) and _SLUG.fullmatch(value) is not None and value not in RESERVED_SLUGS


def _parts(value, *, conversation_id=None):
    if not isinstance(value, str) or len(value) > 512 or '\\' in value or '\x00' in value:
        raise PublicationError('发布目录格式无效，请使用当前对话 public/ 下的普通目录')
    parts = value.split('/')
    legacy = len(parts) >= 2 and parts[0] == 'public'
    scoped = (len(parts) >= 4 and parts[0] == 'conversations'
              and _identifier(parts[1]) and parts[2] == 'public')
    if not (legacy or scoped) or len(parts) > MAX_DEPTH:
        raise PublicationError('发布目录必须位于当前对话的 public/ 下')
    if scoped and conversation_id is not None and parts[1] != conversation_id:
        raise PublicationError('发布目录不属于当前任务的对话')
    if any(not part or part.startswith('.') or any(ord(c) < 32 for c in part) for part in parts):
        raise PublicationError('发布目录不能包含隐藏路径或上级目录')
    return parts


def _public_base(value):
    if not isinstance(value, str) or '\\' in value or any(ord(c) < 32 for c in value):
        raise PublicationError('公网访问地址配置无效')
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError:
        raise PublicationError('公网访问地址配置无效') from None
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise PublicationError('公网访问地址配置无效')
    return value.rstrip('/')


def _configured_public_base(value):
    value = config().get('site_base_url', '') if value is None else value
    if not value:
        raise PublicationError('尚未配置网站发布地址；请管理员设置 site_base_url 并启用静态网站 Nginx 配置')
    return _public_base(value)


def _normalized_directory(value, conversation_id):
    parts = _parts(value, conversation_id=conversation_id)
    return '/'.join(parts[2:] if parts[0] == 'conversations' else parts)


def _project_key(user_id, conversation_id, directory):
    import hashlib
    return hashlib.sha256(json.dumps([user_id, conversation_id, directory],
                                    separators=(',', ':')).encode()).hexdigest()


def _read_json(directory_fd, name):
    fd = os.open(name, _FILE_FLAGS, dir_fd=directory_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_MANIFEST_BYTES:
            raise PublicationError('发布说明必须是小型普通 JSON 文件')
        raw = bytearray()
        while len(raw) <= MAX_MANIFEST_BYTES:
            piece = os.read(fd, min(4096, MAX_MANIFEST_BYTES + 1 - len(raw)))
            if not piece:
                break
            raw.extend(piece)
        after = os.fstat(fd)
        if len(raw) > MAX_MANIFEST_BYTES or before.st_mtime_ns != after.st_mtime_ns or before.st_ctime_ns != after.st_ctime_ns:
            raise PublicationError('发布说明正在变化，请完成写入后再发布')
        result = json.loads(raw.decode('utf-8'))
        if not isinstance(result, dict):
            raise PublicationError('发布说明必须是 JSON 对象')
        return result
    except (UnicodeError, ValueError):
        raise PublicationError('发布说明不是有效的 JSON') from None
    finally:
        os.close(fd)


@contextmanager
def _directory_at(parent_fd, name):
    fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        yield fd
    finally:
        os.close(fd)


def _host_directory(path, mode):
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    fd = os.open(path, _DIRECTORY_FLAGS)
    os.fchmod(fd, mode)
    if os.geteuid() == 0:
        os.fchown(fd, 0, 0)
    return fd


def _record_valid(record, *, user_id, task_id=None, conversation_id=None):
    if not isinstance(record, dict) or record.get('user_id') != user_id or not _identifier(record.get('id')) or not _identifier(record.get('task_id')):
        return False
    if task_id is not None and record['task_id'] != task_id:
        return False
    stored_conversation = record.get('conversation_id')
    if stored_conversation is not None and (not _identifier(stored_conversation)
            or conversation_id is not None and stored_conversation != conversation_id):
        return False
    try:
        parts = _parts(record['directory'], conversation_id=conversation_id or stored_conversation)
        if parts[0] == 'conversations' and stored_conversation != parts[1]:
            return False
        _public_base(record['public_base'])
    except (KeyError, PublicationError):
        return False
    site_id = record.get('site_id')
    if site_id is not None and (not _slug(site_id) or type(record.get('site_revision')) is not int
            or record['site_revision'] < 1 or not _identifier(stored_conversation)):
        return False
    suffix = '/' + site_id + '/' if site_id is not None else '/sites/' + record['id'] + '/'
    if record.get('url') != record['public_base'].rstrip('/') + suffix:
        return False
    if not isinstance(record.get('title'), str) or len(record['title']) > 160:
        return False
    return (type(record.get('file_count')) is int and 1 <= record['file_count'] <= MAX_FILES
            and type(record.get('total_bytes')) is int and 0 <= record['total_bytes'] <= MAX_TOTAL_BYTES)


def _public_record(record):
    result = {key: record[key] for key in _PUBLIC_FIELDS}
    if record.get('site_id') is not None:
        result['site_id'] = record['site_id']
    return result


def _existing(records_fd, published_fd, user_id, task_id, conversation_id):
    try:
        mapping = _read_json(records_fd, 'task-' + task_id + '.json')
    except FileNotFoundError:
        return None
    if mapping.get('user_id') != user_id or mapping.get('task_id') != task_id or not _identifier(mapping.get('id')):
        raise PublicationError('发布记录校验失败')
    record = _read_json(records_fd, mapping['id'] + '.json')
    if record.get('id') != mapping['id'] or not _record_valid(record, user_id=user_id, task_id=task_id, conversation_id=conversation_id):
        raise PublicationError('发布记录校验失败')
    try:
        with _directory_at(published_fd, record['id']):
            pass
    except FileNotFoundError:
        # The mapping was durable but the atomic site rename did not happen.
        # Keep this private abandoned record and retry with a fresh snapshot.
        return None
    return record


def _write_json(directory_fd, name, value):
    temporary = '.record-' + uuid.uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    try:
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        offset = 0
        while offset < len(raw):
            offset += os.write(fd, raw[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _site_valid(site, *, user_id=None, conversation_id=None):
    if (not isinstance(site, dict) or not _slug(site.get('id'))
            or not _identifier(site.get('user_id')) or not _identifier(site.get('conversation_id'))):
        return False
    if user_id is not None and site['user_id'] != user_id or conversation_id is not None and site['conversation_id'] != conversation_id:
        return False
    try:
        normal = _normalized_directory(site['directory'], site['conversation_id'])
        if normal != site.get('normalized_directory'):
            return False
        base = _public_base(site['public_base'])
    except (KeyError, PublicationError):
        return False
    snapshot = site.get('current_snapshot')
    revision, allocated = site.get('revision'), site.get('allocated_revision')
    return (site.get('url') == base + '/' + site['id'] + '/'
            and (snapshot is None or _identifier(snapshot))
            and type(revision) is int and revision >= 0
            and type(allocated) is int and allocated >= revision
            and ((snapshot is None and revision == 0) or (snapshot is not None and revision > 0))
            and isinstance(site.get('title'), str) and len(site['title']) <= 160)


def _new_slug():
    return ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(6))


def _exists_at(directory_fd, name):
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _reserve_site(sites_fd, projects_fd, short_fd, current, directory, title, base, *, requested=None):
    """Make a durable owner/project reservation before exposing any alias."""
    if requested is not None and not _slug(requested):
        raise PublicationError('网站 ID 必须是 4 到 10 位小写字母或数字，且不能使用保留名称')
    normal = _normalized_directory(directory, current['conversation_id'])
    key = _project_key(current['user_id'], current['conversation_id'], normal)
    try:
        mapping = _read_json(projects_fd, key + '.json')
    except FileNotFoundError:
        mapping = None
    if mapping is not None:
        if (mapping.get('user_id') != current['user_id']
                or mapping.get('conversation_id') != current['conversation_id']
                or mapping.get('directory') != normal or not _slug(mapping.get('site_id'))):
            raise PublicationError('网站项目记录校验失败')
        if requested is not None and requested != mapping['site_id']:
            raise PublicationError('这个项目已有固定网站 ID，请沿用原 ID')
        site = _read_json(sites_fd, mapping['site_id'] + '.json')
        if not _site_valid(site, user_id=current['user_id'], conversation_id=current['conversation_id']):
            raise PublicationError('网站归属记录校验失败')
        return site
    if requested is not None:
        try:
            site = _read_json(sites_fd, requested + '.json')
        except FileNotFoundError:
            site = None
        if site is not None and not _site_valid(site, user_id=current['user_id'], conversation_id=current['conversation_id']):
            raise PublicationError('这个网站 ID 不属于当前用户和对话')
        if site is None and _exists_at(short_fd, requested):
            raise PublicationError('这个网站 ID 已被占用')
    else:
        site = None
        for _ in range(128):
            requested = _new_slug()
            if (_slug(requested) and not _exists_at(sites_fd, requested + '.json')
                    and not _exists_at(short_fd, requested)):
                break
        else:
            raise PublicationError('暂时无法分配网站 ID，请稍后再试')
    if site is None:
        now = time.time()
        site = {'id': requested, 'user_id': current['user_id'],
                'conversation_id': current['conversation_id'], 'directory': directory,
                'normalized_directory': normal, 'current_snapshot': None,
                'revision': 0, 'allocated_revision': 0, 'public_base': base,
                'url': base + '/' + requested + '/', 'title': title,
                'created_at': now, 'updated_at': now}
        _write_json(sites_fd, requested + '.json', site)
    # A failed reservation may leave an unexposed slug record; never reuse it
    # for another owner. Once an alias is exposed this mapping is already fsynced.
    _write_json(projects_fd, key + '.json', {'site_id': site['id'],
                'user_id': current['user_id'], 'conversation_id': current['conversation_id'],
                'directory': normal})
    return site


def _allocate_release(sites_fd, site, record):
    revision = site['allocated_revision'] + 1
    reserved = {**site, 'allocated_revision': revision}
    _write_json(sites_fd, site['id'] + '.json', reserved)
    return {**record, 'site_id': site['id'], 'site_revision': revision,
            'public_base': site['public_base'], 'url': site['url']}


def _alias_target(short_fd, slug):
    try:
        info = os.stat(slug, dir_fd=short_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(info.st_mode) or os.geteuid() == 0 and info.st_uid != 0:
        raise PublicationError('网站访问入口校验失败')
    target = os.readlink(slug, dir_fd=short_fd)
    if not target.startswith('../published/') or not _identifier(target[len('../published/'):]):
        raise PublicationError('网站访问入口校验失败')
    return target[len('../published/'):]


def _replace_alias(short_fd, slug, snapshot):
    _alias_target(short_fd, slug)  # Never replace a directory or an alien link.
    temporary = '.alias-' + uuid.uuid4().hex
    os.symlink('../published/' + snapshot, temporary, dir_fd=short_fd)
    try:
        if os.geteuid() == 0:
            os.chown(temporary, 0, 0, dir_fd=short_fd, follow_symlinks=False)
        os.replace(temporary, slug, src_dir_fd=short_fd, dst_dir_fd=short_fd)
        os.fsync(short_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=short_fd)
        except FileNotFoundError:
            pass


def _commit_release(records_fd, published_fd, sites_fd, short_fd, record):
    """Finish a prepared release; a recovered old task never rolls back a site."""
    if not _record_valid(record, user_id=record.get('user_id'),
                         conversation_id=record.get('conversation_id')) or not record.get('site_id'):
        raise PublicationError('网站发布恢复记录校验失败')
    with _directory_at(published_fd, record['id']):
        pass
    site = _read_json(sites_fd, record['site_id'] + '.json')
    if (not _site_valid(site, user_id=record['user_id'], conversation_id=record['conversation_id'])
            or record['site_revision'] > site['allocated_revision']):
        raise PublicationError('网站归属记录校验失败')
    if record['site_revision'] > site['revision']:
        _replace_alias(short_fd, site['id'], record['id'])
        site = {**site, 'directory': record['directory'],
                'normalized_directory': _normalized_directory(record['directory'], record['conversation_id']),
                'current_snapshot': record['id'], 'revision': record['site_revision'],
                'title': record['title'], 'updated_at': time.time()}
        _write_json(sites_fd, site['id'] + '.json', site)
    elif record['site_revision'] == site['revision']:
        if site['current_snapshot'] != record['id']:
            raise PublicationError('网站版本记录校验失败')
        # Metadata is committed after the alias fsync. If an operator removed
        # the link, restore only this current revision, never an older release.
        if _alias_target(short_fd, site['id']) != record['id']:
            _replace_alias(short_fd, site['id'], record['id'])
    else:
        if _alias_target(short_fd, site['id']) != site['current_snapshot']:
            raise PublicationError('网站访问入口与当前版本不匹配')
    _write_json(records_fd, record['id'] + '.json', record)
    # LAST: only a fully recoverable snapshot, live alias and committed site
    # metadata may become this task's idempotent completed publication.
    _write_json(records_fd, 'task-' + record['task_id'] + '.json',
                {'id': record['id'], 'user_id': record['user_id'], 'task_id': record['task_id']})
    try:
        os.unlink('pending-task-' + record['task_id'] + '.json', dir_fd=records_fd)
        os.fsync(records_fd)
    except FileNotFoundError:
        pass
    return _public_record(record)


def _pending(records_fd, published_fd, current):
    try:
        record = _read_json(records_fd, 'pending-task-' + current['id'] + '.json')
    except FileNotFoundError:
        return None
    if (not _record_valid(record, user_id=current['user_id'], task_id=current['id'],
                          conversation_id=current['conversation_id']) or not record.get('site_id')):
        raise PublicationError('网站发布恢复记录校验失败')
    try:
        with _directory_at(published_fd, record['id']):
            pass
    except FileNotFoundError:
        return None  # Crash happened before the immutable snapshot rename.
    return record


def _copy_tree(source_fd, destination_fd, counts, depth=0):
    if depth >= MAX_DEPTH:
        raise PublicationError('网站目录层级过深')
    with os.scandir(source_fd) as entries:
        for entry in entries:
            counts['entries'] += 1
            if counts['entries'] > MAX_ENTRIES:
                raise PublicationError('网站目录包含过多条目')
            name = entry.name
            if name.startswith('.'):
                continue  # Never publish .env, .git, or any hidden entry.
            if '\\' in name or any(ord(c) < 32 for c in name):
                raise PublicationError('网站文件名包含不支持的字符')
            descriptor = os.open(name, _FILE_FLAGS, dir_fd=source_fd)
            try:
                before = os.fstat(descriptor)
                if stat.S_ISDIR(before.st_mode):
                    # The opened inode, rather than a later path lookup, is used
                    # recursively. Replacing the source directory cannot escape.
                    os.mkdir(name, mode=0o755, dir_fd=destination_fd)
                    with _directory_at(destination_fd, name) as child:
                        os.fchmod(child, 0o755)
                        _copy_tree(descriptor, child, counts, depth + 1)
                elif stat.S_ISREG(before.st_mode):
                    if before.st_nlink != 1:
                        raise PublicationError('网站中不能包含硬链接')
                    counts['file_count'] += 1
                    if counts['file_count'] > MAX_FILES:
                        raise PublicationError('网站最多可发布 1000 个文件')
                    if before.st_size > MAX_FILE_BYTES:
                        raise PublicationError('网站单个文件不能超过 16 MiB')
                    output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=destination_fd)
                    copied = 0
                    try:
                        os.fchmod(output, 0o644)
                        while True:
                            piece = os.read(descriptor, 128 * 1024)
                            if not piece:
                                break
                            copied += len(piece)
                            counts['total_bytes'] += len(piece)
                            if copied > MAX_FILE_BYTES or counts['total_bytes'] > MAX_TOTAL_BYTES:
                                raise PublicationError('网站大小超出限制：单文件 16 MiB，总计 64 MiB')
                            offset = 0
                            while offset < len(piece):
                                offset += os.write(output, piece[offset:])
                        os.fsync(output)
                    finally:
                        os.close(output)
                    after = os.fstat(descriptor)
                    if copied != before.st_size or before.st_mtime_ns != after.st_mtime_ns or before.st_ctime_ns != after.st_ctime_ns or after.st_nlink != 1:
                        raise PublicationError('网站文件正在变化，请完成构建后再发布')
                else:
                    raise PublicationError('网站中只能包含普通文件和目录')
            finally:
                os.close(descriptor)
    os.fsync(destination_fd)


def _assert_current_task(store, task, *, allow_completed=False, connection=None):
    if (not isinstance(task, dict) or not _identifier(task.get('id'))
            or not _identifier(task.get('user_id')) or not _identifier(task.get('conversation_id'))):
        raise PublicationError('发布任务无效')
    current = (store.get_task(task['user_id'], task['id']) if connection is None
               else dict(store._require_task(connection, task['user_id'], task['id'])))
    if current.get('conversation_id') != task.get('conversation_id'):
        raise PublicationError('发布任务与对话不匹配')
    if allow_completed and current['status'] == 'succeeded' and not current['cancel_requested']:
        return current
    if (current['status'] != 'running' or not task.get('worker_id')
            or current['worker_id'] != task['worker_id'] or current['cancel_requested']
            or (current.get('lease_expires_at') or 0) <= time.time()):
        raise PublicationError('任务已停止或执行权已过期，不能发布网站')
    return current


def publish_requested(store, task, public_base=None, *, allow_completed=False):
    """Publish this task's snapshot behind its project's permanent address.

    A committed task returns its previous canonical URL without changing the
    live version. A trusted operator may explicitly allow a succeeded task;
    never expose allow_completed as a browser/agent-controlled option.
    """
    staging = None
    records_fd = published_fd = lock_fd = sites_fd = projects_fd = short_fd = None
    try:
        current = _assert_current_task(store, task, allow_completed=allow_completed)
        workspace = store.workspace_path(current['user_id'])
        with _directory_at(None, workspace) as workspace_fd:
            published_fd = _host_directory(Path(DATA) / 'published', 0o755)
            records_fd = _host_directory(Path(DATA) / 'publication-records', 0o700)
            sites_fd = _host_directory(Path(DATA) / 'publication-records' / 'sites', 0o700)
            projects_fd = _host_directory(Path(DATA) / 'publication-records' / 'projects', 0o700)
            short_fd = _host_directory(Path(DATA) / 'published-short', 0o755)
            lock_fd = os.open('.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=records_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            pending = _pending(records_fd, published_fd, current)
            if pending is not None:
                with store._transaction() as connection:
                    _assert_current_task(store, task, allow_completed=allow_completed, connection=connection)
                    return _commit_release(records_fd, published_fd, sites_fd, short_fd, pending)
            existing = _existing(records_fd, published_fd, current['user_id'], current['id'], current['conversation_id'])
            if existing:
                return _public_record(existing)
            try:
                with _directory_at(workspace_fd, '.ever-publish') as manifests_fd:
                    manifest = _read_json(manifests_fd, current['id'] + '.json')
            except FileNotFoundError:
                return None
            if type(manifest.get('version')) is not int or manifest['version'] != 1 or manifest.get('task_id') != current['id']:
                raise PublicationError('发布说明与当前任务不匹配')
            parts = _parts(manifest.get('directory'), conversation_id=current['conversation_id'])
            title = manifest.get('title', 'Website')
            if not isinstance(title, str):
                raise PublicationError('网站标题格式无效')
            title = title.strip()[:160] or 'Website'
            base = _configured_public_base(public_base)
            requested = manifest.get('site_id')
            if requested is not None and not _slug(requested):
                raise PublicationError('网站 ID 必须是 4 到 10 位小写字母或数字，且不能使用保留名称')
            site_id = uuid.uuid4().hex
            staging = '.staging-' + site_id
            os.mkdir(staging, mode=0o755, dir_fd=published_fd)
            counts = {'entries': 0, 'file_count': 0, 'total_bytes': 0}
            source_fd = os.dup(workspace_fd)
            try:
                for part in parts:
                    next_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=source_fd)
                    os.close(source_fd)
                    source_fd = next_fd
                with _directory_at(published_fd, staging) as stage_fd:
                    os.fchmod(stage_fd, 0o755)
                    _copy_tree(source_fd, stage_fd, counts)
                    index_fd = os.open('index.html', _FILE_FLAGS, dir_fd=stage_fd)
                    try:
                        if not stat.S_ISREG(os.fstat(index_fd).st_mode):
                            raise PublicationError('发布目录必须包含 index.html')
                    finally:
                        os.close(index_fd)
            finally:
                os.close(source_fd)
            record = {'id': site_id, 'url': base + '/sites/' + site_id + '/',
                      'title': title, 'directory': manifest['directory'],
                      'file_count': counts['file_count'], 'total_bytes': counts['total_bytes'],
                      'user_id': current['user_id'], 'task_id': current['id'],
                      'conversation_id': current['conversation_id'],
                      'public_base': base, 'created_at': time.time()}
            # Only the short commit holds SQLite's write fence. Cancellation
            # or lease recovery cannot race the final ownership check/rename;
            # asset copying never blocks the queue transaction.
            with store._transaction() as connection:
                _assert_current_task(store, task, allow_completed=allow_completed,
                                     connection=connection)
                site = _reserve_site(sites_fd, projects_fd, short_fd, current,
                                     record['directory'], title, base, requested=requested)
                record = _allocate_release(sites_fd, site, record)
                _write_json(records_fd, 'pending-task-' + current['id'] + '.json', record)
                os.rename(staging, site_id, src_dir_fd=published_fd, dst_dir_fd=published_fd)
                staging = None
                os.fsync(published_fd)
                return _commit_release(records_fd, published_fd, sites_fd, short_fd, record)
    except PublicationError:
        raise
    except Exception:
        raise PublicationError('网站发布失败，请检查发布目录、普通文件及 index.html') from None
    finally:
        if staging is not None:
            # Only our unpredictable host-owned staging directory is removed.
            shutil.rmtree(Path(DATA) / 'published' / staging, ignore_errors=True)
        for fd in (lock_fd, records_fd, published_fd, sites_fd, projects_fd, short_fd):
            if fd is not None:
                os.close(fd)


def promote_existing(store, task, snapshot_id, *, slug=None,
                     public_base=None, allow_completed=False):
    """Trusted operator: give an existing owned release a permanent short URL.

    No user files are recopied and the original /sites/<snapshot>/ route stays
    valid. Completed historical tasks require explicit allow_completed=True.
    """
    records_fd = published_fd = lock_fd = sites_fd = projects_fd = short_fd = None
    try:
        current = _assert_current_task(store, task, allow_completed=allow_completed)
        if not _identifier(snapshot_id):
            raise PublicationError('网站快照 ID 无效')
        if slug is not None and not _slug(slug):
            raise PublicationError('网站 ID 必须是 4 到 10 位小写字母或数字，且不能使用保留名称')
        published_fd = _host_directory(Path(DATA) / 'published', 0o755)
        records_fd = _host_directory(Path(DATA) / 'publication-records', 0o700)
        sites_fd = _host_directory(Path(DATA) / 'publication-records' / 'sites', 0o700)
        projects_fd = _host_directory(Path(DATA) / 'publication-records' / 'projects', 0o700)
        short_fd = _host_directory(Path(DATA) / 'published-short', 0o755)
        lock_fd = os.open('.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=records_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        pending = _pending(records_fd, published_fd, current)
        if pending is not None:
            if pending['id'] != snapshot_id or slug is not None and slug != pending['site_id']:
                raise PublicationError('这个任务已有不同的网站发布恢复记录')
            record = pending
        else:
            record = _read_json(records_fd, snapshot_id + '.json')
            if not _record_valid(record, user_id=current['user_id'], task_id=current['id'],
                                 conversation_id=current['conversation_id']):
                raise PublicationError('网站快照不属于当前用户和任务')
            with _directory_at(published_fd, snapshot_id):
                pass
            if record.get('site_id'):
                if slug is not None and slug != record['site_id']:
                    raise PublicationError('这个网站已经有固定 ID，请沿用原 ID')
            else:
                record = {**record, 'conversation_id': current['conversation_id']}
                site = _reserve_site(sites_fd, projects_fd, short_fd, current,
                                     record['directory'], record['title'], _configured_public_base(public_base), requested=slug)
                record = _allocate_release(sites_fd, site, record)
                _write_json(records_fd, 'pending-task-' + current['id'] + '.json', record)
        with store._transaction() as connection:
            _assert_current_task(store, task, allow_completed=allow_completed, connection=connection)
            return _commit_release(records_fd, published_fd, sites_fd, short_fd, record)
    except PublicationError:
        raise
    except Exception:
        raise PublicationError('网站固定地址设置失败，请检查已有网站及发布记录') from None
    finally:
        for fd in (lock_fd, records_fd, published_fd, sites_fd, projects_fd, short_fd):
            if fd is not None:
                os.close(fd)


def list_publications(data, user_id, task_ids=None):
    """Read bounded private records; return only publications owned by user_id."""
    if not _identifier(user_id):
        return []
    if task_ids is not None:
        task_ids = set(task_ids)
        if len(task_ids) > 200 or any(not _identifier(value) for value in task_ids):
            raise PublicationError('一次最多查询 200 个有效任务')
    result = []
    try:
        fd = os.open(Path(data) / 'publication-records', _DIRECTORY_FLAGS)
    except FileNotFoundError:
        return result
    try:
        if task_ids is not None:
            # Conversation history resolves only its own requested releases;
            # it must not depend on a global scan cap or directory ordering.
            with _directory_at(None, Path(data) / 'published') as published_fd:
                for task_id in task_ids:
                    try:
                        mapping = _read_json(fd, 'task-' + task_id + '.json')
                        if mapping.get('user_id') != user_id or mapping.get('task_id') != task_id or not _identifier(mapping.get('id')):
                            continue
                        record = _read_json(fd, mapping['id'] + '.json')
                        if record.get('id') != mapping['id'] or not _record_valid(record, user_id=user_id, task_id=task_id):
                            continue
                        with _directory_at(published_fd, record['id']):
                            pass
                        result.append({**_public_record(record), 'task_id': record['task_id']})
                    except (OSError, PublicationError):
                        continue
        else:
            with os.scandir(fd) as entries:
                for count, entry in enumerate(entries):
                    if count >= MAX_RECORDS:
                        break
                    if len(entry.name) != 37 or not entry.name.endswith('.json') or not _identifier(entry.name[:-5]):
                        continue
                    try:
                        record = _read_json(fd, entry.name)
                        if record.get('id') != entry.name[:-5] or not _record_valid(record, user_id=user_id):
                            continue
                        # A pre-commit record or a removed site is not advertised.
                        with _directory_at(None, Path(data) / 'published' / record['id']):
                            pass
                        result.append({**_public_record(record), 'task_id': record['task_id']})
                    except (OSError, PublicationError):
                        continue
    except FileNotFoundError:
        pass
    finally:
        os.close(fd)
    return sorted(result, key=lambda record: record['id'])


def list_sites(data, user_id, conversation_id):
    """Return live permanent websites owned by this user and conversation."""
    if not _identifier(user_id) or not _identifier(conversation_id):
        return []
    result = []
    try:
        with _directory_at(None, Path(data) / 'publication-records') as records_fd, \
                _directory_at(records_fd, 'sites') as sites_fd, \
                _directory_at(None, Path(data) / 'published') as published_fd, \
                _directory_at(None, Path(data) / 'published-short') as short_fd:
            with os.scandir(sites_fd) as entries:
                for count, entry in enumerate(entries):
                    if count >= MAX_RECORDS:
                        break
                    if not entry.name.endswith('.json') or not _slug(entry.name[:-5]):
                        continue
                    try:
                        site = _read_json(sites_fd, entry.name)
                        if (not _site_valid(site, user_id=user_id, conversation_id=conversation_id)
                                or site['id'] != entry.name[:-5] or not site['current_snapshot']
                                or _alias_target(short_fd, site['id']) != site['current_snapshot']):
                            continue
                        with _directory_at(published_fd, site['current_snapshot']):
                            pass
                        record = _read_json(records_fd, site['current_snapshot'] + '.json')
                        if (not _record_valid(record, user_id=user_id, conversation_id=conversation_id)
                                or record.get('site_id') != site['id'] or record['site_revision'] != site['revision']):
                            continue
                        result.append(_public_record(record))
                    except (OSError, PublicationError):
                        continue
    except (OSError, PublicationError):
        return []
    return sorted(result, key=lambda site: site['site_id'])
