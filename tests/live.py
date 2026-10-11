"""Opt-in tests against real tools and shells.

Locally these tests skip when a tool is missing. CI lists what must be present in
LLM_CREDENTIAL_MAPPING_REQUIRE (comma-separated: claude, codex, opencode, bash, zsh, fish, pwsh),
so a missing tool fails there instead of passing silently as a skip.
"""

import os
import unittest

REQUIRE_VARIABLE = "LLM_CREDENTIAL_MAPPING_REQUIRE"


def required(name: str) -> bool:
    return name in {item.strip() for item in os.environ.get(REQUIRE_VARIABLE, "").split(",") if item.strip()}


def unavailable(test: unittest.TestCase, name: str, reason: str) -> None:
    """Skip the test, or fail it when CI requires ``name``."""
    if required(name):
        test.fail(f"{reason}, but {REQUIRE_VARIABLE} requires {name}.")
    test.skipTest(reason)


def binary_tests(name: str, variable: str):
    """Class decorator: run the tests when ``variable`` names the tool's binary, else skip (or fail when required)."""
    def decorate(cls):
        if os.environ.get(variable):
            return cls
        if not required(name):
            return unittest.skip(f"Set {variable}")(cls)
        original = cls.setUp

        def setUp(self):
            unavailable(self, name, f"{variable} is not set")
            original(self)

        cls.setUp = setUp
        return cls
    return decorate
