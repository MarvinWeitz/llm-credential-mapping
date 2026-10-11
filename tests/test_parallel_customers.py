import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from llm_credential_mapping.adapters import IntegrationContext
from llm_credential_mapping.customers import Attribution
from llm_credential_mapping.integrations.claude import Claude
from llm_credential_mapping.profiles import Profile
from llm_credential_mapping.registry import gateway_for

# Stands in for Claude Code: settings come from --settings, the credential from the apiKeyHelper command.
FAKE_CLAUDE = """
import json, subprocess, sys
from urllib.request import Request, urlopen
settings = json.loads(sys.argv[sys.argv.index("--settings") + 1])
token = subprocess.run(settings["apiKeyHelper"], shell=True, capture_output=True, text=True, check=True).stdout.strip()
headers = dict(line.split(": ", 1) for line in settings["env"]["ANTHROPIC_CUSTOM_HEADERS"].splitlines())
headers["Authorization"] = "Bearer " + token
base = settings["env"]["ANTHROPIC_BASE_URL"]
for path in ("/first", "/second"):
    urlopen(Request(base + path, headers=headers), timeout=15).close()
"""


class ParallelCustomerTests(unittest.TestCase):
    def test_two_real_child_processes_keep_tags_after_the_other_session_starts(self):
        calls = []
        first_a = threading.Event()
        finished_b = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                customer = self.headers.get("x-litellm-tags")
                if customer == "customer:A" and self.path == "/second":
                    if not finished_b.wait(10):
                        self.send_error(504)
                        return
                calls.append((customer, self.path, self.headers.get("Authorization")))
                if customer == "customer:A" and self.path == "/first":
                    first_a.set()
                if customer == "customer:B" and self.path == "/second":
                    finished_b.set()
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        profile = Profile("unit", f"http://127.0.0.1:{server.server_port}", {"team_id": "business-unit"}, allow_http=True)
        # The helper prints a fixed token, standing in for 'llm-credentials token'.
        context = IntegrationContext(
            profile, gateway_for(profile), "unused", cli=(sys.executable, "-c", "print('shared-token')"),
        )
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_claude.py"
            script.write_text(FAKE_CLAUDE, encoding="utf-8")

            def run(name, command, env):
                return subprocess.run([sys.executable, str(script), *command[1:]], env=env, check=False).returncode

            def launch(customer):
                return Claude().launch(context, Attribution(customer, "explicit", "--customer"), [], None)

            try:
                with patch("llm_credential_mapping.integrations.claude.run", run), \
                        ThreadPoolExecutor(max_workers=2) as executor:
                    a = executor.submit(launch, "customer:A")
                    self.assertTrue(first_a.wait(20), "First process did not reach the fixture.")
                    b = executor.submit(launch, "customer:B")
                    self.assertEqual(b.result(timeout=30), 0)
                    self.assertEqual(a.result(timeout=30), 0)
                self.assertEqual(calls, [
                    ("customer:A", "/first", "Bearer shared-token"),
                    ("customer:B", "/first", "Bearer shared-token"),
                    ("customer:B", "/second", "Bearer shared-token"),
                    ("customer:A", "/second", "Bearer shared-token"),
                ])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
