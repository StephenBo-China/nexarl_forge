"""Explicit native platform primitives; never patches os or emulates dir_fd.

Path validation rejects reparse points, but is not a kernel pathname sandbox:
callers must keep writable roots private to the current user.
"""
from __future__ import annotations

import contextlib
import errno
import os
import pathlib
import stat
import tempfile
import time

IS_WINDOWS = os.name == "nt"
if IS_WINDOWS:
    import msvcrt
else:
    import fcntl


def validate_relative_components(parts, *, allow_glob=False):
    for part in parts:
        stem = part.split(".", 1)[0].upper()
        if (not part or part in {".", ".."} or part.rstrip(" .") != part
                or any(character in part for character in ('<>:"|' if allow_glob else '<>:"|?*'))
                or any(ord(character) < 32 for character in part)
                or stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                or stem in {f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)}):
            raise ValueError("unsafe Windows path component")


def validate_path(path: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(os.path.abspath(path))
    for entry in (*reversed(path.parents), path):
        try:
            metadata = entry.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"refusing linked or reparse-point path: {entry}")
    return path


def set_file_mode(descriptor: int, mode: int) -> None:
    """POSIX mode only. Windows privacy must be enforced by the root ACL."""
    if not IS_WINDOWS:
        os.fchmod(descriptor, mode)


def sync_directory(path: pathlib.Path) -> None:
    if IS_WINDOWS:
        # Windows CRT cannot open directories; file contents are flushed before
        # replacement. Do not claim POSIX directory-fsync durability on Windows.
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: pathlib.Path, content: bytes, mode: int = 0o600) -> None:
    path = validate_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    validate_path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            set_file_mode(handle.fileno(), mode)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        validate_path(path)
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def file_lock(path: pathlib.Path, timeout: float = 10.0):
    """Persistent one-byte process lock, released by the OS after a crash.

    Never unlink a lock file: waiters must all lock the same file identity.
    """
    path = validate_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    validate_path(path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    acquired = False
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"lock must be a regular file: {path}")
        deadline = time.monotonic() + max(0, timeout)
        while True:
            try:
                if IS_WINDOWS:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for lock: {path}") from error
                time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        linked = validate_path(path).stat()
        if (linked.st_dev, linked.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise ValueError("lock file changed while acquiring")
        yield
    finally:
        try:
            if acquired:
                if IS_WINDOWS:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def read_regular(path: pathlib.Path, limit: int = 4 * 1024 * 1024) -> bytes:
    """Bounded regular-file read with link and file-identity checks."""
    path = validate_path(path)
    before = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"not a regular file: {path}")
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("file changed while opening")
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"file exceeds read limit: {path}")
    validate_path(path)
    return raw


def pid_is_alive(pid: int) -> bool:
    """Query process state without delivering a Windows console signal."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only
    if not handle:
        error = ctypes.get_last_error()
        if error == 5:
            return True  # access denied is not proof that the process exited
        if error == 87:
            return False
        raise ctypes.WinError(error)
    try:
        result = kernel.WaitForSingleObject(handle, 0)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 0x102  # WAIT_TIMEOUT means still running
    finally:
        kernel.CloseHandle(handle)


def open_shared_read(path: pathlib.Path) -> int:
    """Read descriptor allowing atomic Windows replacement while held open."""
    path = validate_path(path)
    if not IS_WINDOWS:
        return os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0x00200000, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        validate_path(path)
        return msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise


def private_directory(path: pathlib.Path) -> pathlib.Path:
    """Protect a dedicated runtime directory for this user and SYSTEM."""
    path = validate_path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    validate_path(path)
    if not IS_WINDOWS:
        path.chmod(0o700)
        return path
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD)]
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(buffer, ctypes.POINTER(wintypes.LPVOID))[0]
        rendered = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(rendered)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            sddl = f"D:P(A;OICI;FA;;;{rendered.value})(A;OICI;FA;;;SY)"
        finally:
            kernel.LocalFree(rendered)
        descriptor = wintypes.LPVOID()
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not advapi.SetFileSecurityW(str(path), 4 | 0x80000000, descriptor):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.LocalFree(descriptor)
    finally:
        kernel.CloseHandle(token)
    return path
