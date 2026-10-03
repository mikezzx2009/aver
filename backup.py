#!/usr/bin/env python3
"""Online DB backup + file archive. This does not snapshot live process memory."""
import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile

from common import DATA, CONFIG_PATH, ROOT

def main():
    os.umask(0o077)
    backups=DATA/'backups'
    backups.mkdir(mode=0o700, exist_ok=True)
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    # Keep all records. A full disk stops backups explicitly; nothing is silently purged.
    if shutil.disk_usage(backups).free < 2*1024**3:
        raise RuntimeError('Insufficient disk space for backup')
    output=backups/('agent-cloud-'+stamp+'.tar.gz')
    temporary=output.with_suffix('.partial')
    with tempfile.TemporaryDirectory(dir=backups) as temp:
        temp=Path(temp)
        for database in ('agent-cloud.sqlite3','proxy.sqlite3'):
            source=DATA/'data'/database
            if source.exists():
                with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=30) as src, sqlite3.connect(temp/database) as dst:
                    src.backup(dst)
        (temp/'manifest.json').write_text(json.dumps({'created_at':stamp,'consistency':'SQLite databases are individually consistent online snapshots; files are copied live; no RAM/process snapshot','restore':'Restore data database and full home/workspaces, recreate sandbox from pinned image, reconcile interrupted tasks'}))
        with tarfile.open(temporary,'w:gz',dereference=False) as archive:
            for source in temp.iterdir():
                archive.add(source, arcname='databases/'+source.name)
            for name in ('sandboxes','runs','data/workspaces','published','published-short','publication-records'):
                source=DATA/name
                if source.exists():
                    archive.add(source,arcname=name)
            if CONFIG_PATH.exists():
                archive.add(CONFIG_PATH,arcname='private/config.json')
            for name in ('common.py','server.py','server_stats.py','store.py','worker.py','publisher.py','backup.py','sandbox','deploy','static'):
                archive.add(ROOT/name,arcname='source/'+name)
    os.replace(temporary,output)
    print(json.dumps({'backup':str(output),'offsite':False}),flush=True)

if __name__=='__main__':
    main()
