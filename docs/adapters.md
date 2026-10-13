# Adding a gateway, sign-in kind or tool

Each is one module plus one line in `src/llm_credential_mapping/registry.py`. The interfaces
are in `adapters.py`; background in [architecture.md](architecture.md#3-adapters-why-the-core-knows-no-vendor).
`tests/test_adapters.py` contains a complete worked example (`FakeGateway`, `FixedKeyAuth`)
that runs through every integration and the CLI.

## A gateway

Create `gateways/<kind>.py` with a class taking the `Profile`:

```python
class MyGateway:
    def __init__(self, profile): self.profile = profile

    def endpoint(self, api: Api) -> str:
        # Base URL per wire API; raise CredentialError for APIs this gateway lacks.
        ...

    def attribution_headers(self, attribution: Attribution) -> dict[str, str]:
        # How this gateway expects a customer (and optionally source/repository) as headers.
        ...

    def attribution_header_names(self) -> tuple[str, ...]:
        # Every name attribution_headers may return (Codex's config lists them ahead of time).
        ...

    def visible_customers(self, credential: Credential) -> list[Tag] | None:
        # A customer catalogue from the gateway, or None if it has none.
        ...
```

Register it: `GATEWAYS = {"litellm": LiteLLMGateway, "<kind>": MyGateway}`. Header names must
be letters, digits and `-`; values printable ASCII. Nothing else changes: the integrations,
the shell hooks, the OpenCode plugin and the VS Code extension take headers and URLs from
the gateway.

## A sign-in kind

Create `auth/<kind>.py` with a `settings(values: dict) -> dict` validator (reject settings
you do not use) and a class:

```python
class MyAuth:
    def __init__(self, profile, store, secrets):   # secrets() opens the OS vault; call it only if you store something
        ...
    def login(self, notify, **options) -> Credential: ...
    def logout(self, notify) -> None: ...
    def status(self) -> dict: ...                   # nonsecret; include "state"
    def credential(self) -> Credential: ...         # never prompts; renew here if needed
```

Register it: `"<kind>": AuthKind(MyAuth, my_settings, frozenset({"gateway-kind"}) or None)`.
If it needs new `profile add` options, add them to `profile_add` in `cli.py`; each auth kind
rejects settings it does not know. A fixed API key, for example, is about 50 lines: `login`
asks for the key with hidden input and stores it with `secrets()`, `credential()` returns it
with `expires_at=None`.

## A tool

Create `integrations/<tool>.py` with `setup(context, model, replace)`, `remove(context)`,
`command(context, attribution, args, model)` and `launch(...)`, and add it to
`INTEGRATIONS`. Use only the `IntegrationContext` (profile, gateway, `cli_command(...)` for
helper commands, the configuration folder) and the attribution; get URLs from
`context.gateway.endpoint(...)` and headers from `integrations.common.attribution_headers`.

- Prefer the tool's own renewing credential hook (a helper command it reruns) over passing a
  token at launch.
- Prefer per-request or per-directory attribution from the tool's own mechanism; if the tool
  only reads headers from its environment, use `header_variable(name)` and the
  `shell-init` hooks, like Codex.
- `setup` must keep unrelated configuration, refuse to replace another tool's credential
  setup without `--replace`, and `remove` must undo only what it wrote.
- Add tests with `tests/isolation.py`, and ideally an opt-in live test with the real binary
  against a fake gateway (see `tests/test_tools_native.py`).

`tests/test_architecture.py` fails if an integration imports a gateway or auth module or
contains vendor header names or URL paths.
