<div align="center">

<img src="https://raw.githubusercontent.com/cbs-consulting/virtual-key-mapper/main/.github/assets/logo.svg" alt="" width="120" />

# cbs Virtual Key Mapper

**Short-lived keys from your company gateway, labelled with the repository you work in, for every tool you use.**

[![PyPI](https://img.shields.io/pypi/v/virtual-key-mapper?logo=pypi&logoColor=white)](https://pypi.org/project/virtual-key-mapper/)
[![VS Code Marketplace](https://img.shields.io/visual-studio-marketplace/v/cbs-consulting.vkm?label=VS%20Code)](https://marketplace.visualstudio.com/items?itemName=cbs-consulting.vkm)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-%3E%3D3.11-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![Platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20macOS%20%7C%20Linux-0F4C5C)](#-what-works-where)
[![Gateway](https://img.shields.io/badge/gateway-LiteLLM-0F4C5C)](docs/architecture.md#11-gateway-requirements)

</div>

---

`vkm` signs you in to your company's gateway once, with its single sign-on. From then on your
tools get short-lived keys from it, and every request carries the **labels** of the
repository you are working in: key/value pairs such as `customer = "acme"` and
`project = "webshop"`. Spend is attributed by label without anyone pasting API keys into
config files, and nothing goes out while a required label has no value.

`vkm` does not depend on a particular gateway or tool. Gateways, sign-in methods and tools
are separate adapters ([docs/adapters.md](docs/adapters.md)), so new ones can be added
without touching the rest.

## ✨ What works where

### Gateways and sign-in

| Gateway | Sign-in | What the gateway receives |
| --- | --- | --- |
| [LiteLLM](https://github.com/BerriAI/litellm) | Its native CLI single sign-on in the browser, with team choice. The refresh token stays in the OS vault. | The SSO user and team (from the token), the labels as request tags `x-litellm-tags` (by default one `KEY:VALUE` tag per label, e.g. `customer:acme`), and `x-litellm-spend-logs-metadata` = `{"labels": {"customer": "acme"}, "sources": {"customer": "explicit\|env\|local\|file\|rules"}, "repository": "github.com/org/repo"}` (repository only inside a Git checkout with an origin). |
| LiteLLM, from a VS Code dev container, SSH or WSL window | `forwarded`: the two VS Code extensions relay short-lived keys from the sign-in on your own computer; the refresh token never leaves it. | The same. |

More gateways and sign-in methods are adapters ([docs/adapters.md](docs/adapters.md)).

### Tools

| Tool | Connected with | Labels | Key renewal | While a required label has no value |
| --- | --- | --- | --- | --- |
| Claude Code CLI | `vkm setup claude` | per repository, checked before each prompt | ✅ | the prompt is blocked |
| Claude Code for VS Code | `vkm setup claude` + `"claudeCode.disableLoginPrompt": true` | per repository, checked before each prompt | ✅ | the prompt is blocked |
| Codex CLI | `vkm setup codex` + the shell hook | of the folder where Codex starts | ✅ | no key is sent; the gateway refuses |
| OpenCode v1 | `vkm setup opencode` | per request, of the folder OpenCode runs in | ✅ | the request is blocked |
| OpenCode v2 | `vkm run opencode` (`setup` refuses v2) | fixed at launch | ❌ key from launch time; restart when it expires | as for `vkm run` |
| GitHub Copilot Chat in VS Code | the Virtual Key Mapper extension | per workspace folder, shown in the status bar | ✅ | blocked, with an "Assign labels" button that runs `vkm repo bind` |
| any of the above, one launch | `vkm run [--label KEY=VALUE]... TOOL` | fixed at launch (or the ones you give) | ✅ (OpenCode v2: ❌) | asks in a terminal for each missing label and saves the answers; blocked without a terminal or outside Git |
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

### Choosing labels

Each label is resolved on its own, so `customer` can come from a central rule and `project`
from a committed file. For each key the first source with a value wins:

| Source | Who sets it | Applies to |
| --- | --- | --- |
| `vkm run --label KEY=VALUE` | you | that launch |
| `VKM_LABEL_<KEY>` (e.g. `VKM_LABEL_CUSTOMER`) | you | every process started with the variable set |
| `vkm repo bind --label KEY=VALUE` | you | clones of that repository on this computer (same origin, or it as upstream), only for you |
| a committed `.vkm` file (`vkm repo bind --shared --label KEY=VALUE` writes it at the repository root) | the team | that folder and everything below it, unless a nearer `.vkm` exists |
| central rules (a JSON file or URL in the profile) | your organisation | repositories matching a pattern, e.g. `github.com/acme-*/**` |

Which labels exist and which are required is set by your organisation's central rules
document or by your profile ([Labels](#-labels)).

## 🚀 Quick start

Needs Python 3.11 or newer and an OS credential vault.

```bash
uv tool install virtual-key-mapper      # or: pipx install virtual-key-mapper
```

Update later with `uv tool upgrade virtual-key-mapper` (or `pipx upgrade virtual-key-mapper`).
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
#    If your organisation publishes its label definitions, pass --rules URL instead of --label.
vkm profile add work --gateway https://llm.company.example --team-id YOUR_TEAM_ID \
  --label customer --required --description "Who the work is billed to"
vkm login          # browser sign-in; choose the same team there
vkm status         # "state": "ready"

# 2. Once per machine: connect your tools.
vkm setup claude --model YOUR_CLAUDE_ALIAS   # start model; /model lists all gateway models
vkm setup codex --model YOUR_CODEX_ALIAS
vkm setup opencode

# 3. Once per repository (unless a central rule or a committed .vkm covers it):
cd ~/src/some-repo
vkm repo bind --label customer=acme     # or plain 'vkm repo bind' to be asked
vkm repo show
```

The gateway address is its root URL (no `/v1`); the team ID is the exact ID from the gateway,
not its display name. Your sign-in renews itself; sign in again only after logging out, a
long pause or a change of team membership.
The gateway must be served at its host root with LiteLLM's `PROXY_BASE_URL` set to that URL
(see [Gateway requirements](docs/architecture.md#11-gateway-requirements)).

## 🧭 Labels

A label is a key with a value, such as `customer = "acme"` or `project = "webshop"`. What a
label means is up to your organisation; `vkm` knows the keys, which of them are required,
where their values may come from and how the gateway receives them. The full reference is
[docs/labels.md](docs/labels.md).

### Definitions

Labels are defined in your organisation's central rules document (`profile add --rules URL|PATH`
or `profile update NAME --rules ...`) and, for keys the document does not define, in your profile:

```bash
vkm profile update work --label project --description "Project within the customer"
vkm profile update work --label customer --required --value acme=Acme --value globex=Globex --strict
vkm profile update work --no-label project
vkm labels list            # the labels in effect, where each is defined, and notices
```

One label per call. `--required`: nothing is sent while it has no value. `--value ID[=NAME]`:
the choices the picker offers; with `--strict` only those are accepted. `--source NAME`
(repeatable) limits where values may come from. Keys are 1-32 lowercase letters, digits, `_`
or `-`, starting with a letter. A profile label for a key the central document defines is
refused (or, when the document cannot be read at the time, ignored with a notice): the central
one applies. If no label is defined anywhere, requests carry only the repository, and `setup`,
`run` and `repo show` say so.

### Values

For each defined key, the first source with a value wins:

1. `--label KEY=VALUE` (for `run` and `repo show`);
2. the environment variable `VKM_LABEL_<KEY>` (exactly: uppercase, `-` as `_`; empty counts as
   unset; other `VKM_LABEL_*` names are ignored);
3. your personal binding: `vkm repo bind --label KEY=VALUE`, stored only for you
   (`repositories.json` on this computer), keyed by the repository's `origin`/`upstream`
   address, so every clone on this computer shares it. It keeps keys you do not name; a
   different value for a key already bound needs `--replace`. `repo unbind` removes the
   binding, `repo unbind --label KEY` one key;
4. the nearest committed `.vkm` file, written with `repo bind --shared --label KEY=VALUE`:
   ```toml
   # Labels for AI tool requests in this folder and below (vkm).
   [labels]
   customer = "acme"
   project = "webshop"
   ```
   A file in a parent folder such as `~/src/acme/` covers all repositories below it, and the
   nearest file wins as a whole (a nearer file without `project` does not fall through to a
   parent's). Inside your home folder the search stops below home, so a file in home or above
   it never applies (`repo bind --shared` refuses to write one there); for folders outside home,
   such as `/workspaces` in a dev container or another drive on Windows, it goes up to the root
   of the drive;
5. a central rule (below).

`--label KEY=` on the command line, `""` in a `.vkm` and `null` in a rule mean "no value here,
stop searching". `--label` refuses keys that no definition knows (typos); values for unknown keys
from other sources are not sent, and `repo show` lists them. Values are 1-256 printable ASCII
characters without `,`, surrounding spaces or a leading `!` or `&`.

### The central rules document

```json
{
  "version": "2.0",
  "labels": [
    {"key": "customer", "required": true, "description": "Who the work is billed to",
     "values": [{"id": "acme", "name": "Acme"}, {"id": "globex", "name": "Globex"}]},
    {"key": "project", "description": "Project within the customer"}
  ],
  "outputs": {"litellm": {"tags": {"format": ["${customer}", "${customer}:${project}"]}}},
  "rules": [
    {"match": "github.com/acme-*/**", "labels": {"customer": "acme"}},
    {"match": "**", "labels": {"project": null}}
  ]
}
```

Rules are tried in order, each against `origin`, then `upstream`; each key takes its value from
the first matching rule that sets it, so a catch-all below can still supply another label.
`**` spans path segments, `*`/`?` stay within one. Write the repository as `repo show` prints it
(lowercase, `%20` decoded to a space). `outputs` are grouped by gateway type, so one document
can serve profiles on different gateways. A URL is cached for an hour; if a fetch fails or a
newly published document is invalid, the last good copy is used.

### Required labels

A required label without a value blocks every request with a message starting
`Required labels have no value:`, naming the keys and the ways to set them. `vkm run` and
`vkm repo bind` in a terminal, and "Assign labels" in VS Code, ask for each missing one and save
the answers as your personal binding. The picker offers the label's `values`, else the
gateway's catalogue, else accepts any valid typed value without signing in (`vkm labels values KEY`
shows which). `vkm repo bind` asks only for labels a personal binding (with `--shared`: a `.vkm`
file) may set.
`repo show` tells you each label's value and where it came from.

### What the gateway receives

For LiteLLM, by default:

```text
x-litellm-tags: customer:acme,project:webshop
x-litellm-spend-logs-metadata: {"labels":{"customer":"acme","project":"webshop"},"repository":"github.com/acme-shop/web","sources":{"customer":"rules","project":"file"}}
```

The tags format can be changed in the central document (it wins) or in the profile;
the metadata can be switched off:

```bash
vkm profile update work --output 'tags=${customer}' --output 'tags=${customer}:${project}'
vkm profile update work --output-off metadata
vkm profile update work --output-default tags
```

An entry that uses an optional label without a value is left out; `$$` is a literal `$`.
Formats that could make one label's value look like another's are refused, as is any setup in
which a required label reaches no output. The rules are in
[docs/labels.md](docs/labels.md#3-outputs).

Labels are attribution, not access control: enforce allowed values on the gateway if you need
that. Gateway settings that break or leak attribution (tag routing, client-side tag rejection,
header forwarding) and why the picker needs `values` in the central document are listed in
[Gateway requirements](docs/architecture.md#11-gateway-requirements).

## 🧩 Tools

### Claude Code (CLI and the official VS Code extension)

```bash
vkm setup claude [--model ALIAS] [--replace] [--remove]
```

Writes `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR`): an `apiKeyHelper` that runs
`vkm token`, the gateway address, gateway model discovery and a prompt hook. `/model` (also in the
VS Code extension) then lists the models your gateway offers you; `--model` only picks the one new
sessions start on. Before each prompt the hook
resolves the labels, writes the resulting headers to the repository's `.claude/settings.local.json`
(excluded from Git) and asks you once to send the prompt again; prompts are blocked while a
required label has no value.
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

Codex reads the label headers from environment variables that the shell hook sets per directory.
Without them, or while a required label has no value, Codex gets no credential and the gateway refuses the request. The hook also makes
`codex` start with `--no-daemon`: Codex's shared background daemon would keep the variables of the
terminal that started it. If one is running from before, stop it once: `codex app-server daemon stop`.

### OpenCode

```bash
vkm setup opencode [--model ALIAS] [--remove]
```

Adds a provider with the gateway's models and a small plugin to
`~/.config/opencode/opencode.json`; the plugin adds a fresh credential and the folder's
label headers to every request, and blocks requests while a required label has no value. OpenCode v2 is supported only through `vkm run opencode`.

### One-off launches

```bash
vkm run [--label KEY=VALUE]... [--model ALIAS] claude|codex|opencode [-- TOOL ARGUMENTS]
```

Starts the tool with child-only configuration, without changing your setup; each `--label`
sets that key for that process (`--label KEY=` for no value), the others resolve as usual. In a
terminal it asks for missing required labels and saves the answers as your personal binding.

### GitHub Copilot Chat (VS Code)

Install **Virtual Key Mapper for Copilot Chat** (`cbs-consulting.vkm`) from the Extensions view,
the [Visual Studio Marketplace](https://marketplace.visualstudio.com/items?itemName=cbs-consulting.vkm).
For editors without the Marketplace (Cursor, VSCodium), install `vkm.vsix` from the
[latest release](https://github.com/cbs-consulting/virtual-key-mapper/releases/latest)
(Extensions → ··· → Install from VSIX):

```bash
code --install-extension cbs-consulting.vkm
```

Set `vkm.command` in your VS Code user settings to how the CLI runs, e.g.
`["/path/to/.venv/bin/python", "-m", "virtual_key_mapper"]`. Pick a "Virtual Key Mapper"
model in Copilot Chat; the status bar shows the folder's label values (`acme · webshop`),
missing required labels in warning style, and `no labels` when none are defined; its tooltip
lists each label with its description and source. If you use VS Code profiles,
install the extension in the profile you work in. Inline completions and next-edit
suggestions always use GitHub's models and are not attributed.

## 🐳 Dev Containers, SSH and WSL

The container gets short-lived tokens from the sign-in on your own computer; nothing
long-lived is stored inside it.

1. Local VS Code: install **Virtual Key Mapper: host sign-in** (`cbs-consulting.vkm-host`) and set `vkm.localCommand` in your user
   settings to the local CLI command. Containers get tokens only for your local default
   profile; list others in `vkm.localProfiles` (user settings) to allow them.
2. In the container: install the CLI, e.g.
   `uv tool install --python 3.12 virtual-key-mapper`, and install `cbs-consulting.vkm` into the
   container (VS Code offers "Install in Dev Container"; or list it under
   `customizations.vscode.extensions` in `devcontainer.json`). Reload the window.
3. The extension creates a container profile that uses your local sign-in
   (`vkm profile list` shows `forwarded`), with your local profile's own label definitions and
   output overrides and its central rules URL. Do not run `login` in the container.

All tools work in the container as described above. Central rules given as a local file cannot
be copied; the extension then warns that the organisation's label definitions are missing in the
container (set them with `vkm profile update NAME --rules URL|PATH` there). Personal `repo bind`
bindings made inside a container live in the container.

## 🗂️ Where things are stored

| What | Where |
| --- | --- |
| Profiles and their labels, personal label bindings, rules cache, tool state | `%LOCALAPPDATA%\virtual-key-mapper` or `~/.config/virtual-key-mapper` (`--home` or `VKM_HOME` to change) |
| Sign-in (access and refresh token) | the OS credential vault only; never in files |
| Claude / Codex / OpenCode configuration | each tool's own user configuration, changed only by `setup` |

## 🌐 Network access

Outbound HTTPS to the gateway (sign-in, renewal and every model request), plus the central rules
URL if a profile has one; the browser also reaches your identity provider during `login`.
Nothing listens on the network. Requests, headers that must pass a proxy, certificates and
installation hosts: [docs/network.md](docs/network.md).

## ⌨️ Commands

`profile add|list|show|use|update`, `login`, `status`, `token [--json]`, `logout`,
`repo show|bind|unbind|list`, `labels list|values`, `setup claude|codex|opencode`, `run`,
`shell-init`, and `claude guard` (the hook). `vkm COMMAND --help` explains each.

## 📌 Releases

| What | Where | Name |
| --- | --- | --- |
| CLI | [PyPI](https://pypi.org/project/virtual-key-mapper/) | `virtual-key-mapper` (command `vkm`) |
| Copilot Chat extension | [Visual Studio Marketplace](https://marketplace.visualstudio.com/items?itemName=cbs-consulting.vkm), `vkm.vsix` on each [GitHub release](https://github.com/cbs-consulting/virtual-key-mapper/releases) | `cbs-consulting.vkm` |
| Local sign-in extension | [Visual Studio Marketplace](https://marketplace.visualstudio.com/items?itemName=cbs-consulting.vkm-host), `vkm-host.vsix` on each GitHub release | `cbs-consulting.vkm-host` |

All three share one version. Releases are made by the "Release" workflow in the Actions tab, which

1. runs the Python and extension tests on the commit to release and builds everything,
2. requires a `## VERSION - DATE` entry in [`CHANGELOG.md`](CHANGELOG.md),
3. waits for a maintainer to approve the `release` environment,
4. publishes the package to PyPI (trusted publishing: no token in the repository) and the
   GitHub release `vVERSION` with the changelog entry as notes and the built files attached.

The extensions are then uploaded to the Visual Studio Marketplace by hand, so no store token is
stored anywhere: download `vkm.vsix` and `vkm-host.vsix` from the release, open
[marketplace.visualstudio.com/manage](https://marketplace.visualstudio.com/manage) → publisher
`cbs-consulting`, and choose **⋯ → Update** for each extension (the first time: **New extension →
Visual Studio Code**). The Marketplace accepts a package only for the publisher named inside it.

Every change goes into the "Unreleased" section of `CHANGELOG.md` in its pull request; the
"Changelog entry" check fails otherwise, unless the pull request has the label "no changelog".
To prepare a release, set the same version in `pyproject.toml`, `src/virtual_key_mapper/__init__.py`
and both `package.json` files (`npm install` in `vscode-extension/` updates the lockfile), and
rename "Unreleased" to `VERSION - DATE`; the tests check that they agree. The workflow can be run
again and skips what is already published; its "dry run" option only checks and builds.

## 🛠️ Contributing

Pull requests need two checks: **Test features** (all CI jobs on Windows, macOS and Linux) and
**Changelog entry**.

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
