import json
import os
import re
import sys
from pathlib import Path

from .copilot import CopilotConnection
from .errors import CredentialError
from .jsonc import JsoncDocument, Member, Value

_FILENAME = "chatLanguageModels.json"
_SECRET_REFERENCE = re.compile(r"\$\{input:[^{}:\s]+\}")


def models_file(explicit: Path | None = None) -> Path:
    """Resolve an existing desktop model file; ambiguous profiles require an explicit path.

    Reject workspace/Remote Machine files and remote-host autodetection with CredentialError.
    No directories or files are created, and stored secrets are never inspected.
    """
    try:
        if explicit is not None:
            path = explicit.expanduser()
        else:
            if any(os.environ.get(name) for name in (
                "VSCODE_AGENT_FOLDER", "REMOTE_CONTAINERS", "CODESPACES", "SSH_CONNECTION",
            )):
                raise CredentialError(
                    "Copilot model registrations belong to the desktop VS Code profile, not Remote Machine settings. "
                    "Run --apply on the desktop or use --models-file with its accessible desktop model file."
                )
            if portable := os.environ.get("VSCODE_PORTABLE"):
                root = Path(portable).expanduser()
                if not root.is_absolute():
                    raise CredentialError("VSCODE_PORTABLE must be absolute; use --models-file.")
                user = root / "user-data" / "User"
            else:
                if sys.platform == "win32":
                    if not os.environ.get("APPDATA"):
                        raise CredentialError("Cannot locate desktop VS Code without APPDATA; use --models-file.")
                    root = Path(os.environ["APPDATA"])
                elif sys.platform == "darwin":
                    root = Path.home() / "Library" / "Application Support"
                else:
                    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
                if not root.is_absolute():
                    raise CredentialError("The VS Code configuration directory must be absolute; use --models-file.")
                candidates = [
                    root / folder / "User"
                    for folder in ("Code", "Code - Insiders") if (root / folder / "User").is_dir()
                ]
                if len(candidates) != 1:
                    raise CredentialError("Cannot select one desktop VS Code settings location; use --models-file.")
                user = candidates[0]
            profiles = user / "profiles"
            if profiles.is_dir() and any(item.is_dir() for item in profiles.iterdir()):
                raise CredentialError(
                    "VS Code desktop profiles found; use --models-file for the intended profile's model configuration."
                )
            path = user / _FILENAME
        resolved = path.resolve()
        for candidate in (path, resolved):
            if (
                candidate.name != _FILENAME or candidate.parent.name.casefold() == "machine"
                or any(
                    parent.name.casefold() in (".vscode", ".vscode-server", ".vscode-server-insiders")
                    for parent in candidate.parents
                )
            ):
                raise CredentialError(
                    "Choose a desktop profile's chatLanguageModels.json, not Workspace or Remote Machine settings."
                )
        if not resolved.is_file():
            raise CredentialError(
                "The model configuration file does not exist. Register Custom Endpoint in "
                "Chat: Manage Language Models first, then use --models-file with the file VS Code opens."
            )
        return resolved
    except (OSError, RuntimeError, ValueError):
        raise CredentialError(
            "Cannot resolve the desktop model configuration; use --models-file with an accessible chatLanguageModels.json."
        ) from None


class ModelsDocument(JsoncDocument):
    """Merge a connection into its existing Custom Endpoint group without changing native secret references."""

    def __init__(self, source: str) -> None:
        super().__init__(
            source, root_type=list, label="VS Code model configuration",
            root_error="VS Code model configuration must contain a JSON array of provider groups.",
        )
        if any(not isinstance(group.value, dict) for group in self._elements(self.root)):
            raise CredentialError("VS Code model provider groups must be JSON objects; no model settings were applied.")

    def _unique_members(self, node: Value) -> dict[str, Member]:
        members = self._members(node)
        if len({member.name for member in members}) != len(members):
            raise CredentialError("Duplicate properties in the matching Copilot group/model; repair them before applying.")
        return {member.name: member for member in members}

    def _merge_fields(self, node: Value, fields: dict[str, object]) -> list[tuple[int, int, str]]:
        members = self._unique_members(node)
        edits = []
        additions = []
        for name, value in fields.items():
            member = members.get(name)
            if member is None:
                additions.append((json.dumps(name) + ": ", value))
            elif type(member.value.value) is not type(value) or member.value.value != value:
                edits.append((member.value.start, member.value.end, json.dumps(value)))
        edits.extend(self._append(
            node, [member.start for member in members.values()],
            [member.value.end for member in members.values()], additions,
        ))
        return edits

    def merge(self, connection: CopilotConnection) -> str:
        """Return merged JSONC for an enabled connection's exact group name and model ID.

        Preserve other groups/models, custom fields and formatting. A native-looking apiKey
        reference is required but its stored secret cannot be verified here. Missing, ambiguous
        or malformed registrations raise CredentialError without exposing configuration values.
        This operation performs no I/O and never creates a provider or secret reference.
        """
        if not connection.enabled:
            raise CredentialError("Cannot apply a disabled Copilot connection; rerun setup first.")
        groups = [
            group for group in self._elements(self.root)
            if isinstance(group.value, dict)
            and group.value.get("name") == connection.group_name and group.value.get("vendor") == "customendpoint"
        ]
        if not groups:
            raise CredentialError(
                f"No matching Custom Endpoint group. In Chat: Manage Language Models, register "
                f"{json.dumps(connection.group_name)} and enter the local key from 'copilot key', "
                "then rerun the same setup with --apply. No provider or secret reference was created."
            )
        if len(groups) != 1:
            raise CredentialError("Multiple matching Custom Endpoint groups; repair duplicate registrations before applying.")
        group = groups[0]
        members = self._unique_members(group)
        secret = members.get("apiKey")
        if (
            secret is None or not isinstance(secret.value.value, str)
            or not _SECRET_REFERENCE.fullmatch(secret.value.value)
        ):
            raise CredentialError(
                "The matching group needs a native apiKey secret reference. Store its local key through "
                "the Custom Endpoint API-key prompt first; literal keys are not accepted or replaced."
            )
        generated = connection.model_settings()
        models = members.get("models")
        if models is None:
            edits = self._merge_fields(group, generated)
        else:
            if not isinstance(models.value.value, list):
                raise CredentialError("The matching Custom Endpoint models field must be an array; repair it before applying.")
            entries = self._elements(models.value)
            if any(not isinstance(model.value, dict) for model in entries):
                raise CredentialError("Custom Endpoint models must be JSON objects; repair them before applying.")
            expected = generated["models"][0]
            matching = [
                model for model in entries if isinstance(model.value, dict) and model.value.get("id") == connection.model
            ]
            if len(matching) > 1:
                raise CredentialError("Multiple matching Copilot model IDs; repair duplicate models before applying.")
            edits = self._merge_fields(group, {"apiType": generated["apiType"]})
            if matching:
                model = matching[0]
                if isinstance(model.value, dict) and "apiType" in model.value:
                    expected = {**expected, "apiType": generated["apiType"]}
                edits.extend(self._merge_fields(model, expected))
            else:
                edits.extend(self._append(
                    models.value, [model.start for model in entries], [model.end for model in entries], [("", expected)],
                ))
        return self._edited(edits)