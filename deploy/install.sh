#!/usr/bin/env bash
# Run from the unpacked deployment source: sudo bash deploy/install.sh
# This installs the private application. Publishing the HTTPS vhost is a separate step.
set -Eeuo pipefail
umask 027

APP_DIR=/opt/agent-cloud
DATA_DIR=/srv/agent-cloud
CONFIG_DIR=/etc/agent-cloud
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

die() { printf '%s\n' "$*" >&2; exit 1; }
[[ ${EUID} -eq 0 ]] || die 'Run this installer as root (sudo bash deploy/install.sh).'
[[ -r /etc/os-release ]] || die 'A Linux server is required.'
. /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || die 'This installer targets Ubuntu 24.04 LTS.'
[[ $(dpkg --print-architecture) == amd64 ]] || die 'This deployment targets x86_64 / amd64.'
for source_file in server.py server_stats.py common.py store.py worker.py publisher.py backup.py sandbox/Dockerfile sandbox/task_runner.py deploy/wait-ready.py deploy/renew-nginx.sh; do
    [[ -f "$SOURCE_DIR/$source_file" ]] || die "Deployment source is missing $source_file. Unpack the complete project first."
done

export DEBIAN_FRONTEND=noninteractive
apt-get -o Acquire::Retries=3 update
apt-get install -y --no-install-recommends \
    docker.io docker-buildx nginx python3-venv python3-pip ca-certificates curl gnupg \
    iptables rsync util-linux certbot python3-certbot-nginx

# Official signed release repository: https://gvisor.dev/docs/user_guide/install/
# Install the whole package, including sidecar binaries required by current runsc releases.
# A failed download/signature check is fatal. There is no ordinary-runc fallback.
task_tmp_dir="$(mktemp -d /tmp/agent-cloud-install.XXXXXXXX)"
trap 'rm -rf -- "$task_tmp_dir"' EXIT
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    --retry 3 https://gvisor.dev/archive.key -o "$task_tmp_dir/archive.key"
gpg --batch --yes --dearmor --output "$task_tmp_dir/gvisor-archive-keyring.gpg" "$task_tmp_dir/archive.key"
install -m 0644 "$task_tmp_dir/gvisor-archive-keyring.gpg" /usr/share/keyrings/gvisor-archive-keyring.gpg
printf '%s\n' 'deb [arch=amd64 signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main' \
    > /etc/apt/sources.list.d/gvisor.list
printf '%s\n' 'Package: runsc' 'Pin: origin "storage.googleapis.com"' 'Pin-Priority: 1001' \
    > /etc/apt/preferences.d/agent-cloud-runsc
apt-get -o Acquire::Retries=3 update
apt-get install -y --no-install-recommends --reinstall --allow-downgrades runsc
# Use the executable owned by the verified package, not a PATH-shadowing legacy copy.
RUNSC_BIN="$(dpkg-query -L runsc | python3 -c '
import os, pathlib, sys
paths = [p.strip() for p in sys.stdin if pathlib.Path(p.strip()).name == "runsc"
         and pathlib.Path(p.strip()).is_file() and os.access(p.strip(), os.X_OK)]
if len(paths) != 1:
    raise SystemExit("Cannot uniquely locate the package-installed runsc executable.")
print(paths[0])
')"
[[ -n "$RUNSC_BIN" && -x "$RUNSC_BIN" ]] || die 'The verified gVisor package did not provide runsc.'
"$RUNSC_BIN" --version

install -d -m 0750 "$APP_DIR" "$DATA_DIR" "$DATA_DIR/sandboxes" "$DATA_DIR/backups" "$CONFIG_DIR"
install -d -m 0700 "$DATA_DIR/data" "$DATA_DIR/data/workspaces"
install -d -m 0700 "$CONFIG_DIR/docker-client"
install -d -m 0755 /var/www/html /etc/docker

# Merge only our runtime into existing daemon configuration; do not erase other settings.
# overlay2=none makes writable rootfs changes visible to Docker's backing filesystem.
daemon_changed="$(python3 - "$RUNSC_BIN" <<'PY'
import json, os, pathlib, sys, tempfile
path = pathlib.Path('/etc/docker/daemon.json')
settings = json.loads(path.read_text()) if path.exists() else {}
if not isinstance(settings, dict):
    raise SystemExit('Docker daemon.json must be an object; refusing to replace it.')
desired = {'path': sys.argv[1], 'runtimeArgs': ['--platform=systrap', '--overlay2=none']}
runtimes = settings.setdefault('runtimes', {})
if not isinstance(runtimes, dict):
    raise SystemExit('Docker runtimes setting must be an object.')
changed = runtimes.get('runsc-systrap') != desired
if changed:
    runtimes['runsc-systrap'] = desired
    fd, tmp = tempfile.mkstemp(prefix='.agent-cloud-', dir=str(path.parent))
    with os.fdopen(fd, 'w') as stream:
        json.dump(settings, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
print('yes' if changed else 'no')
PY
)"

# Never copy a local virtualenv, user data, or credentials into the server runtime.
if [[ "$SOURCE_DIR" != "$APP_DIR" ]]; then
    rsync -a --exclude='.venv/' --exclude='.git/' --exclude='__pycache__/' \
        --exclude='.pytest_cache/' --exclude='var/' --exclude='data/' \
        --exclude='config.json' --exclude='.env' "$SOURCE_DIR/" "$APP_DIR/"
fi
chown -R root:root "$APP_DIR"
chmod 0750 "$APP_DIR"
chmod 0755 "$APP_DIR/deploy/install.sh" "$APP_DIR/deploy/network.sh"
[[ -x "$APP_DIR/.venv/bin/python" ]] || python3 -m venv "$APP_DIR/.venv"
if [[ -f "$APP_DIR/requirements.txt" ]]; then
    "$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --no-cache-dir -r "$APP_DIR/requirements.txt"
fi
if [[ ! -e "$CONFIG_DIR/config.json" ]]; then
    printf '%s\n' '{}' > "$CONFIG_DIR/config.json"
fi
[[ -f "$CONFIG_DIR/config.json" && ! -L "$CONFIG_DIR/config.json" ]] || die 'config.json must be a regular file.'
chown root:root "$CONFIG_DIR/config.json"
chmod 0600 "$CONFIG_DIR/config.json"

for unit in agent-cloud.service agent-cloud-worker.service agent-cloud-network.service \
            agent-cloud-backup.service agent-cloud-backup.timer; do
    install -m 0644 "$APP_DIR/deploy/$unit" "/etc/systemd/system/$unit"
done
# Install a template only. Do not expose setup credentials or login over HTTP.
install -m 0644 "$APP_DIR/deploy/agent-cloud.nginx.conf" /etc/nginx/sites-available/agent-cloud
# Only copy the public-static template; enabling it and directory traversal
# permissions is an explicit publication step after the IP URL is configured.
if [[ -f "$APP_DIR/deploy/agent-cloud-sites.nginx.conf" ]]; then
    install -m 0644 "$APP_DIR/deploy/agent-cloud-sites.nginx.conf" /etc/nginx/sites-available/agent-cloud-sites
fi
install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
install -m 0755 "$APP_DIR/deploy/renew-nginx.sh" /etc/letsencrypt/renewal-hooks/deploy/agent-cloud-nginx

systemctl daemon-reload
systemctl enable docker.service
if [[ "$daemon_changed" == yes ]]; then
    systemctl restart docker.service
else
    systemctl start docker.service
fi
systemctl enable agent-cloud-network.service
systemctl restart agent-cloud-network.service

# Build the trusted base separately from untrusted tenant execution.
# The Dockerfile uses COPY --chmod, which requires BuildKit. Ubuntu's docker.io
# does not guarantee that the optional buildx plugin is installed with it.
docker --config "$CONFIG_DIR/docker-client" buildx version
DOCKER_BUILDKIT=1 docker --config "$CONFIG_DIR/docker-client" build \
    --tag agent-cloud-sandbox:0.1 "$APP_DIR/sandbox"
docker --config "$CONFIG_DIR/docker-client" image inspect agent-cloud-sandbox:0.1 \
    > "$DATA_DIR/sandboxes/base-image.json"
chmod 0600 "$DATA_DIR/sandboxes/base-image.json"
docker --config "$CONFIG_DIR/docker-client" run --rm \
    --runtime=runsc-systrap --network=none --read-only \
    --cap-drop=ALL --security-opt=no-new-privileges=true \
    --memory=256m --memory-swap=256m --cpus=0.5 \
    --entrypoint=/usr/bin/python3 agent-cloud-sandbox:0.1 \
    -c 'print("gVisor systrap runtime check passed")'

systemctl enable agent-cloud.service agent-cloud-worker.service agent-cloud-backup.timer
systemctl restart agent-cloud.service agent-cloud-worker.service
systemctl start agent-cloud-backup.timer
printf '%s\n' \
    'Private application installed: http://127.0.0.1:8080' \
    'Sandbox model proxy: http://172.29.0.1:8090 (agent bridge only)' \
    'Nginx templates remain disabled. Replace their example domain/IP with your own values.' \
    'Set domain and site_base_url in /etc/agent-cloud/config.json; obtain TLS before enabling the application vhost.' \
    'Enable the separate static sites vhost and its directory permissions before publishing Agent websites.' \
    'Set the model key through the authenticated HTTPS setup page after publication.'
