# Changelog

What changed in each release of Virtual Key Mapper (the `virtual-key-mapper` package and
the two VS Code extensions), newest first. Add your change under "Unreleased" in the same
pull request. To release, raise the version and rename "Unreleased" to that version and
the date.

## Unreleased

- Claude Code lists your gateway's models: `vkm setup claude` and `vkm run claude` turn on Claude
  Code's gateway model discovery, so `/model` in the CLI and the VS Code extension shows the models
  your key may use, as Copilot Chat does. `--model` is optional and only sets the starting model;
  `setup claude --remove` turns discovery off again unless you set it yourself.

## 0.1.2 - 2026-10-23

- The local sign-in extension `cbs-consulting.vkm-host` is now called "Virtual Key Mapper: host
  sign-in": the Marketplace still holds the old display name for the removed `vkm-local`. Messages
  that ask you to install it name its ID.

## 0.1.1 - 2026-10-23

- The local sign-in extension is now `cbs-consulting.vkm-host` (file `vkm-host.vsix`): the
  Marketplace no longer lists `cbs-consulting.vkm-local` and does not accept its name again.
  Uninstall `vkm-local` and install `vkm-host`; the settings (`vkm.localCommand`,
  `vkm.localProfiles`) stay the same.

## 0.1.0 - 2026-10-22

- First public version, under the name Virtual Key Mapper: the command is `vkm`, the
  package `virtual-key-mapper`, the repository file `.vkm` and the environment variables
  `VKM_*`. The VS Code extensions are `cbs-consulting.vkm` (Copilot Chat) and
  `cbs-consulting.vkm-local` (local sign-in for dev containers, SSH and WSL).
- Licensed under the Apache License 2.0.
- Published on PyPI (`virtual-key-mapper`) by a release workflow with one shared version; the
  VS Code extensions (`cbs-consulting.vkm`, `cbs-consulting.vkm-local`) are attached to each
  GitHub release and uploaded to the Visual Studio Marketplace by hand.
- Labels: requests carry key/value labels such as
  `customer = "acme"` and `project = "webshop"`, defined in the central rules document
  (version `"2.0"`) or the profile, with required labels, allowed values and per-label
  sources. Each label is resolved on its own from `--label KEY=VALUE`, `VKM_LABEL_<KEY>`,
  `vkm repo bind --label`, a `.vkm` `[labels]` table or a central rule. Gateway adapters
  declare outputs; LiteLLM sends `x-litellm-tags` (default `KEY:VALUE` per label,
  overridable with `${KEY}` formats) and spend-log metadata with all labels and their
  sources. New `vkm labels list|values`; `profile add/update --label/--output`;
  "Assign labels" and a status bar with all label values in VS Code. See
  [docs/labels.md](docs/labels.md).
