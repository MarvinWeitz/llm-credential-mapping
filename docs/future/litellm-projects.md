# LiteLLM Projects for customer attribution (deferred)

**Status:** deferred on 2026-10-07. The CLI keeps using request tags
(`x-litellm-tags`) for customer attribution. Revisit Projects when LiteLLM lets
SSO session credentials carry or select a project (see
[Revisit when](#revisit-when)).

Facts below were verified in LiteLLM source at commit
`10444df3a0173a99ab4e4858ce6777f31530e6c5` (2026-10-06). Upstream `main`
(`d364d5e`) had no relevant changes on 2026-10-07. Line numbers refer to
`10444df`. Enterprise paths are under
`enterprise/litellm_enterprise/proxy/management_endpoints/`.

## Why Projects fit

Our domain model is: a team (business unit, e.g. "BU CP") launches projects
(e.g. "smart"), and each project belongs to a customer. LiteLLM Projects model
exactly this: Org > Team > Project > Keys. `LiteLLM_ProjectTable` has
`team_id`, `project_alias`, `metadata`, `models`, budget, `spend` and `blocked`.

When a request uses a credential bound to a project, LiteLLM handles it on the
server:

- rejects blocked projects, applies the project model allowlist, enforces
  `max_budget` (plus soft-budget alerts) and per-model rpm/tpm limits stored in
  project metadata;
- merges project tags into the request tags. A project tag such as
  `customer:X` therefore gives per-customer spend via `/tag/daily/activity`;
- adds spend to the project counter and writes `user_api_key_project_id` and
  `user_api_key_project_alias` into each `LiteLLM_SpendLogs` row's metadata.

Attribution would come from the credential, not from a client-supplied
header. So it can neither be spoofed through a header nor silently dropped.

## Why it is deferred

### 1. SSO session tokens cannot carry or select a project

- `get_cli_jwt_auth_token` builds the session credential with `user_id` and
  `team_id` only (`litellm/proxy/auth/auth_checks.py:3810-3827`). Every SSO
  minting path passes only user and team (`proxy_api_credentials.py:41-77`,
  `gateway_endpoints.py:264-274`, `ui_sso.py:2571-2578`).
- The per-request grant refresh re-reads only user and team
  (`litellm/proxy/auth/user_api_key_auth.py:1452-1490`, called at `2051-2058`).
- The consent page lets the user pick a team only
  (`litellm/proxy/common_utils/html_forms/native_client_consent.py`).
- No request header, body or metadata field selects a project. The project
  id is always copied from the authenticated token
  (`litellm/proxy/litellm_pre_call_utils.py:1637-1638`). Caller-supplied
  `user_api_key_*` metadata is stripped (`2255-2279`).
- Project checks run only when the token has a `project_id`
  (`user_api_key_auth.py:2935-2948`). With a session token, project budgets,
  models, tags and spend are skipped silently. Requests are attributed to
  user and team only.

### 2. The only workaround means managing `sk-` keys

The workaround: the CLI uses the session token to call `POST /key/generate`
with `{team_id, project_id, duration}`, then sends traffic with the returned
short-lived virtual key. We rejected it for now because:

- **Bearer keys on developer machines.** The keys are independent of SSO.
  Logout or IdP deprovisioning does not revoke them before they expire.
  Removing the user from the team does delete their team keys.
- **Housekeeping.** LiteLLM never deletes expired keys. There is no per-user
  key limit, and `key_alias` is unique across all keys, expired ones included
  (`key_management_endpoints.py:7800-7830`). The CLI would have to reuse and
  clean up its keys.
- **Permissions must be granted.** By default, SSO users get the role
  `internal_user_viewer`, which cannot call `/key/generate`. By default,
  `team_member_permissions` allow only `/key/info` and `/key/health`
  (`litellm/proxy/management_helpers/team_member_permission_checks.py:18-23`).
  Admins would have to grant minting rights.
- **Cross-team gap.** Nothing checks that `project.team_id` equals the key's
  team, or that the caller belongs to the project's team. This applies both
  when the key is minted (`key_management_endpoints.py:1764-1812`, `2139-2146`)
  and when it is used (`auth_checks.py:742-786`). Personal keys (no
  `team_id`) skip the team-permission check. A `general_settings.custom_key_policy`
  hook would be needed. An upstream fix is open:
  [BerriAI/litellm#44165](https://github.com/BerriAI/litellm/pull/44165)
  ("keep keys and projects on the same team").
- **Non-expiring keys.** Sending `"duration": null` on `/key/update` makes a key
  never expire and bypasses `upperbound_key_generate_params.duration`.

### 3. Enterprise licence

Project create, update and delete, and project tags, are premium-gated
(`project_endpoints.py:485-498`, `686-690`, `889-893`). The docs mark Projects
as Beta.

### Other routes considered

- **JWT to virtual-key mapping (Enterprise).** The client sends the IdP JWT
  instead of the session token. One claim value maps to one admin-created key,
  and through it to one project. Only proxy admins manage mappings, so there
  is no per-repo choice. Enabling it disables the RFC 8693 token exchange.
- **Custom auth hook.** `general_settings.custom_auth` can return a
  `UserAPIKeyAuth` with `project_id`, which is then enforced
  (`user_api_key_auth.py:1590-1622`, `3972-3981`). But it bypasses the built-in
  session-token validation, so we would maintain auth code in the gateway.

## Revisit when

- LiteLLM lets session credentials be bound to a project. That could be a
  project picker on the consent page, a project-scoped session and refresh
  token, or a per-request project selector checked against team membership.
- Or, as a weaker trigger: #44165 merges and LiteLLM cleans up expired keys
  automatically. That would make the minting workaround acceptable.

To check quickly, search for `project` in:

- `gateway_dcr_flow.py`, `proxy_api_credentials.py`,
  `native_client_consent.py` and `get_cli_jwt_auth_token`;
- release notes;
- <https://docs.litellm.ai/docs/proxy/cli_sso> and
  <https://docs.litellm.ai/docs/proxy/project_management>.

## How the CLI would adopt Projects

- **Mapping lives in LiteLLM.** Admins put repositories in project metadata,
  e.g. `{"repositories": ["github.com/acme/smart"]}`. Proxy admins can always
  edit it. Team admins can only if `general_settings.team_admin_editable_team_fields`
  includes `"projects"`. `/project/update` replaces metadata wholesale, so
  tools must read, modify and write back the whole object.
- **Discovery with no developer interaction.**
  - `GET /project/list` with the session token returns the projects of every
    team the user belongs to, including metadata. Any role may call it, with
    no premium check (`project_endpoints.py:967-1088`).
  - The CLI matches the canonical Git origin against that metadata. Unlike
    `/tag/list`, this list is not empty for new users.
  - If a repo appears in several projects, fall back to a local override or a
    prompt.
- **Credentials.** The tool hooks return the credential for the current repo's
  project instead of the session token plus a tag header. Those hooks are
  Claude `apiKeyHelper` (runs in the session's working directory), Codex
  `model_providers.<id>.auth`, the OpenCode plugin and the VS Code extension.
- **Customer label.** Use a project tag `customer:X` (premium) or
  `project_alias`. Custom project metadata keys are *not* copied into spend
  logs.
- **Tags remain the fallback** for unmapped repos and folders outside Git.
- **Known reporting gaps today:**
  - no per-project daily table, and no project view on the Usage page;
  - `project.spend` resets with `budget_duration`;
  - project-wide rpm/tpm are not enforced, only per-model limits are;
  - `project.model_spend` appears never to be written (inferred);
  - with `enable_tag_filtering`, project tags also influence routing.

## Keep the tag implementation ready for this

- The customer resolver should return an attribution result (source, customer
  tag, repository identity) rather than a bare string, so a project can be
  added later.
- Canonical repository identity is needed by both approaches.
- Send the repository identity in `x-litellm-spend-logs-metadata` now, so
  historical spend can later be re-attributed to projects.
