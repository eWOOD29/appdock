from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import build_portable


@unittest.skipUnless(os.name == "nt", "Windows installer safety tests")
class WindowsInstallerSafetyTests(unittest.TestCase):
    def run_probe(self, script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def copy_bundle(self, destination: Path) -> None:
        root = Path(__file__).resolve().parents[1]
        shutil.copytree(
            root,
            destination,
            ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "dist", ".release-verify"),
        )

    def test_shared_path_guard_allows_scoped_destination_and_rejects_dangerous_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = root / "bundle"
            safe_install = root / "programs" / "AppDock"
            source.mkdir()
            payload = json.dumps(
                {
                    "source": str(source),
                    "safe": str(safe_install),
                    "dangerous": [
                        str(source),
                        str(source / "nested"),
                        str(Path.home()),
                        str(Path.home().parent),
                        str(Path.home().anchor),
                        str(Path.home() / "Documents"),
                        str(Path.home() / "AppData"),
                        os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")),
                        os.environ.get("WINDIR", r"C:\Windows"),
                    ],
                }
            ).replace("'", "''")
            probe = rf"""
$ErrorActionPreference = 'Stop'
. .\scripts\path_safety.ps1
$case = ConvertFrom-Json '{payload}'
Assert-AppDockSafePath -Path $case.safe -Role InstallDir -SourceRoot $case.source
foreach ($candidate in $case.dangerous) {{
    $rejected = $false
    try {{ Assert-AppDockSafePath -Path $candidate -Role InstallDir -SourceRoot $case.source }}
    catch {{ $rejected = $true }}
    if (-not $rejected) {{ throw "unsafe path accepted: $candidate" }}
}}
Write-Output 'path safety probe passed'
"""
            result = self.run_probe(probe)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("path safety probe passed", result.stdout)

    def test_uninstall_requires_an_appdock_installation_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            unrelated = root / "unrelated"
            (unrelated / "scripts").mkdir(parents=True)
            (unrelated / "appdock.py").write_text("# unrelated", encoding="utf-8")
            (unrelated / "scripts" / "uninstall.ps1").write_text("# unrelated", encoding="utf-8")
            probe = rf"""
$ErrorActionPreference = 'Stop'
. .\scripts\path_safety.ps1
$rejected = $false
try {{ Assert-AppDockInstallMarker -Path '{str(unrelated).replace("'", "''")}' }}
catch {{ $rejected = $true }}
if (-not $rejected) {{ throw 'unrelated directory accepted for uninstall' }}
Write-Output 'marker probe passed'
"""
            result = self.run_probe(probe)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("marker probe passed", result.stdout)

    def test_install_marker_accepts_inventory_valid_v010_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve() / "legacy"
            files = {
                "appdock.py": b"legacy app",
                "scripts/uninstall.ps1": b"legacy uninstall",
                "scripts/update_helper.py": b"legacy helper",
                "static/app.js": b"legacy js",
                "static/app.css": b"legacy css",
            }
            inventory = []
            import hashlib
            for relative, content in files.items():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                inventory.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest()})
            (root / "run-appdock.cmd").write_text("@echo off", encoding="utf-8")
            (root / "RELEASE-MANIFEST.json").write_text(
                json.dumps({"schema_version": 1, "files": inventory}), encoding="utf-8"
            )
            probe = rf"""
$ErrorActionPreference = 'Stop'
. .\scripts\path_safety.ps1
Assert-AppDockInstallMarker -Path '{str(root).replace("'", "''")}' | Out-Null
Write-Output 'legacy marker accepted'
"""
            result = self.run_probe(probe)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("legacy marker accepted", result.stdout)

    def test_process_match_requires_the_exact_installed_entry_point_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            install = (Path(temp).resolve() / "AppDock")
            exact = f'python.exe "{install / "appdock.py"}" --port 8765'
            exact_unquoted = f'python.exe {install / "appdock.py"} --port 8765'
            prefix_collision = f'python.exe "{install}-dev\\appdock.py" --port 8765'
            relative = 'python.exe appdock.py --port 8765'
            config_collision = f'python.exe other.py --config "{install / "appdock.py"}"'
            quoted_executable_collision = f'"{install / "notpython.exe"}" "{install / "appdock.py"}"'
            unquoted_executable_collision = f'{install / "notpython.exe"} "{install / "appdock.py"}"'
            payload = json.dumps(
                {
                    "install": str(install),
                    "exact": exact,
                    "exact_unquoted": exact_unquoted,
                    "prefix": prefix_collision,
                    "relative": relative,
                    "config": config_collision,
                    "quoted_executable_collision": quoted_executable_collision,
                    "unquoted_executable_collision": unquoted_executable_collision,
                }
            ).replace("'", "''")
            probe = rf"""
$ErrorActionPreference = 'Stop'
. .\scripts\path_safety.ps1
$case = ConvertFrom-Json '{payload}'
if (-not (Test-AppDockOwnedProcessCommandLine -CommandLine $case.exact -InstallDir $case.install)) {{ throw 'exact entry point was not matched' }}
if (-not (Test-AppDockOwnedProcessCommandLine -CommandLine $case.exact_unquoted -InstallDir $case.install)) {{ throw 'unquoted exact entry point was not matched' }}
if (Test-AppDockOwnedProcessCommandLine -CommandLine $case.prefix -InstallDir $case.install) {{ throw 'prefix collision was matched' }}
if (Test-AppDockOwnedProcessCommandLine -CommandLine $case.relative -InstallDir $case.install) {{ throw 'relative ambiguous entry point was matched' }}
if (Test-AppDockOwnedProcessCommandLine -CommandLine $case.config -InstallDir $case.install) {{ throw 'non-entry-point config token was matched' }}
if (Test-AppDockOwnedProcessCommandLine -CommandLine $case.quoted_executable_collision -InstallDir $case.install) {{ throw 'quoted executable suffix collision was matched' }}
if (Test-AppDockOwnedProcessCommandLine -CommandLine $case.unquoted_executable_collision -InstallDir $case.install) {{ throw 'unquoted executable suffix collision was matched' }}
Write-Output 'process ownership probe passed'
"""
            result = self.run_probe(probe)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("process ownership probe passed", result.stdout)

    def test_uninstaller_bootstraps_path_safety_hash_before_dot_sourcing(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "uninstall.ps1").read_text(encoding="utf-8")
        bootstrap = script.index("Get-AppDockBootstrapSha256")
        dot_source = script.index(". (Join-Path $PSScriptRoot 'path_safety.ps1')")
        self.assertLess(bootstrap, dot_source)
        self.assertIn("System.Security.Cryptography.SHA256", script)
        self.assertNotIn("Get-FileHash", script)

    def test_uninstall_rejects_file_valued_data_dir_before_deleting_anything(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            install = root / "installed" / "AppDock"
            scripts = install / "scripts"
            scripts.mkdir(parents=True)
            (install / "appdock.py").write_text("# marker", encoding="utf-8")
            shutil.copy2(Path(__file__).resolve().parents[1] / "scripts" / "uninstall.ps1", scripts / "uninstall.ps1")
            shutil.copy2(Path(__file__).resolve().parents[1] / "scripts" / "path_safety.ps1", scripts / "path_safety.ps1")
            data_file = root / "important.txt"
            data_file.write_text("keep", encoding="utf-8")

            result = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(scripts / "uninstall.ps1"), "-InstallDir", str(install),
                    "-DataDir", str(data_file), "-RemoveUserData",
                ],
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(data_file.is_file())
            self.assertTrue(install.is_dir(), "uninstaller modified program files before rejecting DataDir")

    def test_installer_rejects_tampered_safety_module_before_marker_or_action(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            bundle = root / "bundle"
            install = root / "installed" / "AppDock"
            data = root / "data" / "AppDock"
            self.copy_bundle(bundle)
            safety = bundle / "scripts" / "path_safety.ps1"
            safety.write_text(safety.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
            result = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(bundle / "scripts" / "install.ps1"),
                    "-InstallDir", str(install), "-DataDir", str(data),
                    "-PythonExe", sys.executable, "-NoStart",
                ],
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(install.exists(), "tampered safety module was allowed to create an install marker")
            self.assertFalse(data.exists(), "tampered safety module was allowed to create data")

    def test_packaged_installer_rejects_tampered_helper_before_side_effects(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            archive = root / "artifact" / "appdock-windows.zip"
            bundle = root / "bundle"
            install = root / "installed" / "AppDock"
            data = root / "data" / "AppDock"
            build_portable.build_archive(archive, source_root)
            with zipfile.ZipFile(archive) as packaged:
                packaged.extractall(bundle)
            safety = bundle / "scripts" / "path_safety.ps1"
            safety.write_bytes(safety.read_bytes() + b"\n# tampered packaged helper\n")

            result = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(bundle / "scripts" / "install.ps1"),
                    "-InstallDir", str(install), "-DataDir", str(data),
                    "-PythonExe", sys.executable, "-NoStart",
                ],
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(install.exists(), "tampered packaged helper was allowed to create an install marker")
            self.assertFalse(data.exists(), "tampered packaged helper was allowed to create data")

    def test_packaged_installer_accepts_the_production_archive_and_preserves_data(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        installer_source = (source_root / "scripts" / "install.ps1").read_text(encoding="utf-8")
        trusted = installer_source.split("$TrustedPathSafetySha256 = '", 1)[1].split("'", 1)[0]
        source_helper_digest = hashlib.sha256((source_root / "scripts" / "path_safety.ps1").read_bytes()).hexdigest()
        self.assertEqual(trusted, source_helper_digest)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            archive = root / "artifact" / "appdock-windows.zip"
            bundle = root / "bundle"
            install = root / "installed" / "AppDock"
            data = root / "data" / "AppDock"
            data.mkdir(parents=True)
            sentinel = data / "preserve-me.txt"
            sentinel.write_text("user data must survive", encoding="utf-8")

            build_portable.build_archive(archive, source_root)
            with zipfile.ZipFile(archive) as packaged:
                manifest = json.loads(packaged.read("RELEASE-MANIFEST.json"))
                packaged_helper = packaged.read("scripts/path_safety.ps1")
                manifest_entry = next(item for item in manifest["files"] if item["path"] == "scripts/path_safety.ps1")
                self.assertEqual(hashlib.sha256(packaged_helper).hexdigest(), source_helper_digest)
                self.assertEqual(manifest_entry["sha256"], source_helper_digest)
                packaged.extractall(bundle)

            result = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(bundle / "scripts" / "install.ps1"),
                    "-InstallDir", str(install), "-DataDir", str(data),
                    "-PythonExe", sys.executable, "-NoStart",
                ],
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue((install / "run-appdock.cmd").is_file())
            self.assertTrue((install / "appdock.py").is_file())
            self.assertTrue((install / "RELEASE-MANIFEST.json").is_file())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "user data must survive")

    def test_installer_uses_a_staged_mirror_and_rejects_source_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            bundle = root / "bundle"
            install = root / "installed" / "AppDock"
            data = root / "data" / "AppDock"
            self.copy_bundle(bundle)
            installer = bundle / "scripts" / "install.ps1"
            command = [
                "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(installer), "-InstallDir", str(install), "-DataDir", str(data),
                "-PythonExe", sys.executable, "-NoStart",
            ]

            first = subprocess.run(command, text=True, capture_output=True, timeout=60, check=False)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            obsolete = install / "obsolete-owned-file.txt"
            obsolete.write_text("old", encoding="utf-8")

            second = subprocess.run(command, text=True, capture_output=True, timeout=60, check=False)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertFalse(obsolete.exists(), "staged mirror left an obsolete program file")
            self.assertTrue((install / "appdock.py").is_file())

            overlap = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(installer), "-InstallDir", str(bundle), "-DataDir", str(data),
                    "-PythonExe", sys.executable, "-NoStart",
                ],
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            self.assertNotEqual(overlap.returncode, 0)
            self.assertIn("must not overlap", overlap.stdout + overlap.stderr)


if __name__ == "__main__":
    unittest.main()
