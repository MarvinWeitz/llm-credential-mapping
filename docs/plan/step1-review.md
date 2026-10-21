# Step 1 review findings

Adversarial review of commit `8533732` (three reviewers, one skeptical verifier per finding).
12 findings were confirmed by the verifier, 2 were rejected.
Several entries describe the same problem from different lenses. Distinct issues:

1. Validity is measured before the refresh call, so a slow failed refresh can hand out a token with < 60 s left or already expired (entries about "remaining" / "stale remaining").
2. A refused refresh (e.g. replay after a lost response) inside the early-renewal window discards an access token that is still valid for hours; keep returning it until near expiry while reporting re-login-required.
3. No back-off after a retryable failure: every call in the margin window makes its own blocking refresh attempt; record the last failure and wait before retrying.
4. Login can leave a live, unrevoked grant when interrupted (KeyboardInterrupt or other non-CredentialError) after the browser step; it also overwrites an unreadable previous session silently.
5. `_require_gateway_url` lets a `ValueError` from `urlsplit`/`port` escape.
6. An HTTP 3xx from the token endpoint marks the grant as refused although signing in again cannot help.
7. Missing tests: lock-free login swap/revoke ordering, scheme-downgrade pinning of stored endpoints.

Fix these before starting step 2. Each has a failure scenario, the suggested fix and the verifier's notes.

## 1. [medium] Transient-failure path checks a 'remaining' value taken before the network call, so it can hand out a token with under 60 s left or one that has already expired

- **Where:** `src/virtual_key_mapper/sessions.py:111` (review lens: renewal)
- **Scenario:** `remaining` is set at line 99, before `client.refresh()`. The 20 s transport timeout applies to each socket operation, not to the whole request. socket.create_connection tries every resolved address with its own 20 s timeout, and the TLS handshake and each read get 20 s too, so a failing refresh can take 40-80 s or more. Example: the gateway has an A and an AAAA record and the network is blackholed. The stored token has 70 s left, so 70 > 60 and the refresh starts. It fails as retryable after 40 s, and line 111 still compares the old 70 > 60, so a token with 30 s left is returned. Reproduced with a clock that advances during refresh: 70 s left before an attempt that takes 40 s gives a token with 30 s left. 61 s left before an attempt that takes 80 s gives a token that expired 19 s ago. This breaks the MINIMUM_VALIDITY_SECONDS promise ('never handed out'). Claude's apiKeyHelper or `token --json` consumers then cache an expired token. No test advances the clock during refresh.
- **Suggested fix:** Recompute after the failed attempt: `if session.expires_at - self.clock() > MINIMUM_VALIDITY_SECONDS: return session`. Add a test whose refresh side effect moves the clock forward before raising GatewayError(status=None).
- **Verifier's fix:** The suggested fix is right and is the minimal one. At sessions.py:111, replace the stale check with `if session.expires_at - self.clock() > MINIMUM_VALIDITY_SECONDS: return session`. Add a regression test: store a session with 70 s left, give it a refresh side effect that does `self.clock.now += 20` and then raises GatewayError("x", status=None), and assert that token() raises CredentialError and leaves the stored session unchanged.

Optional extra: give JsonTransport an overall deadline per request. The fix above is correct without it, but a deadline would also make the "two round trips at the transport timeout" assumption behind LOCK_WAIT_SECONDS=60 hold, so a slow-trickling gateway could not hold the profile lock for much longer than other processes wait for it.

<details><summary>Verifier reasoning</summary>

I could not refute it. The bug is real and I reproduced it.

The code: in /home/user/virtual-key-mapper/src/virtual_key_mapper/sessions.py, `remaining` is computed at line 99, before `client.refresh(session)` runs at line 104. The retryable-failure branch at line 111 (`if remaining > MINIMUM_VALIDITY_SECONDS: return session`) reuses that old value and never reads the clock again. The time spent inside refresh() is therefore never counted. That breaks the promise in the comment at line 13: "A token closer than this to expiry is never handed out, even when renewal is temporarily failing".

Reproduction 1 (scratchpad/repro_stale_remaining.py). I used the test suite's MemoryStore, Clock and make_session helpers with a refresh side effect that moves the clock forward and then raises GatewayError(status=None):
- 70 s left, refresh takes 20 s: a token with 50 s left is returned.
- 70 s left, refresh takes 40 s: a token with 30 s left is returned.
- 61 s left, refresh takes 80 s: a token that expired 19 s ago is returned.

Reproduction 2 (scratchpad/repro_timeout_total.py). Can one refresh really take longer than REQUEST_TIMEOUT_SECONDS? Yes. The timeout applies to each socket operation, not to the whole request. With the timeout scaled down to 1 s, a local server that sent a 503 status line one byte at a time made JsonTransport.request run for 5.1 s before raising a retryable GatewayError(503). Multiple resolved addresses, the TLS handshake and the reads each get their own timeout in the same way.

A single plain 20 s read timeout is already enough. Any token with 60-80 s left when the attempt starts comes back with under 60 s. To reach that band, renewal must have been failing since the margin opened (600 s or more before expiry for a 1 h token). A sustained gateway outage does exactly that, and that is the situation this code path exists for. The existing tests never move the clock during refresh: test_transient_failure_keeps_a_valid_token_and_retries_later and test_transient_failure_near_expiry_is_an_error_but_keeps_the_grant both use a fixed clock, so they cannot catch it.

Severity: low to medium. It only bites near the end of a long outage, but it can hand out an already-expired token to apiKeyHelper or to `token --json` consumers, which then cache it.

The suggested fix is correct: re-check the clock after the failed attempt with `if session.expires_at - self.clock() > MINIMUM_VALIDITY_SECONDS: return session`. Add the proposed test: a refresh side effect that moves the clock forward (for example from 70 s left by 20 s) and then raises GatewayError(status=None), asserting that CredentialError is raised and the stored grant is unchanged.

</details>

## 2. [medium] A refused refresh during the early-renewal window throws away an access token that is still valid for hours (6 h with 24 h tokens), and a lost refresh response makes this likely

- **Where:** `src/virtual_key_mapper/sessions.py:106` (review lens: renewal)
- **Scenario:** With a lifetime of 86400 s, refresh_margin is 21600, so renewal starts 6 h before expiry. Suppose LiteLLM commits a rotation but the response is lost: read timeout, RemoteDisconnected, or the helper is killed by its caller. That is GatewayError(status=None), which counts as retryable, so the old refresh token stays in the vault. The next apiKeyHelper or Codex call replays that spent refresh token and gets 400 invalid_grant. Lines 106-110 then set needs_login and raise. Every later call fails at line 92 even though the stored access token is valid for about 5.9 more hours. Reproduced: call 1 returns the token, and call 2 five minutes later raises 'The gateway refused the sign-in' with 5.92 h of validity left. Before this diff, refresh started only with less than 60 s left, so a refused grant cost at most about a minute. Now any 4xx from the token endpoint during the long early window, including one from a WAF or load balancer, stops all agents until a browser login. The class docstring says 'Renewal starts early so a failed attempt still leaves a usable token', and this path does not do that.
- **Suggested fix:** On a refusal (non-retryable GatewayError, or a CredentialError other than an identity or team mismatch), still persist needs_login so no further refresh is attempted. Keep returning the stored access token while `expires_at - clock() > MINIMUM_VALIDITY_SECONDS`, and report 're-login-required' through status() and stderr. Raise only once the token is near expiry. This needs the line 92 check moved after the validity check, or a separate 'refresh_refused' flag. Add a test for 'lost response, then 400 on replay' inside the margin.
- **Verifier's fix:** The suggested fix is right in direction. Refine it like this:

1. Keep persisting needs_login so no further refresh is attempted.

2. In session(), compute `remaining` first. Replace the unconditional raise at lines 94-98 with:
   `if session.needs_login: if remaining > MINIMUM_VALIDITY_SECONDS and not session.blocked: return session; raise CredentialError(...)`

3. In the non-retryable GatewayError branch (lines 106-110): after `_mark_needs_login`, return the stored session when `remaining > MINIMUM_VALIDITY_SECONDS`. Raise only when the token is near expiry. Have the CLI token path print a one-line 're-login required' warning to stderr. status() already reports re-login-required.

4. Keep failing immediately for the CredentialError branch (lines 116-120: identity or team changed on renewal, or an unusable response). Store a distinct hard-stop state there, for example a `blocked` field or a deleted session, rather than the same needs_login flag. A response that changed identity should not keep the old token flowing.

5. Optionally treat 403 the same way. It is how LiteLLM signals access_denied, for example membership removed, so a prompt stop is wanted there. 400 and 401 (spent or expired refresh token, which includes the lost-response replay) keep serving the valid token.

6. Add tests. Inside the margin: GatewayError(status=None), then GatewayError(status=400). Assert that the stored access token is still returned, that refresh is not called again, that status is 're-login-required', and that token() raises once remaining is 60 s or less.

7. Fix the line reference: the check to move is needs_login at lines 94-98, not line 92.

<details><summary>Verifier reasoning</summary>

I reproduced the finding by running the real SessionManager against a fake server that follows LiteLLM's semantics: the server claims the refresh token after minting, and a replayed token gets 400. The script is at /tmp/claude-0/-home-user-virtual-key-mapper/758eb7e3-cfad-5c2e-84d5-e72f0911dcc7/scratchpad/renewal/repro.py.

Setup: 24 h lifetime, clock just inside the 21600 s margin. The server commits the rotation, but the client sees a read timeout. The run printed:
- call 1: 'access-secret', 6.00 h remaining
- call 2, 5 minutes later: raised "Gateway returned HTTP 400. The gateway refused the sign-in; run 'vkm login --profile a'." with 5.92 h remaining
- status: re-login-required
- every later call raised "The sign-in for 'a' is no longer accepted"

How the code gets there:
- transport.py:61-74: a read timeout (TimeoutError, which is an OSError) or RemoteDisconnected (an HTTPException) becomes GatewayError with status None, so `retryable` is True.
- sessions.py:111-112: a retryable failure keeps the old refresh token and returns the old access token.
- Next call: LiteLLM has already claimed that refresh token, so the replay gets 400 invalid_grant. That is non-retryable, so lines 106-110 set needs_login and raise.
- From then on, lines 94-98 (not line 92 as the finding says; 92 is the expected_user check) raise before `remaining` is ever looked at.

The same thing happens when the caller kills the helper after the server has committed. It also happens on any 4xx from a WAF or load balancer, since 403 and 404 are classified as terminal.

The stored token really is still usable. docs/plan/research/auth.md:146-148 confirms the native access token is a stateless sealed credential: it cannot be revoked and expires on its own after CLI_JWT_EXPIRATION_HOURS. The same research notes that LiteLLM's own client (pkce_login.fresh_api_key) returns the still-valid key on any refresh failure.

It is a regression from this diff. Before, refresh started at less than 60 s remaining, so a dead grant cost at most about a minute of token. Now it costs up to min(max(600, L/4), L/2): 6 h for a 24 h token. That contradicts the class docstring ("Renewal starts early so a failed attempt still leaves a usable token") and research item (3). The only refusal test, test_refused_grant_requires_login_without_further_refreshes, uses an already-expired session, so the inside-the-margin case is untested.

Caveat: the grant really is dead after a lost response, so a re-login is needed eventually either way. The defect is that the diff forces it at once and throws away hours of valid token. It does not create a new need to log in. Severity is medium to low; the failure needs a lost response, a killed helper or an intermediary 4xx during the window.

</details>

## 3. [medium] No backoff after a retryable failure: every token() call in the margin window makes its own blocking refresh attempt under the profile lock, so callers fail with 'busy' even though a valid token exists

- **Where:** `src/virtual_key_mapper/sessions.py:88` (review lens: renewal)
- **Scenario:** Nothing records a failed attempt, so every call while remaining <= margin (up to 6 h with 24 h tokens) refreshes again while holding the lock. Throttling (429) and a DB outage (503) therefore get one token-endpoint request per call, and Retry-After is ignored. When the token endpoint hangs (slow DB, or connect timeouts across several addresses), each attempt takes 20 s or more. Concurrent callers (Claude helper, Codex auth command, the VS Code extension, or the Copilot bridge, which calls token() on every proxied request) queue behind each attempt, and each one then makes its own attempt. Reproduced at reduced scale (2 s refresh hang, 5 s lock wait, 4 threads, token valid for 600 s): 3 refresh attempts, one caller got 'Another credential operation is busy; try again.' and another waited 6 s. With the real values (20 s+ per attempt, 60 s wait) the 4th concurrent caller fails, although the stored token is valid. The same lock timeout hits login() after the browser step (line 63): the freshly issued grant is revoked and the user must sign in again. The LOCK_WAIT_SECONDS comment assumes two 20 s round trips, but the per-operation timeout does not bound a round trip.
- **Suggested fix:** Persist a 'next_refresh_attempt' (or 'last_refresh_failure') timestamp in the stored session, under the lock, on every retryable failure. Skip refresh and return the current session while `now < next_refresh_attempt` and the token has more than MINIMUM_VALIDITY_SECONDS left. Use exponential backoff starting around 30-60 s and honour Retry-After when present. Also consider a whole-request deadline in JsonTransport for refresh, or a fast path that returns the stored token, read without waiting, when another process holds the lock and the token is still above the minimum.
- **Verifier's fix:** The suggested fix is right in substance. These details matter:

1. Record the failure under the lock on each retryable GatewayError. Either store a `retry_after` timestamp on the Session (schema version 2, so no migration concern) or write it to a small non-secret per-profile state file next to the lock. The state file avoids rewriting the keyring secret on every failure. If that write itself fails, ignore it.
2. In session(), after taking the lock and re-loading, add a check before the refresh. If `remaining <= refresh_margin(session)`, `remaining > MINIMUM_VALIDITY_SECONDS` and `now < retry_after`, return the stored session without a network call. Waiters queued behind the first failed attempt then return immediately, and the queue collapses into one attempt per backoff period.
3. Back off exponentially from about 30-60 s, but cap it so the next attempt always falls before `expires_at - MINIMUM_VALIDITY_SECONDS - REQUEST_TIMEOUT_SECONDS`. At least one more attempt then happens before the hard floor. Below the floor, always attempt. Clear the state on a successful refresh and on login (a new Session replaces it naturally).
4. Retry-After is optional. Honouring it needs JsonTransport to put the header on GatewayError.
5. Add a test: three concurrent token() calls, with a client whose refresh sleeps and raises GatewayError(status=503 or None). Assert exactly one refresh attempt, that every caller gets the stored token, and that a call after the backoff expires tries again.

Do not add the suggested lock-free fast path that returns a token read without the lock, unless it is confirmed that LiteLLM does not invalidate the previous access token on rotation. Otherwise it could hand out a token that was just revoked. Backoff alone removes the cascade.

<details><summary>Verifier reasoning</summary>

I reproduced this against the committed code (8533732, the same content as step1.diff) using the real ProfileStore file lock and the default LOCK_WAIT_SECONDS of 60. The script is /tmp/claude-0/-home-user-virtual-key-mapper/758eb7e3-cfad-5c2e-84d5-e72f0911dcc7/scratchpad/backoff/repro.py.

Setup: a 24 h token issued 20 h ago, so 4 h remain and the refresh margin is 6 h. The refresh takes 20 s and then raises a retryable GatewayError(status=None). 20 s is the transport's per-operation timeout (REQUEST_TIMEOUT_SECONDS), so this is what a black-holed connect or a stalled read looks like. Five threads call token() at once. Result: 3 serial refresh attempts, at t=0, 20 s and 40 s. Two callers fail after 60 s with "Another credential operation is busy; try again." The others wait 20, 40 and 60 s, even though the stored token is valid for 4 more hours. A scaled-down run (2 s hang, 5 s wait, 4 threads) gives the result the finding quotes.

Why it happens: sessions.session() (lines 88-115) holds the profile lock for the whole refresh. A retryable failure returns the stored session without recording anything, so each caller waiting on the lock re-reads the same in-margin session and makes its own blocking attempt. The queue never collapses into one attempt. Even with one caller at a time, every call in the margin window (up to 6 h for 24 h tokens) pays the full hang for as long as it lasts. The Copilot bridge calls token() on every request (8 slots plus /health), and the Claude helper calls it every 60 s (CLAUDE_CODE_API_KEY_HELPER_TTL_MS). So once callers arrive faster than one per 20 s, waits pass 60 s and calls fail.

This was introduced by this diff. The old code refreshed only in the last 60 s and hard-failed after any failed attempt (refresh_pending), so it never retried. The new retry-on-every-call design has no rate limit. That defeats its own stated goal ("a failed attempt still leaves a usable token") and breaks the LOCK_WAIT_SECONDS comment, which assumes at most two round trips per wait.

Caveats on severity: fast failures (503, connection refused, 429) only add one quick round trip per call, which is acceptable apart from ignoring Retry-After. The serious case needs the token endpoint to fail slowly. If the whole gateway is black-holed, the following API calls fail anyway. The case that really hurts is a slow DB that stalls the token endpoint while API key auth is served from cache. The login() lock-timeout path is real but unlikely, because the browser flow has just exchanged a code at the same token endpoint. I would rate this low-to-medium, but it is a real, reproducible defect.

</details>

## 4. [medium] Login leaks a live, unrevoked grant if interrupted (or any non-CredentialError occurs) while waiting for the profile lock after minting

- **Where:** `src/virtual_key_mapper/sessions.py:69` (review lens: security)
- **Scenario:** This diff moved the lock wait to after `client.login()` has already minted the new grant, and raised the wait to 60 s (LOCK_WAIT_SECONDS). Example: the copilot bridge or an agent's apiKeyHelper holds the profile lock during a slow refresh (up to 2x20 s per-op timeouts plus vault I/O). The user finishes browser consent, the page says 'Return to your terminal', and the CLI then waits silently for up to 60 s. If the user presses Ctrl+C, KeyboardInterrupt goes straight through `except CredentialError:` at line 69. main() prints 'Cancelled.', and the new refresh grant is neither stored nor revoked, so it stays live on the gateway until it expires and logout can never reach it. I reproduced this by patching FileLock.acquire to raise KeyboardInterrupt: revoke was never called and the vault kept the old grant. A ValueError from `_load` (see the `_require_gateway_url` finding) takes the same path. Before this diff the lock was taken before any grant was minted, so an interrupt there leaked nothing.
- **Suggested fix:** Make the revoke-on-failure path in login() cover every exception: catch BaseException, revoke best-effort, then re-raise (keep the warning when revoke itself fails). Also notify('Saving sign-in; waiting for another credential operation...') before entering `self._locked(profile)` so users don't think it has hung. Add a test that raises KeyboardInterrupt from the lock acquire during login and asserts client.revoke(new_session) is called.
- **Verifier's fix:** In sessions.py `login()`, catch `BaseException` but revoke only a grant that was not saved. A bare `BaseException` handler would also revoke the session just saved if Ctrl+C arrived after `secrets.put` but before the lock was released. The vault would then hold a revoked grant while the previous grant was never revoked. Sketch:

    session = client.login(notify, **options)
    saved = False
    try:
        session.check_binding(profile)
        notify("Saving the sign-in (waiting for any other credential operation)...")
        with self._locked(profile):
            try:
                previous = self._load(profile)
            except CredentialError:
                previous = None
            self.secrets.put(profile.account, session.to_dict())
            saved = True
    except BaseException:
        if not saved:
            try:
                client.revoke(session)
            except CredentialError:
                notify("Warning: the unsaved grant could not be revoked; its access token expires naturally.")
        raise

Keep the existing previous-grant revoke after this block. Add a test in tests/test_sessions.py that has the lock acquire raise `KeyboardInterrupt`, for example `patch.object(self.profiles, 'locked', side_effect=KeyboardInterrupt)`. It should assert that `KeyboardInterrupt` propagates, that `client.revoke` is called once with the new session, and that the stored grant is unchanged. Optionally add a second test where `KeyboardInterrupt` is raised after `put` and assert the new session is not revoked.

<details><summary>Verifier reasoning</summary>

I reproduced it with a real lock held by another process, not by patching. Script: /tmp/claude-0/-home-user-virtual-key-mapper/758eb7e3-cfad-5c2e-84d5-e72f0911dcc7/scratchpad/verify_login_leak/repro.py.

The setup: a subprocess holds `<home>/<account>.lock` through `filelock`, standing in for a bridge or apiKeyHelper that is mid-refresh. The vault holds an old grant, and the mocked `client.login` returns a new grant. A SIGINT is sent 2 s into `SessionManager.login`. The wait was silent (no notify output), `KeyboardInterrupt` came out after 2.0 s, `client.revoke` was never called, and the vault still held `old-refresh`. So the new refresh grant is neither stored nor revoked, and `logout` can never reach it.

Why it happens, traced through the code:
- `ProfileStore.locked` (profiles.py:122-130) wraps `FileLock`. That only turns `Timeout` and `OSError` into `CredentialError`. Its polling sleep can be interrupted, so Ctrl+C raises `KeyboardInterrupt`.
- In sessions.py:61-74, the cleanup handler only catches `CredentialError`, so `KeyboardInterrupt` passes straight through it.
- `cli.main` (cli.py:444) prints "Cancelled." and returns 130.

The diff caused this. Before it (ee72c6b), `login()` took the profile lock before `client.login()` minted anything, so an interrupt while waiting for the lock leaked nothing. The diff moved the lock wait to after the mint and raised it from 10 s to 60 s. The browser has already shown "Login response received. Return to your terminal." while the CLI waits with no message, which makes Ctrl+C a likely reaction.

The severity is lower than "medium" suggests. The orphaned refresh token only ever existed in the process's memory and dies with it, so nothing is written down or disclosed. The harm is that a live, unreachable grant stays on the gateway until it expires. That goes against the module's own rule, "keep no unsaved grant alive" (sessions.py:124), and the existing revoke-on-failure path. It also needs lock contention at the same moment the user signs in. I'd rate it a real but low-severity hygiene regression.

The suggested fix is mostly right but slightly incomplete; see better_fix. I checked a monkeypatched version with the same repro (fixed.py and repro_fixed.py in the same folder): revoke was called once for the new session, the vault kept `old-refresh`, and the "Saving..." notice appeared before the wait.

</details>

## 5. [medium] No back-off after a retryable refresh failure: every token call in the early-renewal window retries the refresh while holding the profile lock

- **Where:** `src/virtual_key_mapper/sessions.py:99-112` (review lens: regressions)
- **Scenario:** Old code returned tokens straight from the vault until 60 s before expiry. New code calls client.refresh() on every token()/session() call while remaining < refresh_margin, which is 6 h for LiteLLM's default 24 h token, and it does so inside the profile lock. Example: the token endpoint hangs or is slow (blackholed route, slow DB). Every `token`, `claude token` or bridge request then blocks for at least REQUEST_TIMEOUT_SECONDS=20 (DNS time is not bounded by that), only to return a token that still has hours left. Concurrent callers queue on the lock and each runs its own 20 s attempt. With LOCK_WAIT_SECONDS=60, the 4th concurrent caller gets 'Another credential operation is busy' even though a valid token is stored. I reproduced this with scaled timings in scratchpad/storm.py (refresh takes 1 s and fails, lock wait 3 s, 6 concurrent callers): callers 1-4 waited 1/2/3/4 s and callers 5-6 failed as busy. docs/plan/research/auth.md item (2) asks to 'keep the stored refresh token and back off', but no back-off was added. test_transient_failure_keeps_a_valid_token_and_retries_later asserts 5 back-to-back network attempts, so the tests lock in this behaviour.
- **Suggested fix:** After a retryable failure, record a nonsecret next-attempt time, for example a `retry_after` field in the session dict or a sidecar file under the lock, with exponential back-off capped at remaining/4. Until then, return the current session without contacting the gateway while remaining > MINIMUM_VALIDITY_SECONDS. Alternatively or as well, use a short transport timeout (about 5 s) for optional early refreshes and keep the full timeout for refreshes near expiry. Add a test with several concurrent callers and a slow failing refresh: all callers should get the still-valid token quickly and refresh should be called once per back-off window.
- **Verifier's fix:** I agree with the main idea: persist a next-attempt time after a retryable failure. Some details to get right:

1. Where to store it. Record `next_refresh_at` under the profile lock, either as a field in the stored session dict or in a small non-secret sidecar file such as `<home>/<account>.refresh`. Use exponential back-off, for example 30 s doubling up to 15 min. Cap each delay so `next_refresh_at <= expires_at - max(2*MINIMUM_VALIDITY_SECONDS, remaining/4)`, so attempts become more frequent as expiry nears and one is always made before the 60 s floor. A field in the session dict is simplest because `_mark_needs_login` already writes the vault when a refresh fails. Clear it on a successful refresh and on login.

2. Check order in `session()`. When `remaining > MINIMUM_VALIDITY_SECONDS` and `now < next_refresh_at`, return the stored session without contacting the gateway. Callers queued behind the one slow attempt then return as soon as they get the lock, so nobody hits the 60 s lock timeout.

3. Shorter timeout for optional refreshes. While `remaining > refresh_margin/2`, call refresh with a short transport timeout (about 5 s). For example, pass a timeout into `JsonTransport.request` or `LiteLLMClient.refresh`. Keep the 20 s timeout for refreshes near expiry. This keeps the single blocking attempt per back-off window short.

4. Optional: carry the Retry-After header of a 429/503 in `GatewayError` and use it as the lower bound for `next_refresh_at`.

5. Tests:
- Replace the 5-back-to-back-attempts assertion. A retryable failure followed by a second call before `next_refresh_at` makes 0 extra refresh calls, and a call after it makes 1.
- The back-off never pushes the next attempt past the point where fewer than about 2 minutes would remain.
- A multi-process or threaded test with a slow failing refresh: every caller gets the still-valid token within about one timeout, none gets "busy", and refresh runs once per window.

<details><summary>Verifier reasoning</summary>

I could not refute this one. I reproduced it end to end and traced it in the code.

Code trace, src/virtual_key_mapper/sessions.py:99-112. On every call where `remaining <= refresh_margin(session)`, `session()` calls `client.refresh()` while holding the profile FileLock. For a 24 h token, `refresh_margin` is min(max(600, 6 h), 12 h) = 6 h. When the refresh fails with a retryable error, the code returns the stored session and records nothing. The next caller therefore makes the same network attempt again, also under the lock.

The old code (sessions.py at 8533732^) did no network work while `expires_at > now + 60`. It only refreshed once, in the last minute.

`JsonTransport` uses `REQUEST_TIMEOUT_SECONDS = 20` per socket operation. That covers connect and each read, but not the total time, and DNS lookups are not bounded by it at all. `LOCK_WAIT_SECONDS` is 60.

Repro: scratchpad/verify_backoff.py. It uses real processes, the real FileLock, and the real LiteLLMClient/JsonTransport against a loopback gateway that accepts connections but never answers. Timings are scaled with the same 1:3 ratio (timeout 1 s, lock wait 3 s). The token had 5 h of validity left.
- Three sequential callers each took about 1.1 s and each opened a new gateway connection (3 connections).
- With six concurrent callers, the ones that got the lock took 1.05, 2.05 and 3.12 s. The other three failed after 3.03 s with "Another credential operation is busy; try again.", even though the stored token had 5 h left.

The old code would have returned the stored token immediately to all of them.

Real-world callers that hit this:
- The Copilot bridge (a ThreadingHTTPServer) calls `token()` on every chat request (copilot_bridge.py:242) and on `/health` (copilot_bridge.py:149).
- The Claude helper (claude.py:198), the request context check (request_context.py:49) and the CLI token/launch paths also call it.

For up to 6 h, each of these calls pays one full timeout. Once several callers queue up, they get "busy" errors.

`test_transient_failure_keeps_a_valid_token_and_retries_later` asserts 5 back-to-back refresh attempts, so the tests currently lock this behaviour in. docs/plan/research/auth.md item (2) asks to "keep the stored refresh token and back off". (Item (3) says "the next call retries", so the docs are ambiguous on this.)

Mitigating factor: the token endpoint is on the same gateway as the LLM API. If the whole gateway is unreachable, LLM calls fail anyway, and the only change is a slower, misleading error. The defect matters when only the token endpoint is slow or throttled while the LLM path still works. Examples are slow DB writes in LiteLLM's token endpoint while key auth is cached, or a 429 from the token endpoint. In the 429 case, the current code sends one more refresh per bridge request to an endpoint that is already throttling, instead of backing off. Severity: medium to low, but the defect is real and new in this diff.

The suggested fix is right in direction. Notes on it are in better_fix.

</details>

## 6. [medium] A refused refresh inside the early window stops releasing an access token that is still valid for up to a quarter of its lifetime

- **Where:** `src/virtual_key_mapper/sessions.py:94-98,105-110,116-120` (review lens: regressions)
- **Scenario:** Early renewal starts at lifetime/4 before expiry (6 h for the default 24 h, non-revocable LiteLLM access token). Any non-retryable result sets needs_login, and session() then refuses at line 94 before looking at expiry. Plausible sequence: a refresh times out after LiteLLM minted the replacement and claimed the old refresh token. The same happens when Codex kills the helper at its timeout_ms mid-refresh, or the user presses Ctrl+C. The diff treats the timeout as retryable, so the next call replays the spent token and gets 400 invalid_grant, which sets needs_login. From then on every token call, Claude apiKeyHelper call and bridge request fails, and the user must do an interactive browser login, although the stored access token is valid for hours. Before this diff the refresh only ran in the last 60 s, so the same chain cost at most a minute. Reproduced via the CLI: a session with 10 min left plus a refresh returning HTTP 400 makes `token --json` exit 1, and later `token` calls refuse. Tests only cover refusals of already-expired sessions (test_refused_grant_requires_login_without_further_refreshes uses self.expired()), so this case is untested. _mark_needs_login failing to write (sessions.py:137-138) is also untested.
- **Suggested fix:** Separate 'stop refreshing' from 'stop releasing'. When needs_login is set or a refusal happens, return the current session while expires_at - now > MINIMUM_VALIDITY_SECONDS and do not attempt another refresh. Print a stderr warning or show status 're-login-required', and raise only when the token is near expiry. Keep the hard failure for an identity mismatch (expected_user, or the renewed grant changing user or team). If a membership refusal (403 access_denied) should stop release immediately, special-case it. Add tests for a refusal inside the margin with a valid token, and for _mark_needs_login when the vault write fails.
- **Verifier's fix:** The suggested fix is right. Concretely:

1. **Release the stored token while it is still valid.**
   - In session(), after the expected_user check, replace the unconditional raise at lines 94-98 with:
     `if session.needs_login: if remaining > MINIMUM_VALIDITY_SECONDS: return session; raise CredentialError(...login...)`.
   - Compute `remaining` before this check.
   - Do not attempt another refresh while needs_login is set.

2. **Handle a refusal without raising.**
   - In the non-retryable GatewayError branch (105-110) and the CredentialError branch (116-120): call _mark_needs_login(), then `return session` if remaining > MINIMUM_VALIDITY_SECONDS.
   - Raise only when the token is about to expire.

3. **Keep hard failures for identity problems.**
   - Keep the hard failure for the expected_user mismatch.
   - Keep it for a renewal that changes user or team. To tell that apart from a merely malformed response, have LiteLLMClient.refresh raise a dedicated subclass, e.g. `class IdentityChanged(CredentialError)`, and hard-fail only on that.
   - Malformed 200 responses are better kept with the session rather than marking it needs_login.

4. **Tell the user a new login is needed while the token still works.**
   - `status` already reports "re-login-required".
   - `token --json` can add "needs_login": true.
   - The CLI can print a stderr warning; stdout must stay the bare token for apiKeyHelper.
   - SessionManager has no notify, so return the flag and let callers warn.

5. **Add tests.**
   - (a) A 400 refusal with 5 h left on a 24 h session: the token is still returned, no second refresh is made, status is re-login-required, and the call raises once fewer than 60 s remain.
   - (b) The lost response then 400 replay sequence.
   - (c) An identity change still hard-fails.
   - (d) A _mark_needs_login write failure.

<details><summary>Verifier reasoning</summary>

I tried to refute this finding and could not. The mechanism traces cleanly through the code, I reproduced it, and it is a regression compared with the code before the diff.

**What the code does now (sessions.py)**
- With LiteLLM's default 24 h access token, refresh_margin is 21,600 s (the test at test_sessions.py:159 asserts this), so renewal starts 6 h before expiry.
- Lines 105-110: any non-retryable GatewayError marks the session needs_login and raises.
- Lines 116-120: any other CredentialError from refresh does the same. This includes a 200 response with invalid JSON, an oversized body or a missing field, not only an identity change.
- Lines 94-98: once needs_login is set, session() raises before it looks at expires_at.

**Reproduction** (scratchpad/verify_early_refusal/repro.py, using the repo's MemoryStore, Clock and make_session)
- 24 h session with 5 h left.
- Call 1: the refresh fails with no response (status=None). This stands for a timeout, or Codex killing the helper after LiteLLM had already minted the new pair and marked the old refresh token as used. The call correctly returns the current token.
- Call 2, 5 s later: the same refresh token is sent again and gets HTTP 400 (in LiteLLM, a reused refresh token gets 400 invalid_grant). The call raises "gateway refused".
- Every later call raises "no longer accepted" with 17,395 s, 16,795 s and 16,195 s of validity still left. refresh.call_count is 2 and status is re-login-required.
- claude.py:198, copilot_bridge.py:149/242 and request_context.py:49 call token() on every request, so running agent sessions break at once.

**Why it is a regression**
- Before the diff (ee72c6b sessions.py:63), refresh only ran when fewer than 60 s remained. The same lost response cost at most a minute of validity; now it can cost up to 6 h.
- The research note behind this design (docs/plan/research/auth.md:154) says a lost-response replay "is no worse than today's forced re-login". With early renewal that is no longer true.
- The class docstring says "Renewal starts early so a failed attempt still leaves a usable token". The refusal path throws that token away.

**Why withholding the token protects nothing**
- LiteLLM access tokens are stateless and cannot be revoked. See the docstring of revoke_refresh_token in gateway_dcr_flow.py, and cli_sso.md:245: "The key itself is not revocable".
- LiteLLM's own reference client, after a refused renewal, keeps using the key and prints the reason on stderr. It asks for `lite login --pkce` only "once the key has run out" (cli_sso.md:249). A user removed from the team keeps gateway access until the key expires, whatever this client does.

**Tests**
- test_refused_grant_requires_login_without_further_refreshes and test_unexpected_renewal_response_requires_login use self.expired(), so a refusal while the token is still valid is never tested.
- The swallowed write failure in _mark_needs_login (lines 137-138) is untested. That part is minor: the next call just repeats the refresh, gets the same refusal and reports it again.

</details>

## 7. [low] _require_gateway_url lets urlsplit ValueError escape; stored-session check now crashes and blocks re-login recovery

- **Where:** `src/virtual_key_mapper/litellm.py:148` (review lens: security)
- **Scenario:** `parsed = urlsplit(value)` sits outside the try block, and the handler only catches CredentialError. urlsplit raises ValueError for inputs like 'https://[example.com/token' ('Invalid IPv6 URL') or netlocs with NFKC-confusable characters ('https://example.com＃@evil.com/x'). This diff now runs this check on every stored session (check_binding inside _load). With such a stored token_endpoint/revocation_endpoint/resource (a tampered or hand-edited vault entry), token, status and logout all crash with a raw traceback instead of a CredentialError. login(), the documented recovery path, also hits the ValueError in `_load` under the lock. The ValueError gets past both `except CredentialError` clauses, so every login attempt mints a fresh grant, never stores or revokes it, and crashes again. I reproduced this: login raised ValueError, revoke was never called, and the vault was unchanged. A malicious or broken discovery document with such an endpoint also makes login crash with a traceback instead of a clean error.
- **Suggested fix:** Move urlsplit inside the try block and catch ValueError as well: `try: parsed = urlsplit(value); origin = gateway_origin(...) except (ValueError, CredentialError): raise CredentialError('Gateway endpoint is invalid.') from None`. Combined with the BaseException revoke in login(), a corrupted entry then counts as 'unreadable previous' and login can replace it. Add test cases for these inputs to test_stored_endpoints_must_stay_on_the_gateway and test_foreign_endpoints_are_rejected.
- **Verifier's fix:** In `_require_gateway_url`, wrap `parsed = urlsplit(value)` and the `gateway_origin(...)` call in one try, and raise `CredentialError("Gateway endpoint is invalid.")` on `(ValueError, CredentialError)`. Leave the origin, userinfo, query and fragment checks after the try unchanged. Add 'https://[example.com/token' and 'https://example.com＃@evil.com/x' to the stored-endpoint and discovery tests. Also add a test that login() replaces a stored session with such an endpoint and leaves the new grant in the vault.

<details><summary>Verifier reasoning</summary>

I reproduced this with Python 3.13.16 at /tmp/claude-0/-home-user-virtual-key-mapper/758eb7e3-cfad-5c2e-84d5-e72f0911dcc7/scratchpad/verify_urlsplit/repro.py, using the MemoryStore and make_session helpers from tests/test_sessions.py.

How the failure happens: in src/virtual_key_mapper/litellm.py:148, `_require_gateway_url` calls `urlsplit(value)` before the try block, and that block only catches CredentialError. This diff adds `Session.check_binding` (litellm.py:121-122), which runs this check on every stored endpoint in `SessionManager._load` (sessions.py:55).

Result: I stored a session whose token_endpoint was "https://[example.com/token" or "https://example.com＃@evil.com/x". Then token, status, logout and login all raised a raw ValueError ("Invalid IPv6 URL" or "...invalid characters under NFKC normalization"). cli.main (cli.py:441) catches only CredentialError and KeyboardInterrupt, so the user sees a traceback.

Login is affected in particular. Login is the documented way to recover, and test_relogin_replaces_an_unreadable_previous_session shows that an unreadable previous session is meant to be replaced. Here, `_load` raises ValueError inside the lock. That skips the inner `except CredentialError: previous = None` and the outer revoke-on-failure handler. So the freshly minted grant is neither stored nor revoked: revoke was called 0 times and the vault was unchanged. Every retry leaks another grant. Logout crashes too, so the only way out is to delete the keyring entry by hand.

Limits on the finding:
- The trigger needs a corrupted or hand-edited vault entry. The tool cannot write such a value itself, because Discovery.parse runs the same check during login. Severity "low" is right.
- The "malicious discovery document crashes login" part is not new. The old Discovery.parse code had the same `urlsplit` outside the try, and it caught ValueError only around gateway_origin, which never raises one. This diff only moved that code into the helper.

Checking the fix: I monkeypatched `_require_gateway_url` with the suggested version in fixcheck.py, in the same scratchpad folder. Token, status and logout then raise a clean CredentialError. Login treats the corrupt entry as an unreadable previous session and stores the new grant, so the vault ends up with a valid token_endpoint. The suggested fix is correct and needs nothing else (it does not depend on the BaseException revoke change). Add the two malformed URLs to test_stored_endpoints_must_stay_on_the_gateway and test_foreign_endpoints_are_rejected, and add a relogin test showing that a session with such an endpoint is replaced.

</details>

## 8. [low] Login silently overwrites an unreadable previous session without revoking it or warning

- **Where:** `src/virtual_key_mapper/sessions.py:66` (review lens: security)
- **Scenario:** The new `except CredentialError: previous = None` discards every load failure without telling the user. Example: NativeKeyring.get raises _invalid() after a fragment hash mismatch (an interrupted write or a manual vault edit). That error message tells the user to 'Ask the gateway administrator to revoke the damaged grant'. A transient read failure (`Cannot read the OS credential vault; unlock it and retry`) followed by a successful put has the same effect. In both cases login stores the new session over the old one, never revokes the old refresh grant (which may still be live), and prints nothing. I reproduced this with a vault whose first get() raises CredentialError: revoke calls [] and no notify. Before this diff the error stopped the login, so the admin-revocation advice reached the user. Skipping revocation is correct when the stored endpoints fail pinning, but the user still needs to be told.
- **Suggested fix:** In the except branch, call notify('Warning: the previous sign-in could not be read and was not revoked; ask the gateway administrator to revoke it.'). Optionally abort, after revoking the new grant, for vault-access errors as opposed to format/pinning errors, so a transient vault failure cannot orphan a valid grant. Extend test_relogin_replaces_an_unreadable_previous_session to assert the warning.
- **Verifier's fix:** Keep the error and pass its text on, so the specific advice ("unlock the vault" or "ask the administrator to revoke the damaged grant") reaches the user:

```python
with self._locked(profile):
    try:
        previous = self._load(profile)
        unreadable = None
    except CredentialError as error:
        previous, unreadable = None, error
    self.secrets.put(profile.account, session.to_dict())
...
if unreadable is not None:
    notify(f"Warning: the previous sign-in for '{profile.name}' could not be read ({unreadable}); "
           "it was replaced without being revoked. Ask the gateway administrator to revoke it.")
```

Notify only after the put succeeds and the lock is released, so the warning appears only when the old entry was actually overwritten.

Extend test_relogin_replaces_an_unreadable_previous_session to assert that notify was called with the warning. Add a test where the store's first get() raises a vault-access CredentialError, and assert that revoke is not called and the warning is shown.

Do not abort the login for unreadable-previous errors. Aborting would discard a completed browser sign-in, and CredentialError cannot reliably tell a transient vault error from corruption. If transient vault errors need handling, do a lock-free readability check before the browser flow so login fails fast, rather than aborting afterwards.

<details><summary>Verifier reasoning</summary>

The finding is real, though low severity. I confirmed it twice in /tmp/claude-0/-home-user-virtual-key-mapper/758eb7e3-cfad-5c2e-84d5-e72f0911dcc7/scratchpad/verify_unreadable/; the repo was not modified.

1. **Stub vault (repro.py):** the first get() raises "Cannot read the OS credential vault; unlock it and retry." and later calls work. login() runs `except CredentialError: previous = None` at sessions.py:66-67, then put() stores the new session. Result: stored refresh is NEW-REFRESH, revoke calls [], notify calls []. The old refresh token (OLD-LIVE-REFRESH) is still live on the gateway, is no longer stored anywhere, and the user is told nothing. This can happen with real backends. For example, Secret Service shows an unlock prompt on get. If the user dismisses it, get fails with KeyringLocked. If they accept the next prompt, which put()'s own _read triggers, put succeeds.

2. **Real NativeKeyring on a dict backend (repro_native.py):** I stored a fragmented session and damaged fragment 0. NativeKeyring.get raises _invalid (hash mismatch), and that message contains "Ask the gateway administrator to revoke the damaged grant". login swallows it. put() only parses the root manifest, which is still valid, so it writes the new session and removes the old fragments. Login succeeds with no revoke and no notify; one vault key is left. Before this diff, `previous = self._load(profile)` ran first and its error stopped the login before the browser flow, so the user saw the admin-revocation advice. That advice is now silently dropped.

**The diff is inconsistent here.** login warns "previous refresh grant could not be revoked; contact the gateway administrator" when a revoke call fails. When the previous grant cannot even be read, the outcome is the same (an unrevoked grant), but there is no warning. That gap is what makes this a defect rather than a design choice.

**Mitigating factors (why severity is low):**
- In the corruption case the old token cannot be revoked by the client anyway, so only the advice is lost.
- Users who ran token or status first already saw the _invalid message.
- With account = hash(name, gateway, team), a check_binding failure on a stored session realistically needs tampering.

**Related but separate:** if the root entry itself is corrupt, put() fails when it parses the old root. login now runs the full browser flow, revokes the new grant, and fails with "remove this profile's entries from the OS vault". So this "replace unreadable session" path does not cover root corruption, and the browser flow is wasted. This is not the reported defect.

**On the suggested fix:** adding the warning is right. I would not adopt the optional "abort on vault-access errors". CredentialError does not tell access errors apart from format errors, and aborting would throw away a completed browser login. See better_fix for a variant.

</details>

## 9. [low] Retry path compares the remaining validity measured before the network call, so it can return a token with less than 60 s left or already expired

- **Where:** `src/virtual_key_mapper/sessions.py:111` (review lens: security)
- **Scenario:** `remaining` is computed at line 99, before `client.refresh()`. JsonTransport's 20 s timeout applies to each connect, TLS and read step separately (DNS has no timeout), so a failed refresh can take tens of seconds. With 70 s left, a refresh that times out after 40 s still passes `remaining > MINIMUM_VALIDITY_SECONDS` and returns the old token with about 30 s left (reproduced with an injected clock). After a long DNS or slow-drip stall the token can already be expired. This breaks the new rule in the MINIMUM_VALIDITY_SECONDS comment that such tokens are never handed out, and credential helpers cache a token that is about to fail.
- **Suggested fix:** Recompute after the failure: `if session.expires_at - self.clock() > MINIMUM_VALIDITY_SECONDS: return session`. Add a test whose fake refresh advances the clock before raising GatewayError.
- **Verifier's fix:** The suggested fix is correct. After the retryable failure, recompute the remaining time with the current clock: `if session.expires_at - self.clock() > MINIMUM_VALIDITY_SECONDS: return session`, and raise the "Renewal will be retried" CredentialError otherwise. Add a test whose refresh side effect moves self.clock.now forward (for example by 40 s from 70 s remaining) before raising GatewayError(status=None or 503), and assert that CredentialError is raised and the stored session is unchanged. Optionally fix the related edge case too: make refresh_margin return at least MINIMUM_VALIDITY_SECONDS (for example `max(MINIMUM_VALIDITY_SECONDS, min(max(600.0, lifetime / 4), lifetime / 2))`), so a token with less than 60 s left is never returned at line 101 without first trying to refresh.

<details><summary>Verifier reasoning</summary>

I confirmed this by reading the code and reproducing it. In src/virtual_key_mapper/sessions.py, line 99 computes `remaining = session.expires_at - self.clock()` once, before the blocking `client.refresh(session)` call at line 104. When the refresh fails with a retryable error, line 111 compares that old value against MINIMUM_VALIDITY_SECONDS and returns the old session. The time spent in the failed network call is never subtracted. LiteLLMClient.refresh (litellm.py:310) makes one POST through JsonTransport. JsonTransport (transport.py:61) passes timeout=20, and that limit applies to each step separately: each address returned by DNS gets its own 20 s connect attempt, the TLS handshake gets 20 s, and each read gets 20 s. getaddrinfo has no timeout at all. So a retryable failure (no status, or a slow 5xx) can easily take 40 s or more. That is likely in exactly this situation: a token only gets down to about 60-120 s if every attempt in the previous ~9 minutes has already failed, which means the gateway is having an outage.

Reproduction script: /tmp/claude-0/-home-user-virtual-key-mapper/758eb7e3-cfad-5c2e-84d5-e72f0911dcc7/scratchpad/repro_stale_remaining.py. It uses the test suite's Clock, MemoryStore and make_session, stores a token with 70 s left, and uses a fake refresh that moves the clock forward and then raises GatewayError with no status. Output: "stall=40: returned token with 30 s remaining (minimum 60)" and "stall=80: returned token with -10 s remaining". So SessionManager.session(), used by both `token` and `token --json`, returns a token with less than the minimum validity, or one that has already expired. This breaks the rule in the comment at sessions.py:13 ("never handed out, even when renewal is temporarily failing"). The impact is a reliability defect rather than a real security issue. The token is still the user's own grant and an expired one is simply rejected. What actually happens is that the credential helper gets a token that is about to fail, instead of the clear "Renewal will be retried" error. Low severity is right. The existing test test_transient_failure_near_expiry_is_an_error_but_keeps_the_grant uses a refresh that fails instantly, so it does not catch this.

Related issue from the same diff (not part of this finding): refresh_margin returns lifetime/2 for any lifetime under 120 s. For example, a 100 s lifetime gives a margin of 50 s. A token with 55 s left is then returned at line 101 without even trying to refresh, which breaks the same 60 s rule. This only matters for gateways that issue very short-lived tokens.

</details>

## 10. [low] No tests for the new lock-free login swap/revoke ordering or for scheme-downgrade pinning of stored endpoints

- **Where:** `tests/test_sessions.py:295` (review lens: security)
- **Scenario:** Correct behaviour depends on two properties that no test checks. (1) `previous` is read under the lock after the browser flow, so a login that overlaps a refresh revokes the rotated grant P' and not the stale P. If someone moves `_load` back before client.login() (as in the old code), P' becomes a live, orphaned grant and every test still passes. (2) When two logins run concurrently, the losing grant is revoked. Pinning tests only use a foreign host ('https://attacker.example/token'). Nothing checks that an 'http://' endpoint on the same host, an endpoint with userinfo/query/fragment, or an http endpoint on a non-loopback allow_http profile is rejected for stored sessions. These checks pass today, verified with a probe, but have no regression protection.
- **Suggested fix:** Add tests: (a) client.login side_effect calls manager.token() inside the refresh window so a rotation happens during the browser flow, then assert revoke was called with the rotated session; (b) two sequential logins after a shared browser phase, asserting the first new grant is revoked and the second is stored; (c) extend test_stored_endpoints_must_stay_on_the_gateway with 'http://example.com/token', 'https://u@example.com/token', 'https://example.com/token?x=1' and 'https://example.com./token'.
- **Verifier's fix:** 1. Add a test in tests/test_sessions.py:
   - Store self.session.
   - Make client.login's side_effect set clock.now += 3000 (inside the refresh window), call self.manager.token(self.profile) in a worker thread and join it, then return a new grant.
   - Set client.refresh.return_value = self.renewal() (refresh_token='rotated').
   - Assert client.revoke was called once with the rotated session, never with self.session, and that the stored session is the new grant.
   This fails against mutant 1.

2. Test (b) (a nested or concurrent second login, asserting the earlier new grant is revoked and the later one stored) is optional. It catches the same mutant.

3. Put the URL-shape cases in one parametrized test in test_oauth.py that runs every value through both Discovery.parse and Session.check_binding, since both use _require_gateway_url. Values: 'http://gateway.example/token', 'https://u@gateway.example/token', 'https://u:p@gateway.example/token', 'https://gateway.example/token?x=1', 'https://gateway.example/token#f', 'https://gateway.example:8443/token'. Also add an allow_http loopback profile with an endpoint on a different loopback port. The trailing-dot case is optional: it only locks in the current strict behaviour.

<details><summary>Verifier reasoning</summary>

This is a real gap in test coverage. The code works today, but the test suite would not notice if the new logic broke. I confirmed this by changing a scratch copy of the code in two ways ("mutants") and running the full suite (264 tests) against each. The repo was not modified.

Mutant 1: in SessionManager.login (src/virtual_key_mapper/sessions.py:57-80), I moved the read of `previous` so it happens before client.login(), under the lock, which is how the old code did it. All 264 tests still pass. tests/test_sessions.py:117 and :130 cannot tell the difference: in both, nothing refreshes during the browser phase, so `previous` is the same whichever order is used.

A probe (scratchpad/overlap_probe.py) shows the effect. The stored grant is P, and the browser callback moves the clock into the refresh window and calls manager.token(), which rotates P to P'.
- Current code: revokes 'rotated' (P'). Correct.
- Mutant 1: revokes 'refresh-secret' (the old P, already spent). P' is left live, not stored and not revoked: an orphaned refresh grant.
Two logins running at the same time hit the same problem under this mutant, because both would read P and the first login's new grant would be orphaned. So the swap-then-revoke ordering, which this diff introduced, has no regression test.

Mutant 2: I made _require_gateway_url (litellm.py:147-154) compare only host and port. That drops the scheme check and the userinfo, query and fragment checks. All 264 tests still pass, because the only pinning tests (test_sessions.py:295 and test_oauth.py:45) use a foreign host. A probe (scratchpad/pin_probe.py) shows the current code rejects http://example.com/token, https://u@example.com/token, ?x=1, #f and https://example.com./token, while the mutant accepts all of them. With the http case, the refresh token would be sent in cleartext to the gateway host and no test would fail.

Part of this gap predates the diff: the Discovery checks were never tested for these cases either. What is new is that check_binding now runs on stored sessions, and its only test is the foreign-host one.

Severity is low (missing tests only, no bug today), but the finding is accurate. The suggested fix is right. Test (a) is the one that matters most; it catches mutant 1 directly. Test (b) catches the same mutant through concurrent logins, so it adds little. For (c), see better_fix.

</details>

## 11. [low] Remaining validity is measured before the refresh, so a slow failed refresh can return a token with under 60 s left, or an expired one

- **Where:** `src/virtual_key_mapper/sessions.py:99,111` (review lens: regressions)
- **Scenario:** `remaining` is computed before client.refresh(). The retryable branch then checks `remaining > MINIMUM_VALIDITY_SECONDS` against that old value. The urllib timeout applies to each socket operation, not to the whole request, and DNS resolution is not covered at all, so a failing refresh can take 20 s or much longer. Example: 70 s remain, the refresh drips for 80 s and then fails, and session() returns a token that expired 10 s ago (reproduced in scratchpad/stale.py with the injectable clock: 'seconds of validity left: -10.0'). Even a plain 20 s connect timeout returns a token with 41-60 s left, which breaks the documented MINIMUM_VALIDITY_SECONDS guarantee.
- **Suggested fix:** In the retryable branch, recompute `remaining = session.expires_at - self.clock()` after the exception before comparing with MINIMUM_VALIDITY_SECONDS. Add a test whose fake refresh advances the Clock before raising GatewayError.
- **Verifier's fix:** The suggested fix is correct and enough. In the retryable GatewayError branch (sessions.py:111), re-read the clock before the comparison:

    remaining = session.expires_at - self.clock()
    if remaining > MINIMUM_VALIDITY_SECONDS:
        return session

Add a test to tests/test_sessions.py:
1. Store a session with 70 s left.
2. Use a fake `client.refresh` that advances `clock.now` by 20 s, then raises `GatewayError("...")` with status None.
3. Assert that `manager.session(profile)` raises CredentialError and that the stored session is unchanged (needs_login is still False).

A second test should check the other side: a session with 200 s left and a 20 s stall still gets the old token back.

Optional hardening, not required for this finding: cap the whole refresh with an overall deadline (for example, total elapsed time at most REQUEST_TIMEOUT_SECONDS times a small factor). Multiple addresses and DNS can stall far beyond 20 s, and that time is spent while holding the profile lock that other callers wait on for up to 60 s.

<details><summary>Verifier reasoning</summary>

Confirmed by reading the code and running a reproduction.

In src/virtual_key_mapper/sessions.py:99, `remaining = session.expires_at - self.clock()` is computed once, before `client.refresh(session)` at line 104. The retryable branch at line 111 (`if remaining > MINIMUM_VALIDITY_SECONDS: return session`) compares against that old value and never re-reads the clock. This branch is new in this diff: the old code always raised when a refresh failed and never returned the old token, so the defect comes from this change.

A refresh can take a long time. transport.py passes `timeout=REQUEST_TIMEOUT_SECONDS` (20) to urllib, and that limit applies to each socket operation:
- `socket.create_connection` tries every getaddrinfo address, up to 20 s each, so a dual-stack host with dropped packets takes 40 s.
- Each TLS handshake or read step can wait up to 20 s, so a slowly dripping response can run much longer.
- getaddrinfo itself has no timeout.

During a gateway outage the refresh fails repeatedly from the margin (about 900 s for a 1 h lifetime) down to the last minute. In that situation a call at 70 s remaining with a 40 s connection stall returns a token with 30 s left, and an 80 s stall returns one that is already expired.

Running the existing scratch scripts against the unmodified repo code with the injectable Clock gives:
- stale.py: "seconds of validity left: -10.0"
- repro_stale_remaining.py: "stall=40: returned token with 30 s remaining (minimum 60)" and "stall=80: returned token with -10 s remaining".

This breaks the invariant stated in the comment at sessions.py:13 ("A token closer than this to expiry is never handed out, even when renewal is temporarily failing") and README.md:286. The impact is low: the caller gets a token that is nearly or already expired, so a helper consumer such as Claude's apiKeyHelper fails with a 401 later instead of getting a clear "Renewal will be retried" error now. Low severity is appropriate.

I checked the suggested fix on a scratch copy of the package. With the clock re-read in the retryable branch, stale.py raises "Gateway connection failed Renewal will be retried; the stored sign-in was kept." instead of returning an expired token. The existing test suite still passes (264 tests, OK).

</details>

## 12. [low] An HTTP 3xx from the token endpoint marks the grant as refused even though signing in again cannot help

- **Where:** `src/virtual_key_mapper/transport.py:27` (review lens: regressions)
- **Scenario:** GatewayError.retryable is False for 3xx, so session() calls _mark_needs_login. A redirect from the stored token endpoint is either temporary, such as a 302 to a maintenance or status page during a gateway deploy, so a later retry would succeed with the same refresh token, or a lasting misconfiguration that `login` also cannot get past, because discovery and the token exchange use the same NoRedirects transport. In both cases the refresh token is still valid, but the session is forced into 're-login-required'. Combined with the early window, a deploy-time redirect stops token release hours before expiry. No test covers 3xx classification in SessionManager.
- **Suggested fix:** Treat 3xx as retryable for refresh (keep the grant and report the redirect message), or add a separate 'not a refusal' category so only 4xx answers from the token endpoint set needs_login. Add a session test for a 302 or 307 refresh response.
- **Verifier's fix:** Leave `GatewayError.retryable` as it is, because repeating a redirected request unchanged is not expected to succeed. Handle 3xx separately in `SessionManager.session` (`sessions.py:105`):

```python
if error.status is not None and 300 <= error.status < 400:
    if remaining > MINIMUM_VALIDITY_SECONDS:
        return session
    raise CredentialError(f"{error} The stored sign-in was kept; if the gateway moved its endpoints, run 'vkm login --profile {profile.name}' to rediscover them.") from None
```

Put this before the `not error.retryable` branch, and do not call `_mark_needs_login` for 3xx. This keeps the still-valid grant and keeps handing out the current token during the early window. A transient redirect then clears without a browser login, and a lasting one (such as a moved endpoint) points the user to `login`, which rediscovers the endpoints.

Add session tests:
1. A 302 inside the margin returns the current token and leaves `needs_login` False.
2. A 308 near expiry raises a message that mentions login, and refresh succeeds on the next call once the redirect is gone.

<details><summary>Verifier reasoning</summary>

I confirmed this with a reproduction, but the impact is narrow and low severity. Part of the finding's argument is also wrong.

Trace: `JsonTransport._request` (`transport.py:66-69`) turns any 3xx into `GatewayError(status=3xx)`. `retryable` (`transport.py:27`) returns False for 300-399. In `SessionManager.session` (`sessions.py:105-110`), every non-retryable `GatewayError` calls `_mark_needs_login` and raises "The gateway refused the sign-in".

Reproduction (`scratchpad/v3xx/repro.py`): a real loopback HTTP server, the real `LiteLLMClient` and `JsonTransport`, and an 8 h session with 7000 s left. That is inside the refresh margin of min(max(600, 7200), 14400) = 7200 s. The token endpoint answers 302.
- Output: "Gateway redirect refused; configure its final public HTTPS origin. The gateway refused the sign-in; run 'vkm login --profile a'." Status becomes re-login-required.
- After the server goes back to answering 200, `token()` still fails with "The sign-in for 'a' is no longer accepted". The refresh token was never presented to LiteLLM, so it is still valid, and an access token valid for almost 2 more hours is withheld.

Before this change, refresh ran only when 60 s or less were left. A redirect that came and went earlier had no effect, so the early window does make this a small regression.

The behaviour also contradicts the class docstring: "Only a refused grant or an unexpected identity requires signing in again." An OAuth token endpoint never answers a refused grant with a 3xx (RFC 6749 5.2 uses 400/401). The research basis cited in the task lists refusals only as 4xx. The message "The gateway refused the sign-in" is factually wrong for a redirect.

No test covers it. `test_refused_grant_requires_login_without_further_refreshes` uses only 400/401/403/404, and the transient test uses only None/408/429/500/503.

Where the finding is wrong: "signing in again cannot help" is false when a token endpoint has moved. Refresh no longer rediscovers, so if a gateway or LiteLLM upgrade moves the token endpoint and the old path answers 308 while discovery returns the new path, `login` is exactly the fix. So the finding's main fix, simply making 3xx retryable, would leave that user seeing only "Renewal will be retried; the stored sign-in was kept" after expiry, with no hint to log in. Realistic causes of a 3xx are rare: an intermediary such as a TLS-intercepting corporate proxy auth redirect, a maintenance redirect, or a moved endpoint. Hence low severity.

</details>

## Rejected by the verifier

- [low] For token lifetimes under 120 s, refresh_margin is below MINIMUM_VALIDITY_SECONDS: tokens with under 60 s left are handed out without a renewal attempt, and there is no grace window (`src/virtual_key_mapper/sessions.py:22`): The code does what the finding says, but the case cannot happen with the gateway this tool targets, and the suggested fix would make things worse.

What I confirmed (scratchpad/renewal/repro.py): refresh_margin is min(max(600, lifetime/4), lifetime/2), so a 90 s lifetime gives a margin of 45 s. With 46 s left, sessions.py:100 returns the stored session without calling refresh. That is below MINIMU...
- [low] The rewritten login/status/logout CLI handlers are never run by any test (`src/virtual_key_mapper/cli.py:92-98,117-123`): This finding points to a coverage gap, not a defect, and I could not show any failure. The scenario only imagines one ("a wrong attribute name ... would only show up for users at runtime"). I checked it three ways:

1) The new handlers at src/virtual_key_mapper/cli.py:92-123 do the same thing as the old branches in main() at HEAD~2 (cli.py:303-314). They check the same --timeout range of 1-900...
