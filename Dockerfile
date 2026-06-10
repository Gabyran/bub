FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Update system and install base dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    git \
    curl \
    wget \
    vim \
    sudo \
    procps \
    openssh-client \
    ca-certificates \
    tini

# Install development tools
RUN apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    nodejs \
    npm \
    golang \
    jq \
    socat \
    htop \
    tree \
    unzip \
    protobuf-compiler \
    zip fd-find gh

# Install JS CLIs used by the native channel startup scripts.
RUN npm install -g \
    pnpm \
    @openai/codex \
    @jackwener/opencli@1.8.1 \
    @larksuite/cli@1.0.44

WORKDIR /app
# Install Python dependencies first (cached layer)
COPY pyproject.toml uv.lock README.md LICENSE entrypoint.sh ./
# Copy the full source and install
COPY src ./src
RUN uv sync --no-dev --no-editable && \
    uv pip install "any-llm-sdk[gemini,xai]" "lark-oapi==1.6.8" && \
    chmod +x /app/entrypoint.sh

WORKDIR /workspace

VOLUME /root/.bub

ENTRYPOINT ["/usr/bin/tini", "--"]

CMD ["/app/entrypoint.sh"]
