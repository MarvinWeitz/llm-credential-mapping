# Implementation plan: zero-touch customer tagging with LiteLLM SSO

This is the working plan for the redesign agreed on 2026-10-07. It is written so
the work can continue in a fresh session (local Claude Code or by hand) without
the original conversation. Start with [Current state](#current-state), then
continue at **step 2**.

Related files:

- `docs/plan/drafts/` – unreviewed drafts for step 2
  (`repositories.py`, `customers.py`). Copy them into
  `src/llm_credential_mapping/` as a starting point; they have no tests yet.
- `docs/plan/research/` – raw research notes with source references
  (LiteLLM, Claude Code, Codex, OpenCode, VS Code/Copilot). Line numbers in
  those notes are sometimes off; the claims were cross-checked by a second
  reviewer, see each file's "VERIFY" section.
- `docs/future/litellm-projects.md` – why LiteLLM Projects are deferred.

## Goals and decisions

Goals, in priority order:

1. As little developer interaction as possible to assign each project its
   customer tag.
2. It "just works" for Claude Code CLI, Claude Code VS Code extension, Codex
   CLI, OpenCode CLI and GitHub Copilot Chat.
3. Keep LiteLLM native SSO with short-lived tokens. No `sk-` keys to issue,
   rotate or paste.

Decisions made:

| Topic | Decision |
| --- | --- |
| Attribution carrier | LiteLLM request tag header `x-litellm-tags` (Projects deferred, see `docs/future/litellm-projects.md`). |
| Compatibility | Greenfield. Nothing was ever live: no migration code, no compatibility aliases, delete replaced code. |
| Unmapped repository in background tools (VS Code, hooks) | Block with an actionable message. No silent untagged requests. |
| Central rules | Optional per profile (HTTPS URL or absolute file path). Files and local overrides work without it. |
| Claude sessions | Tags come from settings files that Claude reloads live, so a rebind retags running sessions. Accepted. |
| Copilot extension location | `vscode-extension/` in this repository. |
| Naming | "profile" only (done in step 1). |

## Target architecture

```text
one-time per machine   llm-credentials profile add / login
                       llm-credentials setup claude|codex|opencode   (user-level config, reversible)
                       install the VS Code extension (Copilot)
per repository         nothing if a central rule matches; otherwise one committed .llm-customer
per request            each tool calls `llm-credentials token` (cached, renewed early)
                       and sends x-litellm-tags + x-litellm-spend-logs-metadata from the resolver
```

- **Auth**: LiteLLM native CLI SSO. The CLI keeps the grant in the OS keyring
  and renews it. Tools never get a static token; they run a credential helper.
- **Customer resolver** (one function used by every integration), first match
  wins:
  1. explicit `--customer` or `LLM_CREDENTIALS_CUSTOMER`
  2. personal local override (`repositories.json`, written only by `repo bind`)
  3. nearest committed `.llm-customer` (walking up from the working directory;
     skip `$HOME` itself)
  4. central rules (glob on the normalized Git origin, then `upstream`)
  5. unmapped → terminal picker in a TTY, otherwise block
- **Repository identity on every request**:
  `x-litellm-spend-logs-metadata: {"customer_source":"rules","repository":"github.com/acme/foo"}`
  so spend can be re-attributed later (LiteLLM stores this JSON in spend logs;
  verified in `litellm/proxy/litellm_pre_call_utils.py` `_get_spend_logs_metadata_from_request_headers`).

## Current state

Branch: `claude/gifted-hawking-pday1i`.

| Commit | Content |
| --- | --- |
| `ee72c6b` | `docs/future/litellm-projects.md` |
| `8533732` | **Step 1 done**: token reliability + profile rename + CLI handlers |

Step 1 details (already implemented, 264 tests passing):

- `litellm.py`: `Session` v2 stores `issued_at`, `token_endpoint`,
  `revocation_endpoint`, `resource`, `needs_login`. Refresh/revoke use the stored
  endpoints (no discovery). `check_binding` pins endpoints to the gateway origin.
- `transport.py`: `GatewayError(status)`; `retryable` = no response, 408, 429, ≥ 500.
- `sessions.py`: renew when remaining < `min(max(600, lifetime/4), lifetime/2)`.
  Retryable failure → keep session, return current token if > 60 s left.
  Non-retryable gateway error or unexpected response → `needs_login`.
  `login()` runs the browser flow without the profile lock. Lock wait 60 s.
  Basis (LiteLLM source): refresh tokens are claimed only after the replacement is
  minted; outages return 503 and keep the token valid; a replayed refresh token
  gets 400 `invalid_grant` without revoking the grant.
- `profiles.py`: `profiles.json`, `locked(name, timeout=...)`.
- `cli.py`: one handler per subcommand, `--profile` only, `token --json`
  (prints access_token, expires_at, profile, gateway, team_id, user_id).

**Pending:** the adversarial review of step 1 confirmed several issues; they
are listed with scenarios and fixes in [`step1-review.md`](step1-review.md).
Fix them first. The review covered these lenses:

- Concurrency: several processes calling `token()` (Claude helper, Codex auth
  command, VS Code extension); a helper killed mid-refresh; a login in one
  process while another marks `needs_login` (the later `put` could overwrite a
  fresh login with the old session plus `needs_login=True` – consider
  re-reading the vault before `_mark_needs_login` and only marking when the
  stored refresh token is unchanged).
- Security: `token --json` output, error messages never contain response bodies,
  concurrent logins revoke the right grant.
- Regressions: every old CLI option still exists (compare with
  `git show ee72c6b:src/llm_credential_mapping/cli.py`); Windows/macOS file locking.

## Step 2: shared customer resolver

Files: `repositories.py` (rewrite from draft), new `customers.py` (from draft),
`profiles.py` (rules option), `cli.py`, tests.

1. **Repository identity** (`repositories.repository_identity`, draft ready):
   - lowercase host and path for all forges; drop ports and `.git`; strip credentials;
   - Azure DevOps: `dev.azure.com/org/project/_git/repo`,
     `git@ssh.dev.azure.com:v3/org/project/repo`,
     `org.visualstudio.com/[DefaultCollection/]project/_git/repo` and
     `org@vs-ssh.visualstudio.com:v3/org/project/repo` → `dev.azure.com/org/project/repo`;
   - SSH host aliases resolved with `ssh -G <alias>` (`hostname` line); only for
     hosts matching `[A-Za-z0-9][A-Za-z0-9._-]*` (no option injection);
   - `find_checkout(directory) -> Checkout(root, origin, upstream) | None`
     (None outside Git; dubious ownership is an error; invalid remotes become None).
   - Update `tests/test_repositories.py` (old expectations keep ports and case).
2. **`.llm-customer`** (TOML, exactly `customer = "TAG"`, ≤ 4 KiB), nearest
   ancestor wins, `$HOME` itself ignored. `repo bind --shared` writes it at the
   checkout root (or the given directory outside Git).
3. **Central rules** (`customers.Rules`, `RulesLoader`, draft ready):
   - Profile gets optional `rules` (HTTPS URL, loopback HTTP with
     `--allow-insecure-localhost`, or absolute file path). Not part of
     `Profile.account`. Add `profile add --rules` and
     `profile update NAME --rules X | --no-rules`.
   - Document format v1:
     ```json
     {"version": 1,
      "customers": [{"tag": "customer-acme", "name": "Acme"}],
      "rules": [{"match": "github.com/acme-*/**", "customer": "customer-acme"}]}
     ```
     `**` spans segments, `*`/`?` stay within a segment, full match, lowercase.
     Origin is tried against all rules before upstream.
   - URLs cached in `<home>/rules-cache/<sha256(url)>.json` for 1 h with ETag;
     on fetch failure use the stale cache; bearer token sent only when the
     URL has the gateway's origin.
   - The catalogue replaces `/tag/list` in the picker when present
     (`/tag/list` is empty for new users).
4. **Resolver** `customers.resolve_customer(store, profile, directory, explicit=None)
   -> Attribution | None` with `Attribution(customer, source, detail, repository)`
   and `Attribution.headers()` (tag + spend-log metadata JSON, compact, ASCII).
   `unmapped_error()` gives the actionable message.
5. **CLI**:
   - `repo show [--directory D] [--json]` → attribution or exit 1 with the
     unmapped message (used later by hooks and the VS Code extension);
   - `repo bind [--customer TAG] [--shared] [--directory D] [--tag-prefix P]`;
   - `repo unbind`, `repo list` (local overrides only);
   - `run` uses the resolver; unmapped + TTY → picker, saves a local override.
6. **Tests**: identity normalization table (Azure forms, SSH alias via injected
   resolver, case, ports); file lookup (subfolder, parent folder, `$HOME`
   skipped, invalid file is an error); rules parsing/matching/order/upstream;
   cache (fresh, stale on outage, 304, bearer only same-origin); resolution order;
   headers JSON.

## Step 3: Claude Code via native settings (CLI and VS Code extension)

Verified facts (Claude Code docs, `code.claude.com/docs/en/settings-reference`,
`llm-gateway-connect`, `vs-code`, `hooks`; tested locally with Claude Code 2.1.291):

- `apiKeyHelper` output is sent as `Authorization: Bearer` and `x-api-key`. It is
  re-run after the TTL (5 min default, `CLAUDE_CODE_API_KEY_HELPER_TTL_MS`) and
  after a 401/403 **only when `ANTHROPIC_AUTH_TOKEN` is unset**. The "expired
  JWT" trigger does not apply: LiteLLM session tokens are opaque.
- The helper runs in the session's working directory. It cannot set headers.
- The only per-request header source is `ANTHROPIC_CUSTOM_HEADERS` (newline
  separated). Project/local `env` applies after workspace trust (immediately in
  `-p`/SDK mode). `.claude/settings.local.json` is read at the git root even
  from subdirectories and worktrees (not on Windows: starting directory).
  Local replaces user-level values. Settings files are watched and reloaded.
- A `UserPromptSubmit` hook sees the merged `ANTHROPIC_CUSTOM_HEADERS` and can
  block with `{"decision":"block","reason":"..."}` before any API call.
- A file created mid-session by a hook is only used from the *next* request
  (reload race) → the guard must block once after writing it.
- The VS Code extension runs the same CLI and reads `~/.claude/settings.json`,
  but its own login check needs `claudeCode.disableLoginPrompt: true` (docs also
  suggest `ANTHROPIC_BASE_URL` in `claudeCode.environmentVariables`).
- `claudeCode.claudeProcessWrapper` side effects: conversations start in Manual
  permission mode; the "conversation open elsewhere" check is skipped.

Implementation:

1. `llm-credentials setup claude [--profile P] [--model M] [--remove]` edits
   `${CLAUDE_CONFIG_DIR:-~/.claude}/settings.json` (strict JSON, file lock,
   atomic write, keep permissions):
   - `apiKeyHelper` = `<sys.executable> -I -m llm_credential_mapping token --profile P`
     (quote with `shlex.join`; Windows `subprocess.list2cmdline`, reject
     `%!^&|<>"` in the interpreter path);
   - `env.ANTHROPIC_BASE_URL` = gateway; optional `model`;
   - `hooks.UserPromptSubmit` entry running `... claude guard --profile P`
     (replace our previous entry, keep others);
   - refuse to overwrite a foreign `apiKeyHelper`/`ANTHROPIC_BASE_URL` unless
     `--replace`; warn when `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` are set
     in the environment (they take precedence over the helper);
   - print the VS Code user setting `{"claudeCode.disableLoginPrompt": true}`.
2. `llm-credentials claude guard --profile P` (hook; reads hook JSON from stdin,
   uses its `cwd`):
   - tag present in `ANTHROPIC_CUSTOM_HEADERS` and not written by us → allow
     (user override, e.g. `claude --settings`);
   - tag written by us and still matching the resolver → allow;
   - otherwise resolve: found → write `.claude/settings.local.json`
     (`env.ANTHROPIC_CUSTOM_HEADERS` = other header lines + tag + metadata;
     keep other keys) at the checkout root (cwd on Windows / outside Git), add
     `.claude/settings.local.json` to `git rev-parse --git-path info/exclude`
     unless already ignored, then block once: "Customer X assigned (source);
     send the prompt again." Not found → block with `unmapped_error`.
   - `repo bind/unbind` also rewrite that file directly when it exists.
3. `run claude`: `claude --settings '<json>'` with `apiKeyHelper`,
   `env.ANTHROPIC_BASE_URL`, `env.ANTHROPIC_CUSTOM_HEADERS`; remove
   `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `CLAUDE_CODE_USE_BEDROCK|VERTEX|FOUNDRY`
   from the child environment. `--settings` outranks local settings, so
   `--customer` works as a one-off override.
4. Delete: `claude.py` wrapper code (`wrapper_main`, `wrapped_command`,
   `ClaudeContext`, `project_directory`, `verify_wrapper`, `helper_token`),
   `windows_process.py`, `editor_settings.py`, `workspaces.py`,
   `request_context.py` (if unused), the `llm-credentials-claude` entry point,
   their tests (`test_claude_adapter.py`, `test_claude_disable.py`,
   `test_editor_settings.py`, `test_workspaces.py`, `test_windows_process.py`,
   rewrite `test_claude_native.py`).
5. Live test with the installed `claude` against a local fake gateway
   (127.0.0.1 HTTP server recording headers): tag from a subdirectory, guard
   blocks unmapped repo with zero gateway requests, 401 → helper re-run,
   whether blocking once after writing is really needed for `UserPromptSubmit`.

## Step 4: Codex and OpenCode without wrappers

Codex (verified in source, `openai/codex` rust-v0.160.1 and main):

- `[model_providers.<id>.auth]` with `command`, `args`, `timeout_ms` (default
  5000), `refresh_interval_ms` (default 300000), `cwd`. Re-run lazily after the
  interval and once after a 401. Mutually exclusive with `env_key`,
  `experimental_bearer_token`, `requires_openai_auth = true`. Inherits Codex's
  environment. Killed on timeout (`kill_on_drop`) → use `timeout_ms = 30000`.
- `env_http_headers = { "x-litellm-tags" = "VAR" }` – header silently omitted
  when the variable is unset or empty.
- Project `.codex/config.toml` cannot set `model_provider`/`model_providers`
  (denylist), so the per-repo tag must come from the environment.
- Codex passes `*TOKEN*`/`*KEY*` variables to tool shells by default → never put
  the token in Codex's environment.

Implementation:

1. `setup codex [--profile P] [--model M] [--remove]` writes
   `${CODEX_HOME:-~/.codex}/config.toml` with two marker-delimited managed
   blocks: top-level keys at the top (`model_provider = "llm-credentials"`,
   optional `model`) and tables at the end:
   ```toml
   [model_providers.llm-credentials]
   name = "LiteLLM (llm-credentials)"
   base_url = "<gateway>/v1"
   wire_api = "responses"
   env_http_headers = { "x-litellm-tags" = "LLM_CREDENTIALS_TAG", "x-litellm-spend-logs-metadata" = "LLM_CREDENTIALS_SPEND_METADATA" }

   [model_providers.llm-credentials.auth]
   command = "<abs python>"
   args = ["-I", "-m", "llm_credential_mapping", "token", "--profile", "P", "--require-tag-env"]
   timeout_ms = 30000
   refresh_interval_ms = 240000
   ```
   Refuse if the user already has a top-level `model_provider` outside the
   markers. Validate the result with `tomllib`.
2. `token --require-tag-env` fails (no token) when `LLM_CREDENTIALS_TAG` is
   missing → fail closed even though Codex would drop the header.
3. `llm-credentials shell-init bash|zsh|fish|pwsh [--profile P]` prints a hook
   that on directory change runs `repo show --env` and exports/unsets only the
   non-secret `LLM_CREDENTIALS_TAG` and `LLM_CREDENTIALS_SPEND_METADATA`
   (quote safely per shell; tags may contain `'`). These variables are *not*
   read by the resolver (`LLM_CREDENTIALS_CUSTOMER` is the explicit override).
4. `run codex`: same provider via `-c` overrides plus the two variables; no
   token in the environment.
5. OpenCode: global plugin (verified in source, `anomalyco/opencode`):
   - v1: plugin receives `{directory, worktree}`; hook `chat.headers` runs per
     LLM request and its headers are merged last (exact key `Authorization`).
   - v2.0.24+: default export `{id, setup|effect}`; session hook
     `model.request` can set headers and `baseURL`; context has `location`.
   - The plugin calls `llm-credentials token --json` (cache until shortly before
     expiry) and `repo show --json --directory <worktree>`; sends the bearer only
     to the profile gateway; refuses requests when unmapped.
   - `setup opencode` installs the plugin and a provider (OpenAI-compatible,
     `baseURL <gateway>/v1`, models from `/v1/models`). Check whether a plugin
     `config` hook can inject the provider so no JSON editing is needed.
   - Open question: header-case collision on v1 (`Authorization` vs
     provider-generated header) – test on the wire.
6. Live tests: install `@openai/codex` and `opencode-ai` with npm and run them
   against the fake gateway.

## Step 5: Copilot Chat via a small VS Code extension

Verified (VS Code source/docs, `microsoft/vscode`, `vscode-docs`):

- `LanguageModelChatProvider` API is stable since VS Code 1.104
  (`vscode.lm.registerLanguageModelChatProvider`,
  `contributes.languageModelChatProviders`, activation event generated).
- Extension models appear in Copilot Chat (ask/edit/agent) when they declare
  tool calling. Org policy "Bring Your Own Language Model Key" must allow it
  (docs disagree on the default). Inline suggestions, NES, embeddings and some
  agent-mode actions always use GitHub models (untagged).
- Custom Endpoint `apiKey` can only come from VS Code secret storage, so it
  cannot hold a renewing SSO token; the extension route avoids that.
- Base to fork: `BerriAI/litellm` `vscode-extension/` (MIT, ~730 lines TS,
  static virtual key, `/model_group/info` discovery, streaming, tools, images).

Implementation:

1. `vscode-extension/` (TypeScript, MIT notice from the fork). Settings:
   `llmCredentials.command` (path to `llm-credentials`), `llmCredentials.profile`.
2. Token: `llm-credentials token --json` (cache until 60 s before expiry,
   retry once on 401). Customer: `repo show --json --directory <workspace folder>`
   per window; multi-root: use the folder of the active editor, auto-select if
   all folders resolve to the same customer, else status-bar choice.
3. Requests: `POST <gateway>/v1/chat/completions` with bearer, `x-litellm-tags`,
   `x-litellm-spend-logs-metadata`. Models from the gateway (check that session
   tokens work on `/model_group/info` or `/v1/models`).
4. Status bar shows the customer; notification "Sign in" runs
   `llm-credentials login` when the CLI reports re-login.
5. Optional command: set `claudeCode.disableLoginPrompt` for the Claude extension.
6. Build with npm/tsc, unit tests for conversion and resolution, `vsce package`
   for a VSIX, CI job. Pilot in real VS Code (cannot be tested headless).

## Step 6: cleanup and documentation

- Delete `copilot.py`, `copilot_bridge.py`, their tests and CLI commands.
- Rewrite README: one-screen quickstart (profile add → login → setup → done),
  then one short section per tool; move details to `docs/`.
- CI: keep the Python matrix, add the extension build; consider ruff.
- Final end-to-end review against the goals.

## Testing approach

- Unit tests: `python -m unittest discover -s tests -v`.
- Fake gateway: a 127.0.0.1 `http.server` that implements
  `/.well-known/litellm-cli-auth`, `/token`, `/revoke`, `/v1/messages`,
  `/v1/responses`, `/v1/chat/completions`, records request headers and returns
  minimal valid responses (see `tests/test_oauth.py` and `tests/test_parallel_customers.py`
  for existing fixtures). Profiles for it need `--allow-insecure-localhost`.
- Real gateway (only possible on your side): SSO login, tags and repository
  metadata in spend logs, renewal over a long session, refused grant → re-login.

## Follow-ups (not in steps 1–6)

- `login --no-browser`: accept the pasted redirect URL (state-checked) for
  SSH/devcontainers. LiteLLM also has a server-side "manual delivery" mode
  (source only; check whether the consent page exposes it).
- Opt-in `--storage file` (0600) for headless Linux/containers, or a token agent
  with socket forwarding.
- Ask the gateway admin to lower `LITELLM_CLI_JWT_EXPIRATION_HOURS` (default 24 h)
  once every tool renews automatically, and to run Redis for multi-worker setups.
- Optional gateway `async_pre_call_hook` that validates the tag against a
  per-user/team allowlist (tag budgets are checked before hooks, so validate the
  client tag rather than inject one).
- Revisit LiteLLM Projects (`docs/future/litellm-projects.md`).
