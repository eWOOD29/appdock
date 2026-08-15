from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import asdict, dataclass
from typing import Iterable

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
ERROR_NO_MORE_FILES = 18
MAX_PATH = 260


@dataclass(frozen=True)
class ProcessEntry:
    pid: int
    parent_pid: int
    executable: str


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    parent_pid: int
    creation_time: int
    executable: str

    def to_json(self) -> dict[str, int | str]:
        return asdict(self)

    @classmethod
    def from_json(cls, value: object) -> ProcessIdentity:
        if not isinstance(value, dict):
            raise AssertionError("process identity must be an object")
        return cls(
            pid=int(value["pid"]),
            parent_pid=int(value["parent_pid"]),
            creation_time=int(value["creation_time"]),
            executable=str(value["executable"]),
        )


if os.name == "nt":
    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * MAX_PATH),
        ]


    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_snapshot = _kernel32.CreateToolhelp32Snapshot
    _create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _create_snapshot.restype = wintypes.HANDLE
    _process_first = _kernel32.Process32FirstW
    _process_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    _process_first.restype = wintypes.BOOL
    _process_next = _kernel32.Process32NextW
    _process_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    _process_next.restype = wintypes.BOOL
    _open_process = _kernel32.OpenProcess
    _open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _open_process.restype = wintypes.HANDLE
    _get_process_times = _kernel32.GetProcessTimes
    _get_process_times.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    _get_process_times.restype = wintypes.BOOL
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = [wintypes.HANDLE]
    _close_handle.restype = wintypes.BOOL


def _filetime_value(value: wintypes.FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _creation_time(pid: int) -> int:
    ctypes.set_last_error(0)
    handle = _open_process(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        raise OSError(error, f"process identity query failed for PID {pid}") from ctypes.WinError(error)
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ctypes.set_last_error(0)
        if not _get_process_times(handle, created, exited, kernel, user):
            error = ctypes.get_last_error()
            raise OSError(error, f"process time query failed for PID {pid}") from ctypes.WinError(error)
        return _filetime_value(created)
    finally:
        _close_handle(handle)


def snapshot_processes() -> dict[int, ProcessEntry]:
    if os.name != "nt":
        raise RuntimeError("Windows process snapshots are only available on Windows")
    ctypes.set_last_error(0)
    handle = _create_snapshot(TH32CS_SNAPPROCESS, 0)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    result: dict[int, ProcessEntry] = {}
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        ctypes.set_last_error(0)
        available = _process_first(handle, ctypes.byref(entry))
        if not available:
            error = ctypes.get_last_error()
            if error == ERROR_NO_MORE_FILES:
                return {}
            raise ctypes.WinError(error)
        while True:
            identity = ProcessEntry(
                pid=int(entry.th32ProcessID),
                parent_pid=int(entry.th32ParentProcessID),
                executable=str(entry.szExeFile),
            )
            result[identity.pid] = identity
            ctypes.set_last_error(0)
            if not _process_next(handle, ctypes.byref(entry)):
                error = ctypes.get_last_error()
                if error != ERROR_NO_MORE_FILES:
                    raise ctypes.WinError(error)
                break
    finally:
        _close_handle(handle)
    return result


def process_identity(
    pid: int,
    processes: dict[int, ProcessEntry] | None = None,
) -> ProcessIdentity:
    if processes is None:
        processes = snapshot_processes()
    entry = processes.get(pid)
    if entry is None:
        raise AssertionError(f"process {pid} was not present in the Windows snapshot")
    return ProcessIdentity(
        pid=entry.pid,
        parent_pid=entry.parent_pid,
        creation_time=_creation_time(pid),
        executable=entry.executable,
    )


def identities_for_pids(
    pids: Iterable[int],
    processes: dict[int, ProcessEntry] | None = None,
) -> set[ProcessIdentity]:
    if processes is None:
        processes = snapshot_processes()
    return {process_identity(pid, processes) for pid in pids}


def descendants_or_self(
    root: ProcessIdentity,
    processes: dict[int, ProcessEntry] | None = None,
) -> dict[int, ProcessIdentity]:
    attempts = 1 if processes is not None else 5
    for attempt in range(attempts):
        current = processes if processes is not None else snapshot_processes()
        if root.pid not in current:
            return {}
        try:
            if process_identity(root.pid, current) != root:
                return {}
            selected = {root.pid}
            changed = True
            while changed:
                changed = False
                for entry in current.values():
                    if entry.pid not in selected and entry.parent_pid in selected:
                        selected.add(entry.pid)
                        changed = True
            return {pid: process_identity(pid, current) for pid in selected}
        except OSError:
            if attempt + 1 == attempts:
                raise
    raise AssertionError("unreachable process snapshot retry state")


def lineage_until(
    pid: int,
    stop_before_pid: int,
    processes: dict[int, ProcessEntry] | None = None,
) -> dict[int, ProcessIdentity]:
    if processes is None:
        processes = snapshot_processes()
    result: dict[int, ProcessIdentity] = {}
    current = pid
    while current and current != stop_before_pid:
        entry = processes.get(current)
        if entry is None or entry.pid in result:
            break
        result[entry.pid] = process_identity(entry.pid, processes)
        current = entry.parent_pid
    return result


def merge_identities(*groups: Iterable[ProcessIdentity]) -> dict[tuple[int, int], ProcessIdentity]:
    return {
        (identity.pid, identity.creation_time): identity
        for group in groups
        for identity in group
    }


def running_identities(
    identities: Iterable[ProcessIdentity],
    processes: dict[int, ProcessEntry] | None = None,
) -> list[ProcessIdentity]:
    if processes is None:
        processes = snapshot_processes()
    survivors: list[ProcessIdentity] = []
    for identity in identities:
        if identity.pid not in processes:
            continue
        try:
            current = process_identity(identity.pid, processes)
        except OSError:
            refreshed = snapshot_processes()
            if identity.pid not in refreshed:
                continue
            current = process_identity(identity.pid, refreshed)
        if current == identity:
            survivors.append(identity)
    return survivors
