import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from virtual_key_mapper.adapters import IntegrationContext
from virtual_key_mapper.integrations.claude import Claude
from virtual_key_mapper.labels import LAUNCH_VARIABLE, Attribution, Label, LabelDefinition
from virtual_key_mapper.profiles import Profile
from virtual_key_mapper.registry import gateway_for

# Stands in for Claude Code: settings come from --settings, the credential from the apiKeyHelper command.
FAKE_CLAUDE = """
import json, os, subprocess, sys
from urllib.request import Request, urlopen
settings = json.loads(sys.argv[sys.argv.index("--settings") + 1])
token = subprocess.run(settings["apiKeyHelper"], shell=True, capture_output=True, text=True, check=True).stdout.strip()
headers = dict(line.split(": ", 1) for line in settings["env"]["ANTHROPIC_CUSTOM_HEADERS"].splitlines())
headers["Authorization"] = "Bearer " + token
base = settings["env"]["ANTHROPIC_BASE_URL"]
headers["X-Launch"] = os.environ.get("VKM_LAUNCH_LABELS", "")
for path in ("/first", "/second"):
    urlopen(Request(base + path, headers=headers), timeout=15).close()
"""


A = "customer:acme,project:web"
B = "customer:globex"


class ParallelLabelTests(unittest.TestCase):
    def test_two_real_child_processes_keep_tags_after_the_other_session_starts(self):
        calls = []
        first_a = threading.Event()
        finished_b = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                tags = self.headers.get("x-litellm-tags")
                if tags == A and self.path == "/second":
                    if not finished_b.wait(10):
                        self.send_error(504)
                        return
                labels = json.loads(self.headers.get("x-litellm-spend-logs-metadata"))["labels"]
                calls.append((tags, labels, json.loads(self.headers.get("X-Launch")), self.path, self.headers.get("Authorization")))
                if tags == A and self.path == "/first":
                    first_a.set()
                if tags == B and self.path == "/second":
                    finished_b.set()
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        profile = Profile(
            "unit", f"http://127.0.0.1:{server.server_port}", {"team_id": "business-unit"}, allow_http=True,
            labels=(LabelDefinition("customer", required=True), LabelDefinition("project")),
        )
        # The helper prints a fixed token, standing in for 'vkm token'.
        context = IntegrationContext(
            profile, gateway_for(profile), "unused", cli=(sys.executable, "-c", "print('shared-token')"),
        )
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake_claude.py"
            script.write_text(FAKE_CLAUDE, encoding="utf-8")

            def run(name, command, env):
                return subprocess.run([sys.executable, str(script), *command[1:]], env=env, check=False).returncode

            def launch(**values):
                labels = tuple(Label(key, values.get(key), "explicit" if key in values else None,
                                     "--label" if key in values else None) for key in ("customer", "project"))
                return Claude().launch(context, Attribution(labels), [], None)

            try:
                with patch("virtual_key_mapper.integrations.claude.run", run), \
                        patch.dict(os.environ, {LAUNCH_VARIABLE: '{"customer":"parent"}'}), \
                        ThreadPoolExecutor(max_workers=2) as executor:
                    a = executor.submit(launch, customer="acme", project="web")
                    self.assertTrue(first_a.wait(20), "First process did not reach the fixture.")
                    b = executor.submit(launch, customer="globex")
                    self.assertEqual(b.result(timeout=30), 0)
                    self.assertEqual(a.result(timeout=30), 0)
                a_labels, b_labels = {"customer": "acme", "project": "web"}, {"customer": "globex"}
                self.assertEqual(calls, [
                    (A, a_labels, a_labels, "/first", "Bearer shared-token"),
                    (B, b_labels, b_labels, "/first", "Bearer shared-token"),
                    (B, b_labels, b_labels, "/second", "Bearer shared-token"),
                    (A, a_labels, a_labels, "/second", "Bearer shared-token"),
                ])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
