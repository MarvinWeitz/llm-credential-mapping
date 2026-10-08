import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_credential_mapping.auth import forwarded
from llm_credential_mapping.cli import main
from llm_credential_mapping.errors import CredentialError
from llm_credential_mapping.profiles import Profile


class FakeAgent:
    """Answers one connection per call like the VS Code extension; records each request."""

    def __init__(self, answer):
        self.answer = answer
        self.requests = []
        self.paths = []

    def connect(self, path):
        self.paths.append(path)
        client, server = socket.socketpair()

        def serve():
            with server:
                raw = b""
                while not raw.endswith(b"\n"):
                    raw += server.recv(65536)
                self.requests.append(json.loads(raw))
                answer = self.answer if isinstance(self.answer, bytes) else json.dumps(self.answer).encode() + b"\n"
                server.sendall(answer)

        threading.Thread(target=serve, daemon=True).start()
        return client


TOKEN = {"ok": True, "result": {"access_token": "access-from-host", "expires_at": 1234.5, "profile": "work", "user": "employee"}}


class ForwardedTests(unittest.TestCase):
    def setUp(self):
        self.profile = Profile("container", "https://gateway.example", {"host_profile": "work"}, auth_type="forwarded")

    def test_credentials_come_from_the_local_sign_in(self):
        agent = FakeAgent(TOKEN)
        auth = forwarded.Forwarded(self.profile, None, connect=agent.connect)
        credential = auth.credential()
        self.assertEqual((credential.secret, credential.expires_at, credential.user), ("access-from-host", 1234.5, "employee"))
        self.assertEqual(agent.requests, [{"version": 1, "operation": "token", "profile": "work"}])
        self.assertEqual(auth.status()["state"], "ready")

    def test_local_errors_and_invalid_answers_are_reported(self):
        for answer, expected in (
            ({"ok": False, "error": "No login for 'work'; run 'llm-credentials login --profile work'."}, "No login"),
            (b"not json\n", "unreadable"),
            ({"ok": True}, "unexpected"),
            ({"ok": True, "result": {"access_token": "with space"}}, "invalid token"),
            ({"ok": True, "result": {"access_token": "t", "expires_at": "soon"}}, "invalid token"),
        ):
            with self.subTest(answer=answer), self.assertRaises(CredentialError) as error:
                forwarded.Forwarded(self.profile, None, connect=FakeAgent(answer).connect).credential()
            self.assertIn(expected, str(error.exception))

    def test_without_the_vs_code_relay_the_error_explains_what_to_install(self):
        def refused(path):
            raise FileNotFoundError(path)

        auth = forwarded.Forwarded(self.profile, None, connect=refused)
        with self.assertRaises(CredentialError) as error:
            auth.credential()
        self.assertIn("local sign-in", str(error.exception))
        self.assertEqual(auth.status()["state"], "unavailable")

    def test_sign_in_and_out_happen_on_the_local_machine(self):
        auth = forwarded.Forwarded(self.profile, None)
        for action in (lambda: auth.login(print), auth.logout):
            with self.assertRaises(CredentialError) as error:
                action()
            self.assertIn("local machine", str(error.exception))

    def test_socket_location(self):
        self.assertEqual(forwarded.agent_path({"LLM_CREDENTIALS_AGENT": "/run/x.sock"}), "/run/x.sock")
        self.assertEqual(
            Path(forwarded.agent_path({"XDG_RUNTIME_DIR": "/run/user/1000"})), Path("/run/user/1000/llm-credentials/agent.sock"),
        )
        self.assertTrue(forwarded.agent_path({}).endswith("agent.sock"))

    def test_settings(self):
        self.assertEqual(forwarded.settings({}), {})
        for values in ({"host_profile": "../x"}, {"team_id": "t"}):
            with self.subTest(values=values), self.assertRaises(CredentialError):
                forwarded.settings(values)


class ForwardedCliTests(unittest.TestCase):
    def test_a_container_profile_works_without_an_os_vault(self):
        with tempfile.TemporaryDirectory() as home:
            agent = FakeAgent(TOKEN)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()), \
                    patch("llm_credential_mapping.cli.NativeKeyring", side_effect=AssertionError("no vault here")), \
                    patch.object(forwarded, "_connect", agent.connect), \
                    patch.dict(os.environ, {"LLM_CREDENTIALS_AGENT": "/tmp/agent.sock"}):
                self.assertEqual(main([
                    "--home", home, "profile", "add", "container", "--gateway", "https://gateway.example",
                    "--auth", "forwarded", "--host-profile", "work",
                ]), 0)
                stdout.truncate(0)
                stdout.seek(0)
                self.assertEqual(main(["--home", home, "token"]), 0)
            self.assertEqual(stdout.getvalue(), "access-from-host\n")
            self.assertEqual(agent.paths, ["/tmp/agent.sock"])
            saved = json.loads((Path(home) / "profiles.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["profiles"]["container"]["auth"], {"type": "forwarded", "host_profile": "work"})
