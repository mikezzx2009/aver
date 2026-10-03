# Aver

**A persistent cloud workspace for AI agents.**

[中文](README.zh-CN.md) · [Deployment and recovery](docs/deployment.zh-CN.md) · [Contributing](CONTRIBUTING.md)

Aver lets users chat with an agent running in their own persistent Linux environment. Tasks run on the server after the browser closes. Reopening the website brings back conversations, execution logs and saved files.

This is an early, self-hosted project for small trusted groups. The current runtime uses the [DeepSeek Harness SDK](https://pypi.org/project/deepseek-harness-sdk/) with DeepSeek models. The interface is currently in Chinese.

## Features

- A persistent gVisor container and home/workspace directories for each user.
- A durable SQLite task queue, with one task executing at a time across the installation.
- Conversation history, execution details, cancellation and explicit retry controls.
- Files organized by conversation, with upload, download and paginated browsing.
- Static website publishing with a permanent short URL per project, atomic updates and retained snapshots.
- Live CPU, memory, disk and load indicators, plus scheduled local backups.

There is no application-imposed queue length or task duration limit by default. Disk space, memory, provider limits and API costs still bound the workload.

## How it works

| Component | Responsibility |
| --- | --- |
| `static/` | Plain HTML, CSS and JavaScript chat interface |
| `server.py` | Authentication, tenant-scoped APIs, file access and model proxy |
| `store.py` | SQLite WAL storage, task leases and persistent conversation directories |
| `worker.py` | Queue execution and per-user container lifecycle |
| `sandbox/` | Ubuntu image, DeepSeek runtime and persistent agent daemon |
| `publisher.py` | Validated static snapshots and stable public aliases |
| `backup.py`, `deploy/` | Backups, systemd services, network policy and Nginx templates |

The browser submits work to the queue; it does not own the task's lifetime. The worker uses Docker with gVisor, a non-root sandbox user, a read-only container root and persistent bind mounts. Each conversation has its own file directory inside that user's environment. Conversations belonging to the same user share tools and processes; they are not separate security sandboxes.

The real model API key stays in the host configuration. Agents receive a temporary task credential for the host model proxy instead of the provider key. The proxy accepts only an active task's credential.

## Try the interface locally

Use Python 3.12 on Linux or macOS. The web service uses the Python standard library; no package installation or model key is needed for this preview.

```bash
git clone https://github.com/mikezzx2009/aver.git
cd aver
python3 server.py
```

In another terminal, from the same directory, read the newly generated setup token:

```bash
python3 -c 'from common import config; print(config()["bootstrap_token"])'
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080), enter that token and create the first administrator account. Preview state is saved in `var/`; restarting the web service retains it. Keep that directory private.

This preview supports the interface, account setup, queued tasks and file management. It does not start the container worker or execute agent tasks. Full execution requires the Linux deployment below. `AGENT_CLOUD_DATA` and `AGENT_CLOUD_CONFIG` can override the preview's state and configuration paths.

## Deploy on a server

The installer targets **Ubuntu 24.04 LTS, x86_64**. A 2 vCPU / 2 GiB server with a 40 GiB disk is a starting point for a small pilot; measure usage before adding users. Each resident container consumes resources even when idle.

1. Take a snapshot of an existing server, clone the repository and run `sudo bash deploy/install.sh`. The script installs Docker, the official gVisor package, Nginx and systemd units, then builds and checks the sandbox image.
2. Set `domain` to your login hostname in `/etc/agent-cloud/config.json`. Configure DNS and HTTPS, replacing the example domain in the Nginx template and renewal hook. The installer leaves public Nginx templates disabled.
3. Create the initial administrator using the generated setup token. In the website's **设置** panel, save your DeepSeek API key and choose whether to enable registration. Keep registration closed for a private pilot.
4. To publish generated static sites, set `site_base_url` to their public HTTP(S) origin, such as `http://YOUR_PUBLIC_IP`. Configure and enable `deploy/agent-cloud-sites.nginx.conf` separately. Use a different origin from the authenticated Aver website.

Public website addresses are **not configured by default**. Without `site_base_url`, a publication request fails clearly rather than returning an invented address. `203.0.113.10` and `aver.example.com` in this repository are documentation examples, not working services.

These are the fields to merge into the existing private configuration, replacing both example values. Preserve any existing settings and credentials:

```json
{
  "domain": "aver.example.com",
  "site_base_url": "http://203.0.113.10"
}
```

The default installation uses `/opt/agent-cloud` for code, `/srv/agent-cloud` for data and `/etc/agent-cloud/config.json` for private configuration. These names remain stable for compatibility. The web service listens on loopback port 8080; the task model proxy uses the private agent bridge on port 8090. Expose the Nginx website, not these internal ports or the Docker API.

See the [deployment guide](docs/deployment.zh-CN.md) for certificate setup, public static hosting, resource limits, maintenance and recovery. Reinstalling or restarting services is a maintenance operation and may interrupt running tasks.

## Data and recovery

Conversations, queue state and events are committed to SQLite. User home directories, SDK sessions and conversation files live in host directories outside the disposable container root. Normal task completion leaves the user's daemon and background processes running.

```bash
sudo systemctl start agent-cloud-backup.service
sudo journalctl -u agent-cloud-backup.service -n 30 --no-pager
```

Backups include online SQLite copies, user files, published versions and aliases, private publication metadata, configuration and source. They contain secrets and must be handled as private archives. Each database is consistent independently; live file copies are not a single atomic checkpoint. RAM, running processes, the sandbox image and TLS certificates are not included.

Backups remain on the same server by default. Add off-server copies and rehearse restoration before relying on the service. Disk failure or server deletion can destroy both live data and local backups. Follow the [recovery procedure](docs/deployment.zh-CN.md#恢复数据库和文件) in a clean staging directory; never extract an archive over an active installation.

## Current limits

- One host and one executing task at a time; no high availability or automatic offsite backup.
- Cancellation, worker recovery, OOM or host restart can stop processes. Saved data survives ordinary restarts, but process memory and unsaved state do not. Interrupted work is not automatically replayed.
- The image includes shell, Python, Git, Node/npm and build tools. It has no Chromium, GUI desktop or preauthorized external connectors.
- The publisher serves static frontend projects. Dynamic backend hosting requires additional process supervision and proxy configuration.
- Each container defaults to 1 CPU and 768 MiB memory. More users require capacity planning; logs, snapshots and backups are not silently pruned.
- The host control services run as root to manage Docker. This is a small-pilot implementation, not a claim of production-ready isolation or guaranteed uninterrupted service.

## Tests

Python 3.12 and Node 24 are used in CI. Install Nginx locally to include the isolated real-server publication tests; without it, those checks are skipped.

```bash
python3 -m unittest discover -s tests -v
node --check static/app.js
node tests/test_frontend_history.js
python3 tests/smoke_web.py --app-dir .
```

The smoke test uses disposable state and loopback ports to check authentication, files, queue persistence, backup and restore. No provider key is needed. CI runs the Python suite as root on a disposable Ubuntu runner so the container UID permission tests also execute. Docker/gVisor and billable model execution are separate deployment checks.

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md). For security reports, follow [SECURITY.md](SECURITY.md). Aver is released under the [MIT license](LICENSE).
