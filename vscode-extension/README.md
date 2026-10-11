# LLM Credentials for Copilot Chat

Adds your gateway's models to Copilot Chat (ask, edit and agent mode). Every request
uses a fresh credential from your `llm-credentials` sign-in and carries the customer
of the workspace folder you are working in. Nothing is stored in VS Code.

## Requirements

- VS Code 1.104 or newer with GitHub Copilot Chat. Your organization's Copilot
  policy "Bring Your Own Language Model Key" must allow model provider extensions.
- The `llm-credentials` CLI on the same machine as the extension host (inside the
  container or SSH host for remote windows), with a signed-in profile:
  `llm-credentials login`.
- A customer for each workspace folder (`llm-credentials repo show` must succeed
  there). Without one, requests fail before anything is sent.

## Settings

- `llmCredentials.command`: how to run the CLI, e.g. `["/path/to/venv/bin/llm-credentials"]`
  or `["python", "-m", "llm_credential_mapping"]`. Default: `["llm-credentials"]` from `PATH`.
- `llmCredentials.profile`: the profile; empty for the CLI's default.
- `llmCredentials.maxInputTokens`, `llmCredentials.maxOutputTokens`, `llmCredentials.imageInput`:
  what Copilot is told about the models. These are not discovered from the gateway.

## Use

Choose an "llm-credentials" model in the Copilot Chat model picker. The status bar
shows the customer of the current folder; in multi-root workspaces the active
editor's folder wins, and the status bar lets you choose a folder or assign a
customer. "LLM Credentials: Sign in" opens a terminal running `llm-credentials login`.

Not covered: inline completions, next edit suggestions, embeddings and some agent
actions always use GitHub's own models, which this extension cannot attribute.
