import json
import re

from ..adapters import Api, Credential
from ..customers import Attribution, Tag, validate_customer
from ..errors import CredentialError
from ..profiles import Profile
from ..transport import JsonTransport

TAG_HEADER = "x-litellm-tags"
# LiteLLM stores this JSON with each spend log entry (litellm_pre_call_utils, spend_logs_metadata).
SPEND_METADATA_HEADER = "x-litellm-spend-logs-metadata"
# Tags LiteLLM adds by itself as f"{header}: {value}" (litellm_logging._get_user_agent_tags, extra_spend_tag_headers).
_AUTOMATIC_TAG = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+: ")


def _is_customer(name: object) -> bool:
    """A valid customer name that is not one of the tags LiteLLM adds by itself."""
    try:
        validate_customer(name)
    except CredentialError:
        return False
    return not _AUTOMATIC_TAG.match(name)


def list_tags(profile: Profile, token: str, *, transport: JsonTransport | None = None) -> list[Tag]:
    """Return sorted visible tags from this gateway; scope is server-defined and errors never become an empty catalogue."""
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
        # /tag/list returns spend tags, not customers: skip rows that cannot be one instead of failing the list.
        if not isinstance(row, dict):
            continue
        name, description = row.get("name"), row.get("description")
        if _is_customer(name) and name not in tags and (description is None or isinstance(description, str)):
            tags[name] = Tag(name, description)
    return [tag for _, tag in sorted(tags.items())]


class LiteLLMGateway:
    """A LiteLLM proxy: Anthropic and OpenAI APIs on one origin, customers as request tags."""

    def __init__(self, profile: Profile) -> None:
        self.profile = profile

    def endpoint(self, api: Api) -> str:
        if api is Api.ANTHROPIC_MESSAGES:
            return self.profile.gateway
        if api in (Api.OPENAI_CHAT, Api.OPENAI_RESPONSES):
            return self.profile.gateway + "/v1"
        raise CredentialError(f"The LiteLLM gateway does not offer the {api} API.")

    def attribution_headers(self, attribution: Attribution) -> dict[str, str]:
        """The customer as a request tag, plus where it came from as spend-log metadata for later re-attribution."""
        metadata = {"customer_source": attribution.source}
        if attribution.repository is not None:
            metadata["repository"] = attribution.repository
        return {
            TAG_HEADER: attribution.customer,
            SPEND_METADATA_HEADER: json.dumps(metadata, separators=(",", ":"), sort_keys=True),
        }

    def attribution_header_names(self) -> tuple[str, ...]:
        return (TAG_HEADER, SPEND_METADATA_HEADER)

    def visible_customers(self, credential: Credential) -> list[Tag] | None:
        return list_tags(self.profile, credential.secret)
