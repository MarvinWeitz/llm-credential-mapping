#!/usr/bin/env bash
# On the CI host, inside `dbus-run-session`: a real sign-in in the OS vault, the companion stand-in, and a
# dev container that uses both. Needs: gnome-keyring, the CLI installed, both extensions compiled, npx.
set -euo pipefail
GATEWAY_PORT=47801
COMPANION_PORT=47802
export LLMC_GATEWAY="http://127.0.0.1:$GATEWAY_PORT"
HOST_HOME="$(mktemp -d)"
export LLMC_HOST_CLI="[\"python\", \"-m\", \"llm_credential_mapping\", \"--home\", \"$HOST_HOME\"]"
cli() { python -m llm_credential_mapping --home "$HOST_HOME" "$@"; }

printf ci | gnome-keyring-daemon --unlock --components=secrets > /dev/null
python ci/devcontainer/gateway.py "$GATEWAY_PORT" &
for _ in $(seq 30); do curl -sf "$LLMC_GATEWAY/v1/models" > /dev/null && break; sleep 1; done

cli profile add work --gateway "$LLMC_GATEWAY" --team-id team-a --allow-insecure-localhost
BROWSER="python $PWD/ci/devcontainer/browser.py %s" cli login --profile work --timeout 60
cli status --profile work

node ci/devcontainer/companion.mjs "$COMPANION_PORT" &
for _ in $(seq 30); do curl -s "http://127.0.0.1:$COMPANION_PORT" > /dev/null && break; sleep 1; done

devcontainer=(npx --yes @devcontainers/cli@0.89.0)
"${devcontainer[@]}" up --workspace-folder . --config ci/devcontainer/devcontainer.json
"${devcontainer[@]}" exec --workspace-folder . --config ci/devcontainer/devcontainer.json \
  --remote-env "LLMC_GATEWAY=$LLMC_GATEWAY" --remote-env "LLMC_COMPANION=http://127.0.0.1:$COMPANION_PORT" \
  bash ci/devcontainer/container-test.sh
