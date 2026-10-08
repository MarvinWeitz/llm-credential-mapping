import json

from ..adapters import Api, Credential
from ..customers import Attribution, Tag
from ..errors import CredentialError
from ..profiles import Profile
from ..transport import JsonTransport

TAG_HEADER = "x-litellm-tags"
# LiteLLM stores this JSON with each spend log entry (litellm_pre_call_utils, spend_logs_metadata).
SPEND_METADATA_HEADER = "x-litellm-spend-logs-metadata"


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
