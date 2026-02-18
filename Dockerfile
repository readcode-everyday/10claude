# Single image for both openclaw (control plane) and runtime-daemon.
# runtime-daemon additionally needs Node.js for the Claude CLI code engine.

FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV CLAUDE_CODE_SKIP_ONBOARDING=1

# ── System deps ───────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Node.js 20 (for Claude CLI) ───────────────────────────────────────────────
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# ── Claude CLI (code engine) ──────────────────────────────────────────────────
RUN npm install -g @anthropic-ai/claude-code

# Pre-create claude config dir to avoid first-run prompts
RUN mkdir -p /root/.claude && \
    echo '{"autoUpdaterStatus":"disabled","hasCompletedOnboarding":true}' \
    > /root/.claude/settings.json

# Trust all directories (workspace volumes are created by host user, run as root)
RUN git config --global --add safe.directory '*'

# ── Python packages ───────────────────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Install openclaw as a package ─────────────────────────────────────────────
COPY setup.py .
COPY openclaw/ openclaw/
RUN pip install -e .

# Default working directory; volumes are mounted at runtime
WORKDIR /app
