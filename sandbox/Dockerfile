FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates python3 python3-venv python3-pip python3-dev \
       git curl nodejs npm build-essential pkg-config tini ripgrep unzip zip \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash agent \
    && mkdir -p /workspace /home/agent/.agent-cloud /home/agent/.dsh \
    && chown -R 10001:10001 /workspace /home/agent \
    && python3 -m venv /opt/harness \
    && /opt/harness/bin/python -m pip install --no-cache-dir 'deepseek-harness-sdk==0.1.5rc1'

# SDK's wheel carries its own Node runtime; apt's Node/npm serve user tasks.
# Chromium is omitted initially: browser workers need a measured RAM budget.
COPY --chmod=0555 task_runner.py /opt/task_runner.py
ENV HOME=/home/agent \
    USER=agent \
    LOGNAME=agent \
    PATH=/opt/harness/bin:/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONNOUSERSITE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

USER 10001:10001
WORKDIR /workspace
# Supervisor runs this container with --read-only, /tmp as tmpfs, and only
# this user's home and workspace bound writable. Never mount Docker's socket.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sleep", "infinity"]
