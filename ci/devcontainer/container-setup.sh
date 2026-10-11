#!/usr/bin/env bash
# Inside the dev container, once: the CLI (as a developer would pip install it) and the pinned agents.
set -euo pipefail
# In its own environment, so it imports from any folder (agents run it from the repositories they work in).
python -m venv "$HOME/venv"
"$HOME/venv/bin/python" -m pip install --quiet -e .
# A container-local copy, so host and container node_modules (different platforms) never mix.
mkdir -p "$HOME/agents"
cp ci/agents/package.json ci/agents/package-lock.json ci/agents/locate.mjs "$HOME/agents/"
(cd "$HOME/agents" && npm ci --no-audit --no-fund)
