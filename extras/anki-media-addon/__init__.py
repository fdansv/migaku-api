"""Optional AnkiConnect extension for tracking pen photo uploads."""
import importlib
import re
import sqlite3
from pathlib import Path

from aqt import gui_hooks, mw


def status(filenames):
    if not isinstance(filenames, list) or not 1 <= len(filenames) <= 100 or any(
        not isinstance(name, str) or not re.fullmatch(r"pen_[a-f0-9]{64}\.jpg", name) for name in filenames
    ):
        raise ValueError("Supply bounded pen media filenames.")
    folder = Path(mw.pm.profileFolder())
    connection = sqlite3.connect((folder / "collection.media.db2").resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        rows = {name: connection.execute("SELECT csum,dirty FROM media WHERE fname=?", (name,)).fetchone() for name in filenames}
    finally:
        connection.close()
    uploaded = all(row and row[0] and row[1] == 0 and (folder / "collection.media" / name).is_file()
                   for name, row in rows.items())
    return {"enabled": mw.pm.media_syncing_enabled(), "active": mw.col.media_sync_status().active,
            "uploaded": bool(uploaded), "pending": sum(not row or not row[0] or row[1] != 0 for row in rows.values()),
            "progress": mw.media_syncer.last_progress}


def install():
    # AnkiConnect is intentionally reachable only on this cloud's loopback.
    module = importlib.import_module("2055492159")

    @module.util.api()
    def penEnsureMediaSync(self, filenames):
        current = status(filenames)
        if current["enabled"] and not current["active"] and not current["uploaded"]:
            mw.media_syncer.start()
        return status(filenames)

    @module.util.api()
    def penMediaSyncStatus(self, filenames):
        return status(filenames)

    module.AnkiConnect.penEnsureMediaSync = penEnsureMediaSync
    module.AnkiConnect.penMediaSyncStatus = penMediaSyncStatus


gui_hooks.profile_did_open.append(install)
