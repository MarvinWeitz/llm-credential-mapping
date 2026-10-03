import ctypes
import os
import queue
import subprocess
import sys
import threading
import unittest
from ctypes import wintypes


@unittest.skipUnless(os.name == "nt", "Windows process-lifecycle tests")
class WindowsProcessTests(unittest.TestCase):
    def test_stdio_and_exit_status_are_preserved(self):
        program = (
            "import os, sys; from llm_credential_mapping.windows_process import execute; "
            "sys.exit(execute([sys.executable, '-c', 'import sys; print(sys.stdin.readline().strip()); sys.exit(7)'], dict(os.environ)))"
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", program], input="protocol-line\n", capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout, "protocol-line\n")

    def test_terminating_wrapper_process_does_not_leave_claude_child_running(self):
        program = (
            "import os, sys; from llm_credential_mapping.windows_process import execute; "
            "sys.exit(execute([sys.executable, '-c', 'import os,time;print(os.getpid(),flush=True);time.sleep(60)'], dict(os.environ)))"
        )
        supervisor = subprocess.Popen(
            [sys.executable, "-I", "-c", program], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        output = queue.Queue()
        reader = threading.Thread(target=lambda: output.put(supervisor.stdout.readline()), daemon=True)
        reader.start()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        child = None
        try:
            pid = int(output.get(timeout=10).strip())
            child = kernel.OpenProcess(0x00100000, False, pid)
            self.assertTrue(child, "Claude fixture child was not running.")
            supervisor.kill()
            supervisor.wait(timeout=10)
            self.assertEqual(kernel.WaitForSingleObject(child, 5000), 0, "Child survived wrapper termination.")
        finally:
            if supervisor.poll() is None:
                supervisor.kill()
                supervisor.wait(timeout=10)
            if child:
                kernel.CloseHandle(child)
            reader.join(timeout=5)
            supervisor.stdout.close()
            supervisor.stderr.close()
