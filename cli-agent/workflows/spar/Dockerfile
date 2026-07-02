FROM node:24-bookworm

# System tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ripgrep bash curl ca-certificates \
    python3 python3-pip pipx jq sudo \
    dbus gnome-keyring libsecret-tools \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /usr/lib/python3.*/EXTERNALLY-MANAGED

# AI panel tools
RUN npm install -g @anthropic-ai/claude-code @openai/codex opencode-ai

# Non-root user
ARG USER_UID=501
RUN if getent passwd ${USER_UID} >/dev/null; then \
      existing=$(getent passwd ${USER_UID} | cut -d: -f1); \
      usermod -l medulla "$existing"; \
      usermod -d /home/medulla -m medulla || true; \
    else \
      useradd -m -u ${USER_UID} -s /bin/bash medulla; \
    fi \
    && echo "medulla ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/medulla
USER medulla

# agy (antigravity CLI)
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash

# medulla
RUN pipx install git+https://github.com/skopanev/medulla.git
ENV PATH="/home/medulla/.local/bin:$PATH"

ENV MEDULLA_DOCKER=1
ENV GIT_CONFIG_COUNT=2
ENV GIT_CONFIG_KEY_0=safe.directory
ENV GIT_CONFIG_VALUE_0=/workspace
ENV GIT_CONFIG_KEY_1=safe.directory
ENV GIT_CONFIG_VALUE_1=*

WORKDIR /workspace
# init-docker.sh is mounted from the medulla package to /mnt/init-docker.sh at runtime
ENTRYPOINT ["/mnt/init-docker.sh"]
