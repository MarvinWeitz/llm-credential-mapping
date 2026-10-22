# Changelog

What changed in each release of Virtual Key Mapper (the `virtual-key-mapper` package and
the two VS Code extensions), newest first. Add your change under "Unreleased" in the same
pull request. To release, raise the version and rename "Unreleased" to that version and
the date.

## Unreleased

- First public version, under the name Virtual Key Mapper: the command is `vkm`, the
  package `virtual-key-mapper`, the repository file `.vkm` and the environment variables
  `VKM_*`. The VS Code extensions are `cbs-consulting.vkm` (Copilot Chat) and
  `cbs-consulting.vkm-local` (local sign-in for dev containers, SSH and WSL).
- Licensed under the Apache License 2.0.
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
