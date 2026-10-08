from dataclasses import dataclass

from .errors import CredentialError


def validate_customer(value: str) -> str:
    """Validate one exact customer name; reject empty, padded, non-ASCII or comma-containing values.

    The rules are strict enough for any gateway's request header, so no gateway needs its own.
    """
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or value != value.strip()
        or "," in value
        or any(not 32 <= ord(char) <= 126 for char in value)
    ):
        raise CredentialError(
            "Customer must be 1-256 printable ASCII characters, without commas or surrounding whitespace. "
            "Use the exact name the gateway expects; no prefix is added."
        )
    return value


@dataclass(frozen=True)
class Tag:
    """A customer offered by a catalogue, not an assertion that its caller may charge the customer."""

    name: str
    description: str | None = None


@dataclass(frozen=True)
class Attribution:
    """The customer chosen for a request and why; ``repository`` is the normalized Git identity, if any.

    Gateway adapters turn an attribution into request headers; nothing else knows their names.
    """

    customer: str
    source: str
    detail: str
    repository: str | None = None

    def __post_init__(self) -> None:
        validate_customer(self.customer)
