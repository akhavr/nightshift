FROM ubuntu:24.04

RUN apt-get update && apt-get install -y \
    git curl jq nodejs npm openssh-client python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

RUN curl -sL https://github.com/git-bug/git-bug/releases/latest/download/git-bug_linux_amd64 \
    -o /usr/local/bin/git-bug && chmod +x /usr/local/bin/git-bug

COPY requirements.txt /opt/agent-worker/
RUN pip install --break-system-packages -r /opt/agent-worker/requirements.txt

COPY core/ /opt/agent-worker/core/
COPY adapters/ /opt/agent-worker/adapters/
COPY entrypoint.py /opt/agent-worker/
COPY docker-entrypoint.sh /opt/agent-worker/

RUN useradd -m -s /bin/bash agent && \
    chmod +x /opt/agent-worker/docker-entrypoint.sh

ENV PYTHONPATH=/opt/agent-worker
USER agent

RUN git config --global --add safe.directory /workspace

WORKDIR /workspace

ENTRYPOINT ["/opt/agent-worker/docker-entrypoint.sh"]
