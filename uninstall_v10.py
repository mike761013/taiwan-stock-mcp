"""Restore server.py and requirements.txt from the installer backups."""

from pathlib import Path
import shutil

root = Path(__file__).resolve().parent
backup = root / ".v10_backup"
for name in ("server.py", "requirements.txt"):
    source = backup / name
    if source.exists():
        shutil.copy2(source, root / name)
        print("Restored", name)
print("V10 added files remain; remove them manually only if desired.")
