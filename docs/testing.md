# Testing

## What CI covers

| Job | Systems | What it proves |
| --- | --- | --- |
| `test` | Windows, macOS, Linux; Python 3.11 and 3.13 | The Python suite with fakes: sign-in, renewal, label definitions, resolution and formats, tool setup, shells generation. |
| `live` | Windows, macOS, Linux | The real agents (Claude Code, Codex, OpenCode, pinned in `ci/agents`) against a local fake gateway: credentials renew through the CLI, requests carry the labels' headers, folders with a missing required label send nothing. The shell hooks run in bash, zsh, fish (not Windows) and PowerShell. |
| `vault` | Windows, macOS, Linux | The real OS vault (Credential Manager, Keychain, Secret Service): sign-in, renewal by a second process, logout, sessions split across vault entries. |
| `devcontainer` | Linux | A real dev container: the host signs in with its OS vault and answers through the companion extension's handler; inside, the main extension's relay, a forwarded profile created from the host's, and Codex and Claude Code using the relayed credential with the folder's labels; the host profile's own label definitions are copied into the container profile. No refresh token reaches the container. |
| `vscode-extension` | Windows, macOS, Linux | Both extensions' unit tests, a real VS Code extension host with a fake CLI and gateway, and the relay against the real CLI (Linux, macOS). VSIX packages are built on Linux. |
| `latest-agents` (weekly) | Windows, macOS, Linux | The `live` tests with the newest agent releases, to catch vendor changes before bumping `ci/agents`. |

`VKM_REQUIRE` lists what a job must have (`claude`, `codex`, `opencode`, `bash`, `zsh`,
`fish`, `pwsh`, `vault`, `devcontainer`, `relay`). Locally a missing tool skips its tests; in CI it fails.

## Running parts locally

```bash
python -m unittest discover -s tests -v                       # always

cd ci/agents && npm ci && cd ../..                            # live agent tests
export $(node ci/agents/locate.mjs)
python -m unittest discover -s tests -v

VKM_TEST_VAULT=1 \
  python -m unittest discover -s tests -p test_vault_native.py -v   # writes test entries to YOUR vault

cd vscode-extension && npm ci && npm test && npm run test:host     # extensions (downloads VS Code)
```

The dev container test needs Docker and runs as in CI: compile both extensions
(`npx tsc -p . && node node_modules/typescript/bin/tsc -p local` in `vscode-extension`), then on Linux
`dbus-run-session -- bash ci/devcontainer/host.sh` (GNOME keyring and the Dev Container CLI via `npx`).

## Manual checklist

CI cannot drive the VS Code UI, other remote kinds or your gateway. Check these when changing the
corresponding parts, and before a release:

- **Copilot Chat** (desktop VS Code): the Virtual Key Mapper models appear in the model picker and in agent
  mode; a chat in a repository with labels shows their values in the status bar (`acme · webshop`, a tooltip
  with descriptions and sources); a folder with a missing required label shows it in warning style and
  offers "Assign labels", which asks for it in a terminal; a profile without labels shows `no labels`;
  the organization's "Bring Your Own Language Model Key" policy allows it.
- **Interactive Codex on Windows** (CI drives the interactive UI only on Linux and macOS): in a new
  PowerShell window with the hook, plain `codex` in a repository with labels answers with them, also
  after `codex app-server daemon start` from another folder (the hook's `codex` adds `--no-daemon`).
- **Windows PowerShell 5.1 where scripts are blocked** (CI runs PowerShell 7 with a bypassed policy): the
  `vkm shell-init pwsh --terminal` command line in a Windows Terminal or VS Code profile loads the hook,
  and `Get-Command codex` shows `Function`.
- **OpenCode v2** (CI pins OpenCode v1): `vkm run opencode` in a repository with labels answers with
  them; `vkm setup opencode` refuses v2.
- **OpenCode in a dev container** (the dev container job runs Claude Code and Codex): `opencode run` in
  the container answers with the folder's labels.
- **KWallet** (CI uses GNOME Keyring): `vkm login`, a renewal and `vkm logout` on a KDE desktop.
- **Claude Code extension**: after `setup claude` and `claudeCode.disableLoginPrompt`, a new conversation
  works without a Claude login; a repository with a missing required label is blocked with the guard's
  message (`Required labels have no value: ...`); the launch folder in multi-root workspaces resolves
  the expected labels.
- **Dev container in VS Code**: with both extensions installed, opening the container creates the forwarded
  profile with the local profile's labels and outputs (central rules in a local file give a warning
  that the organisation's label definitions are missing); a second window on the same container takes
  over the relay when the first closes.
- **SSH and WSL windows**: the same relay checks as for dev containers.
- **Your gateway**: SSO login with the real identity provider, the label tags and the labels, sources and
  repository in the spend-log metadata, the picker's values or the `/tag/list` catalogue, renewal over a
  long session, a refused grant leading to a clear sign-in prompt.
