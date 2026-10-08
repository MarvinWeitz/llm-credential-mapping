"""Keep vendor knowledge in adapters: the core and the integrations must not depend on a specific gateway."""

import ast
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "llm_credential_mapping"
ADAPTER_PACKAGES = ("auth", "gateways")
# The legacy Copilot bridge uses LiteLLM directly; deleted in plan step 7.
LEGACY = {"copilot.py", "copilot_bridge.py", "request_context.py"}

VENDOR_STRINGS = ("x-litellm", "/tag/list", "/v1", "litellm-cli-auth")


def neutral_modules():
    """Core and integration modules, i.e. everything outside the adapter packages and legacy modules."""
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE)
        if relative.parts[0] in ADAPTER_PACKAGES or (len(relative.parts) == 1 and relative.name in LEGACY):
            continue
        yield relative, ast.parse(path.read_text(encoding="utf-8"))


def imported_modules(relative: Path, tree: ast.Module):
    """Package-relative module paths imported by a module, e.g. ('auth', 'litellm_sso')."""
    package = relative.parts[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[:len(package) - node.level + 1]
                module = node.module.split(".") if node.module else []
                for alias in node.names if not module else [None]:
                    yield (*base, *module, *([alias.name] if alias else []))
            elif node.module and node.module.startswith("llm_credential_mapping."):
                yield tuple(node.module.split(".")[1:])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("llm_credential_mapping."):
                    yield tuple(alias.name.split(".")[1:])


class ArchitectureTests(unittest.TestCase):
    def test_only_the_registry_imports_auth_and_gateway_adapters(self):
        checked = 0
        for relative, tree in neutral_modules():
            if relative.name == "registry.py":
                continue
            checked += 1
            for module in imported_modules(relative, tree):
                with self.subTest(module=str(relative), imports=".".join(module)):
                    self.assertNotIn(module[0], ADAPTER_PACKAGES)
        self.assertGreater(checked, 5)

    def test_vendor_header_names_and_url_paths_stay_in_adapters(self):
        for relative, tree in neutral_modules():
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for vendor in VENDOR_STRINGS:
                        with self.subTest(module=str(relative), string=vendor):
                            self.assertNotIn(vendor, node.value.lower())

    def test_the_guard_recognizes_adapter_imports(self):
        tree = ast.parse(
            "from ..auth.litellm_sso import SessionManager\nfrom .. import gateways\n"
            "import llm_credential_mapping.auth.litellm_oauth\nfrom .common import run\n"
        )
        self.assertEqual(
            [module[0] for module in imported_modules(Path("integrations") / "x.py", tree)],
            ["auth", "gateways", "auth", "integrations"],
        )
