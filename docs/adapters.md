# Adding a gateway, sign-in kind or tool

Each is one module plus one line in `src/virtual_key_mapper/registry.py`. The interfaces
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

    @staticmethod
    def outputs() -> tuple[Output, ...]:
        # The headers labels can fill on this gateway (static: profiles are validated without a gateway).
        return OUTPUTS

    def catalogue(self, key: str, credential: Credential, config: LabelConfig) -> list[Choice] | None:
        # Values the gateway lists for one label key (for the picker), or None if it cannot list them.
        ...
```

An adapter does not build headers; it declares **outputs** and the core fills them
(`LabelConfig.headers`) with the formats, checks and limits of [labels.md](labels.md#3-outputs):

```python
OUTPUTS = (
    # A list output: formatted entries joined with ","; the default gives them for the label keys, in order.
    Output("tags", LIST, "x-my-tags", lambda keys: tuple(f"{key}:${{{key}}}" for key in keys), _reserved),
    # A json output: {"labels", "sources", "repository"} as compact JSON, on or off.
    Output("metadata", JSON, "x-my-metadata"),
)
```

- `name` is what the central document and `profile update --output NAME=FORMAT` refer to; the
  header name is fixed here and never chosen by a format.
- A `list` default must satisfy the format rules for any set of keys (all entries that start with a
  placeholder start with the same one) and let every required label reach the output.
- `reserved(result)` refuses results the gateway gives another meaning to (LiteLLM: `User-Agent: `
  tags and its routing tag `default`); such a result blocks the request.
- `catalogue` may use `config.inversions(output_name, key)`, the `(prefix, suffix)` pairs of entries
  `PREFIX${KEY}SUFFIX`, to read values back from what the gateway stored (LiteLLM: `/tag/list`).

List its requests and label headers in [network.md](network.md#per-gateway).

Register it: `GATEWAYS = {"litellm": LiteLLMGateway, "<kind>": MyGateway}`. Header names must
be letters, digits and `-`; values printable ASCII. Nothing else changes: the integrations,
the shell hooks, the OpenCode plugin and the VS Code extension take headers and URLs from
the gateway (through `repo show --json` and the `IntegrationContext`).

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
helper commands, the configuration folder, the effective label configuration `labels`) and the
resolved labels (`Attribution`); get URLs from `context.gateway.endpoint(...)`, headers from
`integrations.common.attribution_headers` and every header name the adapter can send from
`context.labels.header_names()` (to clear stale ones).

- Prefer the tool's own renewing credential hook (a helper command it reruns) over passing a
  token at launch.
- Prefer per-request or per-directory attribution from the tool's own mechanism; if the tool
  only reads headers from its environment, use `header_variable(name)` and the
  `shell-init` hooks, like Codex.
- Send nothing while required labels have no value (`attribution.complete`); show the
  `MissingLabelsError` message, which starts with `Required labels have no value:`.
- `setup` must keep unrelated configuration, refuse to replace another tool's credential
  setup without `--replace`, and `remove` must undo only what it wrote.
- Add tests with `tests/isolation.py`, and ideally an opt-in live test with the real binary
  against a fake gateway (see `tests/test_tools_native.py`).

`tests/test_architecture.py` fails if an integration imports a gateway or auth module or
contains vendor header names or URL paths.
