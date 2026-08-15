from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import windows_process_tree as process_tree
from windows_process_tree import ProcessEntry, ProcessIdentity, descendants_or_self, running_identities


class StableProcessIdentityTests(unittest.TestCase):
    def test_reparented_root_keeps_owned_descendants_and_returns_current_diagnostics(self) -> None:
        saved_root = ProcessIdentity(100, 10, 1000, "python.exe")
        current_root = ProcessIdentity(100, 99, 1000, "pythonw.exe")
        current_child = ProcessIdentity(200, 100, 2000, "worker.exe")
        current = {100: current_root, 200: current_child}
        processes = {
            100: ProcessEntry(100, 99, "pythonw.exe"),
            200: ProcessEntry(200, 100, "worker.exe"),
        }

        with patch.object(process_tree, "process_identity", side_effect=lambda pid, _processes: current[pid]):
            result = descendants_or_self(saved_root, processes)

        self.assertEqual(result, current)
        self.assertIs(result[100], current_root)
        self.assertIs(result[200], current_child)

    def test_reparented_survivor_keeps_saved_identity_as_diagnostic(self) -> None:
        saved = ProcessIdentity(300, 10, 3000, "python.exe")
        current = ProcessIdentity(300, 77, 3000, "pythonw.exe")
        processes = {300: ProcessEntry(300, 77, "pythonw.exe")}

        with patch.object(process_tree, "process_identity", return_value=current):
            result = running_identities([saved], processes)

        self.assertEqual(result, [saved])
        self.assertIs(result[0], saved)

    def test_changed_creation_time_rejects_same_pid_root_and_survivor(self) -> None:
        saved = ProcessIdentity(400, 10, 4000, "python.exe")
        current = ProcessIdentity(400, 10, 4999, "python.exe")
        processes = {400: ProcessEntry(400, 10, "python.exe")}

        with patch.object(process_tree, "process_identity", return_value=current):
            self.assertEqual(descendants_or_self(saved, processes), {})
            self.assertEqual(running_identities([saved], processes), [])

    def test_reused_numeric_listener_pid_is_not_a_member_of_saved_tree(self) -> None:
        saved_listener = ProcessIdentity(500, 10, 5000, "python.exe")
        reused_listener = ProcessIdentity(500, 10, 5999, "python.exe")

        self.assertFalse(process_tree.identities_subset([reused_listener], [saved_listener]))

    def test_reparented_listener_remains_a_member_by_stable_identity(self) -> None:
        saved_listener = ProcessIdentity(600, 10, 6000, "python.exe")
        current_listener = ProcessIdentity(600, 88, 6000, "pythonw.exe")

        self.assertTrue(process_tree.identities_subset([current_listener], [saved_listener]))


@unittest.skipUnless(os.name == "nt", "native Windows failure controls are Windows-specific")
class NativeFailureControls(unittest.TestCase):
    def test_process32first_non_end_error_fails_closed(self) -> None:
        with (
            patch.object(process_tree, "_create_snapshot", return_value=123),
            patch.object(process_tree, "_process_first", return_value=0),
            patch.object(process_tree, "_close_handle"),
            patch.object(process_tree.ctypes, "get_last_error", return_value=5),
        ):
            with self.assertRaises(OSError):
                process_tree.snapshot_processes()

    def test_process32next_non_end_error_fails_closed(self) -> None:
        with (
            patch.object(process_tree, "_create_snapshot", return_value=123),
            patch.object(process_tree, "_process_first", return_value=1),
            patch.object(process_tree, "_process_next", return_value=0),
            patch.object(process_tree, "_close_handle"),
            patch.object(process_tree.ctypes, "get_last_error", return_value=5),
        ):
            with self.assertRaises(OSError):
                process_tree.snapshot_processes()

    def test_open_process_failure_fails_closed(self) -> None:
        with (
            patch.object(process_tree, "_open_process", return_value=0),
            patch.object(process_tree.ctypes, "get_last_error", return_value=5),
        ):
            with self.assertRaises(OSError):
                process_tree._creation_time(700)

    def test_get_process_times_failure_fails_closed(self) -> None:
        with (
            patch.object(process_tree, "_open_process", return_value=123),
            patch.object(process_tree, "_get_process_times", return_value=0),
            patch.object(process_tree, "_close_handle"),
            patch.object(process_tree.ctypes, "get_last_error", return_value=5),
        ):
            with self.assertRaises(OSError):
                process_tree._creation_time(701)


if __name__ == "__main__":
    unittest.main()
