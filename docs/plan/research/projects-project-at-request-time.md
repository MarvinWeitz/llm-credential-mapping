# project-at-request-time
## VERDICT
Projects work at request time, but only on requests made with a virtual key whose project_id column is set. The SSO session token has no project_id, and a request made with it is attributed to the user and team only (docs cli_sso.md L237, L354). So "Projects with SSO" means the client CLI uses its session token to call /key/generate with project_id (and team_id) and get a short-lived key tied to the project. No human creates or pastes that key, but the CLI holds it.

What LiteLLM does with that key on each request, with no licence check:
- it rejects the request if the project is blocked;
- it applies the project's model allowlist;
- it enforces the project's hard max_budget, and sends a soft-budget alert;
- it enforces per-model rpm/tpm/itpm/otpm limits stored in project metadata;
- it adds the project's tags to the request tags;
- it adds the cost to LiteLLM_ProjectTable.spend and to the project's spend counter;
- it writes user_api_key_project_id and user_api_key_project_alias into the metadata JSON of every LiteLLM_SpendLogs row.

What it does not do:
- It does not enforce the project-wide rpm_limit/tpm_limit (no code reads them; inferred).
- It does not put custom project metadata keys into spend logs. A key like {"customer":"X"} is dropped.
- It has no project column in SpendLogs and no daily per-project table.
- It has no project view on the usage page and no project filter on /spend/logs.
- It never fills project.model_spend (inferred).
- project.spend resets with the project's budget_duration, so it is not a historical total.

Enterprise licence: creating, updating and deleting projects needs one, and so does setting project tags. Request-time enforcement and spend tracking do not, except project guardrails and policies, which return 403 at request time without a licence.

Customer label: yes, two ways.
- Put the customer in project_alias (no licence needed; it lands on every spend-log row).
- Give the project a tag such as "customer:X" (setting it needs a licence). The tag is added to every request's tags, so spend can be reported per customer through /tag/daily/activity (no licence) and the Tag Usage page.
Do not use LiteLLM's own "Customer" (end_user) for this. It comes from the request, not from the project.

Attribution per request with a project key:
- employee: SpendLogs.user column;
- team: SpendLogs.team_id column, taken from the key's team_id;
- project: project_id and project_alias in the metadata JSON;
- customer: only through the project alias or a project tag.

Caveat: /key/generate does not check that the key's team_id matches the project's team (inferred), so the CLI must set team_id to the project's team.

Upstream main (d364d5e, 2026-10-07) changes none of the request-time project code.
## FINDINGS
- [confirmed-in-source] Project request-time checks (blocked, model allowlist, hard max_budget, soft-budget alert) run inside common_checks for any token whose project_id is set. There is no premium/licence check on this path.
  EVIDENCE: litellm/proxy/auth/auth_checks.py:742-783 (_run_project_checks: blocked L757-761, can_project_access_model L763-770, _project_max_budget_check L772-778, _project_soft_budget_check L780-785); called from common_checks L1168-1177; user_api_key_auth.py:3128 passes project_object into common_checks
- [confirmed-in-source] The project is loaded from the key's project_id. If the row is missing, all project checks are silently skipped (fail-open). The project row is cached, and edits invalidate the cache.
  EVIDENCE: user_api_key_auth.py:2600-2611 (key path), 3069-3071 (centralized prefetch), 3972-3981; auth_checks.py:6408-6453 get_project_object returns None when not found; auth_checks.py:754-755 early return; auth_checks.py:6456-6468 delete_cached_project_object. Note: project_endpoints.py:932-945 refuses to delete a project that still has keys
- [confirmed-in-source] The hard project budget uses a live spend counter (falling back to project.spend) and raises BudgetExceededError with entity_type PROJECT. The soft budget only sends an alert.
  EVIDENCE: auth_checks.py:6305-6360 (get_current_spend with project_spend_counter_key L6329-6333; raise L6356-6360); auth_checks.py:6363-6405 (soft budget, alert only); counter increment in proxy_server.py:3530-3545 (_prepare_project_spend_increment)
- [confirmed-in-source] The project model allowlist also narrows keys that hold all-team-models or all-proxy-models. /key/generate checks that the key's models are a subset of the project's and that the key's max_budget is <= the project's max_budget.
  EVIDENCE: auth_checks.py:5209-5221 and 4950-4971; key_management_endpoints.py:1764-1812 (_check_project_key_limits) called at 2139-2146
- [inferred] Project rate limits are enforced only per model, from project metadata keys model_rpm_limit, model_tpm_limit, model_itpm_limit and model_otpm_limit, by the default parallel_request_limiter (v3). No code reads the project-wide rpm_limit/tpm_limit stored in the project's budget table. /project/new only checks them against the team's limits.
  EVIDENCE: hooks/parallel_request_limiter_v3.py:3413-3441 (model_per_project descriptor), 3443-3480 (ITPM/OTPM), wired at 3903-3912; hooks/__init__.py:21 (v3 is the default parallel_request_limiter); project_endpoints.py:141-222 (_check_team_project_limits), 333-355 (_create_budget_for_project stores tpm/rpm in the budget table); grep for project rpm/tpm enforcement in parallel_request_limiter_v3.py found only the project_metadata lookups
- [confirmed-in-source] Project tags (project.metadata['tags']) are merged into the request's metadata.tags after request and team tags, on every request, with no premium check at request time. They then flow into request_tags.
  EVIDENCE: litellm_pre_call_utils.py:1782-1812 (add_team_and_project_level_controls; project_tags L1789, merge L1795-1801), call site L2363-2366, inherited_tags L2383-2386; litellm_logging.py (10444df):6358-6374 _get_request_tags; spend_tracking_utils.py:586-590 request_tags written to SpendLogs
- [confirmed-in-source] Only team metadata, not project metadata, contributes spend_logs_metadata and non-reserved metadata keys to the request. A custom project metadata key such as {"customer":"X"} never reaches LiteLLM_SpendLogs.
  EVIDENCE: litellm_pre_call_utils.py:1791-1794 (_merge_spend_logs_metadata uses team_metadata only); L2388-2393 adds only team_metadata to user_api_key_auth_metadata; spend_tracking_utils.py:239-241 filters spend-log metadata to SpendLogsMetadata keys only (_types.py:4213-4266)
- [confirmed-in-source] Every spend-log row carries the project's id and alias inside the metadata JSON. LiteLLM_SpendLogs has no dedicated project column. user and team_id are real columns.
  EVIDENCE: litellm_pre_call_utils.py:1637-1638 (user_api_key_project_id/alias set from UserAPIKeyAuth); _types.py:4228-4229 (in SpendLogsMetadata); spend_tracking_utils.py:795-805 (user=user_api_key_user_id, team_id, metadata=clean_metadata, request_tags, end_user); schema.prisma@10444df:704-747 (no project column; L722 comment 'project_id stored here'); types/utils.py@10444df:2844-2864 (StandardLoggingUserAPIKeyMetadata incl. project id/alias for callbacks)
- [inferred] Project spend is persisted by adding the cost to LiteLLM_ProjectTable.spend through the batched spend-update queue. Only 'spend' is incremented, so project.model_spend appears never to be written, even though the UI and docs display it.
  EVIDENCE: db_spend_update_writer.py:1355-1378 (_update_project_db), 1079-1089 (called per request), 2267-2283 (commit to litellm_projecttable), 2378-2381 (data={'spend': {'increment': ...}}); proxy_track_cost_callback.py:340-344, 396 (project_id from metadata); grep found no writer of project model_spend in litellm/ or enterprise/; UI ProjectDetailsPage.tsx@10444df:34-39 renders model_spend; docs project_management.md L226-229 shows a populated model_spend
- [confirmed-in-source] project.spend is reset together with the project's budget when the budget has a budget_duration, so it is a current-period figure, not historical reporting.
  EVIDENCE: common_utils/reset_budget_job.py:777-781 (projects fetched as budget-linked rows), 199-204 (project counter/cache keys reset)
- [confirmed-in-source] There is no per-project reporting surface beyond /project/info and /project/list (current spend). There is no daily project spend table, no /project/daily/activity, no project option on the Usage page, no project_id filter on /spend/logs/v2 or /spend/logs/ui, and no project grouping in /global/spend/report, which itself needs a licence. Historical per-project reporting needs SQL on SpendLogs.metadata->>'user_api_key_project_id' or a join of DailyUserSpend.api_key to the key's project_id.
  EVIDENCE: schema.prisma@10444df: daily tables at L849-1110, none for projects; spend_management_endpoints.py:2485-2585 (filters: api_key, user_id, request_id, session_id, team_id, key_alias, end_user, model…; search L2579-2585 has no project); spend_management_endpoints.py:1430-1433 (group_by team|customer|api_key) and 1500-1502 (licence gate); UI UsageViewSelect.tsx@10444df:7-16 (global, my-usage, organization, team, customer, tag, agent, user, user-agent-activity); project_endpoints.py:967-1036 (/project/info), 1038-1095 (/project/list). Docs claim 'You can query and filter logs by project ID' (project_management.md L102-111), but the API has no such parameter
- [confirmed-in-source] Tags give a working per-customer roll-up. Each request tag becomes a LiteLLM_DailyTagSpend row, readable through /tag/daily/activity (no licence gate) and the Tag Usage page (admins, or users with canViewTagUsage).
  EVIDENCE: db_spend_update_writer.py:2971-2999 (add_spend_log_transaction_to_daily_tag_transaction); schema.prisma@10444df:1069-1103 (LiteLLM_DailyTagSpend); tag_management_endpoints.py:725-780 (/tag/daily/activity, no premium check); UsageViewSelect.tsx@10444df:79-84,123-125
- [confirmed-in-source] Enterprise gating: project create, update and delete require premium_user, and so does setting project tags. /project/info and /project/list have no premium check but live in the enterprise package's router. At request time only project guardrails and policies are gated; with them set and no licence, every request with that key fails 403.
  EVIDENCE: project_endpoints.py (enterprise, identical to git show 10444df):486-500 (new: tags and project management premium), 678-692 (update), 889-895 (delete); enterprise/litellm_enterprise/proxy/management_endpoints/__init__.py:4,8; _types.py:5052-5062 ('tags','guardrails','policies' are premium metadata fields); litellm_pre_call_utils.py:3044-3048 and 3098-3102 (_premium_user_check for project guardrails/policies) -> utils.py:8326-8346 raises HTTP 403
- [confirmed-in-docs] The docs label Projects '[Beta]' and an Enterprise feature. The CLI SSO docs say session-token requests are attributed to user and team only, with no mention of projects.
  EVIDENCE: https://raw.githubusercontent.com/BerriAI/litellm-docs/main/docs/proxy/project_management.md L1-3, L31; https://raw.githubusercontent.com/BerriAI/litellm-docs/main/docs/proxy/cli_sso.md L237, L354
- [confirmed-in-source] A customer label lands on every spend-log row if it is encoded in project_alias, because user_api_key_project_alias is written per request. Setting the alias needs only project create/update, which is itself enterprise.
  EVIDENCE: litellm_pre_call_utils.py:1638; _types.py:4229; spend_tracking_utils.py:239-241; user_api_key_auth.py:2610-2611 (alias copied from the project row)
- [confirmed-in-source] LiteLLM's built-in 'Customer' (end_user, the Customer Usage page, /customer/daily/activity) comes from the request's user field or headers. The project does not set it, so it is not a server-side customer attribution.
  EVIDENCE: utils.py:10022-10049 (end_user_id from litellm_params/metadata user_api_key_end_user_id); spend_tracking_utils.py:553, 585, 805
- [inferred] /key/generate only checks the key's models and budget against the project. It does not check that the key's team_id equals the project's team_id, or that the caller belongs to the project's team. A key can therefore carry team A plus a project of team B, and spend logs would show that mismatch. Projects cannot be reassigned on /key/update, only detached.
  EVIDENCE: key_management_endpoints.py:1764-1812, 2139-2146 (no team comparison); grep for project/team comparisons in litellm/proxy found only management_helpers/auto_router_permissions.py:180; key_management_endpoints.py:3115-3118 (reassignment rejected)
- [confirmed-in-source] For a non-admin caller (e.g. a CLI session token), /key/generate forces the key's user_id to the caller. Employee attribution therefore survives on the minted key. The CLI-session-token branches require team_id when max_budget or budget_limits are set.
  EVIDENCE: key_management_endpoints.py:2096-2104 (auto-assign user_id), 1059-1065 and 1279-1292 (is_session_token branches)
- [confirmed-in-source] The JWT/OIDC auth path also loads the project when the resolved token (e.g. an auto-registered or mapped key) has project_id. This is separate from native CLI SSO session tokens, which carry no project_id.
  EVIDENCE: user_api_key_auth.py:1872-1883
## RISKS
- Project-wide rpm_limit/tpm_limit are accepted by /project/new and shown in the docs, but no request-time enforcement was found (inferred from source search). Only per-model limits in project metadata are enforced.
- project.model_spend appears never to be written (no writer found), so the UI's project 'Spend by Model' and the docs' /project/info example may stay empty.
- Without a licence a project cannot be created or edited through the API (premium gate), even though request-time enforcement on an existing project works without one. Seeding projects directly in the DB would bypass the supported path and may breach licence terms; not recommended.
- Customer labels sent by the client (request metadata.tags, key spend_logs_metadata or key metadata set by the developer's own CLI) can be spoofed. Only project_alias and project tags are admin-controlled. reject_clientside_metadata_tags blocks only request metadata.tags (auth_checks.py:803-818).
- If enable_tag_filtering (tag-based routing) is on, project tags such as 'customer:X' also take part in deployment selection (router_strategy/tag_based_routing.py@10444df:477-508), which could restrict or fail routing. Use distinct tag names or keep tag filtering off.
- No check ties the key's team to the project's team at /key/generate (inferred). The CLI must send team_id equal to project.team_id, or team attribution and project attribution can disagree.
- If a project row disappears or is not found, project checks fail open (auth_checks.py:754-755). Deleting a project that still has keys is blocked, but direct DB edits are not.
- Per-project historical reporting needs a direct SQL/BI query on LiteLLM_SpendLogs.metadata JSON (not indexed) or a join through keys. SpendLogs retention settings will limit history; DailyTagSpend persists aggregated rows.
- The minted project key is a real virtual key held by the CLI (keychain or file). Its lifetime and revocation depend on the duration the CLI requests at /key/generate, separate from the SSO refresh-token lifetime.
- The Q3 scope did not verify how key_generation_check gates non-admin team members creating team keys under different team settings, or the exact key duration limits for CLI session tokens.
## SETTINGS
- premium_user / LITELLM_LICENSE (project CRUD, project tags, project guardrails/policies, /global/spend/report)
- general_settings.enforce_project_model_quota (requires per-model rpm/tpm on project models)
- project metadata: model_rpm_limit, model_tpm_limit, model_itpm_limit, model_otpm_limit, tags, guardrails, policies
- project budget: max_budget, soft_budget, budget_duration (resets project.spend)
- general_settings.reject_clientside_metadata_tags
- litellm.disable_end_user_cost_tracking
- router enable_tag_filtering (tag-based routing interacts with project tags)
- custom_prometheus_metadata_labels (can expose user_api_key_project_alias as a Prometheus label, prometheus.py@10444df:4866-4890)
- LITELLM_CLI_JWT_EXPIRATION_HOURS (session-token lifetime, per cli_sso.md)
## UPSTREAM: Yes. Upstream main is d364d5e7 ('refactor: expose public hidden_params accessors (#44668)', committed 2026-10-07T00:01:33-07:00), 85 commits after 10444df (from git ls-remote plus a blobless clone in the scratchpad). For the files relied on here, the diff 10444df..main changes:
- project_endpoints.py: /project/info and /project/list visibility now also let team or org admins see projects (_can_view_team_projects, _visible_projects_where), plus docstring edits.
- schema.prisma: only adds the LiteLLM_LensReview and LiteLLM_LensDataset tables.
- auth_checks.py: the project lines touched are only a search-tool object_type change.
- user_api_key_auth.py, db_spend_update_writer.py, proxy_track_cost_callback.py and litellm_pre_call_utils.py: unrelated refactors (lock timeout retry, typing, comments).
- parallel_request_limiter_v3.py, key_management_endpoints.py, spend_management_endpoints.py and _types.py: no project-related lines changed.
None of these changes affect project behaviour at request time or in spend logs.
## REVIEW verdict_holds=partially
### CORRECTED VERDICT
The request-time and spend-log mechanics hold up against the source at 10444df. Upstream main is still d364d5e7 (confirmed with git ls-remote). What I would change is the framing, because it decides the user's question.

1) Without an Enterprise licence you cannot use Projects at all through the supported API. /project/new, /project/update and /project/delete all return 403 when premium_user is false (enterprise project_endpoints.py:494-498, 686-690, 889-893). No path outside the enterprise package creates a project row. So "request-time enforcement needs no licence" is true but does not help: with no licence, no project exists to enforce. The verdict's line "put the customer in project_alias (no licence needed)" contradicts the investigator's own finding. The alias is only copied onto each request without a licence. Setting it needs one. The docs also mark Projects as "[Beta]" and as an <EnterpriseFeature/> (project_management.md L1-3).

2) SSO is not the blocker. A CLI session token has no project_id, and requests made with it are attributed to the user and to the team picked on the consent page (cli_sso.md L237, L354, L383). To get project attribution while keeping SSO, the client CLI uses the session token to call /key/generate with team_id and project_id and gets back a key tied to the project. Nobody pastes or rotates that key by hand, but the CLI holds a real virtual key whose lifetime is whatever duration it asks for. I did not check whether team settings or key_generation_check stop a non-admin internal user from minting team keys this way.

3) With such a key, LiteLLM does the following on every request, with no licence check on the path:
- rejects the request if the project is blocked;
- applies the project's model allowlist;
- enforces the project's hard max_budget and sends a soft-budget alert (auth_checks.py:741-783, called at 1168-1176);
- enforces per-model rpm/tpm/itpm/otpm limits from project metadata (parallel_request_limiter_v3.py:3413-3480);
- merges project tags into the request's tags (litellm_pre_call_utils.py:1782-1812);
- adds the cost to LiteLLM_ProjectTable.spend through the spend queue (db_spend_update_writer.py:1355-1378, 1079-1083);
- writes user_api_key_project_id and user_api_key_project_alias into the metadata JSON of each SpendLogs row (litellm_pre_call_utils.py:1637-1638; spend_tracking_utils.py:239-241). Each row also has the user and team_id columns (spend_tracking_utils.py:795-796).

4) Gaps:
- Project-wide rpm_limit/tpm_limit are never read (inferred from grep).
- Custom project metadata keys such as {"customer":"X"} are dropped from spend logs.
- There is no per-project daily table, no project filter on /spend/logs and no project view on the Usage page. /key/list can filter keys by project_id (key_management_endpoints.py:6549).
- project.spend resets with the project's budget_duration.

5) Customer attribution: put the customer in project_alias, or give the project a tag such as customer:X, which rolls up through /tag/daily/activity. Either way the label lives on the project, so it needs the licence. Without a licence, the realistic route is the team (BU CP) plus tags or spend_logs_metadata set by the admin, or a key alias or metadata set by the CLI. Keys the developer sets can be spoofed.

6) Security gap that matters for this design: /key/generate checks only the key's models and max_budget against the project (key_management_endpoints.py:1764-1812, 2139-2146). It does not check that the key's team matches the project's team, or that the caller belongs to the project's team. Any user who can mint keys and knows a project_id can attach their spend to that project and draw down its budget (inferred from source).

Per request with a project key, you get: employee (SpendLogs.user), team (SpendLogs.team_id), project (id and alias in the metadata JSON) and customer (only through the project alias or a project tag). All of it depends on an Enterprise licence for the project to exist.
### CLAIM CHECKS
- [confirmed] Project request-time checks (blocked, model allowlist, hard budget, soft-budget alert) run in common_checks with no premium check
  NOTE: auth_checks.py:741-783 _run_project_checks; called at 1168-1176. No premium check on this path.
- [confirmed] The project is loaded from the key's project_id and project_metadata/alias are copied onto the token; a missing project fails open
  NOTE: user_api_key_auth.py:2600-2611; auth_checks.py:753-754 return early when project_object is None.
- [confirmed] The hard project budget uses the live spend counter with project.spend as fallback; the soft budget only alerts
  NOTE: auth_checks.py:6305-6335 (get_current_spend with project_spend_counter_key, fallback project.spend). budget_reservation.py:607-612 and 832-843 also add a project counter to budget reservation; the investigator did not mention this, but it is consistent.
- [confirmed] /key/generate checks key models are a subset of project models and key max_budget <= project max_budget
  NOTE: key_management_endpoints.py:1764-1812, called at 2139-2146. The all-team-models/all-proxy-models sentinels are exempt.
- [confirmed] Project-wide rpm_limit/tpm_limit are not enforced; only per-model limits in project metadata are
  NOTE: Grep for project budget_table rpm/tpm across litellm/proxy finds only max_budget (6321) and soft_budget (6377). The limiter reads only project_metadata model_*_limit (parallel_request_limiter_v3.py:3413-3480; batch_rate_limiter.py:303, 881-891). Still an inference from the absence of code.
- [confirmed] Project tags are merged into request tags with no premium check at request time
  NOTE: litellm_pre_call_utils.py:1782-1812. Project tags are merged after request tags and team tags.
- [confirmed] Only team metadata contributes spend_logs_metadata, so custom project metadata keys never reach SpendLogs
  NOTE: litellm_pre_call_utils.py:1792-1794 passes only team spend_logs_metadata. spend_tracking_utils.py:239-241 filters to SpendLogsMetadata keys.
- [confirmed] Every SpendLogs row carries project id/alias in the metadata JSON; user and team_id are columns
  NOTE: litellm_pre_call_utils.py:1637-1638; spend_tracking_utils.py:795-797. _STAMPED_METADATA_KEYS (153-159) does not remove the project keys.
- [partially] Project spend is incremented through the spend queue; model_spend is never written
  NOTE: _update_project_db (db_spend_update_writer.py:1355-1378) and its call at 1079-1083 are confirmed. I did not re-run the search for a model_spend writer, so that part stays inferred.
- [confirmed] project.spend resets with the project budget_duration
  NOTE: common_utils/reset_budget_job.py:199-204 resets the project counter and cache keys.
- [partially] There is no per-project reporting beyond /project/info and /project/list
  NOTE: There is no DailyProject table in schema.prisma@10444df, and the only project_ids parameter in spend_management_endpoints.py (1223-1266) is for OpenAI projects. The investigator missed that /key/list and the key-search endpoints can filter by project_id (key_management_endpoints.py:6549, 7049), which helps join daily key spend to projects.
- [confirmed] /tag/daily/activity has no licence gate
  NOTE: tag_management_endpoints.py:725-780 contains no premium check.
- [confirmed] Project create, update and delete need a licence, and so do project tags; project guardrails and policies return 403 at request time
  NOTE: Enterprise project_endpoints.py:486-498, 678-690, 889-893; litellm_pre_call_utils.py:3044-3048. No non-enterprise project creation path was found. Team admins can manage projects only via the 'projects' permission (team_admin_field_permissions.py:97-100), which still goes through the premium-gated endpoints.
- [confirmed] Docs mark Projects as Beta and Enterprise; CLI SSO docs attribute requests to user and team only
  NOTE: project_management.md L1-3 and L31; cli_sso.md L237, L354, L383 (the team is picked on the consent page and re-checked on each refresh).
- [partially] A customer label in project_alias lands on every row, 'no licence needed' (verdict wording)
  NOTE: Propagation is confirmed. 'No licence needed' is misleading: the alias can only be set through premium-gated project create or update, as the investigator's own finding says.
- [unverified] The built-in Customer (end_user) comes from the request, not the project
  NOTE: I did not re-open utils.py:10022-10049. SpendLogs end_user=end_user_id at spend_tracking_utils.py:805 is consistent with the claim.
- [confirmed] /key/generate does not check that the key's team matches the project's team
  NOTE: Every project_id reference in key_management_endpoints.py is listed; none compares team_id or caller membership. Update rejects reassigning to a different project (L3115).
- [partially] For a non-admin caller, /key/generate forces user_id to the caller
  NOTE: L2099-2100 sets user_id to the caller only when the request omits it. A different user_id is refused separately ('User can only create keys for themselves', L595-605), so the overall effect holds but the mechanism is two checks.
- [confirmed] Session-token branches require team_id when max_budget or budget_limits are set
  NOTE: key_management_endpoints.py:1059-1065 and 1278-1292. For session tokens with a team, the delegation ceiling falls back to team.max_budget (L1293-1298).
- [partially] Upstream main is d364d5e and changes no request-time project code
  NOTE: Head d364d5e7 is confirmed via git ls-remote. I did not re-diff the files.
### MISSED
- The verdict understates that Projects are an Enterprise feature end to end: without premium_user no project can be created (project_endpoints.py:494-498), so the 'no licence' request-time behaviour does not help an unlicensed deployment. For the user's question, the deciding constraint is the licence, not SSO.
- The verdict says customer-in-project_alias needs no licence. Setting the alias is premium-gated.
- Security: any user allowed to mint keys can attach an arbitrary project_id. There is no check of project-team or caller membership (key_management_endpoints.py:1764-1812), so project spend attribution and budget can be polluted or drained by users outside the project's team.
- /key/list and the key-search endpoints filter by project_id (key_management_endpoints.py:6549, 7049). This gives a supported way to map keys to a project, which can then be joined with daily per-key spend.
- Budget reservation (spend_tracking/budget_reservation.py:607-612, 832-843) also reserves against the project counter. This is extra enforcement the investigator did not cite.
- No one checked whether a non-admin internal user (the CLI session token's role) may call /key/generate for a team under default team settings, or what duration caps apply to the minted key. This is critical for the 'no manual keys' design and remains open.