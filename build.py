"""
build.py — package time_budget into a distributable .ankiaddon file.

Usage:
    python build.py

Output: time_budget.ankiaddon in the project root.

Anki expects the zip to be flat (files at the root, no parent directory)
with manifest.json at the root. It uses the `package` field in the manifest
to determine the installation directory name.
"""

import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
SRC  = ROOT / "time_budget"
OUT  = ROOT / "time_budget.ankiaddon"

INCLUDE = {".py", ".json", ".md"}
EXCLUDE_DIRS = {"__pycache__"}


def collect_files():
    for path in sorted(SRC.rglob("*")):
        if path.is_dir():
            continue
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        if path.suffix not in INCLUDE:
            continue
        yield path


def build():
    OUT.unlink(missing_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in collect_files():
            arcname = path.relative_to(SRC)
            zf.write(path, arcname)
            print(f"  {arcname}")
    print(f"\nWrote {OUT.name} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    build()
