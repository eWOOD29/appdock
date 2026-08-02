from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import appdock
from scripts import build_portable, check_docs


class Generation10ZipMetadataTests(unittest.TestCase):
    def release_members(self) -> dict[str, bytes]:
        members = {
            "appdock.py": b"app",
            "static/app.js": b"js",
            "static/app.css": b"css",
            "scripts/update_helper.py": b"helper",
            "scripts/path_safety.ps1": b"safety",
            "scripts/install.ps1": b"install",
            "scripts/uninstall.ps1": b"uninstall",
        }
        manifest = {
            "schema_version": 2,
            "files": [
                {"path": name, "sha256": hashlib.sha256(content).hexdigest()}
                for name, content in sorted(members.items())
            ],
        }
        members[appdock.RELEASE_MANIFEST_NAME] = (json.dumps(manifest, sort_keys=True) + "\n").encode()
        return members

    def make_release_zip(self, metadata: dict[str, int] | None = None) -> bytes:
        stream = io.BytesIO()
        metadata = metadata or {}
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, content in self.release_members().items():
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = metadata.get(name, (stat.S_IFREG | 0o644) << 16)
                archive.writestr(info, content)
        return stream.getvalue()

    def test_validate_zip_rejects_fifo_socket_and_device_member_metadata(self) -> None:
        for kind in (stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK):
            with self.subTest(kind=oct(kind)), self.assertRaises(appdock.AppDockError):
                appdock.validate_zip(self.make_release_zip({"static/app.js": (kind | 0o644) << 16}))

    def test_validate_zip_accepts_unspecified_and_regular_member_metadata(self) -> None:
        for mode in (0, (stat.S_IFREG | 0o644) << 16):
            with self.subTest(mode=oct(mode)):
                names = appdock.validate_zip(self.make_release_zip({"static/app.js": mode}))
                self.assertIn("static/app.js", names)


class Generation10DocumentationTests(unittest.TestCase):
    def test_documentation_checker_reports_broken_local_fragment_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "guide.md").write_text(
                "See [recovery](UPDATES.md#manual-rollback) and [local](#missing-anchor).\n",
                encoding="utf-8",
            )
            (root / "UPDATES.md").write_text("# Updates\n\n## Manual recovery\n", encoding="utf-8")
            failures = check_docs.broken_links(root)
        self.assertTrue(any("manual-rollback" in failure for failure in failures))
        self.assertTrue(any("missing-anchor" in failure for failure in failures))


class Generation10PortableBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "appdock.py").write_bytes(b"portable app\n")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_builder_rejects_hardlinked_source_member_without_reading_external_sentinel(self) -> None:
        sentinel = self.root / "source-sentinel.txt"
        sentinel.write_bytes(b"outside bytes")
        (self.source / "appdock.py").unlink()
        os.link(sentinel, self.source / "appdock.py")
        with self.assertRaises(ValueError):
            build_portable.build_archive(self.root / "out.zip", self.source)
        self.assertEqual(sentinel.read_bytes(), b"outside bytes")

    def test_builder_rejects_oversized_source_before_packaging(self) -> None:
        with patch.object(build_portable, "MAX_SOURCE_FILE_BYTES", 4, create=True):
            with self.assertRaises(ValueError):
                build_portable.build_archive(self.root / "out.zip", self.source)

    def test_builder_rejects_existing_output_hardlink_without_mutating_sentinel(self) -> None:
        sentinel = self.root / "output-sentinel.zip"
        sentinel.write_bytes(b"preserve output")
        output = self.root / "out.zip"
        os.link(sentinel, output)
        with self.assertRaises(ValueError):
            build_portable.build_archive(output, self.source)
        self.assertEqual(sentinel.read_bytes(), b"preserve output")

    def test_builder_rejects_existing_sidecar_hardlink_without_mutating_sentinel(self) -> None:
        sentinel = self.root / "sidecar-sentinel.txt"
        sentinel.write_bytes(b"preserve sidecar")
        os.link(sentinel, self.root / "SHA256SUMS.txt")
        with self.assertRaises(ValueError):
            build_portable.build_archive(self.root / "out.zip", self.source)
        self.assertEqual(sentinel.read_bytes(), b"preserve sidecar")

    def test_builder_rejects_existing_output_symlink_without_mutating_sentinel_when_supported(self) -> None:
        sentinel = self.root / "output-symlink-sentinel.zip"
        sentinel.write_bytes(b"preserve output")
        output = self.root / "out.zip"
        try:
            output.symlink_to(sentinel)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        with self.assertRaises(ValueError):
            build_portable.build_archive(output, self.source)
        self.assertEqual(sentinel.read_bytes(), b"preserve output")

    def test_builder_rejects_existing_sidecar_symlink_without_mutating_sentinel_when_supported(self) -> None:
        sentinel = self.root / "sidecar-symlink-sentinel.txt"
        sentinel.write_bytes(b"preserve sidecar")
        sidecar = self.root / "SHA256SUMS.txt"
        try:
            sidecar.symlink_to(sentinel)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        with self.assertRaises(ValueError):
            build_portable.build_archive(self.root / "out.zip", self.source)
        self.assertEqual(sentinel.read_bytes(), b"preserve sidecar")

    def test_builder_rejects_symlinked_source_member_when_supported(self) -> None:
        sentinel = self.root / "symlink-source-sentinel.txt"
        sentinel.write_bytes(b"outside bytes")
        member = self.source / "appdock.py"
        member.unlink()
        try:
            member.symlink_to(sentinel)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        with self.assertRaises(ValueError):
            build_portable.build_archive(self.root / "out.zip", self.source)
        self.assertEqual(sentinel.read_bytes(), b"outside bytes")