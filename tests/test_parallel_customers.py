import os
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from llm_credential_mapping.agents import launch
from llm_credential_mapping.profiles import Profile


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
        origin = f"http://127.0.0.1:{server.server_port}"
        profile = Profile("unit", origin, "business-unit", True)
        script = (
            "import os; from urllib.request import Request, urlopen; "
            "headers = dict(line.split(': ', 1) for line in os.environ['ANTHROPIC_CUSTOM_HEADERS'].splitlines()); "
            "headers['Authorization'] = 'Bearer ' + os.environ['ANTHROPIC_AUTH_TOKEN']; "
            "base = os.environ['ANTHROPIC_BASE_URL']; "
            "urlopen(Request(base + '/first', headers=headers), timeout=15).close(); "
            "urlopen(Request(base + '/second', headers=headers), timeout=15).close()"
        )
        try:
            with patch("llm_credential_mapping.agents.shutil.which", return_value=sys.executable), \
                    patch.dict(os.environ, {"ANTHROPIC_CUSTOM_HEADERS": "x-litellm-tags: parent"}), \
                    ThreadPoolExecutor(max_workers=2) as executor:
                a = executor.submit(launch, "claude", profile, "shared-token", ["-c", script], None, "customer:A")
                self.assertTrue(first_a.wait(10), "First process did not reach the fixture.")
                b = executor.submit(launch, "claude", profile, "shared-token", ["-c", script], None, "customer:B")
                self.assertEqual(b.result(timeout=20), 0)
                self.assertEqual(a.result(timeout=20), 0)
                self.assertEqual(os.environ["ANTHROPIC_CUSTOM_HEADERS"], "x-litellm-tags: parent")
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
