<div align="center">

<img src=".github/assets/logo.svg" alt="" width="120" />

# cbs Virtual Key Mapper

**Short-lived keys from your company gateway, attributed to the right customer, for every tool you use.**

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.11-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20macOS%20%7C%20Linux-0F4C5C)](#-what-works-where)
[![Gateway](https://img.shields.io/badge/gateway-LiteLLM-0F4C5C)](docs/architecture.md#11-gateway-requirements)

</div>

---

`vkm` signs you in to your company's gateway once, with its single sign-on. From then on your
tools get short-lived keys from it, and every request carries the **customer** of the
repository you are working in. Spend is attributed per customer without anyone pasting API
keys into config files, and nothing goes out without a customer.

`vkm` does not depend on a particular gateway or tool. Gateways, sign-in methods and tools
are separate adapters ([docs/adapters.md](docs/adapters.md)), so new ones can be added
without touching the rest.

## ✨ What works where

### Gateways and sign-in

| Gateway | Sign-in | What the gateway receives |
| --- | --- | --- |
| [LiteLLM](https://github.com/BerriAI/litellm) | Its native CLI single sign-on in the browser, with team choice. The refresh token stays in the OS vault. | The SSO user and team (from the token), the customer as the request tag `x-litellm-tags`, and `x-litellm-spend-logs-metadata` = `{"customer_source": "explicit\|local\|file\|rules", "repository": "github.com/org/repo"}` (repository only inside a Git checkout with an origin). |
| LiteLLM, from a VS Code dev container, SSH or WSL window | `forwarded`: the two VS Code extensions relay short-lived keys from the sign-in on your own computer; the refresh token never leaves it. | The same. |

More gateways and sign-in methods are adapters ([docs/adapters.md](docs/adapters.md)).

### Tools

| Tool | Connected with | Customer | Key renewal | In a folder without a customer |
| --- | --- | --- | --- | --- |
| Claude Code CLI | `vkm setup claude` | per repository, checked before each prompt | ✅ | the prompt is blocked |
| Claude Code for VS Code | `vkm setup claude` + `"claudeCode.disableLoginPrompt": true` | per repository, checked before each prompt | ✅ | the prompt is blocked |
| Codex CLI | `vkm setup codex` + the shell hook | of the folder where Codex starts | ✅ | no key is sent; the gateway refuses |
| OpenCode v1 | `vkm setup opencode` | per request, of the folder OpenCode runs in | ✅ | the request is blocked |
| OpenCode v2 | `vkm run opencode` (`setup` refuses v2) | fixed at launch | ❌ key from launch time; restart when it expires | as for `vkm run` |
| GitHub Copilot Chat in VS Code | the Virtual Key Mapper extension | per workspace folder, shown in the status bar | ✅ | blocked, with an "Assign customer" button that runs `vkm repo bind` |
| any of the above, one launch | `vkm run [--customer NAME] TOOL` | fixed at launch (or the one you give) | ✅ (OpenCode v2: ❌) | asks in a terminal and saves your choice; blocked without a terminal or outside Git |
| Codex for VS Code | not supported yet | | | |
| Copilot inline completions | not possible: they always use GitHub's models | | | |

### Platforms

✅ = tested in CI on every push, with the real tools (pinned in `ci/agents`, plus a weekly
run with their latest releases) against a local test gateway. ☑️ = supported and checked by
hand with the [manual checklist](docs/testing.md#manual-checklist).

| | Windows | macOS | Linux | Dev container | SSH, WSL |
| --- | --- | --- | --- | --- | --- |
| Claude Code CLI, Codex CLI, OpenCode v1 | ✅ (interactive Codex ☑️) | ✅ | ✅ | ✅ Claude Code and Codex, ☑️ OpenCode | ☑️ |
| Claude Code for VS Code, Copilot Chat, OpenCode v2 | ☑️ | ☑️ | ☑️ | ☑️ | ☑️ |
| Credential vault | ✅ Credential Manager | ✅ Keychain | ✅ Secret Service (GNOME Keyring), ☑️ KWallet | not needed | not needed |
| Shell hooks | ✅ PowerShell 7, bash; ☑️ Windows PowerShell 5.1 and PCs that block scripts (`shell-init pwsh --terminal`) | ✅ bash, zsh, fish, PowerShell 7 | ✅ bash, zsh, fish, PowerShell 7 | as on Linux | as on Linux |
| VS Code extensions | ✅ in a VS Code extension host | ✅ in an extension host, with the relay | ✅ in an extension host, with the relay | ✅ relay in a real dev container | ☑️ |

### Choosing the customer

First match wins:

| Source | Who sets it | Applies to |
| --- | --- | --- |
| `vkm run --customer NAME` | you | that launch |
| `VKM_CUSTOMER` | you | every process started with the variable set |
| `vkm repo bind` | you | clones of that repository on this computer (same origin, or it as upstream), only for you |
| a committed `.vkm` file (`vkm repo bind --shared` writes it at the repository root) | the team | that folder and everything below it, unless a nearer `.vkm` exists |
| central rules (a JSON file or URL in the profile) | your organisation | repositories matching a pattern, e.g. `github.com/acme-*/**` |

## 🚀 Quick start

Needs Python 3.11 or newer and an OS credential vault.

```bash
uv tool install git+https://github.com/cbs-consulting/virtual-key-mapper    # or: pipx install git+https://...
```

From a checkout instead:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .          # Windows: .venv\Scripts\python.exe -m pip install -e .
```

`vkm` is the installed command. If your machine blocks pip-generated `.exe` launchers (some
Windows policies do), use `python -m virtual_key_mapper` everywhere instead; it accepts the
same arguments.

```bash
# 1. Once per machine: a profile for your gateway and your business-unit team, then sign in.
vkm profile add work --gateway https://llm.company.example --team-id YOUR_TEAM_ID
vkm login          # browser sign-in; choose the same team there
vkm status         # "state": "ready"

# 2. Once per machine: connect your tools.
vkm setup claude --model YOUR_CLAUDE_ALIAS
vkm setup codex --model YOUR_CODEX_ALIAS
vkm setup opencode

# 3. Once per repository (unless a central rule or a committed .vkm covers it):
cd ~/src/some-repo
vkm repo bind --customer customer-acme
vkm repo show
```

The gateway address is its root URL (no `/v1`); the team ID is the exact ID from the gateway,
not its display name. Your sign-in renews itself; sign in again only after logging out, a
long pause or a change of team membership.
The gateway must be served at its host root with LiteLLM's `PROXY_BASE_URL` set to that URL
(see [Gateway requirements](docs/architecture.md#11-gateway-requirements)).

## 🧭 Customers

The customer of a directory is chosen in this order (first match wins):

1. `--customer NAME` (for `run`) or the environment variable `VKM_CUSTOMER`;
2. your personal assignment: `vkm repo bind --customer NAME`, stored only for you
   (`repositories.json` on this computer), keyed by the repository's `origin`/`upstream`
   address, so every clone on this computer shares it;
3. the nearest committed `.vkm` file (`customer = "NAME"`), written with
   `repo bind --shared`; a file in a parent folder such as `~/src/acme/` covers all
   repositories below it, and a nearer file wins. Inside your home folder the search stops
   below home, so a file in home or above it never applies (`repo bind --shared` refuses to
   write one there); for folders outside home, such as `/workspaces` in a dev container or
   another drive on Windows, it goes up to the root of the drive;
4. central rules of the profile (`profile add --rules URL|PATH` or `profile update NAME --rules ...`):
   ```json
   {"version": 1,
    "customers": [{"id": "customer-acme", "name": "Acme"}],
    "rules": [{"match": "github.com/acme-*/**", "customer": "customer-acme"}]}
   ```
   `**` spans path segments, `*`/`?` stay within one; the first matching rule wins, tried
   against `origin`, then `upstream`. Write the repository as `repo show` prints it
   (lowercase, `%20` decoded to a space).

Nothing is sent without a customer: unassigned folders are blocked with an explanation.
`repo show` tells you which customer applies and why; `repo list` / `repo unbind` manage your
personal assignments. Customer tags and repository mappings are attribution, not access
control: enforce allowed customers on the gateway if you need that.
Gateway settings that break or leak attribution (tag routing, client-side tag rejection,
header forwarding) and why the picker needs the rules' `customers` list are listed in
[Gateway requirements](docs/architecture.md#11-gateway-requirements).

## 🧩 Tools

### Claude Code (CLI and the official VS Code extension)

```bash
vkm setup claude [--model ALIAS] [--replace] [--remove]
```

Writes `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR`): an `apiKeyHelper` that runs
`vkm token`, the gateway address, and a prompt hook. Before each prompt the hook
looks up the customer, writes it to the repository's `.claude/settings.local.json` (excluded
from Git) and asks you once to send the prompt again; prompts without a customer are blocked.
For the VS Code extension also set `"claudeCode.disableLoginPrompt": true` in your VS Code
user settings. `--remove` undoes exactly what setup wrote.

### Codex CLI

```bash
vkm setup codex [--model ALIAS] [--remove]
eval "$(vkm shell-init bash)"     # add to ~/.bashrc; also zsh, fish, pwsh
```

On Windows, add `vkm shell-init pwsh | Out-String | Invoke-Expression` to your PowerShell
`$PROFILE`. Where the execution policy blocks scripts (common on company PCs, "running scripts is
disabled on this system"), a profile cannot load; run `vkm shell-init pwsh --terminal`
instead and paste its command line into Windows Terminal (Settings → your PowerShell profile →
Command line) or its snippet into your VS Code user settings. Check in a new terminal:
`Get-Command codex` shows `Function`.

Codex reads the customer from environment variables that the shell hook sets per directory.
Without them, Codex gets no credential and the gateway refuses the request. The hook also makes
`codex` start with `--no-daemon`: Codex's shared background daemon would keep the variables of the
terminal that started it. If one is running from before, stop it once: `codex app-server daemon stop`.

### OpenCode

```bash
vkm setup opencode [--model ALIAS] [--remove]
```

Adds a provider with the gateway's models and a small plugin to
`~/.config/opencode/opencode.json`; the plugin adds a fresh credential and the folder's
customer to every request. OpenCode v2 is supported only through `vkm run opencode`.

### One-off launches

```bash
vkm run [--customer NAME] [--model ALIAS] claude|codex|opencode [-- TOOL ARGUMENTS]
```

Starts the tool with child-only configuration, without changing your setup; `--customer`
overrides the repository's customer for that process.

### GitHub Copilot Chat (VS Code)

Build and install the extension (`vscode-extension/`, see its README):

```bash
cd vscode-extension && npm ci && npm run package
code --install-extension vkm.vsix   # or Extensions → ··· → Install from VSIX
```

Set `vkm.command` in your VS Code user settings to how the CLI runs, e.g.
`["/path/to/.venv/bin/python", "-m", "virtual_key_mapper"]`. Pick a "Virtual Key Mapper"
model in Copilot Chat; the status bar shows the customer. If you use VS Code profiles,
install the extension in the profile you work in. Inline completions and next-edit
suggestions always use GitHub's models and are not attributed.

## 🐳 Dev Containers, SSH and WSL

The container gets short-lived tokens from the sign-in on your own computer; nothing
long-lived is stored inside it.

1. Local VS Code: build the companion (`cd vscode-extension && npm ci && cd local && npm run package`)
   and install `vscode-extension/local/vkm-local.vsix`
   ("Virtual Key Mapper: local sign-in") and set `vkm.localCommand` in your user
   settings to the local CLI command. Containers get tokens only for your local default
   profile; list others in `vkm.localProfiles` (user settings) to allow them.
2. In the container: install the CLI, e.g.
   `uv tool install --python 3.12 "git+https://github.com/cbs-consulting/virtual-key-mapper@BRANCH_OR_COMMIT"`
   and install `vscode-extension/vkm.vsix` (`npm run package` in `vscode-extension/`) into
   the container (Install from VSIX → Show Local). Reload the window.
3. The extension creates a container profile that uses your local sign-in
   (`vkm profile list` shows `forwarded`). Do not run `login` in the container.

All tools work in the container as described above. Personal `repo bind` assignments made
inside a container live in the container.

## 🗂️ Where things are stored

| What | Where |
| --- | --- |
| Profiles, personal assignments, rules cache, tool state | `%LOCALAPPDATA%\virtual-key-mapper` or `~/.config/virtual-key-mapper` (`--home` or `VKM_HOME` to change) |
| Sign-in (access and refresh token) | the OS credential vault only; never in files |
| Claude / Codex / OpenCode configuration | each tool's own user configuration, changed only by `setup` |

## ⌨️ Commands

`profile add|list|show|use|update`, `login`, `status`, `token [--json]`, `logout`,
`repo show|bind|unbind|list`, `tags list`, `setup claude|codex|opencode`, `run`,
`shell-init`, and `claude guard` (the hook). `vkm COMMAND --help` explains each.

## 🛠️ Contributing

```bash
.venv/bin/python -m unittest discover -s tests -v
cd vscode-extension && npm test && npm run test:host     # extension unit tests; real VS Code host
cd vscode-extension/local && npm test
```

Live tests run the real tools against a local fake gateway (no model calls, no secrets). Install
the pinned versions and point the tests at them:

```bash
cd ci/agents && npm ci && cd ../..
export $(node ci/agents/locate.mjs)    # sets VKM_TEST_{CLAUDE,CODEX,OPENCODE}_BINARY
.venv/bin/python -m unittest discover -s tests -v
```

`VKM_TEST_VAULT=1` also runs `tests/test_vault_native.py` against your real OS vault (uniquely named test entries, removed afterwards).

Locally, missing tools and shells skip their tests; CI requires them. CI runs everything on Windows,
macOS and Linux: the Python suite, the live agents and shell hooks, the real OS vault, both extensions
in a real VS Code, and a real dev container on Linux; a weekly run tries the newest agent releases.
Details and the manual checklist: [docs/testing.md](docs/testing.md). How it works and why:
[docs/architecture.md](docs/architecture.md); extending it: [docs/adapters.md](docs/adapters.md).

## 📄 License

Copyright © 2026 cbs Corporate Business Solutions Unternehmensberatung GmbH.
Licensed under the [Apache License 2.0](LICENSE).

<sub>LiteLLM is a project of BerriAI. Claude Code is a product of Anthropic, Codex of OpenAI, OpenCode of SST, GitHub Copilot and VS Code of GitHub and Microsoft. This project is not affiliated with or endorsed by any of them.</sub>
