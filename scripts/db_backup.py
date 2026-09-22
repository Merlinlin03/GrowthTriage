"""SQLite consistent backup/restore to a NEW file; no app/config imports."""
from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path


def validate(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("DATABASE_INTEGRITY_FAILED")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("DATABASE_FOREIGN_KEY_FAILED")
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"workspace", "analysis_run", "approval_request", "report_bundle", "daily_live_quota"} <= tables:
        raise ValueError("DATABASE_SCHEMA_INVALID")


def copy_database(source: Path, destination: Path) -> None:
    source, destination = source.resolve(), destination.resolve()
    if not source.is_file():
        raise ValueError("SOURCE_NOT_FOUND")
    # Exclusive creation refuses existing files, including the source itself.
    with destination.open("xb"):
        pass
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(destination)) as target:
                original.backup(target)
                validate(target)
                target.execute("PRAGMA journal_mode=DELETE")
                target.commit()
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["backup", "restore"])
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    try:
        copy_database(args.source, args.destination)
    except FileExistsError:
        print("DESTINATION_EXISTS")
        return 1
    except (OSError, sqlite3.Error, ValueError):
        print("DATABASE_COPY_FAILED")
        return 1
    print("DATABASE_COPY_VERIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
