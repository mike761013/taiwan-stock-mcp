"""One-time local installer for taiwan-stock-mcp V10.

Run from the repository root:
    python install_v10.py

It safely:
- adds asyncpg to requirements.txt
- injects V10 tool registration into server.py
- writes .env.v10.example
- creates backups before modifying existing files
"""

from __future__ import annotations

from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "server.py"
REQ = ROOT / "requirements.txt"
BACKUP_DIR = ROOT / ".v10_backup"

IMPORT_MARKER = "# === V10 PostgreSQL Tool Registration ==="
REGISTER_BLOCK = f"""
\n{IMPORT_MARKER}
try:
    from server_v10_tools import register_v10_tools
    register_v10_tools(mcp)
except Exception as exc:
    # PostgreSQL is optional and must never prevent the existing MCP from starting.
    print(f"V10 PostgreSQL tools were not registered: {{type(exc).__name__}}: {{exc}}")
\n
"""


def backup(path: Path) -> None:
    BACKUP_DIR.mkdir(exist_ok=True)
    target = BACKUP_DIR / path.name
    if not target.exists():
        shutil.copy2(path, target)


def update_requirements() -> None:
    text = REQ.read_text(encoding="utf-8")
    if "asyncpg" not in text:
        backup(REQ)
        REQ.write_text(text.rstrip() + "\nasyncpg>=0.29,<1\n", encoding="utf-8")


def patch_server() -> None:
    text = SERVER.read_text(encoding="utf-8")
    if IMPORT_MARKER in text:
        return
    needle = '\nif __name__ == "__main__":\n'
    if needle not in text:
        raise RuntimeError("Could not locate server.py main block.")
    backup(SERVER)
    text = text.replace(needle, REGISTER_BLOCK + needle, 1)
    SERVER.write_text(text, encoding="utf-8")


def write_env_example() -> None:
    path = ROOT / ".env.v10.example"
    if not path.exists():
        path.write_text(
            """# Never commit the real DATABASE_URL
DATABASE_URL=postgresql://USER:PASSWORD@INTERNAL_HOST/DATABASE
STOCK_DB_ENABLED=false
STOCK_DB_READ_PREFERRED=false
STOCK_DB_DAILY_UPDATE=false
STOCK_DB_FALLBACK_ENABLED=true
STOCK_DB_HISTORY_YEARS=3
STOCK_DB_POOL_MIN=1
STOCK_DB_POOL_MAX=3
STOCK_DB_STATEMENT_TIMEOUT_SECONDS=30
""",
            encoding="utf-8",
        )


def cleanup_previous_flat_upload() -> None:
    """Remove files created by the earlier flattened web upload.

    Only known V10 draft filenames are removed. Existing production files
    such as server.py, monitor_worker.py and requirements.txt are untouched.
    """
    obsolete = [
        "REQUIREMENTS_ADD.txt",
        "UPLOAD_INSTRUCTIONS.txt",
        "__init__.py",
        "config.py",
        "connection.py",
        "indicators.py",
        "jobs.py",
        "repository.py",
        "schema.sql",
        "init_stock_database.py",
        "backfill_daily_bars.py",
        "update_daily_bars.py",
        "test_stock_db_config.py",
    ]
    removed = []
    for name in obsolete:
        path = ROOT / name
        if path.is_file():
            backup(path)
            path.unlink()
            removed.append(name)
    if removed:
        print("Removed previous flattened upload:", ", ".join(removed))


def main() -> int:
    if not SERVER.exists() or not REQ.exists():
        print("ERROR: Run this script from the taiwan-stock-mcp repository root.")
        return 1
    cleanup_previous_flat_upload()
    update_requirements()
    patch_server()
    write_env_example()
    print("V10 installation completed.")
    print("Backups:", BACKUP_DIR)
    print("Next: review changes in GitHub Desktop, commit, then push.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
