import ctypes
import os
import subprocess
import threading
from ctypes import wintypes

from .errors import CredentialError


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
        ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t), ("maximum", ctypes.c_size_t),
        ("active_processes", wintypes.DWORD), ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits), ("io", ctypes.c_ulonglong * 6),
        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t),
    ]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
        ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
        ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
        ("name", wintypes.WCHAR * 260),
    ]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("tid", wintypes.DWORD),
        ("owner", wintypes.DWORD), ("priority", wintypes.LONG), ("delta", wintypes.LONG),
        ("flags", wintypes.DWORD),
    ]


def execute(command: list[str], env: dict[str, str]) -> int:
    """Run a Windows stdio child in a kill-on-close job; also stop it if the console-entrypoint launcher is terminated."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
        "Process32FirstW": ([wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)], wintypes.BOOL),
        "Process32NextW": ([wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)], wintypes.BOOL),
        "Thread32First": ([wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL),
        "Thread32Next": ([wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL),
        "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
        "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result

    def failure(operation: str) -> CredentialError:
        return CredentialError(f"Cannot supervise Claude's Windows process ({operation}, error {ctypes.get_last_error()}).")

    def entries(flag, entry_type, first, next_entry):
        snapshot = kernel.CreateToolhelp32Snapshot(flag, 0)
        if snapshot == ctypes.c_void_p(-1).value:
            raise failure("process snapshot")
        try:
            entry = entry_type()
            entry.size = ctypes.sizeof(entry)
            present = first(snapshot, ctypes.byref(entry))
            while present:
                yield entry
                present = next_entry(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise failure("process enumeration")
        finally:
            kernel.CloseHandle(snapshot)

    processes = {
        entry.pid: (entry.parent, entry.name)
        for entry in entries(2, _ProcessEntry, kernel.Process32FirstW, kernel.Process32NextW)
    }
    parent_id = processes.get(os.getpid(), (None, ""))[0]
    parent = None
    if parent_id is not None and processes.get(parent_id, (None, ""))[1].lower() == "llm-credentials-claude.exe":
        parent = kernel.OpenProcess(0x00100000, False, parent_id)
        if not parent:
            raise failure("launcher supervision")
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        if parent:
            kernel.CloseHandle(parent)
        raise failure("job creation")
    child = None
    watcher = None
    finished = threading.Event()
    watcher_failed = threading.Event()
    try:
        limits = _ExtendedLimits()
        limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise failure("job limits")
        # Assign the child before it can create any descendants.
        child = subprocess.Popen(command, env=env, creationflags=0x00000004)  # CREATE_SUSPENDED
        process = kernel.OpenProcess(0x0101, False, child.pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if not process:
            raise failure("child process handle")
        try:
            if not kernel.AssignProcessToJobObject(job, process):
                raise failure("job assignment")
        finally:
            kernel.CloseHandle(process)

        resumed = False
        for entry in entries(4, _ThreadEntry, kernel.Thread32First, kernel.Thread32Next):
            if entry.owner != child.pid:
                continue
            thread_handle = kernel.OpenThread(0x0002, False, entry.tid)  # THREAD_SUSPEND_RESUME
            if not thread_handle:
                raise failure("child thread handle")
            try:
                if kernel.ResumeThread(thread_handle) == 0xFFFFFFFF:
                    raise failure("child start")
                resumed = True
            finally:
                kernel.CloseHandle(thread_handle)
        if not resumed:
            raise failure("child thread lookup")

        if parent:
            def watch_launcher():
                while not finished.is_set():
                    state = kernel.WaitForSingleObject(parent, 100)
                    if state == 258:  # WAIT_TIMEOUT
                        continue
                    if state != 0:
                        watcher_failed.set()
                    if not kernel.TerminateJobObject(job, 1):
                        watcher_failed.set()
                    return

            watcher = threading.Thread(target=watch_launcher, daemon=True)
            watcher.start()
        result = child.wait()
        if watcher_failed.is_set():
            raise CredentialError("Claude's Windows launcher supervision failed.")
        return result
    finally:
        finished.set()
        if watcher:
            watcher.join()
        if child is not None and child.poll() is None:
            kernel.TerminateJobObject(job, 1)
            child.kill()
            child.wait()
        kernel.CloseHandle(job)
        if parent:
            kernel.CloseHandle(parent)
