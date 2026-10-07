AREA: customer-assignment

## CURRENT ASSESSMENT
- The customer is chosen per developer, per repo, and stored only on that machine. `run` resolves `--customer` first, then the local repositories.json binding. If neither exists it opens an interactive picker and saves the choice locally (cli.py:315-325, the bind is at cli.py:324). Bindings are keyed by sha256(gateway, repo) in a per-user file (repositories.py:86-146). Nothing is shared with the team and there is no central source.
  IMPACT: N developers times M repos means N*M manual decisions, and different developers can pick different tags for the same repo. Fails goal 1: the target is zero per-developer steps.
  FILES: src/llm_credential_mapping/cli.py:315-325; src/llm_credential_mapping/repositories.py:86-146
- The picker is fed by GET /tag/list (tags.py:197-224; cli.py:110-131). Confirmed in LiteLLM source: for internal_user roles, /tag/list is scoped to tags already used by that user's own keys. It returns [] when there are none (tag_management_endpoints.py:181-193 and 633). The list also contains automatic 'User-Agent: ...' tags. The README already hedges on this (README.md:152-153, cli.py:116).
  IMPACT: A new developer, which is exactly the case that needs the picker, gets an empty or misleading list and has to get the tag name some other way. The picker cannot be the main mechanism; it needs a central customer catalogue instead.
  FILES: src/llm_credential_mapping/tags.py:197-224; src/llm_credential_mapping/cli.py:110-131
- Background integrations cannot prompt. resolve_identity raises when there is no binding (request_context.py:375-380). The Claude extension wrapper fails with 'No fallback repository or customer was selected' (claude.py:331-365, README.md:501). Copilot setup needs a binding (copilot.py:227-228) and pins the checkout, which is 'not inferred from the active editor' (README.md:676). The picker also refuses to run without a TTY (cli.py:111-112).
  IMPACT: In VS Code (Claude extension, Copilot) every repo needs a prior `repo bind` in a terminal, so the integrations do not 'just work' (goal 2).
  FILES: src/llm_credential_mapping/request_context.py:375-380; src/llm_credential_mapping/claude.py:331-365; src/llm_credential_mapping/copilot.py:212-230
- Identity comes only from the `origin` remote (repositories.py:83), and only github.com paths are lowercased (repositories.py:48-49). I ran repository_identity() on sample remotes. Azure DevOps HTTPS gives `dev.azure.com/acme/Proj/_git/foo`, while SSH gives `ssh.dev.azure.com/v3/acme/Proj/foo` and the legacy host gives `acme.visualstudio.com/Proj/_git/foo`. GitLab and GHE case variants also differ. SSH host aliases leak through (`git@github-work:acme/foo` gives `github-work/acme/foo`). Personal forks resolve to the fork, not the upstream.
  IMPACT: This is harmless for exact-match local bindings, since each developer binds their own clone. It blocks central pattern rules, which need one canonical identity per repo. Canonicalisation has to be fixed before central rules can work.
  FILES: src/llm_credential_mapping/repositories.py:13-51, 54-83
- Folders that are not a Git checkout get no customer. Parent or multi-root workspaces need an exact-path, non-recursive `claude workspace bind` per developer (workspaces.py:307-311; README.md:339), even when every child repo maps to the same customer. Local/file remotes and folders outside Git need `--customer` every time (repositories.py:36-37; README.md:179).
  IMPACT: Extra per-developer steps in VS Code. There is no way to declare a customer once for a whole folder of client repos, such as ~/src/acme/.
  FILES: src/llm_credential_mapping/workspaces.py:254-325
- Only the tag is sent to the gateway (agents.py:27-29, 119, 145). No repo identity is sent.
  IMPACT: Wrong or missing attribution cannot be fixed later at the gateway, so the client has to block instead of falling back to 'unassigned'. The gateway also cannot derive or check the customer itself.
  FILES: src/llm_credential_mapping/agents.py:15-30,119,145
- Trust: the tag is supplied by the client. Confirmed in LiteLLM source: caller-supplied x-litellm-tags are always merged, with no opt-out ('No allow_client_tags opt-in', litellm_pre_call_utils.py:1947-1996). The README acknowledges that mappings are not access controls (README.md:265-273).
  IMPACT: Any developer can bill any customer. Today the only possible enforcement point is gateway-side custom logic.
  FILES: README.md:257-273
- Parts worth keeping: strict tag validation (tags.py:172-186); no silent untagged fallback; customer pinned per process and per session (claude.py ClaudeContext); protection against concurrent first binds (repositories.py:140-144); a normalised host/path identity that never contacts the remote.
  IMPACT: These carry straight into a layered resolver. Only the source of the decision changes, not the pinning or injection.
  FILES: src/llm_credential_mapping/tags.py:172-186; src/llm_credential_mapping/repositories.py:140-144

## OPTIONS
### [reject/high] A. Status quo: per-developer local binding plus interactive /tag/list picker
 interaction: one-time per repo per developer
 providers: All five, but VS Code ones only after a terminal `repo bind`
 sso: True
 renewal: Not affected by this area
 how: On the first `run` in an unbound repo, the developer picks a tag from GET /tag/list. The choice is saved in local repositories.json, keyed by gateway and origin identity. VS Code and Copilot need a `repo bind` beforehand.
 pros: ['Already built and tested', 'Explicit, no guessing', 'Works offline once bound']
 cons: ['Every developer has to decide every repo', '/tag/list is empty for new internal users (scoped to tags they already used)', 'Background VS Code launches cannot prompt', 'Inconsistent choices across the team']
 sources: ['/home/user/llm-credential-mapping/src/llm_credential_mapping/cli.py:110-131,315-325', 'https://github.com/BerriAI/litellm/blob/10444df3a0173a99ab4e4858ce6777f31530e6c5/litellm/proxy/management_endpoints/tag_management_endpoints.py#L181-L193']
### [adopt/high] B. Committed per-repo or per-folder file (e.g. `.llm-customer`), found by nearest-ancestor lookup
 interaction: one-time per repo (shared by team)
 providers: All five, through the one resolver used by run, claudeProcessWrapper and the Copilot bridge
 sso: True
 renewal: N/A
 how: A tool-neutral file such as `.llm-customer` containing `customer = "customer-acme"` is committed once per repo, or placed in a parent folder like ~/src/acme/. The resolver walks up from the launch or project-config-root directory to the first such file, stopping at $HOME. The same resolver serves run, the Claude wrapper and the Copilot bridge. Monorepos can put a file in each customer subfolder. Folders outside Git and multi-root parent folders resolve when the parent holds a file. Forks inherit the file because it travels with the code.
 pros: ['One decision per repo for the whole team, versioned and auditable in Git history', 'Works offline, needs no forge API or credentials', 'Covers forks, non-git folders, parent and multi-root folders and monorepo subfolders', "A parent-folder file gives a 'one per customer folder' convention for repos you cannot commit to"]
 cons: ['Customer-owned repos may not accept a vendor file. Fall back to a parent-folder file or central rules', 'Anyone with write access can change it, so it is spoofable', 'It is one more file to keep in sync if a repo changes customer']
 sources: ['Design proposal (inferred); replaces lookups in /home/user/llm-credential-mapping/src/llm_credential_mapping/repositories.py:54-83 and workspaces.py']
### [adopt/high] C. Central pattern rules (org/group/path glob to customer), fetched and cached by the CLI
 interaction: none
 providers: All five
 sso: True
 renewal: N/A
 how: Admins maintain a versioned JSON document, for example {catalogue:[{tag,name}], rules:[{match:'github.com/acme-*/**',customer:'customer-acme'},{match:'dev.azure.com/contoso/*/**',customer:'customer-contoso'}], unmapped:'prompt|unassigned|block'}. It is served from a static URL next to the gateway (reverse-proxy path), a raw file in an internal config repo, or a team metadata key read through /team/info. The CLI fetches it with the SSO bearer, caches it with ETag and uses the cache when offline. The catalogue replaces /tag/list for the picker. This first needs a canonical repo identity: Azure SSH, HTTPS and visualstudio.com forms collapsed to one; host and path lowercased for case-insensitive forges; SSH host aliases resolved via `ssh -G`; optional fallback to the `upstream` remote for forks.
 pros: ['No developer steps at all where repos follow an org-per-customer or group-per-customer layout, which is common for agencies and consultancies', 'New repos in a customer org are mapped automatically', 'One place to fix mistakes', 'Provides a real customer catalogue (fixes the empty-picker problem)']
 cons: ['Needs canonicalisation work and a hosting location (LiteLLM has no native repo-to-tag rules endpoint)', 'Does not help repos with inconsistent naming. Combine with B', 'Client-side, so still spoofable']
 sources: ['Design proposal (inferred)', 'Normalisation gaps verified by running repository_identity() from /home/user/llm-credential-mapping/src/llm_credential_mapping/repositories.py:13-51']
### [consider/medium] D. Live forge-metadata lookup from the CLI (GitHub custom properties or topics, GitLab topics)
 interaction: one-time per machine
 providers: All five, but GitHub-hosted repos only
 sso: True
 renewal: N/A
 how: The CLI calls GET /repos/{owner}/{repo}/properties/values (or /topics) using the developer's `gh` token or `git credential fill`, and reads a `customer` single_select property. On GitLab it would read project `topics`, because custom attributes are admin-only.
 pros: ['Uses metadata admins already manage; single_select with up to 200 allowed values and values_editable_by org_actors keeps it central', 'Any user with read access can read the values']
 cons: ['The CLI would handle GitHub tokens (needs `gh auth login`: one step per machine)', 'Network latency and rate limits (5,000/h authenticated, 60/h unauthenticated for public repos only)', 'Only works for repos in orgs you administer; customer-owned orgs will not carry your property', 'GitLab custom attributes need an instance administrator and only exist on Self-Managed/Dedicated; topics are free-form', 'Azure DevOps has no equivalent']
 sources: ['https://docs.github.com/en/rest/repos/custom-properties (text verified via https://github.com/github/rest-api-description)', 'https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api', 'https://docs.gitlab.com/api/custom_attributes/', 'https://docs.gitlab.com/api/projects/']
### [consider/medium] E. Forge metadata synced server-side into the central rules (feeds C)
 interaction: none
 providers: All five (via C)
 sso: True
 renewal: N/A
 how: A small job or GitHub App periodically calls GET /orgs/{org}/properties/values (members can read; one paginated call per org). It turns a required `customer` single_select property into exact-repo entries in the option C document. Optionally GitHub's external custom-property sync (preview) fills the property from a CRM. GitLab group paths and Azure DevOps project names go into C as patterns.
 pros: ['Repo admins set the customer in the GitHub UI; nobody edits JSON', 'Required properties with a default_value catch new repos', 'Developers need no GitHub tokens']
 cons: ['Extra service to run', 'Only for repos in your own orgs', 'Sync lag after a property changes']
 sources: ['https://docs.github.com/en/rest/orgs/custom-properties (verified via https://github.com/github/rest-api-description)', 'https://docs.github.com/en/organizations/managing-organization-settings/managing-custom-properties-for-repositories-in-your-organization']
### [adopt/medium] F. Gateway-side derivation and enforcement via a LiteLLM async_pre_call_hook
 interaction: none
 providers: All traffic through the gateway
 sso: True
 renewal: N/A
 how: The client always sends its canonical repo identity (e.g. header `x-llm-repo`) and the tag it resolved. A CustomLogger registered in config.yaml `litellm_settings.callbacks` implements async_pre_call_hook(user_api_key_dict, cache, data, call_type). It reads data['proxy_server_request']['headers'] together with user_api_key_dict.user_id and team_id, which are refreshed every request for SSO session tokens. It applies the same rules document as C and checks a per-user or per-team allowlist of customers. It then replaces any client customer-* tags in metadata.tags (or litellm_metadata, depending on route) with exactly one validated tag, or rejects the request. Caveat: tag max_budget checks run during auth (common_checks), before the hook. A tag added only by the hook is tracked but not budget-checked, so keep the client sending the tag and let the hook validate it rather than inject it.
 pros: ['The only option that adds server-side enforcement (who may bill which customer) while keeping SSO session tokens', 'Rules are fixed centrally without client releases', 'Can correct or normalise tags from all five tools, including Copilot via the bridge']
 cons: ['Custom Python in the gateway deployment', "The repo header is still client-asserted; the hook limits abuse to the user's allowed customers but cannot prove the repo", 'Hook-injected tags skip tag-budget pre-checks (inferred from call order)', 'Must handle the metadata vs litellm_metadata route difference']
 sources: ['https://github.com/BerriAI/litellm/blob/10444df3a0173a99ab4e4858ce6777f31530e6c5/litellm/integrations/custom_logger.py#L413-L422', 'https://github.com/BerriAI/litellm/blob/10444df3a0173a99ab4e4858ce6777f31530e6c5/litellm/proxy/common_request_processing.py#L2047-L2224', 'https://github.com/BerriAI/litellm/blob/10444df3a0173a99ab4e4858ce6777f31530e6c5/litellm/proxy/auth/user_api_key_auth.py#L3103-L3111', 'https://github.com/BerriAI/litellm/blob/10444df3a0173a99ab4e4858ce6777f31530e6c5/litellm/proxy/auth/user_api_key_auth.py#L1452-L1497', 'https://docs.litellm.ai/docs/proxy/call_hooks']
### [reject/high] G. LiteLLM Project entity
 interaction: one-time per repo per developer
 providers: Any, but only with virtual keys
 sso: False
 renewal: Static keys, so no renewal
 how: Projects (Org > Team > Project > Keys) carry models, budgets, metadata and tags. Keys are bound with key/generate project_id. Project tags are merged into requests from those keys.
 pros: ['Real per-customer budgets and model limits, enforced server-side']
 cons: ['Enterprise (premium) beta feature', 'Projects attach to virtual keys; the native CLI SSO credential carries only user_id and team_id (no project_id), so using Projects means per-customer sk- keys, which goal 3 rules out']
 sources: ['https://docs.litellm.ai/docs/proxy/project_management', 'https://github.com/BerriAI/litellm/blob/10444df3a0173a99ab4e4858ce6777f31530e6c5/enterprise/litellm_enterprise/proxy/management_endpoints/project_endpoints.py', 'https://github.com/BerriAI/litellm/blob/10444df3a0173a99ab4e4858ce6777f31530e6c5/litellm/proxy/_experimental/mcp_server/proxy_api_credentials.py#L41']
### [reject/high] H. Team or key default tags (metadata.tags)
 interaction: none
 providers: All
 sso: True
 renewal: N/A
 how: Set team.metadata.tags; LiteLLM merges team and project tags into every request from that team's credentials, including SSO session tokens whose team_metadata is reloaded each request.
 pros: ['Zero client work', 'Good for a business-unit tag']
 cons: ['Premium-gated', 'Additive, not a fallback: a business-unit team spans many customers', "Each tag on a request gets the full request spend in DailyTagSpend, so a team 'internal' default would double-count against the customer tag"]
 sources: ['https://github.com/BerriAI/litellm/blob/10444df3a0173a99ab4e4858ce6777f31530e6c5/litellm/proxy/litellm_pre_call_utils.py#L1782-L1806', 'https://github.com/BerriAI/litellm/blob/10444df3a0173a99ab4e4858ce6777f31530e6c5/litellm/proxy/_types.py#L5052-L5062', 'https://github.com/BerriAI/litellm/blob/10444df3a0173a99ab4e4858ce6777f31530e6c5/litellm/proxy/db/db_spend_update_writer.py#L2989-L2999']
### [reject/high] I. Tool-native committed configs (.claude/settings.json env, opencode.json headers)
 interaction: one-time per repo (shared by team)
 providers: Claude Code CLI and extension, OpenCode only
 sso: True
 renewal: N/A
 how: Commit `.claude/settings.json` with env.ANTHROPIC_CUSTOM_HEADERS='x-litellm-tags: customer-acme'. It applies after workspace trust, and `--settings` outranks it. Commit `opencode.json` with provider options.headers or `{file:./.llm-customer}`. Codex cannot do this because its project `.codex/config.toml` denylists model_provider and model_providers. Copilot custom endpoints live in the global chatLanguageModels.json, not the workspace.
 pros: ['Works even when a tool is launched without the wrapper (Claude Code, OpenCode)']
 cons: ['Fragmented per tool', 'Codex and Copilot not covered', 'Adds tool configs to customer repos', 'Claude waits for workspace trust', "Conflicts with the wrapper's own header injection. B gives the same benefit through one file"]
 sources: ['https://code.claude.com/docs/en/settings-reference#env', 'https://code.claude.com/docs/en/env-vars', 'https://github.com/openai/codex/blob/c0c230e6730b3b3c9101b8aff4b9aea4027cea5b/codex-rs/config/src/loader/mod.rs#L85-L102', 'https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/config.mdx']
### [adopt/high] J. Always send repo identity for after-the-fact attribution
 interaction: none
 providers: All five (Claude via ANTHROPIC_CUSTOM_HEADERS, Codex via env_http_headers, OpenCode via options.headers, Copilot via the bridge)
 sso: True
 renewal: N/A
 how: Every launcher and the bridge add `x-litellm-spend-logs-metadata: {"repo":"github.com/acme/foo","customer_source":"rule|file|override|fallback"}`. LiteLLM stores this JSON in the spend logs. This lets the policy for unmapped repos be 'tag unassigned and continue' instead of 'block', with finance reassigning later. Avoid `litellm_settings.extra_spend_tag_headers` for this: it creates one tag per repo and pollutes /tag/list.
 pros: ['Small change to agents.py and the bridge', 'Makes misattribution fixable', 'Required input for option F']
 cons: ['Repo names reach the gateway logs (usually fine internally)', 'Client-asserted']
 sources: ['https://github.com/BerriAI/litellm/blob/10444df3a0173a99ab4e4858ce6777f31530e6c5/litellm/proxy/litellm_pre_call_utils.py#L1311-L1320', 'https://github.com/BerriAI/litellm/blob/10444df3a0173a99ab4e4858ce6777f31530e6c5/litellm/litellm_core_utils/litellm_logging.py#L6336-L6355']
### [consider/medium] K. LiteLLM Customer (end-user) entity as the carrier instead of tags
 interaction: none
 providers: All five
 sso: True
 renewal: N/A
 how: Send `x-litellm-customer-id: acme`. It is read out of the box (STANDARD_CUSTOMER_ID_HEADERS) and maps to end_user_id. Customers get budgets and block/unblock via /customer/new, /customer/update and /customer/block, checked during auth.
 pros: ['Single-valued, so no double counting with User-Agent or team tags', 'Native per-customer budget and blocking']
 cons: ['Changes the carrier, not the identification problem', 'Still client-supplied', "Uses LiteLLM's 'end user' concept, so check how it shows in reports"]
 sources: ['https://github.com/BerriAI/litellm/blob/10444df3a0173a99ab4e4858ce6777f31530e6c5/litellm/constants.py#L1589-L1594', 'https://github.com/BerriAI/litellm/blob/10444df3a0173a99ab4e4858ce6777f31530e6c5/litellm/proxy/management_endpoints/customer_endpoints.py']
### [reject/medium] L. Per-directory shell environment (direnv or mise .envrc exporting the customer)
 interaction: one-time per repo per developer
 providers: CLIs only
 sso: True
 renewal: N/A
 how: A committed .envrc exports the customer variable and the tools read it from the environment.
 pros: ['No code']
 cons: ['Each developer must run `direnv allow` per repo', 'Does not reach VS Code extension hosts or the Copilot bridge']
 sources: ['Inferred']

## RECOMMENDATION
Replace "each developer picks a tag per repo" with one shared resolver, `resolve_customer(directory)`. `run`, the Claude wrapper and the Copilot bridge all call it, and it returns the customer plus the source of the decision. Resolution order, first match wins:

1. `--customer` or `LLM_CREDENTIALS_CUSTOMER`, for one session only.
2. The developer's explicit local override: the existing repositories.json entry, now written only by `repo bind` and never implicitly. The launch notice says "override".
3. The nearest committed `.llm-customer` file, walking up from the launch or project-config-root directory to $HOME. This covers repos, monorepo subfolders, folders outside Git and parent folders like ~/src/acme, and it travels with forks.
4. Central rules (option C), fetched and cached. Repos in your own GitHub orgs can be filled from GitHub custom properties by a sync job (option E). A rule marked `locked` beats the committed file, and any mismatch is warned about.
5. The unmapped policy from the central document:
   - in a terminal, a picker over the central catalogue (not /tag/list, which is empty for new users), offering to write a `.llm-customer` file or a local override;
   - in background VS Code launches, either fail with an actionable message or use `unassigned`.

In parallel:

- **Repo identity on every request (option J, now).** Send `x-litellm-spend-logs-metadata` carrying the repo identity and the decision source, so spend can be re-attributed later.
- **Canonical identity before any rules work.** Collapse Azure SSH, HTTPS and visualstudio.com forms; lowercase case-insensitive forges; resolve SSH host aliases; fall back to `upstream` for forks.
- **Multi-root workspaces.** Auto-select when every folder resolves to the same customer; require workspaces.json only when they differ.

Phase 2, if you need billing you can defend: a LiteLLM async_pre_call_hook (option F) that applies the same rules document and a per-user or per-team customer allowlist to the SSO identity. It validates or rewrites the client tag and rejects anything outside the allowlist. Keep the client sending the tag so tag budgets, which are checked during auth before hooks, still apply.

Do not use LiteLLM Projects (Enterprise beta, bound to virtual keys; SSO session credentials carry no project_id) or team default tags (additive and double-counting) for customer selection.

Expected outcome:

- **Org-per-customer repos:** no developer interaction.
- **Other repos:** one commit per repo, or one folder file per customer.
- **Local override:** stays as an escape hatch.

This fits the short-lived SSO model because nothing here touches credentials.

## OPEN QUESTIONS
- Do customer repos live in your own GitHub, GitLab or Azure orgs, or in customer-owned orgs? This decides whether central pattern rules or custom properties (C/E) can cover most repos, or whether committed or parent-folder files (B) must carry the load.
- Is committing a `.llm-customer` file into customer-owned repos acceptable, or must it live outside the repo (in a parent folder)?
- What should happen in an unmapped repo when launched from VS Code: block, tag `unassigned` (with repo metadata for later reassignment), or tag `internal`?
- Is hard enforcement needed (billing disputes, per-customer budgets per developer)? If so, phase-2 gateway hook work plus a per-user or per-team customer allowlist is required.
- Is the LiteLLM deployment Enterprise-licensed? This affects team, key and project tags and whether premium features are available at all.
- Does any single repo legitimately serve several customers (shared libraries, monorepos), so that a nearest-ancestor per-subfolder resolution would be needed?
- Where should the central rules document live: a reverse-proxy static path next to the gateway, an internal config repo, or LiteLLM team metadata (readable via /team/info by members; inferred, not verified)?

## VERIFY CLAIMS
- [confirmed] For internal_user roles, LiteLLM GET /tag/list returns only tags that already appear in DailyTagSpend for that user's own API keys, and returns [] if there are none, so a new developer's picker is empty.
  correction: Understated. For SSO session tokens the scope is worse than 'new users only'. UserAPIKeyAuth.check_api_key (_types.py ~L3517-3521) sets api_key/token to a hash of the bearer string. The CLI session blob is re-minted with a random token on each login or refresh (auth_checks.py get_cli_jwt_auth_token, ~L3805). Session tokens are not VerificationToken rows. So an SSO-only developer probably sees only tags used with the current access token, not tags from earlier tokens. This is inferred from source and not tested on a live proxy.
  evidence: tag_management_endpoints.py@10444df L158-193: _get_internal_user_api_keys = {caller's api_key} ∪ VerificationToken rows with user_id; _get_tag_list_scope applies {api_key: {in: ...}} for internal-user roles without admin view. L620-633: `if tag_scope is not None and not used_tag_names: return []`. Fetched from raw.githubusercontent.com/BerriAI/litellm/10444df.../litellm/proxy/management_endpoints/tag_management_endpoints.py.
- [confirmed] The native CLI SSO credential is built by get_cli_jwt_auth_token with user_id and team_id and no project_id. Team grants, including team_metadata, are re-read on every request by _refresh_session_token_grants.
  correction: 
  evidence: proxy_api_credentials.py@10444df L41-84: mint_proxy_credential calls get_cli_jwt_auth_token(user_info, team_id, team_alias, team_models, team_model_aliases), with no project parameter. auth_checks.py L3761-3826: the UserAPIKeyAuth has user_id, team_id, is_session_token=True and no project_id. user_api_key_auth.py L1452-1497: it merges team_grants() on each request when valid_token.is_session_token (called at L2051). team_grants.py L67/L110: team_metadata comes from the live team row.
- [partially] LiteLLM Projects are an Enterprise/premium beta feature, bound to virtual keys through project_id, and adding tags to projects is premium-gated.
  correction: I confirmed Enterprise-only and the premium gate on tags in source. I could not verify the 'beta' label: docs.litellm.ai is blocked from this sandbox, and the docs source path was not found in the repo.
  evidence: project_endpoints.py@10444df L486-498 raises 'Only premium users can add tags to projects' and 'Project management is an enterprise feature.' The SSO blob carries no project_id (see above). I found no per-request project header in litellm_pre_call_utils.py or user_api_key_auth.py.
- [confirmed] Key, team and project metadata.tags are merged additively into each request's tags, and setting tags in key or team metadata requires premium.
  correction: 
  evidence: litellm_pre_call_utils.py@10444df L1747-1752 (key tags via _merge_tags) and L1785-1806 (team then project tags merged onto request tags). _types.py L5052-5062: 'tags' is in LiteLLM_ManagementEndpoint_MetadataFields_Premium. key_management_endpoints.py L1429-1433: 'Only premium users can add tags to keys.'
- [confirmed] Every tag on a request receives the full request spend in DailyTagSpend, with no splitting, so extra default tags double-count.
  correction: 
  evidence: db_spend_update_writer.py@10444df L2989-2999: a loop over request_tags creates DailyTagSpendTransaction(tag=tag, **base_daily_transaction), carrying the full spend per tag. litellm_logging.py ~L6320-6333 also adds automatic 'User-Agent: ...' tags, which are counted the same way.
- [confirmed] Caller-supplied x-litellm-tags are always accepted with no opt-out, and they are merged before auth so tag budgets see them. async_pre_call_hook runs after auth and common_checks, so tags added only in the hook are not tag-budget pre-checked.
  correction: The consequence for hook-injected tags is inferred from call order. That ordering is explicit in the source comments, so the inference is strong.
  evidence: litellm_pre_call_utils.py@10444df L1947-1996 apply_client_tag_policy_pre_auth, with the comment 'No allow_client_tags opt-in: caller-supplied tags always flow into metadata.tags'. user_api_key_auth.py L3103-3115 calls it 'BEFORE common_checks' so that _tag_max_budget_check sees header tags. custom_logger.py L413-422: async_pre_call_hook(user_api_key_dict, cache, data, call_type) may raise, return a string to reject, or return modified data.
- [confirmed] LiteLLM stores JSON from an x-litellm-spend-logs-metadata header as spend_logs_metadata; extra_spend_tag_headers turns header values into 'header: value' spend tags; x-litellm-customer-id is read by default as the end-user id.
  correction: An addition for option K: auth_checks.py L2066-2101 resolve_and_validate_end_user_id passes unknown ids through by default. With litellm.validate_end_user_id_in_db=true, unknown ids are dropped, not rejected, unless a default end-user budget exists. So it filters typos but does not enforce.
  evidence: litellm_pre_call_utils.py@10444df L1311-1320 (safe_json_loads of x-litellm-spend-logs-metadata) and L1552-1554. litellm_logging.py L6336-6355 _get_extra_header_tags produces f'{header_name}: {header_value}'. constants.py L1591-1594 STANDARD_CUSTOMER_ID_HEADERS = ['x-litellm-customer-id', 'x-litellm-end-user-id']. auth_utils.py L1702-1705: standard headers take precedence over custom header mappings and over the body 'user' field.
- [confirmed] GitHub custom-properties API: per-repo values are readable by users with read access; org values by org members; single_select with up to 200 allowed values and values_editable_by org_actors or org_and_repo_actors. REST limits are 5,000/h authenticated and 60/h unauthenticated.
  correction: I checked the rate-limit numbers only indirectly. They match long-standing GitHub docs. I did not verify the 'external custom-property sync (preview)' mentioned in option E.
  evidence: github/rest-api-description api.github.com.json (main): GET /repos/{owner}/{repo}/properties/values: 'Users with read access to the repository can use this endpoint.' GET /orgs/{org}/properties/values: 'Organization members can read these properties.' custom-property schema: value_type enum [string, single_select, multi_select, true_false, url]; allowed_values 'up to 200'; values_editable_by enum [org_actors, org_and_repo_actors]. docs.github.com is blocked here. The github/docs source for rate limits exists, but the numbers sit in reusables I could not fetch.
- [confirmed] The GitLab Custom Attributes API requires an instance administrator and is available only on Self-Managed and Dedicated.
  correction: 
  evidence: gitlab.com/gitlab-org/gitlab/-/raw/master/doc/api/custom_attributes.md: 'Offering: GitLab Self-Managed, GitLab Dedicated'; 'Prerequisites: You must be an administrator for the instance.'
- [confirmed] Codex project-local .codex/config.toml cannot set model_provider or model_providers, and env_http_headers take header values from environment variables, so a per-repo customer for Codex must come from the launching environment.
  correction: 
  evidence: codex-rs/config/src/loader/mod.rs@c0c230e L85-102 and on current main L89-102: PROJECT_LOCAL_CONFIG_DENYLIST includes model_provider, model_providers, profile and profiles; keys are stripped at main L1156-1160. model-provider-info/src/lib.rs L172-179: env_http_headers maps a header to an env var name, and the header is omitted if the var is unset or empty.
- [confirmed] Claude Code shared .claude/settings.json can set env values such as ANTHROPIC_CUSTOM_HEADERS; they apply only after workspace trust; --settings outranks project settings; Claude Code sends no repository identity header of its own.
  correction: Claude Code has no inference 'headersHelper'. headersHelper exists only for plugin marketplace downloads. The only dynamic hooks for inference are apiKeyHelper, which supplies the credential only, and otelHeadersHelper, which is for OTel only. So a per-repo customer header for Claude still needs env or --settings injection (the wrapper) or a committed project file.
  evidence: code.claude.com/docs/en/settings-reference.md, 'When Claude Code applies env values': project and local env apply after you trust the workspace, or at startup in -p mode. code.claude.com/docs/en/env-vars, ANTHROPIC_CUSTOM_HEADERS: from project or local settings, a routing or credential header follows the env-apply rules. code.claude.com/docs/en/llm-gateway-protocol, request headers: Authorization/x-api-key, anthropic-version, anthropic-beta, x-claude-code-session-id, x-claude-code-agent-id, x-claude-code-parent-agent-id, plus opt-in hint headers (CLAUDE_CODE_GATEWAY_HINT_HEADERS=1). None of them carries repo or cwd identity.
- [confirmed] The current normaliser maps Azure DevOps HTTPS and SSH remotes of the same repo to different identities, keeps the case of GitLab and GHE paths, and keeps SSH host aliases.
  correction: 
  evidence: I ran repositories.py L13-51 verbatim. dev.azure.com/acme/Proj/_git/foo, ssh.dev.azure.com/v3/acme/Proj/foo and acme.visualstudio.com/Proj/_git/foo come out as three different identities. git@github-work:acme/foo.git gives github-work/acme/foo. https://GitLab.com/Group/Foo gives gitlab.com/Group/Foo, while git@gitlab.com:group/foo gives gitlab.com/group/foo. Only github.com paths are lowercased (L48-49).
- [partially] Current-assessment file:line citations (request_context.py:375-380, tags.py:197-224 and 172-186, workspaces.py:254-325 and 307-311).
  correction: The no-binding error in resolve_identity is at request_context.py:39-44. validate_tag is at tags.py:10-24, and list_tags (GET /tag/list) at tags.py:35-62. The exact-path, non-inheriting WorkspaceStore.get is at workspaces.py:83-87. The concurrent-bind conflict check is at repositories.py:139-144.
  evidence: In the working tree (branch claude/gifted-hawking-pday1i @9f7bf34), request_context.py has 50 lines, tags.py 62 and workspaces.py 101. The substance is correct, but those line numbers do not exist. cli.py:110-131 and 315-325, claude.py:345-359 and copilot.py:227-228 are accurate.
- [partially] Options B and C (and the recommended resolver) cover all five tools, including GitHub Copilot Chat via the bridge.
  correction: For Copilot, a better resolver only changes how the customer is picked at `copilot setup` time. A single bridge still serves every VS Code window with one customer. Per-window or per-repo attribution for Copilot needs a VS Code extension (see missed options) or one bridge port and custom-endpoint entry per customer. For the Claude extension (claudeProcessWrapper), Claude CLI, Codex and OpenCode, the claim holds.
  evidence: copilot.py:212-234: setup() resolves the customer once from a pinned checkout, and README.md:676 says it is 'not inferred from the active editor'. The Copilot Custom Endpoint model definition lives in the global chatLanguageModels.json in the VS Code user data directory, not the workspace (Coder AI Gateway VS Code client docs: https://coder.com/docs/ai-coder/ai-gateway/clients/vscode). Copilot's requests to the local bridge carry no workspace identity.

## OPTION FEASIBILITY
- [yes] A. Status quo: per-developer local binding plus interactive /tag/list picker: It works as described, but it fails goal 1, so reject is correct. The picker is even weaker than stated for SSO users: /tag/list scope keys on the hashed current bearer token plus the user's virtual keys (inferred from source).
- [yes] B. Committed per-repo or per-folder file (.llm-customer), nearest-ancestor lookup: It can be built entirely client-side: run, the claudeProcessWrapper (which knows the project dir, claude.py:345-347) and the OpenCode/Codex launchers can all call one resolver. It is one-time per repo (shared by team). Caveat: for Copilot it only applies when `copilot setup` runs, because the bridge is pinned to one checkout and Copilot requests carry no workspace identity. So 'all five' is overstated.
- [yes] C. Central pattern rules fetched and cached by the CLI: It is feasible and gives no developer interaction for org-patterned repos once identity canonicalisation is fixed (gaps confirmed empirically). LiteLLM has no native repo-to-tag rules store, so it needs a hosting location. I did not verify that internal users can read team metadata via /team/info, so a static file or reverse-proxy path is the safer choice. Same Copilot per-window caveat as B.
- [uncertain] D. Live forge-metadata lookup from the CLI (GitHub custom properties/topics): The API facts are confirmed: read access suffices, and single_select allows up to 200 values. But every developer needs a GitHub token (`gh auth login`) and it covers only repos in orgs you control. Option E gives the same data without tokens on developer machines, so 'consider' is generous.
- [yes] E. Forge metadata synced server-side into central rules: GET /orgs/{org}/properties/values is readable by org members (confirmed in the OpenAPI spec), so a sync job is straightforward. I could not verify the 'external sync (preview)' claim.
- [yes] F. Gateway-side derivation/enforcement via LiteLLM async_pre_call_hook: The hook signature and its reject-or-modify semantics are confirmed (custom_logger.py L413-422). Session-token user_id and team_id are available, and team grants are refreshed per request. Header tags are merged pre-auth, so tag budgets see client tags (user_api_key_auth.py L3103-3111). The hook should validate the client tag rather than inject one, as the researcher says. The repo header is still client-asserted.
- [yes] J. Always send repo identity via x-litellm-spend-logs-metadata: The header is parsed with safe_json_loads into spend_logs_metadata (litellm_pre_call_utils.py L1311-1320, L1552-1554). Every carrier can send it: Claude ANTHROPIC_CUSTOM_HEADERS, Codex env_http_headers, OpenCode options.headers and the bridge. JSON is printable ASCII, which ANTHROPIC_CUSTOM_HEADERS validation accepts. A team metadata spend_logs_metadata merge also exists.
- [yes] K. LiteLLM Customer (end-user) entity via x-litellm-customer-id: It is read by default and takes precedence over the body 'user' field (auth_utils.py L1702-1744). It is single-valued, so there is no double counting. Unknown ids pass through unless litellm.validate_end_user_id_in_db=true, and even then they are dropped silently, not rejected. Because it is still client-supplied, it does not solve identification.
- [yes] I. Tool-native committed configs (.claude/settings.json env, opencode.json headers): Rejecting it is reasonable. The facts are confirmed: Codex project config denylists model_providers, and Claude project env applies only after trust. OpenCode project config sits below OPENCODE_CONFIG_CONTENT in precedence, so the wrapper's inline config wins.
- [no] G. LiteLLM Project entity: Confirmed blocking detail: the SSO session blob has no project_id, and Projects are enterprise-only (project_endpoints.py L494-498).
- [no] H. Team or key default tags: Confirmed: tags merge additively and each tag gets the full spend (db_spend_update_writer.py L2989-2999), and the feature is premium-gated. A team spans customers, so it cannot select a customer.

## MISSED OPTIONS
- OpenCode global plugin with the `chat.headers` hook (one-time per machine, no wrapper needed). A plugin in ~/.config/opencode/plugins/ receives `directory` and `worktree` at init and can set output.headers per request, e.g. x-litellm-tags from the shared resolver and potentially a freshly refreshed Authorization header. This removes per-launch wrapping for OpenCode and fixes long-session token expiry there. Source: https://github.com/anomalyco/opencode/blob/dev/packages/plugin/src/index.ts ("chat.headers" hook, ~L257) and https://github.com/anomalyco/opencode/blob/dev/packages/web/src/content/docs/plugins.mdx (plugin context: directory, worktree).
- A small VS Code extension implementing the Language Model Chat Provider API (vscode.lm.registerLanguageModelChatProvider) instead of the Custom Endpoint plus local bridge, for GitHub Copilot Chat. The extension runs per window, so it knows that window's workspace folders and active editor. It can resolve the customer per window or repo and fetch the SSO token itself, which removes `copilot serve`, the manual endpoint registration and the single-checkout pinning. Caveat: Copilot Business/Enterprise admins can disable it through the 'Bring Your Own Language Model Key' policy. Source: https://github.com/microsoft/vscode-docs/blob/main/api/extension-guides/ai/language-model-chat-provider.md (DateApproved 9/30/2026).
- LiteLLM `litellm.validate_end_user_id_in_db` combined with option K, to drop unknown customer ids at the gateway: a cheap partial guard against typos and stale tags without a custom hook. Source: litellm/proxy/auth/auth_checks.py L2066-2101 at BerriAI/litellm@10444df3.