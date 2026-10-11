#!/usr/bin/env bash
# Inside the dev container: start the relay like the extension does in a remote window, then run the tests.
set -euo pipefail
export LLM_CREDENTIALS_AGENT="$(mktemp -d)/relay/agent.sock"
export PATH="$HOME/venv/bin:$PATH"
node ci/devcontainer/relay.mjs > /tmp/relay.log 2>&1 &
for _ in $(seq 60); do grep -q "created profile" /tmp/relay.log 2>/dev/null && break; sleep 1; done
cat /tmp/relay.log
export $(node "$HOME/agents/locate.mjs")
LLM_CREDENTIAL_MAPPING_TEST_DEVCONTAINER=1 LLM_CREDENTIAL_MAPPING_REQUIRE=devcontainer,claude,codex \
  python -m unittest discover -s tests -p test_devcontainer.py -v
