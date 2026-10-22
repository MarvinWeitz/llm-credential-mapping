import re
from typing import Callable

from ..adapters import Api, Credential
from ..errors import CredentialError
from ..labels import JSON, LIST, MAX_ENTRY, Choice, LabelConfig, Output, validate_value
from ..profiles import Profile
from ..transport import JsonTransport

TAG_HEADER = "x-litellm-tags"
# LiteLLM stores this JSON with each spend log entry (litellm_pre_call_utils, spend_logs_metadata).
SPEND_METADATA_HEADER = "x-litellm-spend-logs-metadata"
# Tags LiteLLM adds by itself (litellm_logging._get_user_agent_tags); extra_spend_tag_headers must stay
# empty (docs/architecture.md, Gateway requirements), so other "Name: value" tags remain valid.
_AUTOMATIC_TAG = re.compile(r"User-Agent: ", re.IGNORECASE)
# The tag LiteLLM's tag routing sends untagged requests to.
_ROUTING_TAG = "default"


def _reserved(tag: str) -> bool:
    return bool(_AUTOMATIC_TAG.match(tag)) or tag == _ROUTING_TAG


OUTPUTS = (
    Output("tags", LIST, TAG_HEADER, lambda keys: tuple(f"{key}:${{{key}}}" for key in keys), _reserved),
    Output("metadata", JSON, SPEND_METADATA_HEADER),
)


def _is_tag(name: object) -> bool:
    """A tag vkm could have sent: valid wire text and not one LiteLLM gives another meaning to."""
    return (
        isinstance(name, str) and 0 < len(name) <= MAX_ENTRY and name == name.strip() and "," not in name
        and name[0] not in "!&" and all(32 <= ord(char) <= 126 for char in name) and not _reserved(name)
    )


def list_tags(profile: Profile, token: str, *, transport: JsonTransport | None = None) -> list[Choice]:
    """Return sorted visible tags from this gateway; scope is server-defined and errors never become an empty catalogue."""
    try:
        rows = (transport or JsonTransport()).get_list(
            profile.gateway + "/tag/list", headers={"Authorization": "Bearer " + token},
        )
    except CredentialError as error:
        raise CredentialError(
            f"Cannot read the LiteLLM tag catalogue: {error} "
            "Ask the gateway administrator for tag-list access, or use --label KEY=VALUE with an approved value."
        ) from None
    tags: dict[str, Choice] = {}
    for row in rows:
        # /tag/list returns all spend tags: skip rows vkm could not have sent instead of failing the list.
        if not isinstance(row, dict):
            continue
        name, description = row.get("name"), row.get("description")
        if _is_tag(name) and name not in tags and (description is None or isinstance(description, str)):
            tags[name] = Choice(name, description=description)
    return [tag for _, tag in sorted(tags.items())]


class LiteLLMGateway:
    """A LiteLLM proxy: Anthropic and OpenAI APIs on one origin, labels as request tags and spend-log metadata."""

    def __init__(self, profile: Profile) -> None:
        self.profile = profile

    def endpoint(self, api: Api) -> str:
        if api is Api.ANTHROPIC_MESSAGES:
            return self.profile.gateway
        if api in (Api.OPENAI_CHAT, Api.OPENAI_RESPONSES):
            return self.profile.gateway + "/v1"
        raise CredentialError(f"The LiteLLM gateway does not offer the {api} API.")

    @staticmethod
    def outputs() -> tuple[Output, ...]:
        return OUTPUTS

    def catalogue(self, key: str, credential: Callable[[], Credential], config: LabelConfig) -> list[Choice] | None:
        """Values for ``key`` from /tag/list, through each tags entry of the form PREFIX${key}SUFFIX; else no sign-in."""
        inversions = config.inversions("tags", key)
        if not inversions:
            return None
        found: dict[str, Choice] = {}
        for tag in list_tags(self.profile, credential().secret):
            for prefix, suffix in inversions:
                if len(tag.id) > len(prefix) + len(suffix) and tag.id.startswith(prefix) and tag.id.endswith(suffix):
                    value = tag.id[len(prefix):len(tag.id) - len(suffix)]
                    try:
                        validate_value(value, key)
                    except CredentialError:
                        continue
                    found.setdefault(value, Choice(value, description=tag.description))
        return [choice for _, choice in sorted(found.items())]
