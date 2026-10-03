from dataclasses import dataclass

from .errors import CredentialError
from .profiles import Profile
from .transport import JsonTransport

TAG_HEADER = "x-litellm-tags"


def validate_tag(value: str) -> str:
    """Validate one exact tag for the CSV header; reject empty, padded, non-ASCII or comma-containing values."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or value != value.strip()
        or "," in value
        or any(not 32 <= ord(char) <= 126 for char in value)
    ):
        raise CredentialError(
            "Customer tag must be 1-256 printable ASCII characters, without commas or surrounding whitespace. "
            "Use the exact gateway tag name; no prefix is added."
        )
    return value


@dataclass(frozen=True)
class Tag:
    """A visible gateway tag, not an assertion that its caller may charge the customer."""

    name: str
    description: str | None = None


def list_tags(
    profile: Profile, token: str, prefix: str | None = None, *, transport: JsonTransport | None = None,
) -> list[Tag]:
    """Return sorted visible tags from this gateway; scope is server-defined and errors never become an empty catalogue."""
    if prefix is not None:
        validate_tag(prefix)
    try:
        rows = (transport or JsonTransport()).get_list(
            profile.gateway + "/tag/list", headers={"Authorization": "Bearer " + token},
        )
    except CredentialError as error:
        raise CredentialError(
            f"Cannot read the LiteLLM tag catalogue: {error} "
            "Ask the gateway administrator for tag-list access, or use --customer with an approved tag."
        ) from None
    tags: dict[str, Tag] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise CredentialError("Gateway returned an invalid tag catalogue entry.")
        name, description = row.get("name"), row.get("description")
        if (
            not isinstance(name, str) or not name or len(name) > 256
            or (description is not None and not isinstance(description, str))
            or name in tags
        ):
            raise CredentialError("Gateway returned an invalid or duplicate tag catalogue entry.")
        tags[name] = Tag(name, description)
    return [tag for name, tag in sorted(tags.items()) if prefix is None or name.startswith(prefix)]
