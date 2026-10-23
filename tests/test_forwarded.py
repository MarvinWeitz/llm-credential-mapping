import contextlib
import io
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from virtual_key_mapper.auth import forwarded
from virtual_key_mapper.cli import main
from virtual_key_mapper.errors import CredentialError
from virtual_key_mapper.profiles import Profile


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
            ({"ok": False, "error": "No login for 'work'; run 'vkm login --profile work'."}, "No login"),
            (b"not json\n", "unreadable"),
            (b"[" * 100000 + b"\n", "unreadable"),  # Too deep for the parser: still a clear error.
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
        self.assertIn("cbs-consulting.vkm-host", str(error.exception))
        self.assertEqual(auth.status()["state"], "unavailable")

    def test_sign_in_and_out_happen_on_the_local_machine(self):
        auth = forwarded.Forwarded(self.profile, None)
        for action in (lambda: auth.login(print), lambda: auth.logout(print)):
            with self.assertRaises(CredentialError) as error:
                action()
            self.assertIn("local machine", str(error.exception))

    def test_socket_location(self):
        self.assertEqual(forwarded.agent_path({"VKM_AGENT": "/run/x.sock"}), "/run/x.sock")
        self.assertEqual(
            Path(forwarded.agent_path({"XDG_RUNTIME_DIR": "/run/user/1000"})), Path("/run/user/1000/vkm/agent.sock"),
        )
        self.assertTrue(forwarded.agent_path({}).endswith("agent.sock"))

    @unittest.skipUnless(hasattr(socket, "AF_UNIX") and hasattr(os, "getuid"), "Unix sockets only")
    def test_a_socket_others_could_have_placed_is_refused(self):
        directory = tempfile.mkdtemp(prefix="vkm-")
        self.addCleanup(shutil.rmtree, directory, True)
        path = os.path.join(directory, "agent.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(path)
        listener.listen()
        forwarded._connect(path).close()  # Ours, in a private directory.
        os.chmod(directory, 0o777)
        with self.assertRaises(CredentialError) as error:
            forwarded._connect(path)
        self.assertIn("Refusing", str(error.exception))
        os.chmod(directory, 0o700)
        if os.getuid() == 0:  # Only root can hand the socket to another user.
            os.chown(path, 65534, 65534)
            with self.assertRaises(CredentialError):
                forwarded._connect(path)
            os.chown(path, 0, 0)
        os.symlink(directory, directory + "-link")
        self.addCleanup(os.unlink, directory + "-link")
        with self.assertRaises(CredentialError):
            forwarded._connect(os.path.join(directory + "-link", "agent.sock"))
        os.unlink(path)
        with open(path, "w"):
            pass  # Not a socket.
        with self.assertRaises(CredentialError):
            forwarded._connect(path)

    def test_settings(self):
        self.assertEqual(forwarded.settings({}), {})
        for values in ({"host_profile": "../x"}, {"team_id": "t"}):
            with self.subTest(values=values), self.assertRaises(CredentialError):
                forwarded.settings(values)


DEFINITIONS = {
    "labels": [{"key": "customer", "required": True, "values": [{"id": "acme", "name": "Acme"}], "strict": True}],
    "outputs": {"tags": {"format": ["${customer}"]}},
}


class ForwardedCliTests(unittest.TestCase):
    def test_a_container_profile_works_without_an_os_vault(self):
        with tempfile.TemporaryDirectory() as home:
            agent = FakeAgent(TOKEN)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()), \
                    patch("virtual_key_mapper.cli.NativeKeyring", side_effect=AssertionError("no vault here")), \
                    patch.object(forwarded, "_connect", agent.connect), \
                    patch.dict(os.environ, {"VKM_AGENT": "/tmp/agent.sock"}):
                # As the VS Code extension creates it: the local profile's own definitions in the same call.
                self.assertEqual(main([
                    "--home", home, "profile", "add", "container", "--gateway", "https://gateway.example",
                    "--auth", "forwarded", "--host-profile", "work", f"--definitions={json.dumps(DEFINITIONS)}",
                ]), 0)
                stdout.truncate(0)
                stdout.seek(0)
                self.assertEqual(main(["--home", home, "token"]), 0)
            self.assertEqual(stdout.getvalue(), "access-from-host\n")
            self.assertEqual(agent.paths, ["/tmp/agent.sock"])
            saved = json.loads((Path(home) / "profiles.json").read_text(encoding="utf-8"))
            container = saved["profiles"]["container"]
            self.assertEqual(container["auth"], {"type": "forwarded", "host_profile": "work"})
            self.assertEqual((container["labels"], container["outputs"]), (DEFINITIONS["labels"], DEFINITIONS["outputs"]))
            self.assertEqual(agent.requests, [{"version": 1, "operation": "token", "profile": "work"}], "labels never travel")
