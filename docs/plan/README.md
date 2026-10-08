# Implementation plan: zero-touch customer tagging with LiteLLM SSO

This is the working plan for the redesign agreed on 2026-10-07. It is written so
the work can continue in a fresh session (local Claude Code or by hand) without
the original conversation. Start with [Current state](#current-state), then
continue at **step 2**.

On 2026-10-08 a new step 2 ([adapter seams](#step-2-adapter-seams-no-behavior-change))
was inserted; the former steps 2–6 are now steps 3–7.

Related files:

- The step 3 drafts (`docs/plan/drafts/`) were implemented and removed;
  see `git show 6c5991f:docs/plan/drafts/` if needed.
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
4. Stay open at both ends without extra complexity now: the credential side
   (today LiteLLM with SSO; later perhaps LiteLLM with fixed keys or another
   gateway) and the tool side are adapters. Adding one is a new module plus a
   registry entry, with no changes elsewhere. See [Adapters](#adapters-both-ends-are-replaceable).

Decisions made:

| Topic | Decision |
| --- | --- |
| Extensibility | Three adapter kinds: gateway, auth, integration (tool). Plain registries in code, no plugin loading. Only `litellm` + `litellm-sso` are implemented. |
| Attribution carrier | Chosen by the gateway adapter. For LiteLLM: request tag header `x-litellm-tags` plus `x-litellm-spend-logs-metadata` (Projects deferred, see `docs/future/litellm-projects.md`). The core only knows the customer. |
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
                       and sends the gateway's attribution headers for the resolved customer
                       (LiteLLM: x-litellm-tags + x-litellm-spend-logs-metadata)
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
- **Repository identity on every request**: the gateway adapter sends the
  customer's source and repository along with the customer. For LiteLLM:
  `x-litellm-spend-logs-metadata: {"customer_source":"rules","repository":"github.com/acme/foo"}`
  so spend can be re-attributed later (LiteLLM stores this JSON in spend logs;
  verified in `litellm/proxy/litellm_pre_call_utils.py` `_get_spend_logs_metadata_from_request_headers`).

## Adapters: both ends are replaceable

The CLI maps *who you are* (credential) and *which customer you work for*
(attribution) onto *the tools you use*. Both ends are adapters. The core in the
middle names neither LiteLLM nor any tool.

```text
input side   auth adapter       how a credential is obtained      litellm-sso (now); static-key (later)
             gateway adapter    endpoint URLs, attribution        litellm (now); other gateways (later)
                                headers, optional tag catalogue
                   |
core               v            profiles, keyring, customer resolver, CLI JSON contracts
                   |            (no vendor names, URL paths or header names)
                   v
output side  integration        writes tool config, launches      claude, codex, opencode (Python);
             adapter            tools, reads the CLI contracts     VS Code extension (TypeScript)
```

**Auth** and **gateway** are separate on purpose. LiteLLM SSO only works with a
LiteLLM gateway, but a fixed key (a LiteLLM virtual key or any bearer key) works
with every gateway. A profile names one of each, and an auth kind can restrict
which gateway kinds it accepts (`litellm-sso` → `litellm` only).

Interfaces (`src/llm_credential_mapping/adapters.py`, the only abstraction layer):

```python
class Api(StrEnum):            # what a tool speaks; a gateway maps each to a base URL
    ANTHROPIC_MESSAGES = "anthropic-messages"
    OPENAI_CHAT = "openai-chat"
    OPENAI_RESPONSES = "openai-responses"

@dataclass(frozen=True)
class Credential:
    secret: str = field(repr=False)
    expires_at: float | None   # None: does not expire (fixed key)
    user: str | None           # who the gateway sees, if known

class Auth(Protocol):          # one instance per profile, e.g. litellm-sso
    def login(self, notify: Callable[[str], None], **options) -> Credential: ...
    def logout(self) -> None: ...
    def status(self) -> dict: ...                 # nonsecret, includes "state"
    def credential(self) -> Credential: ...       # renews when needed, never prompts

class Gateway(Protocol):       # e.g. litellm
    def endpoint(self, api: Api) -> str: ...      # CredentialError if unsupported
    def attribution_headers(self, attribution: Attribution) -> dict[str, str]: ...
    def attribution_header_names(self) -> tuple[str, ...]: ...  # for static tool config (Codex)
    def visible_customers(self, credential: Credential) -> list[Tag] | None: ...  # None: no catalogue

class Integration(Protocol):   # e.g. claude; receives the profile, its gateway and the helper command
    def setup(self, context: IntegrationContext, *, model: str | None) -> None: ...  # user-level, reversible
    def remove(self) -> bool: ...
    def launch(self, context: IntegrationContext, attribution: Attribution, args: list[str]) -> int: ...

AUTH = {"litellm-sso": ...}               # each kind also validates its own profile settings
GATEWAYS = {"litellm": ...}
INTEGRATIONS = {"claude": ..., "codex": ..., "opencode": ...}

# Attribution and Tag are core types (customers.py). IntegrationContext bundles
# the profile, its Gateway instance and the `llm-credentials token` helper command.
```

The signatures are a starting point; adjust them while implementing step 2 as
long as the rules below hold.

Rules:

- Vendor knowledge lives only in `gateways/<kind>.py` and `auth/<kind>*.py`:
  URL paths (`/v1`, `/tag/list`), header names, team IDs, OAuth contracts.
  Tool config formats live only in `integrations/<tool>.py`.
- Core modules and integrations reach adapters only through the registries in
  `adapters.py`. `tests/test_architecture.py` checks this by parsing imports,
  and rejects vendor header names and URL paths (`x-litellm`, `/tag/list`,
  `"/v1"`) outside the adapter modules. User-facing text may still mention
  LiteLLM where an option is LiteLLM-specific.
- Out-of-process clients (Claude/Codex credential helpers, the OpenCode plugin,
  the VS Code extension) only use these CLI JSON contracts and never hard-code a
  header name or URL path:
  - `token --json` → `{"access_token", "expires_at" (number or null), "profile", "user" (or null)}`
  - `repo show --json` → `{"customer", "source", "detail", "repository", "headers": {...}}`
  - `profile show --json` → `{"name", "gateway", "auth", "endpoints": {api: url}}`
- An integration that needs an API the gateway does not offer fails with a
  clear message at setup and launch.

Not built (on purpose):

- no plugin discovery, entry points or config-defined adapters; a new adapter is
  code in this repository plus one registry line;
- no second implementation now. Tests use a fake gateway and a fake auth to
  prove the seams;
- no generic OAuth framework. Renewal, backoff and locking stay inside
  `litellm-sso` until a second auth kind actually needs refreshing.

What a future addition costs:

- **Fixed keys** (LiteLLM virtual key or any bearer gateway): `auth/static_key.py`,
  about 50 lines. `login` asks for the key with hidden input and stores it in the
  keyring, `credential()` returns it with `expires_at=None`, `logout` deletes it.
  No integration changes.
- **Another gateway**: `gateways/<kind>.py` with its endpoints and how a customer
  is expressed as headers, plus an `auth/` module if it has its own sign-in. No
  integration changes.
- **Another tool**: `integrations/<tool>.py`. No gateway or auth changes.

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

**Step 1 review fixed (2026-10-08):** all seven issues in
[`step1-review.md`](step1-review.md) are fixed with regression tests (280 tests
passing on Windows). Not done, optional: an overall per-request deadline in
`JsonTransport` and honoring `Retry-After`. Decided on 2026-10-08: the retry wait after a temporary failure
(fix 3) and the "renewal refused, keep using the token until it nearly expires"
state (fix 2) are stored as fields in the stored session itself, not in a
separate file. Login and a successful renewal replace the session and so clear
them. The schema change means existing test logins sign in again (greenfield).
A session whose renewal changed identity is a separate hard stop that releases
no token. The review covered these lenses:

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

**Step 2 done (2026-10-08), 298 tests passing on Windows.** Layout now:
`adapters.py` (interfaces), `registry.py` (the three tables plus
`validate_profile`, `auth_for`, `gateway_for`), `customers.py` (`Attribution`,
`Tag`, `validate_customer`), `auth/litellm_oauth.py` (was `litellm.py`),
`auth/litellm_sso.py` (was `sessions.py`, plus the `LiteLLMSSO` adapter and
`vault_account`), `gateways/litellm.py` (was `tags.py`), `integrations/`
(was `agents.py`). Differences from the sketch below:

- The registries live in `registry.py`, not `adapters.py`: the adapters import
  the interfaces, so one module would be an import cycle.
- `Integration` has `command()` and `launch()` only; `setup`/`remove` come with
  steps 4 and 5. `IntegrationContext` still carries a static `token` until those
  steps switch the tools to the credential helper.
- `Credential.notice` carries "sign in again soon" warnings, so the CLI prints
  them without knowing the auth kind.
- `Profile(name, gateway, auth_settings, gateway_type="litellm", auth_type="litellm-sso", allow_http=False)`;
  `profiles.json` and the session record stay at version 1 (no users yet).
- Legacy modules (`claude.py`, `copilot*.py`, `request_context.py`) still use
  `auth.litellm_sso.SessionManager` directly; the architecture test exempts
  them until steps 4 and 7 delete them. `README.md` is not updated yet (step 7).

**Step 3 done (2026-10-08), 320 tests passing on Windows.** Implemented as
described below, with these differences:

- The `.llm-customer` search stops *at* the home directory instead of only
  skipping it: folders above home (`C:\Users`, `/home`) are often shared, and a
  file there must not assign customers to everyone's projects. Folders outside
  home are searched up to the filesystem root.
- Rules documents list catalogue entries as `{"id": ..., "name": ...}`, not
  `{"tag": ...}`: the document is gateway-neutral.
- The rules location is validated in `profiles.py` (`validate_rules_location`),
  and `profile update NAME --rules X | --no-rules` keeps the vault account.
- `run` and `repo show` resolve in the current directory; `repo show --json`
  prints `customer`, `source`, `detail`, `repository` and `headers`.
- `repo bind --shared` overwrites an existing `.llm-customer` and warns when a
  personal or environment override still takes precedence there.
- Rule order decides (changed from "origin against all rules before
  upstream"): each rule is tried against origin, then upstream, before the
  next rule. Admins list specific rules before catch-alls, so a fork gets its
  upstream's specific rule even when a later catch-all matches its origin.

**Step 4 done (2026-10-08), 244 tests passing on Windows** (plus 3 live tests,
see below). `integrations/claude.py` now has `setup`/`remove`, the prompt
`guard`, the `.claude/settings.local.json` writer and `run claude` via
`--settings`. Deleted: the `claude.py` wrapper, `windows_process.py`,
`editor_settings.py`, `workspaces.py`, the `llm-credentials-claude` entry point
and their tests. `request_context.py` stays for the Copilot bridge (step 7).

- Commands: `setup claude [--profile P] [--model M] [--replace] [--remove]`
  (a generic `setup TOOL`; Codex/OpenCode answer "not available yet, use run"
  until step 5) and `claude guard [--profile P]` (the hook). Setup records what
  it wrote in `<home>/claude.json`; `--remove` deletes only values that are
  still the ones it wrote. The helper and hook commands include `--home`.
- `repo bind`/`unbind` rewrite `.claude/settings.local.json` files this tool
  wrote (checkout root and the given directory), so running sessions follow.
- Live test `tests/test_claude_native.py` (set
  `LLM_CREDENTIAL_MAPPING_TEST_CLAUDE_BINARY`; temporary `CLAUDE_CONFIG_DIR`,
  fake gateway, stub helper). Passed with the VS Code extension's bundled
  Claude Code 2.1.292 on Windows: unmapped repo blocked with zero gateway
  requests; assignment blocks once, then every request carries the tag and
  spend-log metadata; a 401 makes Claude call the helper again; subdirectory
  sessions are tagged; `run --customer` (`--settings`) passes the user-level
  guard with its own customer and writes no local file.
- Found by the live test: **Claude Code on Windows runs hooks through Git
  Bash**, which stripped the backslashes of the unquoted Windows command, so
  the hook failed and the prompt went out untagged (a failing hook is a
  non-blocking error). `shell_command` now double-quotes every argument with
  forward slashes on Windows (valid in cmd and bash) and refuses `% ! " $` and
  backticks.
- Remaining risk: if the hook command cannot start at all (e.g. the virtual
  environment moved), Claude treats that as a non-blocking error and sends the
  prompt untagged. Rerun `setup claude` after moving the installation; the
  gateway-side tag check (follow-ups) is the real safety net.
- Not verified yet (needs the real extension UI): that
  `claudeCode.disableLoginPrompt` plus the user-level helper is enough for the
  VS Code extension, and which folder it uses as the launch directory in
  multi-root workspaces. `README.md` still describes the removed wrapper
  (rewrite in step 7).

**Step 5 done (2026-10-08), 254 tests passing on Windows** (plus 6 live tests
in `tests/test_tools_native.py` with Codex 0.161.0 and OpenCode 1.18.34; set
`LLM_CREDENTIAL_MAPPING_TEST_CODEX_BINARY` / `..._OPENCODE_BINARY`).

- Codex: `setup codex` writes two marker-delimited blocks into
  `${CODEX_HOME:-~/.codex}/config.toml` (refuses another `model_provider`,
  keeps an existing `model`); `run codex` passes the same provider with `-c`
  and no token in the environment. Provider id is `llm-credentials` for all
  tools. **Verified on the wire: when the token command fails, Codex sends the
  request without any Authorization header** (not a hard stop); the gateway
  rejects it, so nothing untagged is billed, but the prompt text reaches the
  gateway. A gateway-side tag requirement remains the real safety net.
- `token --require-attribution-env`, `repo show --env SHELL` and
  `shell-init bash|zsh|fish|pwsh` (new `shells.py`) set only the nonsecret
  `LLM_CREDENTIALS_HEADER_*` variables per directory. Bash and Windows
  PowerShell hooks are tested in real shells; PowerShell reads multi-line
  functions correctly only from a script file (as a profile is).
- OpenCode v1: a generated plugin (`<home>/opencode/llm-credentials-PROFILE.js`)
  hooks `chat.headers`. Verified with 1.18.34: plugins load from a `file://`
  URL in `plugin`; the hook input has `model.providerID` and
  `provider.options.baseURL` (the plugin refuses other base URLs); setting
  `Authorization` replaces the provider's placeholder key cleanly; throwing in
  the hook stops the request before anything is sent; outside Git `worktree`
  is `/`, so the plugin resolves `directory`. `setup opencode` writes
  `${XDG_CONFIG_HOME:-~/.config}/opencode/opencode.json` (models from the
  gateway's OpenAI model list, or `--model`); `run opencode` uses the same
  plugin via `OPENCODE_CONFIG_CONTENT`, passing `--customer` as
  `LLM_CREDENTIALS_CUSTOMER` to the plugin's resolver.
- OpenCode v2 (different plugin API, not installed here): `setup` refuses;
  `run` keeps the previous launch-time token with fixed headers.
- Not covered: the Codex IDE extension (no per-repository environment).
- Test isolation: a test once ran `setup codex` against the real
  `~/.codex/config.toml` (created it; removed again, nothing else existed).
  Tests that run `setup` now use `tests/isolation.py`, which points
  `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and `XDG_CONFIG_HOME` at temporary folders.

**Step 6 done (2026-10-08).** `vscode-extension/` (TypeScript, written from
scratch rather than forked, so it contains no LiteLLM paths or headers; license
`UNLICENSED` like the repository). It registers vendor `llm-credentials` with
the stable `LanguageModelChatProvider` API (VS Code 1.104+) and uses only the
CLI's JSON contracts: `profile show --json` (the `openai-chat` endpoint),
`token --json` (cached until 60 s before expiry, at most 4 min; one retry
after a 401) and `repo show --json --directory FOLDER` (cached 30 s).

- Folder choice: the only folder; else the active editor's folder; else the
  folder chosen in the status bar; else, if all folders share a customer, that
  one; otherwise requests fail and the status bar asks for a choice.
- Notifications offer "Sign in" (a terminal running `llm-credentials login`)
  or "Assign customer" (a terminal running `repo bind --directory FOLDER`).
- Models from `<openai-chat endpoint>/models`; token limits and image input
  come from settings (not discovered). Requests stream
  `<endpoint>/chat/completions` with tools, tool results, `tool_choice:
  required` and (opt-in) images; redirects are refused.
- Tests: `npm test` (14 unit tests, Node's runner) and `npm run test:host`,
  which downloads VS Code (1.141.0 here) and runs a real extension host with a
  fake CLI and gateway: models listed, text streamed, tool calls returned as
  `LanguageModelToolCallPart`, credential and customer headers on the wire.
  CI runs both (with `xvfb-run`) and packages the VSIX.
- Not done: the optional command to set `claudeCode.disableLoginPrompt`
  (setup claude prints the setting). Still to verify by hand: the model picker
  and agent mode in real Copilot Chat, and the organization's "Bring Your Own
  Language Model Key" policy.

## Step 2: adapter seams (no behavior change)

Goal: put the [adapter interfaces](#adapters-both-ends-are-replaceable) in place
before the resolver and the tool setups are built on top of them. This is mostly
moving code. Do it after the step 1 fixes so the review's line references still
apply while fixing.

Today LiteLLM leaks into the core: `Profile.team_id` (`profiles.py`),
`TAG_HEADER` and `profile.gateway + "/v1"` in `agents.py`, `/tag/list` in
`tags.py`, LiteLLM header names in the step 3 draft `customers.py`.

1. **`adapters.py`**: `Api`, `Credential`, the `Auth`/`Gateway`/`Integration`
   protocols, the three registries.
2. **Core `customers.py`** (extended in step 3): `Attribution(customer, source,
   detail, repository)` and `validate_customer` (today's `validate_tag`: 1–256
   printable ASCII, no commas, no surrounding whitespace; safe for any header).
3. **`gateways/litellm.py`**: endpoints (`anthropic-messages` → root,
   `openai-chat`/`openai-responses` → root + `/v1`), `attribution_headers`
   (`x-litellm-tags`; `x-litellm-spend-logs-metadata` from step 3 on),
   `visible_customers` (today's `list_tags`, `/tag/list`).
4. **`auth/`**: `litellm.py` → `auth/litellm_oauth.py` (discovery, PKCE login,
   refresh, revoke); `sessions.py` → `auth/litellm_sso.py` (the `SessionManager`
   becomes the engine of the `litellm-sso` adapter). The team ID moves into the
   adapter's profile settings.
5. **Profiles** (`profiles.json` stays version 1: the tool has no users yet, so
   formats are changed in place without version bumps; existing local
   profiles are re-added and signed in again):
   ```json
   {"version": 1, "active": "consulting", "profiles": {"consulting": {
     "gateway": {"type": "litellm", "url": "https://llm.company.example", "allow_http": false},
     "auth": {"type": "litellm-sso", "team_id": "BUSINESS_UNIT_TEAM_ID"}}}}
   ```
   The vault account is the hash of the resolved configuration folder + name +
   gateway + auth, so changing any of them gives a separate vault entry.
   Including the folder (decided 2026-10-08) fixes a race: today the lock file
   lives in the configuration folder but the vault entry does not, so two
   folders (`--home`, `LLM_CREDENTIAL_MAPPING_HOME`) with the same profile share
   one sign-in without sharing a lock, and concurrent renewals can lose a
   rotated grant without revoking it. Cost: reaching the same folder through two
   different paths means signing in twice. Add a test with two folders and one
   vault. Step 3 adds `rules`.
6. **CLI**: `profile add NAME --gateway URL [--gateway-type litellm] [--auth litellm-sso] --team-id ID`
   (defaults shown; `--team-id` belongs to `litellm-sso` and is rejected for
   other auth kinds). New `profile show [NAME] [--json]`. `login`, `logout`,
   `status` and `token` go through `AUTH[...]`; `token --json` becomes the
   generic contract (drops `team_id`/`gateway`, adds `user`). `tags list` uses
   `visible_customers` and says so clearly when a gateway has no catalogue.
   Help texts stop naming LiteLLM except where an option is LiteLLM-specific.
7. **Integrations**: `agents.py` → `integrations/claude.py`, `codex.py`,
   `opencode.py`, implementing only `launch` for now via `gateway.endpoint()` and
   `gateway.attribution_headers()`. Setup comes in steps 4 and 5.
8. **Code that is deleted later** (Claude wrapper, workspaces, editor settings,
   Copilot bridge) gets only the import and field changes needed to keep its
   tests passing until steps 4 and 7. Do not port it to the interfaces.
9. **Tests**: move and adjust existing tests. Add `tests/test_architecture.py`
   (import and string guard, see Rules), and a fake gateway (different paths,
   header `x-test-customer`) with a fake fixed-key auth that run the three
   integrations' launch commands, showing that no LiteLLM assumption is left.
   All other behavior stays the same.

## Step 3: shared customer resolver

Files: `repositories.py` (rewrite from draft), `customers.py` (resolver from
draft), `profiles.py` (rules option), `gateways/litellm.py` (spend-log metadata
header), `cli.py`, tests.

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
      "customers": [{"id": "customer-acme", "name": "Acme"}],
      "rules": [{"match": "github.com/acme-*/**", "customer": "customer-acme"}]}
     ```
     `**` spans segments, `*`/`?` stay within a segment, full match, lowercase.
     First matching rule wins; each rule is tried against origin, then upstream.
   - URLs cached in `<home>/rules-cache/<sha256(url)>.json` for 1 h with ETag;
     on fetch failure use the stale cache; bearer token sent only when the
     URL has the gateway's origin.
   - The catalogue replaces the gateway's `visible_customers` in the picker
     when present (LiteLLM's `/tag/list` is empty for new users). Without
     either, the picker asks for an explicit customer.
4. **Resolver** `customers.resolve_customer(store, profile, directory, explicit=None)
   -> Attribution | None` with the step 2 `Attribution(customer, source, detail, repository)`.
   It stays gateway-neutral: headers come from
   `GATEWAYS[profile.gateway_type].attribution_headers(attribution)`. The
   LiteLLM adapter returns the tag plus the spend-log metadata JSON (compact,
   ASCII); the draft's `Attribution.headers()` moves there.
   `unmapped_error()` gives the actionable message.
5. **CLI**:
   - `repo show [--directory D] [--json]` → attribution plus the profile
     gateway's `headers`, or exit 1 with the unmapped message (used later by
     hooks, the OpenCode plugin and the VS Code extension, which therefore never
     need to know header names);
   - `repo bind [--customer TAG] [--shared] [--directory D] [--tag-prefix P]`;
   - `repo unbind`, `repo list` (local overrides only);
   - `run` uses the resolver; unmapped + TTY → picker, saves a local override.
6. **Tests**: identity normalization table (Azure forms, SSH alias via injected
   resolver, case, ports); file lookup (subfolder, parent folder, `$HOME`
   skipped, invalid file is an error); rules parsing/matching/order/upstream;
   cache (fresh, stale on outage, 304, bearer only same-origin); resolution order;
   LiteLLM attribution headers JSON (in the gateway adapter's tests).

## Step 4: Claude Code via native settings (CLI and VS Code extension)

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

Implementation (in `integrations/claude.py`; it uses only the gateway's
`anthropic-messages` endpoint, its attribution headers and the helper command,
so it works unchanged with another gateway or a fixed-key profile):

1. `llm-credentials setup claude [--profile P] [--model M] [--remove]` edits
   `${CLAUDE_CONFIG_DIR:-~/.claude}/settings.json` (strict JSON, file lock,
   atomic write, keep permissions):
   - `apiKeyHelper` = `<sys.executable> -I -m llm_credential_mapping token --profile P`
     (quote with `shlex.join`; Windows `subprocess.list2cmdline`, reject
     `%!^&|<>"` in the interpreter path);
   - `env.ANTHROPIC_BASE_URL` = `gateway.endpoint(Api.ANTHROPIC_MESSAGES)`; optional `model`;
   - `hooks.UserPromptSubmit` entry running `... claude guard --profile P`
     (replace our previous entry, keep others);
   - refuse to overwrite a foreign `apiKeyHelper`/`ANTHROPIC_BASE_URL` unless
     `--replace`; warn when `ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_API_KEY` are set
     in the environment (they take precedence over the helper);
   - print the VS Code user setting `{"claudeCode.disableLoginPrompt": true}`.
2. `llm-credentials claude guard --profile P` (hook; reads hook JSON from stdin,
   uses its `cwd`):
   - attribution headers present in `ANTHROPIC_CUSTOM_HEADERS` and not written
     by us → allow (user override, e.g. `claude --settings`);
   - headers written by us and still matching the resolver → allow;
   - otherwise resolve: found → write `.claude/settings.local.json`
     (`env.ANTHROPIC_CUSTOM_HEADERS` = other header lines + the gateway's attribution headers;
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

## Step 5: Codex and OpenCode without wrappers

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

Implementation (in `integrations/codex.py` and `integrations/opencode.py`;
header names and URLs come from the profile's gateway adapter, never literals):

Codex reads header values from environment variables named in its config, so
each attribution header gets a fixed variable name derived from the header
name: `LLM_CREDENTIALS_HEADER_` + the header name in upper case with every other
character replaced by `_` (`x-litellm-tags` → `LLM_CREDENTIALS_HEADER_X_LITELLM_TAGS`).
Setup and `shell-init` both derive the names, so they always agree.

1. `setup codex [--profile P] [--model M] [--remove]` writes
   `${CODEX_HOME:-~/.codex}/config.toml` with two marker-delimited managed
   blocks: top-level keys at the top (`model_provider = "llm-credentials"`,
   optional `model`) and tables at the end. Output for a LiteLLM profile:
   ```toml
   [model_providers.llm-credentials]
   name = "llm-credentials (P)"
   base_url = "<gateway.endpoint(openai-responses)>"
   wire_api = "responses"
   env_http_headers = { "x-litellm-tags" = "LLM_CREDENTIALS_HEADER_X_LITELLM_TAGS", "x-litellm-spend-logs-metadata" = "LLM_CREDENTIALS_HEADER_X_LITELLM_SPEND_LOGS_METADATA" }

   [model_providers.llm-credentials.auth]
   command = "<abs python>"
   args = ["-I", "-m", "llm_credential_mapping", "token", "--profile", "P", "--require-attribution-env"]
   timeout_ms = 30000
   refresh_interval_ms = 240000
   ```
   Refuse if the user already has a top-level `model_provider` outside the
   markers. Validate the result with `tomllib`. Rerun setup after changing the
   profile's gateway.
2. `token --require-attribution-env` fails (no token) when any of the
   gateway's header variables is missing → fail closed even though Codex would
   drop the header.
3. `llm-credentials shell-init bash|zsh|fish|pwsh [--profile P]` prints a hook
   that on directory change runs `repo show --env` and exports/unsets only the
   non-secret `LLM_CREDENTIALS_HEADER_*` variables (quote safely per shell;
   values may contain `'`). These variables are *not* read by the resolver
   (`LLM_CREDENTIALS_CUSTOMER` is the explicit override).
4. `run codex`: same provider via `-c` overrides plus the header variables; no
   token in the environment.
5. OpenCode: global plugin (verified in source, `anomalyco/opencode`):
   - v1: plugin receives `{directory, worktree}`; hook `chat.headers` runs per
     LLM request and its headers are merged last (exact key `Authorization`).
   - v2.0.24+: default export `{id, setup|effect}`; session hook
     `model.request` can set headers and `baseURL`; context has `location`.
   - The plugin calls `llm-credentials token --json` (cache until shortly before
     expiry; `expires_at: null` means no expiry) and
     `repo show --json --directory <worktree>`, and sends that output's
     `headers` as they are. It sends the bearer only to the profile's endpoint
     and refuses requests when unmapped.
   - `setup opencode` installs the plugin and a provider (OpenAI-compatible,
     `baseURL` = the `openai-chat` endpoint from `profile show --json`, models
     from `<that endpoint>/models`). Check whether a plugin `config` hook can
     inject the provider so no JSON editing is needed.
   - Open question: header-case collision on v1 (`Authorization` vs
     provider-generated header) – test on the wire.
6. Live tests: install `@openai/codex` and `opencode-ai` with npm and run them
   against the fake gateway.

## Step 6: Copilot Chat via a small VS Code extension

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
2. Token: `llm-credentials token --json` (cache until 60 s before expiry or
   indefinitely when `expires_at` is null, retry once on 401). Customer:
   `repo show --json --directory <workspace folder>` per window; multi-root:
   use the folder of the active editor, auto-select if all folders resolve to
   the same customer, else status-bar choice.
3. Requests: `POST <openai-chat endpoint>/chat/completions` (endpoint from
   `profile show --json`) with the bearer and the `headers` from `repo show`.
   The extension contains no gateway-specific header names or paths. Models
   from `<openai-chat endpoint>/models` (check that LiteLLM session tokens work
   there; drop the fork's LiteLLM-only `/model_group/info` discovery).
4. Status bar shows the customer; notification "Sign in" runs
   `llm-credentials login` when the CLI reports re-login.
5. Optional command: set `claudeCode.disableLoginPrompt` for the Claude extension.
6. Build with npm/tsc, unit tests for conversion and resolution, `vsce package`
   for a VSIX, CI job. Pilot in real VS Code (cannot be tested headless).

## Step 7: cleanup and documentation

- Delete `copilot.py`, `copilot_bridge.py`, their tests and CLI commands.
- Rewrite README: one-screen quickstart (profile add → login → setup → done),
  then one short section per tool; move details to `docs/`.
- `docs/adapters.md`: how to add a gateway, an auth kind or a tool, using the
  fake adapters from the step 2 tests as the worked example.
- CI: keep the Python matrix, add the extension build; consider ruff.
- Final end-to-end review against the goals.

## Testing approach

- Unit tests: `python -m unittest discover -s tests -v`.
- Fake gateway: a 127.0.0.1 `http.server` that implements
  `/.well-known/litellm-cli-auth`, `/token`, `/revoke`, `/v1/messages`,
  `/v1/responses`, `/v1/chat/completions`, records request headers and returns
  minimal valid responses (see `tests/test_oauth.py` and `tests/test_parallel_customers.py`
  for existing fixtures). Profiles for it need `--allow-insecure-localhost`.
- Fake adapters (from step 2): a non-LiteLLM gateway (other paths, header
  `x-test-customer`) and a fixed-key auth. Every integration is tested against
  them as well as against LiteLLM.
- Real gateway (only possible on your side): SSO login, tags and repository
  metadata in spend logs, renewal over a long session, refused grant → re-login.

## Follow-ups (not in steps 1–7)

- `auth/static_key.py` for LiteLLM virtual keys or other bearer gateways (see
  [Adapters](#adapters-both-ends-are-replaceable)), when there is a real need.
  Goal 3 still applies to the default setup.

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
