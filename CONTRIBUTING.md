# Contributing to Aver

Use an issue to describe the problem, the expected behavior and a minimal reproduction. For changes, keep pull requests focused and explain what changed, why and how you verified it. The current interface is in Chinese; maintain the English and Chinese introductions together when behavior changes.

## Development

Use Python 3.12 and Node 24 on a POSIX system. The host application and tests use the Python standard library. Agent runtime dependencies are pinned in `sandbox/Dockerfile`; a provider key is not needed for ordinary development.

```bash
python3 -m unittest discover -s tests -v
node --check static/app.js
node tests/test_frontend_history.js
python3 tests/smoke_web.py --app-dir .
```

Install Nginx to include the isolated Nginx integration tests. Linux root-only UID tests skip for ordinary local users; CI runs them on a disposable Ubuntu runner. Do not run privileged tests or `deploy/install.sh` on a machine that hosts users' live data. Use a separate test server for Docker/gVisor execution checks.

For an interactive preview, run `python3 server.py` and follow the setup instructions in [README.md](README.md). Preview state belongs in ignored `var/`, or a separate directory configured with `AGENT_CLOUD_DATA` and `AGENT_CLOUD_CONFIG`.

## Changes that need particular care

- Keep every account, task, conversation and file lookup tenant-scoped. Conversation folders organize one user's files; they are not isolation boundaries.
- Preserve lease/cancellation checks and idempotence. Recovery must not replay external side effects or roll a published website back to an older revision.
- Keep hidden/private files, filesystem links and configuration outside public snapshots. Preserve directory traversal and link checks when changing file access.
- Treat database history and persisted files as user data. Schema, migration and backup changes need explicit compatibility and recovery notes.
- Keep model credentials on the server. Logs, tests and examples must use synthetic credentials and documentation-only addresses.

Add a regression test when fixing behavior with meaningful failure cases. For purely visual changes, inspect the interface at desktop and mobile sizes; use tests where they verify behavior rather than mirroring CSS declarations.

## Before opening a pull request

Run the checks above, inspect your diff and verify that it contains no `var/`, `.env`, real configuration, databases, private logs, backups or user files. Use `203.0.113.10` and `aver.example.com` for deployment examples. CI requires no production credentials and does not deploy the application.

If a change touches the runtime, verify that container resource limits, gVisor execution, network restrictions and persistent mounts still work on a disposable Ubuntu 24.04 x86_64 server. Explain any checks you could not run.

Report vulnerabilities using [SECURITY.md](SECURITY.md), not a public issue containing credentials or exploit details.
