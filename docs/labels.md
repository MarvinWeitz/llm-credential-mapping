# Labels

Labels attribute requests. A repository carries key/value pairs
(`customer = "acme"`, `project = "webshop"`), and each gateway adapter sends them the way that
gateway can carry them: LiteLLM as request tags and spend-log metadata. What a label means is up
to the organisation; `vkm` knows keys, values, which keys are required, where values may come
from, and how to format them for the gateway.

In commands, messages and docs the word is "label"; "customer" is only an example key.

## 1. Label definitions

A label is defined as an object in a list, so its order survives any JSON tool and new fields
fit without changing the shape:

```json
"labels": [
  {
    "key": "customer",
    "required": true,
    "description": "Who the work is billed to",
    "values": [{"id": "acme", "name": "Acme"}, {"id": "globex", "name": "Globex"}],
    "strict": true,
    "sources": ["explicit", "local", "file", "rules"]
  },
  {"key": "project", "description": "Project within the customer"}
]
```

| Field | Type | Default | Meaning |
| --- | --- | --- | --- |
| `key` | string | (required) | `^[a-z][a-z0-9_-]{0,31}$` |
| `required` | bool | `false` | Nothing is sent while this label has no value; tools block or ask. |
| `description` | string | none | 1-200 printable characters. Shown by the picker, `vkm labels list` and the VS Code tooltip. |
| `values` | list of `{"id", "name"?}` | none | The choices the picker offers (at most 200; `name` 1-200 printable characters). |
| `strict` | bool | `false` | With `values`: only those ids are accepted, from any source. Without `values` it is an error. |
| `sources` | list | all five | Which sources (section 2) may set this label. The others are skipped and shown as ignored. |

- **Values**: 1-256 printable ASCII characters, no surrounding whitespace, no `,`, no leading `!`
  or `&` (LiteLLM tag routing directives).
- **Keys** map to `VKM_LABEL_<KEY>` (uppercase, `-` as `_`). Two keys that map to the same name are
  a conflict, also across the central document and the profile.
- **Limits**: at most 16 labels in total (central and profile together).

### Where definitions live

1. **The central rules document** (`profile add --rules URL|PATH`): the organisation's labels.
2. **The profile**: personal labels for keys the central document does not define.

A profile label for a key the central document defines (or that maps to the same variable) is
refused by `profile add/update` when the document can be read, and otherwise dropped at runtime
with a notice; the central one is used. A profile output override that uses the dropped label's
key is dropped with it (with a notice; the adapter default applies), so it cannot block requests.
Order: central labels in document order, then profile labels in the order they were added.

If no label is defined anywhere, requests carry only the repository (in metadata). `setup`,
`run` and `repo show` print one line saying so; hooks stay silent.

## 2. Values

Each key is resolved on its own. The first allowed source with a value wins:

| Order | Source | Set with | `source` |
| --- | --- | --- | --- |
| 1 | the command line | `vkm run --label KEY=VALUE`, `vkm repo show --label KEY=VALUE` | `explicit` |
| 2 | the environment | `VKM_LABEL_<KEY>=VALUE` | `env` |
| 3 | your personal binding | `vkm repo bind --label KEY=VALUE` | `local` |
| 4 | the nearest `.vkm` file | `vkm repo bind --shared --label KEY=VALUE` | `file` |
| 5 | a central rule | the rules document | `rules` |

- **No value on purpose**: `--label KEY=` on the command line, JSON `null` in a rule and `""` in a
  `.vkm` mean "no value here, stop searching". A required label set to nothing is missing.
- **Unknown keys**: `--label` and `repo bind --label` refuse keys no definition knows (typos).
  Values for unknown keys from the environment, bindings, files or rules are ignored, not sent,
  and listed by `repo show`.
- **Environment**: `vkm` reads `VKM_LABEL_<KEY>` for each defined key. Only that exact name counts:
  any other `VKM_LABEL_*` variable (also a differently cased one such as `VKM_LABEL_project`) is
  ignored as an unknown label under its variable name. An empty variable counts as unset; an
  invalid value is an error naming the variable.
- **Mixed sources are normal and shown**: `repo show` and `run` print each label's source.
- Labels from a profile are personal: they are sent by the default outputs, never by a central
  output override (which may only use central labels).

### `.vkm`

```toml
# Labels for AI tool requests in this folder and below (vkm).
[labels]
customer = "acme"
project = "webshop"
```

Only a `[labels]` table with string values; at most 4096 bytes. The nearest file wins as a whole
(a nearer file without `project` does not fall through to a parent's file). The search stops below
the home folder. `repo bind --shared --label` merges the given keys into the `.vkm` at the
repository root (or the folder outside Git) and rewrites the file; the home folder and its parents
are refused.

### Personal bindings (`repositories.json`, version 2)

`{"gateway", "repository", "labels": {KEY: VALUE}}` per binding, keyed by gateway and repository.
Per key the origin's binding is tried, then the upstream's. `repo bind --label` sets the given
keys and keeps the others; a different value for an existing key needs `--replace` (a
session that bound it concurrently must not be overwritten silently). `repo unbind` removes the
binding, `repo unbind --label KEY` one key (the last key removes the binding).

### The central rules document (version `"2.0"`)

```json
{
  "version": "2.0",
  "labels": [
    {"key": "customer", "required": true, "values": [{"id": "acme", "name": "Acme"}]},
    {"key": "project"}
  ],
  "outputs": {"litellm": {"tags": {"format": ["${customer}", "${customer}:${project}"]}}},
  "rules": [
    {"match": "github.com/acme-*/**", "labels": {"customer": "acme"}},
    {"match": "**", "labels": {"project": null}}
  ]
}
```

- **Version**: `"MAJOR.MINOR"`. An unknown major is refused. A newer minor of a known major is
  accepted: unknown fields are ignored and `repo show` says so. With a known minor, unknown fields
  are refused, so `"requried"` cannot silently turn a required label optional.
- **The document must be valid on its own**: rules and central output formats may only use
  central labels. A rule may set a label whose `sources` exclude `rules`; that value is ignored
  ("source not allowed") and listed by `repo show`. An invalid document is not used; the last
  good cached copy is, else it is an error.
- **Rules** are tried in order, each against origin then upstream. Each key takes its value from
  the first matching rule that sets it, so a catch-all below can still supply `project`. A rule's
  `labels` must not be empty. At most 1000 rules; at most 8 `**` per pattern; globs are compiled
  once.
- **Outputs** are grouped by gateway type, so one document can serve profiles on different
  gateways; a profile reads only its own gateway's section.

### Required labels

A required label without a value blocks the request. The error starts with the fixed phrase
`Required labels have no value:` followed by the keys and the ways to set them (tools match on
this phrase). Interactive commands (`vkm run` and `vkm repo bind` in a terminal, the VS Code
"Assign labels" button) ask for each missing required label, offering its `values` or the
gateway's catalogue (section 3), and save the answers as a personal binding. With a catalogue the
picker offers its entries; without one it accepts any valid typed value, and no sign-in is needed
(the gateway is only asked when a catalogue can apply). `vkm repo bind` asks only for labels the
binding may set: those whose `sources` include `local` (with `--shared`: `file`); when none is
left it refuses.

## 3. Outputs

A gateway adapter lists the **outputs** it can fill. Header names are fixed by the adapter; a
format never chooses a header.

| Kind | Format | LiteLLM output | Header | Default |
| --- | --- | --- | --- | --- |
| `list` | a string or a list of strings | `tags` | `x-litellm-tags` | one `KEY:${KEY}` entry per label, in order |
| `json` | none (on or off) | `metadata` | `x-litellm-spend-logs-metadata` | on |

The metadata is `{"labels": {KEY: VALUE}, "sources": {KEY: SOURCE}, "repository": "..."}`
(`repository` only inside a Git checkout with an origin), serialised compactly with sorted keys and
ASCII escapes. It is sent even when no label has a value.

An output is overridden with `{"format": ...}` or switched off with `{"enabled": false}`, in the
central document (per gateway type, wins) or the profile:

```bash
vkm profile update work --output 'tags=${customer}' --output 'tags=${customer}:${project}'
vkm profile update work --output-off metadata
vkm profile update work --output-default tags
```

Several `--output` for the same name in one call replace its whole list. A profile override of an
output the central document overrides is dropped with a notice.

### Format strings

A format is compiled once into literal text and placeholders:
`entry := (literal | "$$" | "${" KEY "}")*`. Any other `$`, an empty, padded or uppercase
placeholder, or a placeholder for an undefined label is an error. Substitution is one left-to-right
pass; substituted text is never scanned again.

- An entry that uses an **optional** label without a value is **dropped**; the other entries are
  still sent. A list whose entries are all dropped omits the header.
- **Literal text** follows the value rules (printable ASCII, no `,`, an entry may not start with `!`
  or `&`).
- **No look-alikes**: a value of one label must not be able to produce a tag that reads as
  another's, so a list output's entries follow these rules:
  - two placeholders may not be adjacent;
  - if any entry starts with a placeholder `${X}`, every entry must start with `${X}`;
  - of entries that start with literal text, take the literal text before the first placeholder
    (the whole entry if it has none): two entries whose texts are equal, or one a prefix of the
    other, must have the same first placeholder (or both none).

  Allowed: `${customer}` next to `${customer}:${project}`; the default `KEY:${KEY}` per label.
  Refused: `customer:${customer}` next to `customer:${project}`, `customer:${customer}` next to
  `${project}`, and `${customer}` next to `${project}` (a project value could then produce a tag
  that reads as a customer).
- Each result must be 1-512 printable ASCII characters without `,`, not starting with `!` or `&`.
  LiteLLM also refuses results that start with `User-Agent: ` (its automatic tags) or are `default`
  (its routing tag). Duplicates are removed.

### Checks

`profile add/update` checks what it can without network access; loading the configuration checks
the combination of central document, profile and adapter defaults every time. When the central
document cannot be read, `profile add/update` still checks the profile on its own for what fails
whatever that document defines (format syntax, look-alikes, a required profile label that reaches
no output); a placeholder for a key the profile does not define may be a central label and counts
as required. Problems that would let a request go out with the wrong or missing attribution block
requests with a clear message:

- an output the gateway does not have, or a format for a `json` output;
- a format error (above);
- a required label that reaches no output: it must appear in an enabled `list` entry whose other
  placeholders are all required labels. Only when no `list` output is enabled does an enabled
  `json` output count.
- a header larger than 4096 bytes at request time.

Conflicts between the profile and the central document are not errors: the profile's piece is
dropped and a notice is shown (`setup`, `repo show`, `labels list`, `profile show`).

## 4. What each part does with labels

| Part | Behaviour |
| --- | --- |
| `vkm repo show` | Text: each label with value, source and detail, missing required, ignored values, notices; exit 1 while required labels are missing. `--json`: always prints `{"labels": [{key, value, name, source, detail, required, description}], "missing", "ignored", "repository", "complete", "headers", "message", "notices"}` and exits 0 (`headers` is empty while incomplete). When the labels cannot be resolved (an invalid `VKM_LABEL_*` or `.vkm` value, a value a `strict` label does not list, also from the launch marker) it still prints the document, with `complete: false`, empty `headers`, the error as `message` and every label without a value. Exit 1 only when the configuration cannot be loaded or a `--label` argument is invalid. |
| `vkm repo bind` | `--label KEY=VALUE` (repeatable), `--shared`, `--replace`; without `--label` it asks for the missing required labels the binding may set (section 2). Refreshing Claude local settings afterwards is best effort: a failure is a warning, the binding is saved. |
| `vkm repo unbind` | the binding, or one key with `--label KEY` |
| `vkm run` | `--label KEY=VALUE` for that launch; asks in a terminal for missing required labels and saves them (inside Git). |
| `vkm labels list` / `vkm labels values KEY` | the effective definitions with their origin and notices / the values for KEY: its `values`, or the gateway's catalogue, or none (any valid typed value is accepted), and which one was used. Without a qualifying tags entry no catalogue applies and no sign-in is needed. |
| `vkm profile add/update` | `--label KEY [--required] [--description TEXT] [--value ID[=NAME]]... [--strict] [--source NAME]...` (one label per call), `--no-label KEY`, `--output NAME=FORMAT`, `--output-off NAME`, `--output-default NAME`, `--definitions JSON` (the profile's whole `labels`/`outputs` in one call, used by the dev container relay). `profile show --json` shows the profile's own definitions. |
| Gateway catalogue | Only when the tags output has an entry of the form `PREFIX${KEY}SUFFIX` with one placeholder: LiteLLM's `/tag/list` entries with that prefix and suffix, stripped. The default `customer:${customer}` qualifies. |
| Launch marker | `VKM_LAUNCH_LABELS`: compact JSON `{KEY: VALUE or null}` of the `--label` values only. It is never a resolution source. The Claude guard and the OpenCode plugin pass it to `vkm` explicitly (`repo show --launch`), which then treats it as `explicit`. A malformed marker counts as absent. |
| Claude Code guard | Resolves the directory with the marker's labels as explicit, computes the headers, writes them to `.claude/settings.local.json`, and allows the prompt only when the session's present headers equal them (an absent header equals an omitted one). When the marker sets any label, the local file (shared by every session in the folder) is never written: the session's headers are only compared with the computed ones (`vkm run claude` passes them to its child directly). |
| Codex | The shell hook and `run` export one variable per header (unset when the header is omitted) and `VKM_ATTRIBUTION=1`; the hook unsets `VKM_ATTRIBUTION` before it runs `vkm`, and when the labels cannot be resolved (also when the profile cannot be loaded) `vkm` unsets it and every `VKM_HEADER_*` variable; `vkm token --require-attribution-env` refuses only when `VKM_ATTRIBUTION` is missing. Setup registers every header the adapter can send, so later definition changes need no new setup. |
| OpenCode plugin | asks `vkm repo show --json --launch` per directory and adds `headers` when `complete`; blocks otherwise. |
| Copilot Chat extension | Status bar: the values of all labels with a value, in order, using catalogue names, joined by ` · `; missing required labels in warning style; `no labels` when none are defined. Tooltip: each label with description and source. Folders of a multi-root window are equivalent when their label values are equal. "Assign labels" opens `vkm repo bind --directory DIR` in a terminal. |
| Dev containers | The companion's profile answer includes the local profile's own `labels` and `outputs`; the container profile is created in one `profile add ... --definitions JSON` call. Rules given as a local file cannot be copied: the warning now says that the organisation's label definitions are missing in the container. |
