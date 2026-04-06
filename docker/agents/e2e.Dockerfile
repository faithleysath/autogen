# syntax=docker/dockerfile:1.7

FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    BUN_INSTALL=/opt/bun \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH=/opt/bun/bin:/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        git \
        gosu \
        jq \
        openssh-client \
        pkg-config \
        procps \
        ripgrep \
        tini \
        unzip \
        xz-utils \
        zip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://bun.com/install | bash

RUN mkdir -p /workspace /run/host-ssh /root/.ssh "${PLAYWRIGHT_BROWSERS_PATH}"

COPY docker/agents/agent-entrypoint.sh /usr/local/bin/agent-entrypoint.sh
RUN chmod +x /usr/local/bin/agent-entrypoint.sh

WORKDIR /workspace

RUN bunx playwright install --with-deps --no-shell chromium

COPY docker/agents/github_known_hosts /etc/ssh/ssh_known_hosts
RUN chmod 644 /etc/ssh/ssh_known_hosts

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/agent-entrypoint.sh"]
CMD ["sleep", "infinity"]
