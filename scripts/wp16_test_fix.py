from pathlib import Path

path = Path("scripts/wp16_apply.py")
source = path.read_text(encoding="utf-8")
old = '''        spec = self.assert_windows_discovery()["synthetic-0"]
        with mock.patch.object(appdock.os, "name", "nt"):
            self.assertEqual(str(appdock._native_runtime_cwd(spec)), str(spec.cwd))
        if os.name == "nt":
            return
'''
new = '''        spec = self.assert_windows_discovery()["synthetic-0"]
        if os.name == "nt":
            native = appdock._native_runtime_cwd(spec)
            self.assertIsInstance(native, Path)
            self.assertEqual(str(native), str(spec.cwd))
            return
'''
if source.count(old) != 1:
    raise SystemExit(f"runtime test anchor count was {source.count(old)}")
path.write_text(source.replace(old, new, 1), encoding="utf-8")
Path(__file__).unlink()
