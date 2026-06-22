# Cardloop — cockpit-only image (no Telegram, no systemd).
#
# The Claude Agent SDK shells out to the official `claude` CLI (a Node app), so the
# runtime needs BOTH Node and Python. We base on Node and add Python.
#
# NOTE: `restart-self.sh` is systemd-only and does NOT work in a container — the
# cockpit's "restart" affordances are no-ops here; recreate the container instead.
FROM node:20-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Official Claude CLI (the binary the SDK invokes).
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

# Python deps first (layer cache).
COPY requirements.txt requirements-dev.txt ./
RUN python3 -m venv venv \
    && venv/bin/pip install --no-cache-dir -r requirements.txt

# Frontend deps (layer cache), then source, then build.
COPY web/package.json web/package-lock.json ./web/
RUN cd web && npm ci

COPY . .
RUN cd web && npm run build

# Bind on all interfaces inside the container; publish via compose / your proxy.
ENV WEB_HOST=0.0.0.0 \
    WEB_PORT=8787
EXPOSE 8787

CMD ["venv/bin/python", "bot.py"]
