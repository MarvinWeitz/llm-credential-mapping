# LLM Credential Mapping for VS Code

Companion to the [Python CLI](../README.md). Choose a LiteLLM project once per
workspace, then use it through Copilot Chat/Agent or Claude Code. Authentication,
team validation, refresh rotation and secure storage remain in the CLI.

## Install

Requires **VS Code 1.140 or newer**, the Python CLI on the workspace host and an
accessible native OS vault. Install/update the CLI from the repository root:

```sh
python -m pip install -e .
```

Install the companion's VSIX with **Extensions: Install from VSIX...**. For a
dev container or SSH workspace, install it on the **remote workspace host**.
The CLI and vault must be accessible there; a container cannot implicitly use
the host's Windows Credential Manager.

To build the private VSIX from source, use Node 24 and npm:

```sh
cd vscode-extension
npm ci
npm run package
```

This produces `llm-credential-mapping-0.1.0.vsix`. There is no Marketplace
publication or assigned open-source license.
Successful repository CI runs also provide a private
`llm-credential-mapping-vsix` download artifact in GitHub Actions.

## Choose and authenticate a workspace project

1. Configure your profiles using the CLI's `project add` command.
2. Run **LLM: Configure CLI Runtime**. Set the executable in **User Settings**
   for local work, or **Remote Settings** for a container/SSH host.
3. Run **LLM: Select Project**.
4. Run **LLM: Login**, open the URL in its terminal and approve the matching
   LiteLLM team.
5. Run **LLM: Show Status**; the expected session state is `ready`.

The default executable is `llm-credentials` on the extension host's `PATH`.
For module execution, configure these nonsecret runtime settings:

```json
{
  "llmCredentials.cliExecutable": "C:\\path\\to\\repo\\.venv\\Scripts\\python.exe",
  "llmCredentials.cliArguments": ["-m", "llm_credential_mapping"]
}
```

In a container, use its absolute Python path instead, for example
`/workspaces/mapping-cli/.venv/bin/python`. `homeDirectory` optionally sets the
CLI's configuration directory. These machine-scoped settings cannot be supplied
by a repository to replace the trusted executable.

The status bar shows the **workspace binding**, not a guarantee that a session
is still valid. Use **LLM: Show Status** for authentication state. Selection is
stored in VS Code workspace state, not in Git or the CLI's global active profile.
Different windows may select different projects. Every credential operation
passes an explicit `--project`; changing `project use` cannot redirect a window.
Changing the selected profile's gateway/team requires explicit reselection.

### Containers and remote login

**LLM: Login** deliberately uses a visible terminal and `--no-browser`. The
callback URI must remain the one registered by the CLI. Forward its exact
localhost port to the same port on your browser's host, then open the URL. An
arbitrary public tunnel URL or different forwarded port is not interchangeable.

On headless Linux, start and unlock Secret Service in a D-Bus session first.
Follow the [keyring headless guide](https://keyring.readthedocs.io/en/latest/#using-keyring-on-headless-linux-systems).
If it was started in an integrated terminal, its D-Bus environment is not
automatically inherited by the extension host. In that terminal:

```sh
printf '%s\n' "$DBUS_SESSION_BUS_ADDRESS"
```

Set that nonsecret address in Remote Settings as
`llmCredentials.dbusSessionBusAddress`. Leave the D-Bus/keyring session running.
Recreate/update it after a container restart. The companion passes the address
to both CLI calls and Claude's helper. No password or token is put in settings,
and there is no plaintext credential fallback.

## GitHub Copilot Chat and Agent

Install the official GitHub Copilot Chat extension and use an eligible account.
The organization's **Bring Your Own Language Model Key** policy must allow
contributed providers.

Open the chat model picker and select a model under **CBS LiteLLM**. Its detail
identifies your workspace project. A fresh CLI credential is obtained for each
inference request; the adapter does not cache tokens between requests.

The gateway must allow these authenticated routes:

- `GET /v1/models`: aliases available to this employee/team.
- `GET /model/info`: token limits and capabilities for those aliases.
- `POST /utils/token_counter`: model-specific token counting.
- `POST /v1/chat/completions`: streaming OpenAI-compatible inference.

Only allowed aliases are exposed. Non-chat modes are excluded. Multiple
deployments of an alias use the smallest declared input/output limits and only
capabilities confirmed by every deployment. Missing capability flags do not
advertise support. Missing token limits produce an explicit configuration error
rather than a guessed context window. **LLM: Configure Copilot Models** can add
project-scoped metadata overrides using operator-verified limits/capabilities.
Gateway tokenizer accuracy and the final context/budget enforcement remain
gateway responsibilities.

Supported: streamed text/refusals, fragmented and parallel function calls,
text tool results, cancellation and PNG/JPEG/WebP/GIF user images when advertised.
Unsupported message parts and non-text tool results fail explicitly, rather
than being dropped. Provider-specific thinking/signature/cache-control protocols
are not implemented; validate an alias's Agent compatibility before relying on
it. Optional request options are `maxTokens` and `temperature`; unsupported
options fail explicitly.

This is **not a universal Copilot interceptor**. GitHub-hosted models, ordinary
Tab completions and other extensions' traffic remain unchanged. You must
explicitly select the companion's model. Requests have a ten-minute timeout;
potentially billed inference is never automatically retried by this adapter.

When switching projects, in-flight requests keep their original profile.
Old model selections fail before a new request; start a new chat and select the
current project's model. One project is selected per workspace, including all
folders of a multi-root workspace.

## Official Claude Code extension

Install Anthropic's official extension. In a **single-folder workspace**:

1. Select and log in to the project as above.
2. Close existing Claude conversations.
3. Run **LLM: Configure Claude Code** and enter a Claude-compatible gateway alias.
4. Confirm the local configuration change and start a new Claude conversation.
5. Approve the credential helper if Claude requests permission.

The command updates `.claude/settings.local.json` with:

- Gateway/model environment settings for this repository.
- `apiKeyHelper` invoking the CLI's `token --project NAME` command.
- A 30-second helper TTL; renewal remains the CLI's responsibility.

It also sets workspace `claudeCode.disableLoginPrompt=true` to skip first-party
onboarding for this third-party gateway. It does **not** write machine-scoped
`claudeCode.environmentVariables` or a user-global project assignment.

Existing unrelated settings, environment entries and JSONC comments are
preserved. Related auth/provider/model settings are replaced with confirmation.
Symlinked settings and unsaved edits are refused. Conflicting credentials/cloud
mode variables or a different gateway/model in the host or Claude extension's
machine environment must be removed first. No LiteLLM session token is copied
into a configuration file. Keep `.claude/settings.local.json` out of Git.

Switching the companion's project does not rewrite Claude settings or hot-switch
an existing conversation. Reconfigure and restart Claude each time. CLI runtime
changes also require rerunning this configuration command. The gateway must
implement Claude's Messages API for the chosen alias.

The native helper command is shell-quoted on POSIX systems; on Windows it uses
PowerShell with an encoded, nonsecret command to preserve literal executable
paths/arguments and propagate failures. Host security policies still apply.
Existing Claude wrappers or managed policy can override client behavior; verify
the selected gateway/model and employee/team attribution in LiteLLM's logs.

## Attribution and budgets

Both clients use native SSO session credentials, not an existing virtual key.
The authenticated employee and team identify the budget owner. Configure
employee/project allowances through LiteLLM team-membership budgets; see the
[CLI guide](../README.md#vs-code-integrations).

In LiteLLM request logs, check `metadata.user_api_key_user_id` and
`metadata.user_api_key_team_id`. The companion does not add an untrusted header
as a substitute for gateway-authenticated identity.

## Development

```sh
npm ci
npm test
npm run test:host
npm run package
```

Node's built-in runner covers CLI output contracts, redaction, cancellation,
SSE framing/tool reconstruction, discovery and Claude settings/helper behavior.
The official VS Code Electron runner uses an isolated workspace, user-data
directory and extensions directory with synthetic CLI/HTTP fixtures. It tests
registration, real message-part classes, token-limit boundaries, project
pinning, tool roundtrips and cancellation without company credentials.

The host runner downloads VS Code 1.140.0 by default. To use an existing matching
installation, set `VSCODE_EXECUTABLE_PATH` to its executable. Linux CI runs the
host suite under `xvfb-run`. Live Copilot/Claude acceptance tests and actual
exhausted-budget enforcement are separate checks, not implied by fixture tests.

## Supported integration references

- [VS Code language-model provider API](https://code.visualstudio.com/api/extension-guides/ai/language-model-chat-provider)
- [VS Code model selection and BYOK policy](https://code.visualstudio.com/docs/agent-customization/language-models)
- [Remote extension execution](https://code.visualstudio.com/api/advanced-topics/remote-extensions)
- [Claude Code VS Code settings](https://code.claude.com/docs/en/vs-code)
- [Claude gateway configuration](https://code.claude.com/docs/en/llm-gateway-connect)
- [Claude configuration reference](https://code.claude.com/docs/en/settings-reference)
