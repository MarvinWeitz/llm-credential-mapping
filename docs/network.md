# Network access

What Virtual Key Mapper and the tools it sets up connect to, for firewall, proxy and TLS
inspection rules. In short: **outbound HTTPS to your LLM gateway**, plus the central rules URL
if you use one. Nothing listens on the network; the sign-in callback and the dev container
relay stay on the machine.

## What to allow

| From | To | Why |
| --- | --- | --- |
| Developer machines (and dev containers, SSH and WSL hosts) | the gateway origin, e.g. `https://llm.company.example` (HTTPS, usually port 443) | sign-in, renewal and every model request |
| Developer browsers | the gateway origin and your identity provider (the SSO login page the gateway redirects to) | the browser part of `vkm login`, for sign-ins that use the browser (LiteLLM SSO) |
| Developer machines | the central rules URL, if a profile has `--rules https://…` | the organisation's label definitions; cached, so a short outage does not block work |
| Developer machines, for installing and updating | see [Installation](#installation) | |

In forwarded mode (dev containers, SSH, WSL) the local machine signs in and renews; the
container still sends its model requests to the gateway itself, so it needs the gateway too.
The forwarded sign-in itself adds no network access.

The gateway must answer at the configured origin directly: vkm refuses redirects (also
`http://` to `https://`), so the credential never goes to another address. Claude Code's model
list does the same.

This table holds for every gateway. Which paths on the gateway are used, which headers carry
the labels and how the sign-in works depend on the gateway and sign-in kind of the profile
(`vkm profile show`); the next section lists them for each one vkm supports.

## Per gateway

A rule for the whole gateway origin covers all requests of a gateway; the paths are for
reference, and tool updates may add requests below the same base URLs.

The tools always speak their own standard API (Anthropic Messages, OpenAI Responses, OpenAI
Chat Completions) below the base URL the gateway gives vkm, so what differs per gateway is
that base URL, the sign-in and the label headers.

### LiteLLM (gateway `litellm`, sign-in `litellm-sso`)

Base URLs: the gateway root for Claude Code, `/v1` for Codex, OpenCode and Copilot Chat. The
sign-in endpoints come from the gateway's discovery document and must be on the gateway origin.

| Request | Sent by | When |
| --- | --- | --- |
| `GET /.well-known/litellm-cli-auth` | vkm | `login` |
| `POST` registration endpoint (from discovery) | vkm | `login` |
| Browser: authorization endpoint (from discovery), then the identity provider, then `http://127.0.0.1:<random port>/callback` | your browser | `login`; the callback is a loopback address on the same machine |
| `POST` token endpoint (from discovery) | vkm | `login`, and renewal whenever the short-lived token expires while tools run |
| `POST` revocation endpoint (from discovery) | vkm | `logout`, re-login |
| `GET /tag/list` | vkm | only to suggest label values (`labels values`, pickers), when tags carry that label |
| `GET /v1/models` | vkm (`setup opencode`), Copilot Chat extension, Claude Code (model list) | setup, extension start, each Claude Code start |
| `POST /v1/messages`, `POST /v1/messages/count_tokens`, `HEAD /api/hello` | Claude Code (CLI and VS Code extension) | every request; the last two are optional |
| `POST /v1/responses` | Codex | every request |
| `POST /v1/chat/completions` | OpenCode, Copilot Chat extension | every request |

A proxy or TLS inspection must pass these request headers unchanged: `Authorization`,
`x-api-key`, `x-litellm-tags`, `x-litellm-spend-logs-metadata` (the labels),
`anthropic-version`, `anthropic-beta`. Streamed responses (`text/event-stream`) must not be
buffered.

### Other gateways

Each further gateway or sign-in kind is an adapter ([adapters.md](adapters.md)) and adds its
own section here. The general table above stays the same: outbound HTTPS to that gateway's
origin, and for a browser sign-in its identity provider.

## Proxies and certificates

- **Proxy:** vkm uses the system proxy settings (Windows: the Internet Options proxy) or the
  `HTTPS_PROXY` and `NO_PROXY` variables. The tools follow their own proxy settings, usually
  the same variables.
- **TLS inspection:** vkm trusts the system certificate store (Windows certificate store; on
  Linux the system CA bundle, or the file in `SSL_CERT_FILE`). If your proxy re-signs HTTPS,
  its CA must be there. VS Code and its extensions use VS Code's proxy and certificate
  settings; the tools have their own (for example `NODE_EXTRA_CA_CERTS` for Claude Code and
  OpenCode), see their documentation.

## Local only, no network

- The OS credential vault (Windows Credential Manager, macOS Keychain, Secret Service on Linux).
- The sign-in callback on `127.0.0.1` with a random port, open only during `login`.
- The dev container relay: a Unix socket inside the container, carried over VS Code's own
  remote connection to the local machine. No port is opened.

## Traffic of the tools themselves

The tools also contact their vendors for their own features (updates, telemetry, checks),
outside vkm's control; see each vendor's documentation. For Claude Code,
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` turns off what is not needed for requests to the
gateway; a few features, such as fast mode's availability check, still call `api.anthropic.com`
directly.

## Installation

| What | Hosts |
| --- | --- |
| The CLI with `pip` or `uv` | `pypi.org`, `files.pythonhosted.org` |
| `uv` itself and the Python it downloads | `astral.sh`, `github.com`, `objects.githubusercontent.com` |
| VS Code extensions from the Marketplace | `marketplace.visualstudio.com`, `*.gallerycdn.vsassets.io`, `*.gallery.vsassets.io` |
| Extensions as `.vsix` files, release downloads | `github.com`, `objects.githubusercontent.com` |
