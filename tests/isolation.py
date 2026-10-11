"""Point every tool's user configuration at temporary folders, so tests never touch the real ones."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TOOL_CONFIGURATION = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME")


def isolate_tool_configuration(test: unittest.TestCase) -> Path:
    """Patch the tools' configuration locations for one test and return the temporary root."""
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    root = Path(temp.name).resolve()
    environment = patch.dict(os.environ, {name: str(root / name.lower()) for name in TOOL_CONFIGURATION})
    environment.start()
    test.addCleanup(environment.stop)
    return root
