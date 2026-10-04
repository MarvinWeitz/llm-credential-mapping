# LLM Credential Mapping

Python CLI for signing in through LiteLLM's native SSO flow and launching CLI
agents with repository-selected, session-local customer tags.
The command is **`llm-credentials`**.

LiteLLM is the first and currently only gateway. The project/credential layer is
separate from the agent launchers so other gateway adapters can be added later.
This is an independent project, not an official LiteLLM client.

## Scope and identity model

An **authentication profile** maps a local name to a trusted LiteLLM gateway
root and an exact **business-unit team ID**. Customers are request tags, not
teams. A profile is not the separate LiteLLM `Project` entity.

```text
Authentication profile -> gateway + business-unit team -> company SSO
                                                          |
                                            employee/team credential in OS vault
                                                          |
Git origin -> repository customer default -> isolated agent process
                                                          |
                                      request-local customer tag -> LiteLLM
```

- The employee is established by SSO, never a local username.
- The user selects a team on LiteLLM's browser consent page. The CLI rejects a
  credential if the returned team ID does not match the selected profile.
- Login returns a short-lived access token and a rotating refresh token.
  These are **not** existing `sk-...` virtual keys. Native session credentials
  inherit employee/team controls; existing key-specific budgets are not reused.
- No new credential is created per inference request. Credentials are reused
  until they need renewal.
- The customer tag is pinned at launch. Two agents can share the credential
  while sending different tags; the CLI never updates tags on a shared key.
- There is no custom VS Code extension, local proxy, new central broker, or
  administrator credential. The optional official-Claude adapter adds a
  rotating credential helper without changing the existing CLI launchers.

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

## Configure and choose authentication profiles

Copy the exact **team ID** from your gateway, not merely its display name:

```powershell
llm-credentials profile add consulting --gateway https://llm.company.example --team-id BUSINESS_UNIT_TEAM_ID
llm-credentials profile list
llm-credentials profile use consulting
```

The first profile becomes the default. `profile use` changes future commands
only; already-running agents are unchanged. `--profile` selects authentication,
not a customer. `project` and `--project` remain compatible aliases, and existing
profiles, vault account identifiers and SSO sessions are preserved. If an old
profile represents a customer-specific team, add a business-unit profile and
sign in to it rather than rewriting that profile's credentials.

Project metadata is stored in `projects.json` in:

- Windows: `%LOCALAPPDATA%\llm-credential-mapping`.
- macOS/Linux: `$XDG_CONFIG_HOME/llm-credential-mapping`, or
  `~/.config/llm-credential-mapping`.

`LLM_CREDENTIAL_MAPPING_HOME` or the global `--home` option changes that
directory. It contains no tokens. Credentials remain in the OS keyring.
Profiles cannot be silently overwritten with a different endpoint/team.

## Login, status and logout

```powershell
llm-credentials login --profile consulting
llm-credentials status --profile consulting
llm-credentials logout --profile consulting
```

`login` opens the default browser and waits up to 180 seconds for a loopback
callback. Use `--timeout 300` to wait longer, or `--no-browser` to print the
authorization URL to stderr and open it yourself on the same machine.
At consent, select the business-unit team corresponding to the profile. You
do not switch teams when changing customers.

Credentials are isolated by profile/gateway/team, with one signed-in employee
per profile. Logging in again replaces that profile's grant and attempts to
revoke the previous refresh token; failed cleanup is reported.

`logout` revokes the refresh token before deleting local credentials. If the
gateway is unavailable, logout fails and keeps the credential so revocation can
be retried. LiteLLM native access tokens are not immediately revocable: existing
copies remain valid until expiry. Do not rely on logout for instant offboarding.

## Discover tags and bind repositories

```powershell
llm-credentials tags list --profile consulting
llm-credentials tags list --profile consulting --prefix customer- --json
```

Discovery uses authenticated `GET /tag/list` on the configured gateway, without
redirects. Output contains tag names and descriptions only. The server defines
visibility: a deployment may return all tags or just tags previously used by
the employee. This API is **not an allowed-customer authorization catalogue**.
It may include non-customer tags such as user-agent labels. `--prefix` filters
locally; it does not create or prepend a prefix to the chosen tag.

Inside a repository, select its customer once:

```powershell
llm-credentials repo bind --profile consulting
llm-credentials repo list --profile consulting
```

The interactive picker displays visible tags; nothing is selected implicitly,
even if only one tag is visible. For a known administrator-approved tag, or if
tag discovery is forbidden/empty, bind it explicitly:

```powershell
llm-credentials repo bind --profile consulting --customer customer-acme
llm-credentials repo unbind --profile consulting
```

Explicit binding/listing/unbinding requires no vault access. `repo bind` and
`repo unbind` accept `--directory PATH` to configure another checkout. Bindings
are stored atomically in nonsecret `repositories.json` beside `projects.json`.
They are scoped by gateway and normalized Git `origin`: HTTPS and SSH URLs for
the same repository match, and embedded remote credentials are never stored.
Separate clones and worktrees share the default; the profile's team is not part
of customer selection. Local/file remotes and missing origins require explicit
`--customer` at launch instead. No Git remote is contacted for detection.

Updating a binding changes only future launches. Use `repo bind --customer TAG`
to replace a default deliberately; concurrent first-time selections cannot
silently overwrite each other. Repository mappings are attribution preferences,
not proof of authorization or of what a developer actually worked on.

## Launch agents with customer context

Install your chosen agent separately and ensure it is on `PATH`.
Use model aliases that your LiteLLM gateway actually exposes.

```powershell
llm-credentials run --profile consulting --model YOUR_CLAUDE_ALIAS claude
llm-credentials run --profile consulting --model YOUR_CODEX_ALIAS codex -- exec "Explain this repository"
llm-credentials run --profile consulting --model YOUR_CHAT_ALIAS opencode
llm-credentials run --profile consulting --model YOUR_CHAT_ALIAS opencode -- run "Explain this repository"
```

The CLI uses the repository binding. An unbound repository opens the tag picker
in an interactive terminal and saves your choice; use `--tag-prefix customer-`
to narrow that picker. Noninteractive launches fail without a binding or an
explicit customer. There is no silent untagged fallback.

Override the repository default for one process without changing the binding:

```powershell
llm-credentials run --profile consulting --customer customer-acme --model YOUR_CHAT_ALIAS opencode
llm-credentials run --profile consulting --customer customer-other --model YOUR_CHAT_ALIAS opencode
```

Both processes may run simultaneously with the same employee credential. Their
customer headers remain independent, including later requests in a long-running
prompt. Explicit `--customer` also works outside a Git repository, without a
tag-list request. Use an existing approved tag: arbitrary or misspelled labels
can misattribute spend. Names must be 1-256 printable ASCII characters, without
commas or surrounding whitespace, for the comma-separated HTTP header.
Start a new conversation when changing customers; do not resume another
customer's conversation under a different tag.

Launcher options must come **before** the agent name; everything after it is
forwarded to the agent. The optional separator `--` is stripped.

| Agent | Child-only configuration |
| --- | --- |
| Claude Code | Gateway/auth environment; customer header in `ANTHROPIC_CUSTOM_HEADERS`; preserves unrelated custom headers, replaces inherited tags, rejects auth/Host overrides |
| Codex | Custom Responses provider via `-c`; credential and customer header read from separate child environment variables |
| OpenCode | Inline OpenAI-compatible provider with `options.headers`; requires `--model`; preserves unrelated inline configuration |

OpenCode uses the `run` subcommand for a non-interactive prompt, not Codex's
`exec`. The launcher supports OpenCode v1 and v2, detecting the installed version
before launch. For v2, interactive and `run` sessions use `--standalone` so an
existing background service cannot retain another project's endpoint/token.
Shared-server `--server`/`--attach` options are rejected in both versions to
protect credential and customer isolation. Other v2 subcommands should
be invoked directly with `opencode`, outside this credential launcher.
The selected model is supplied through inline configuration instead of a root
`--model` flag, which OpenCode v2 does not accept.

The `run` command does not edit agent configuration files or your parent shell.
Tokens are not placed in command arguments. The child receives the token at
startup; **a running agent is not automatically reauthenticated** if it outlives
that token. Restart through the launcher or configure the tool's supported
credential helper. Existing agent-managed settings or provider overrides can
conflict with the launcher; verify the selected gateway in that agent.
The Claude launcher also clears inherited Bedrock/Vertex/Foundry mode flags.
Use a Claude Code version supporting `ANTHROPIC_CUSTOM_HEADERS`, a Codex version
supporting provider `env_http_headers`, and an OpenCode provider supporting
`options.headers`.

Agent behavior and model capabilities depend on client/gateway versions.
Claude, Codex and OpenCode must support the corresponding gateway protocol.
Adapter tests verify generated configuration, and a local HTTP fixture verifies
real parallel child-process isolation. They do not prove that installed vendor
clients honor the headers. Confirm `request_tags` in LiteLLM spend logs for each
client/protocol before relying on customer attribution.

### Budgets and customer authorization

Native SSO authentication is unchanged; this implementation does not import
virtual keys or enable OIDC JWT-to-key mapping. User/team controls remain
separate from customer attribution. LiteLLM supports tag budgets when configured
on a compatible gateway; employee-by-customer budgets require compound tags or
additional server policy.

Client tags and repository mappings are not access controls. For enforced
charging, the gateway must require one customer, reject conflicting header/body
attribution, check the authenticated employee's customer permissions and apply
the validated tag before customer-budget checks and spend logging. A future
allowed-customer endpoint should use the same permission registry as that
policy. Do not grant developers an administrator/master key just to list tags.
Missing, deleted or no-longer-permitted tags in saved bindings are not
revalidated by the CLI on each launch; the gateway must enforce those rules.

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

## Official Claude Code VS Code extension

This integration uses the official extension's `claudeProcessWrapper` and
Claude's `apiKeyHelper`. No companion extension is required. Configure it on the
host where the official extension runs: in the container for Remote Containers,
or locally for a local workspace. The CLI, Git, repository configuration and OS
vault must be available on that same host.
Use official Claude Code extension 2.1.246 or newer.

### Parent and multi-repository workspaces

If VS Code launches Claude from a parent folder rather than a Git checkout,
select one primary repository explicitly:

```bash
llm-credentials claude workspace bind --workspace WORKSPACE_DIRECTORY --repository GIT_CHECKOUT_DIRECTORY
llm-credentials claude workspace list
llm-credentials claude check --directory WORKSPACE_DIRECTORY
```

Use the actual launch directory shown in the adapter error, not the path to a
`.code-workspace` file. The selected checkout must have an `origin` remote.
Its existing gateway/repository customer binding supplies the tag. This extra
mapping does not create a customer tag, change authentication or require vault
access. If the checkout has no customer binding, run
`repo bind --profile demo --directory GIT_CHECKOUT_DIRECTORY` separately.

Mappings are local nonsecret `workspaces.json` alongside `projects.json`, keyed
by the exact absolute launch directory. They contain the selected checkout path
and expected Git origin identity. They are not written into your repositories.
If the checkout moves, disappears or its origin changes, the adapter fails
rather than silently selecting another repository/customer.

For a workspace containing several repositories, **one primary repository**
is used for Claude. The wrapper starts Claude in that checkout and adjusts an
SDK-supplied project root to match, so relative tool operations and customer
attribution use the same selection. Other workspace repositories can still be
accessible to tools: this is not a filesystem or authorization boundary.
Switching editor tabs does not change the primary repository or the tag.

Re-run `workspace bind` to change the selection, then **reload the VS Code
window before starting a conversation**, so prestarted processes cannot retain
the old selection. Existing processes are not modified by the CLI. Do not
resume another customer's conversation after a primary-repository change.
Distinct launch directories can have independent selections; windows using the
same launch directory and CLI configuration share the same primary selection.
Direct launches inside a Git checkout do not inherit a parent directory's
mapping: mappings are exact, not recursive.

Remove a selection with:

```bash
llm-credentials claude workspace unbind --workspace WORKSPACE_DIRECTORY
```

Reload afterwards. Without a mapping or a Git repository at the launch root,
the adapter again fails explicitly instead of guessing among child checkouts.

### Try it

Reinstall the updated CLI to create its second executable entry point:

```bash
python -m pip install -e .
```

Use your active virtual environment, and run inside a bound repository:

```bash
llm-credentials claude setup --profile demo --model YOUR_LITELLM_ALIAS
```

Setup checks the installed wrapper executable, repository binding and signed-in
employee before saving nonsecret `claude.json` beside `projects.json`. It prints
a VS Code JSON settings snippet; it does not modify any editor/project files.
If needed, run `repo bind --profile demo` and `login --profile demo` first.

In VS Code, open **Preferences: Open User Settings (JSON)** and merge the
printed snippet. For a container/SSH workspace, use the **Remote** tab's User
Settings instead. The snippet has these fields:

```json
{
  "claudeCode.claudeProcessWrapper": "/absolute/path/to/venv/bin/llm-credentials-claude",
  "claudeCode.disableLoginPrompt": true,
  "claudeCode.environmentVariables": [
    {
      "name": "LLM_CREDENTIAL_MAPPING_HOME",
      "value": "/absolute/path/to/credential/configuration"
    }
  ]
}
```

Use the actual paths from setup, not these placeholders. On Windows, setup
prints the installed `.exe` path. Keep unrelated `environmentVariables` entries
when merging, but remove old gateway credential/routing overrides. Neither the
snippet nor `claude.json` contains a token. The wrapper setting is
machine-scoped, so putting it only in Workspace Settings does not work.

Reload the VS Code window. Start a **new** conversation in the official Claude
Code panel and send a small prompt. Check **View -> Output -> Claude Code** for
the adapter's profile/customer/repository line, and confirm the customer tag in
LiteLLM spend logs. Model requests go directly from Claude to LiteLLM.

### Session behavior

- At startup the wrapper resolves Claude's explicit `--project-config-root`
  directory when provided by the SDK, otherwise the process's working directory,
  through any exact primary-workspace mapping to the Git `origin`, then uses the
  existing gateway/repository customer binding. There is
  no browser or interactive customer picker in a background process; an unbound
  repository fails with a `repo bind` instruction.
- `claude setup` selects one authentication profile and model for future
  extension sessions. It does not change the default profile used by other CLI
  commands. Rerun setup to choose another profile/model.
- The profile, employee, customer and model are pinned in a nonsecret child
  context. Different repository windows keep independent customer tags.
  Later profile selections, repository rebindings and setup changes do not
  retag an existing child or change its credential helper's profile. If someone
  signs in as a different employee, the old helper fails instead of switching
  identity. Start a new session after an intentional context change.
- Credentials are fetched by `apiKeyHelper`, not embedded in settings, command
  arguments or a static `ANTHROPIC_AUTH_TOKEN`. The helper uses the installation's
  absolute Python interpreter and the session's pinned configuration directory.
  It checks the vault on Claude's one-minute helper cache interval; refreshes
  reuse the existing cross-process locks and single-use rotation safeguards.
  Claude 2.1.246+ also refreshes expired cached JWTs before a later request.
  This does not extend an already in-flight request or remove SSO re-login needs.
- Inline settings preserve extension-provided hooks, permissions and unrelated
  environment values. The adapter overrides gateway/customer/auth settings,
  model/fallback aliases and small-model aliases for this process. The model
  configured by setup takes precedence over the panel's model picker; rerun
  setup and start a new session to change it.
- Nonessential Claude traffic is disabled in wrapped sessions; this also
  disables Claude's own auto-update checks in those processes. Update the
  official extension through VS Code.
- This is local inference through the official extension, not Claude cloud
  sessions or GitHub Copilot integration. The official wrapper hook can change
  permission-mode defaults and session-resume checks; see the vendor reference.

### Diagnostics and removal

```bash
llm-credentials claude check
llm-credentials claude check --directory REPOSITORY_PATH
```

This prints only nonsecret metadata and verifies/renews the credential. It does
not send a model prompt. For extension-host diagnostics, the wrapper executable
also accepts `--llm-check`; never invoke `claude token` to diagnose a problem,
since that internal command prints a secret.

If the terminal works but the extension fails, inspect the adapter error in the
Claude output channel. A Linux vault accessed through a terminal-started D-Bus
session may not be reachable from VS Code's extension host. This adapter does
not redesign container vault storage: keep a secure vault available to that
host, or stop before sending requests. A terminal-only `claude check` is not
proof of extension-host vault access.

For a Git lookup failure, the wrapper reports the actual process cwd, lookup
directory and whether Claude launched a configuration probe. Run `claude check
--directory PATH` with that lookup directory to compare it with your terminal.
The opened VS Code folder can differ from the directory you entered in an
integrated terminal. Open the bound repository directly, supply a primary
workspace mapping, or ensure the host supplies its explicit project root.
The adapter never guesses from child
repositories or the only saved binding. Git trust/ownership failures are
reported separately; it does not bypass Git's `safe.directory` protection.

If setup says it cannot execute the installed wrapper, check the executable
permissions/application policy. Some Windows hosts block pip-generated `.exe`
launchers; calling the main CLI with `python -m ...` does not remove that
restriction for the extension's executable hook. Do not weaken host policy to
try this adapter.

To disable integration, remove `claudeCode.claudeProcessWrapper` and the
integration's `LLM_CREDENTIAL_MAPPING_HOME` environment entry from User/Remote
Settings. Restore `disableLoginPrompt` if you want first-party login prompts,
then reload. Existing vault credentials and repository bindings are untouched.

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
- There is no raw `sk-...` key import or claim that GitHub-hosted Copilot traffic
  is intercepted. The official-Claude wrapper is optional and does not install
  a custom VS Code extension.

## Development and validation

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m pip wheel --no-cache-dir --no-deps --wheel-dir dist .
```

Tests use the standard-library unittest runner, mocked keyrings, and a local
HTTP OAuth fixture; they need no company secrets or external LLM calls.
GitHub Actions runs the suite and builds the wheel on Windows, macOS and Linux.

The optional installed-Claude test uses only a localhost fixture and a temporary
synthetic OS-vault credential. Set `LLM_CREDENTIAL_MAPPING_TEST_CLAUDE_BINARY` to
the official native binary's absolute path and run
`python -m unittest discover -s tests -p test_claude_native.py -v` to exercise its
stream-json protocol, customer headers, helper retry after 401 and refresh
rotation. This test is skipped by default because it needs an installed Claude
binary and an accessible native vault. It is not an extension-host UI test.

Before production use, test against your actual gateway: SSO sign-in, wrong-team
rejection, simultaneous customers, renewal, failed-renewal recovery, logout,
streaming, tool calls, tag logging and configured budget enforcement. The new
tagging behavior is tested locally, not yet against live model calls.
It is private and has no open-source license assigned yet.

### Protocol references

- [LiteLLM native client contract](https://docs.litellm.ai/docs/proxy/cli_sso#native-client-contract)
- [LiteLLM agent wrappers and session credential semantics](https://docs.litellm.ai/docs/proxy/management_cli)
- [Claude Code gateway connection](https://code.claude.com/docs/en/llm-gateway-connect)
- [Codex gateway connection](https://developers.openai.com/codex/enterprise/connect-to-a-gateway)
- [LiteLLM request tags](https://docs.litellm.ai/docs/proxy/request_tags)
- [LiteLLM tag budgets](https://docs.litellm.ai/docs/proxy/tag_budgets)
- [Claude Code custom headers](https://code.claude.com/docs/en/env-vars)
- [Codex provider headers](https://developers.openai.com/codex/config-reference/)
- [OpenCode provider headers](https://opencode.ai/docs/providers/)
- [Official Claude extension wrapper](https://code.claude.com/docs/en/vs-code#extension-settings)
- [Claude credential-helper rotation](https://code.claude.com/docs/en/llm-gateway-connect#rotate-credentials-with-apikeyhelper)
