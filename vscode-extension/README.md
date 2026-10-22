# Virtual Key Mapper for Copilot Chat

Adds your gateway's models to Copilot Chat (ask, edit and agent mode). Every request
uses a fresh credential from your `vkm` sign-in and carries the labels
(such as `customer` and `project`) of the workspace folder you are working in.
Nothing is stored in VS Code.

## Requirements

- VS Code 1.104 or newer with GitHub Copilot Chat. Your organization's Copilot
  policy "Bring Your Own Language Model Key" must allow model provider extensions.
- The `vkm` CLI on the same machine as the extension host (inside the
  container or SSH host for remote windows), with a signed-in profile:
  `vkm login`.
- A value for every required label in each workspace folder (`vkm repo show`
  must succeed there). While one is missing, requests fail before anything is sent.
  See the [labels reference](../docs/labels.md).

## Settings

- `vkm.command`: how to run the CLI, e.g. `["/path/to/venv/bin/vkm"]`
  or `["python", "-m", "virtual_key_mapper"]`. Default: `["vkm"]` from `PATH`.
- `vkm.profile`: the profile; empty for the CLI's default.
- `vkm.maxInputTokens`, `vkm.maxOutputTokens`, `vkm.imageInput`:
  what Copilot is told about the models. These are not discovered from the gateway.

## Use

Choose a "Virtual Key Mapper" model in the Copilot Chat model picker. The status bar
shows the label values of the current folder in order, joined by ` · ` (e.g.
`acme · webshop`), missing required labels in warning style, and `no labels` when
none are defined; its tooltip lists each label with its description and source. In
multi-root workspaces the active editor's folder wins; folders with equal label
values count as one. The status bar lets you choose a folder or assign labels:
"Assign labels" opens a terminal running `vkm repo bind`, which asks for each missing
required label and saves the answers for you only. "Virtual Key Mapper: Sign in"
opens a terminal running `vkm login`.

Not covered: inline completions, next edit suggestions, embeddings and some agent
actions always use GitHub's own models, which this extension cannot attribute.
