# LLM Credential Mapping

First-draft Python CLI for manually selecting a project, signing in through
LiteLLM's native SSO flow, and launching CLI agents without copying API keys.
The command is **`llm-credentials`**.

LiteLLM is the first and currently only gateway. The project/credential layer is
separate from the agent launchers so other gateway adapters can be added later.
This is an independent project, not an official LiteLLM client.

## Scope and identity model

A **project profile** maps a local name to a trusted LiteLLM gateway root and an
exact LiteLLM **team ID**. Here a team represents a business project; it is not
the separate LiteLLM `Project` entity.

```text
Manually selected CLI project -> gateway + expected team ID
                                      |
                          LiteLLM browser login / company SSO
                                      |
                       Employee + consent-selected team token
                                      |
                       OS credential vault, per project profile
                                      |
                       CLI agent -> LiteLLM -> model provider
```

- The employee is established by SSO, never a local username.
- The user selects a team on LiteLLM's browser consent page. The CLI rejects a
  credential if the returned team ID does not match the selected project.
- Login returns a short-lived access token and a rotating refresh token.
  These are **not** existing `sk-...` virtual keys. Native session credentials
  inherit employee/team controls; existing key-specific budgets are not reused.
- No new credential is created per inference request. Credentials are reused
  until they need renewal.
- There is no repository detection, VS Code extension, local proxy, new central
  broker, or administrator credential in this draft.

## Gateway prerequisites

Your gateway operator must configure LiteLLM's native CLI SSO and publish the
version-1 discovery document:

```text
https://your-gateway.example/.well-known/litellm-cli-auth
```

The document must advertise public client registration, authorization code,
PKCE S256, refresh tokens and revocation. Older gateways without this contract
are rejected explicitly; there is no fallback to pasting a key.

Follow [LiteLLM CLI authentication](https://docs.litellm.ai/docs/proxy/cli_sso)
for SSO configuration and `EXPERIMENTAL_UI_LOGIN=True`. CLI SSO is documented as
beta. Check the SSO licensing applicable to your deployment. Use a database,
create the teams and employee memberships, and configure model aliases/budgets.
Set `PROXY_BASE_URL` correctly behind a reverse proxy. For multiple gateway
workers, configure shared Redis as required by LiteLLM's refresh/revocation
contract.

The CLI requires HTTPS and verifies certificates. A gateway hosted under a path
prefix is not supported in this draft; use its public root origin, without
`/v1`. There is no `--insecure` certificate bypass.

## Install

Requires Python 3.11 or newer and an OS credential vault:

- Windows: Credential Manager.
- macOS: Keychain.
- Linux: an accessible Secret Service or KWallet session.

In PowerShell, from the checkout:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\Activate.ps1
llm-credentials --help
```

For an isolated command installation, `pipx install .` is also supported if
pipx is already installed. The LiteLLM server package and `lite` CLI are not
dependencies: this client consumes the documented native OAuth contract.

If Windows denies execution of pip-generated `.exe` launchers, use the module
entry point: `python -m llm_credential_mapping --help`. It accepts exactly the
same arguments as `llm-credentials`; do not weaken your host's security policy.

## Configure and choose projects

Copy the exact **team ID** from your gateway, not merely its display name:

```powershell
llm-credentials project add project-a --gateway https://llm.company.example --team-id TEAM_ID_A
llm-credentials project add project-b --gateway https://llm.company.example --team-id TEAM_ID_B
llm-credentials project list
llm-credentials project use project-a
```

The first configured project becomes the default. `project use` changes the
default for future commands only; it never changes already-running agents.
Use `--project` for deterministic parallel sessions.

Project metadata is stored in `projects.json` in:

- Windows: `%LOCALAPPDATA%\llm-credential-mapping`.
- macOS/Linux: `$XDG_CONFIG_HOME/llm-credential-mapping`, or
  `~/.config/llm-credential-mapping`.

`LLM_CREDENTIAL_MAPPING_HOME` or the global `--home` option changes that
directory. It contains no tokens. Credentials remain in the OS keyring.
Profiles cannot be silently overwritten with a different endpoint/team.

## Login, status and logout

```powershell
llm-credentials login --project project-a
llm-credentials status --project project-a
llm-credentials login --project project-b
llm-credentials logout --project project-a
```

`login` opens the default browser and waits up to 180 seconds for a loopback
callback. Use `--timeout 300` to wait longer, or `--no-browser` to print the
authorization URL to stderr and open it yourself on the same machine.
At consent, select the team corresponding to the CLI project.

Credentials are isolated by profile/gateway/team, with one signed-in employee
per profile. Logging in again replaces that profile's grant and attempts to
revoke the previous refresh token; failed cleanup is reported.

`logout` revokes the refresh token before deleting local credentials. If the
gateway is unavailable, logout fails and keeps the credential so revocation can
be retried. LiteLLM native access tokens are not immediately revocable: existing
copies remain valid until expiry. Do not rely on logout for instant offboarding.

## Launch agents

Install your chosen agent separately and ensure it is on `PATH`.
Use model aliases that your LiteLLM gateway actually exposes.

```powershell
llm-credentials run --project project-a --model YOUR_CLAUDE_ALIAS claude -- --resume
llm-credentials run --project project-b --model YOUR_CODEX_ALIAS codex -- exec "Explain this repository"
llm-credentials run --project project-a --model YOUR_CHAT_ALIAS opencode
```

Launcher options must come **before** the agent name; everything after it is
forwarded to the agent. The optional separator `--` is stripped.

| Agent | Child-only configuration |
| --- | --- |
| Claude Code | Gateway root in `ANTHROPIC_BASE_URL`; access token in `ANTHROPIC_AUTH_TOKEN`; clears `ANTHROPIC_API_KEY` |
| Codex | Custom provider via `-c`, Responses over HTTP/SSE, credential read from a child environment variable |
| OpenCode | OpenAI-compatible provider added through `OPENCODE_CONFIG_CONTENT`; requires `--model`; preserves other inline configuration |

The CLI does not edit the agents' configuration files or your parent shell.
Tokens are not placed in command arguments. The child receives the token at
startup; **a running agent is not automatically reauthenticated** if it outlives
that token. Restart through the launcher or configure the tool's supported
credential helper. Existing agent-managed settings or provider overrides can
conflict with the launcher; verify the selected gateway in that agent.
The Claude launcher also clears inherited Bedrock/Vertex/Foundry mode flags.

Agent behavior and model capabilities depend on client/gateway versions.
Claude, Codex and OpenCode must support the corresponding gateway protocol.
Run-mode adapter tests verify generated configuration, not live agent behavior.

## Credential-helper output

```powershell
llm-credentials token --project project-a
```

On success, stdout contains **only the current access token plus a newline**.
Do not paste this into a file or run it in a logged terminal unless you intend
to display the secret. This command is meant for a supported credential helper.
It never opens a browser: missing/uncertain credentials fail with an actionable
error on stderr.

Tokens with 60 seconds or less remaining are renewed before being returned.
Renewals are serialized with a per-profile cross-process lock. Before a refresh
is sent, the vault is marked pending. If renewal fails, is interrupted, or
cannot be saved, the old refresh token is **not replayed**: sign in again.
Refresh responses must retain the same employee/team binding.

## Security boundaries and current limits

- Only supported native OS keyrings are accepted. There is **no plaintext
  credential fallback**. Headless containers need a secure keyring or another
  deliberately designed storage adapter before this CLI can be used there.
- Sessions larger than Windows Credential Manager's 2,560-byte entry limit are
  split into bounded vault fragments. A small manifest is published only after
  the complete new generation is saved; the previous session remains readable
  until that commit. Missing or corrupt fragments are rejected. Replaced fragments are
  removed, with explicit warnings if cleanup fails. A process crash can leave
  unreferenced fragments in the vault; they never become the active session.
- The gateway is trusted user configuration; no repository-provided endpoint is
  loaded. Discovery endpoints must remain on the configured origin.
- Registration/token/revocation requests do not follow redirects. HTTP error
  bodies and callback query strings are never logged.
- PKCE, state checking, an ephemeral `127.0.0.1` callback and exact returned team
  validation protect the login. There is no embedded browser or password flow.
- Keys/tokens are not sent to GitHub or committed. Child agents can access their
  injected token; operating-system administrators and the selected AI tool
  remain within the trust boundary.
- For local tests only, `project add --allow-insecure-localhost` accepts HTTP on
  `localhost`, `127.0.0.1` or `::1`; it never permits remote cleartext endpoints.
- Manual browser team consent remains required. The profile name is not sent
  as an undocumented automatic team override.
- There is no raw `sk-...` key import, automated repository switching, VS Code
  integration, or claim that GitHub-hosted Copilot traffic is intercepted.

## Development and validation

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip wheel --no-cache-dir --no-deps --wheel-dir dist .
```

Tests use the standard-library unittest runner, mocked keyrings, and a local
HTTP OAuth fixture; they need no company secrets or external LLM calls.
GitHub Actions runs the suite and builds the wheel on Windows, macOS and Linux.

Before production use, test against your actual gateway: SSO sign-in, wrong-team
rejection, simultaneous projects, renewal, failed-renewal recovery, logout,
streaming and tool calls. No live company SSO integration is claimed by this
draft. It is private and has no open-source license assigned yet.

### Protocol references

- [LiteLLM native client contract](https://docs.litellm.ai/docs/proxy/cli_sso#native-client-contract)
- [LiteLLM agent wrappers and session credential semantics](https://docs.litellm.ai/docs/proxy/management_cli)
- [Claude Code gateway connection](https://code.claude.com/docs/en/llm-gateway-connect)
- [Codex gateway connection](https://developers.openai.com/codex/enterprise/connect-to-a-gateway)
