#!/usr/bin/env python3
"""Copy legacy workspace files into an unambiguous existing conversation.

Run after updating Store. Originals and already-published snapshots stay intact.
Users with several conversations require explicit operator classification.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys

FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def digest(fd):
    result = hashlib.sha256()
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, 65536):
        result.update(chunk)
    return result.hexdigest()


def stable(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def copy_tree(source, destination, *, top=False):
    copied = 0
    for name in sorted(os.listdir(source)):
        if top and name in ('conversations', '.ever-publish'):
            continue
        before = os.stat(name, dir_fd=source, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            child_source = os.open(name, FLAGS, dir_fd=source)
            try:
                try:
                    os.mkdir(name, 0o700, dir_fd=destination)
                except FileExistsError:
                    pass
                child_destination = os.open(name, FLAGS, dir_fd=destination)
                try:
                    if os.geteuid() == 0:
                        os.fchown(child_destination, 10001, 10001)
                    copied += copy_tree(child_source, child_destination)
                finally:
                    os.close(child_destination)
            finally:
                os.close(child_source)
        elif stat.S_ISREG(before.st_mode):
            input_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source)
            try:
                opened = os.fstat(input_fd)
                if not stat.S_ISREG(opened.st_mode) or stable(before) != stable(opened):
                    raise RuntimeError('Source changed before copy: ' + name)
                try:
                    existing = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                       dir_fd=destination)
                except FileNotFoundError:
                    existing = None
                if existing is not None:
                    try:
                        if not stat.S_ISREG(os.fstat(existing).st_mode) or digest(existing) != digest(input_fd):
                            raise RuntimeError('Destination already has different content: ' + name)
                        if stable(opened) != stable(os.fstat(input_fd)):
                            raise RuntimeError('Source changed during comparison: ' + name)
                    finally:
                        os.close(existing)
                    continue
                temporary = '.aver-migrate-' + secrets.token_hex(16)
                output_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                    stat.S_IMODE(before.st_mode) & 0o700, dir_fd=destination)
                created_inode = os.fstat(output_fd).st_ino
                try:
                    with os.fdopen(output_fd, 'wb') as output:
                        while chunk := os.read(input_fd, 65536):
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                        if os.geteuid() == 0:
                            os.fchown(output.fileno(), 10001, 10001)
                    if stable(opened) != stable(os.fstat(input_fd)):
                        raise RuntimeError('Source changed during copy: ' + name)
                    # No-clobber atomic commit: a crash leaves at most a hidden
                    # temporary copy; an existing/user-created final is retained.
                    os.link(temporary, name, src_dir_fd=destination, dst_dir_fd=destination,
                            follow_symlinks=False)
                    copied += 1
                finally:
                    try:
                        if os.stat(temporary, dir_fd=destination, follow_symlinks=False).st_ino == created_inode:
                            os.unlink(temporary, dir_fd=destination)
                    except FileNotFoundError:
                        pass
            finally:
                os.close(input_fd)
        else:
            raise RuntimeError('Legacy link or special file needs manual classification: ' + name)
    os.fsync(destination)
    return copied


def migrate(store):
    results = []
    for user in store.list_all_users():
        conversations = store.list_conversations(user['id'])
        if len(conversations) != 1:
            results.append({'user': user['username'], 'copied': 0,
                            'status': 'manual classification required' if conversations else 'no conversation'})
            continue
        conversation = conversations[0]
        workspace = store.workspace_path(user['id'])
        source = os.open(workspace, FLAGS)
        try:
            with store.open_conversation_workspace(user['id'], conversation['id']) as destination:
                count = copy_tree(source, destination, top=True)
        finally:
            os.close(source)
        results.append({'user': user['username'], 'conversation': conversation['id'],
                        'copied': count, 'status': 'classified; originals retained'})
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-dir', type=Path, default=Path('/opt/agent-cloud'))
    parser.add_argument('--data-dir', type=Path, default=Path('/srv/agent-cloud/data'))
    args = parser.parse_args()
    sys.path.insert(0, str(args.app_dir))
    from store import Store
    print(json.dumps(migrate(Store(args.data_dir)), ensure_ascii=False))
