FROM ubuntu:24.04

RUN apt-get update && apt-get install -y \
    git curl jq nodejs npm openssh-client python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

ARG CLAUDE_CODE_VERSION=latest
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}
RUN npm install -g @openai/codex

RUN curl -sL https://github.com/git-bug/git-bug/releases/latest/download/git-bug_linux_amd64 \
    -o /usr/local/bin/git-bug && chmod +x /usr/local/bin/git-bug

COPY requirements.txt /opt/nightshift/
RUN pip install --break-system-packages -r /opt/nightshift/requirements.txt
RUN pip install --break-system-packages 'litellm[proxy]'
RUN pip install --break-system-packages 'openhands==1.13.1' python-frontmatter

COPY core/ /opt/nightshift/core/
COPY adapters/ /opt/nightshift/adapters/
COPY entrypoint.py /opt/nightshift/
COPY overflow-proxy.py /opt/nightshift/
COPY openhands-launcher.py /opt/nightshift/
COPY docker-entrypoint.sh /opt/nightshift/
COPY nightshift-mcp-server.py /opt/nightshift/
COPY mcp-config.json /opt/nightshift/

RUN useradd -m -s /bin/bash agent && \
    chmod +x /opt/nightshift/docker-entrypoint.sh

ENV PYTHONPATH=/opt/nightshift
USER agent

RUN git config --global --add safe.directory /workspace

WORKDIR /workspace

ENTRYPOINT ["/opt/nightshift/docker-entrypoint.sh"]
