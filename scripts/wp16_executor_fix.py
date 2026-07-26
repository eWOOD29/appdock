from pathlib import Path

path = Path("scripts/wp16_apply.py")
source = path.read_text(encoding="utf-8")
old = '\'            "cwd": r"runtime\\\\\\\\worker",\''
new = '\'            "cwd": r"runtime\\\\worker",\''
if source.count(old) != 1:
    raise SystemExit(f"executor escape anchor count was {source.count(old)}")
path.write_text(source.replace(old, new, 1), encoding="utf-8")
Path(__file__).unlink()
