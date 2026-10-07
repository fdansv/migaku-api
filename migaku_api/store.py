import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Conflict(Exception):
    pass


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS words (
                identity TEXT PRIMARY KEY, language TEXT NOT NULL, headword TEXT NOT NULL,
                reading TEXT NOT NULL, part_of_speech TEXT NOT NULL, status TEXT NOT NULL,
                tracked INTEGER NOT NULL, has_card INTEGER NOT NULL, raw TEXT NOT NULL,
                synced_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS words_language_status ON words(language,status);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
                payload TEXT NOT NULL, card_id INTEGER UNIQUE, state TEXT NOT NULL,
                write_attempted INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
        """)
        if "prepared_payload" not in {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN prepared_payload TEXT")
        # An interrupted write must be reconciled against Migaku before another action.
        with self.db:
            self.db.execute("UPDATE jobs SET state='uncertain',error='interrupted_write' WHERE state='running' AND write_attempted=1")
            self.db.execute("UPDATE jobs SET state='queued' WHERE state='running' AND write_attempted=0")

    def close(self):
        self.db.close()

    def _word_rows(self, language, words, stamp):
        normalized = []
        for word in words:
            # This is Migaku 1.31's WordInfo contract. Reject drift instead of silently erasing an index.
            required = {"dictForm", "secondary", "partOfSpeech", "language", "knownStatus", "tracked"}
            if not isinstance(word, dict) or not required <= word.keys() or word["language"] != language:
                raise ValueError("Migaku's WordInfo schema changed; the existing vocabulary index was preserved.")
            status = str(word["knownStatus"]).lower()
            if status not in {"known", "learning", "unknown", "ignored"}:
                raise ValueError("Migaku returned an unrecognized knownStatus; the existing index was preserved.")
            if not all(isinstance(word[key], str) for key in ("dictForm", "secondary", "partOfSpeech")):
                raise ValueError("Migaku returned an invalid word identity.")
            identity = hashlib.sha256(canonical([language, word["dictForm"], word["secondary"], word["partOfSpeech"]]).encode()).hexdigest()
            normalized.append((identity, language, word["dictForm"], word["secondary"], word["partOfSpeech"],
                               status, bool(word["tracked"]), bool(word.get("hasCard", False)), canonical(word), stamp))
        return normalized

    def upsert_word(self, language, word, queue_sync=False):
        # Update this confirmed identity without claiming the full snapshot is fresh.
        rows = self._word_rows(language, [word], now())
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO words VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
            if queue_sync:
                revision = self.word_sync_revision() + 1
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('word_sync_requested',?)", (str(revision),))

    def word_sync_revision(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='word_sync_requested'").fetchone()
        return int(row[0]) if row else 0

    def word_sync_pending(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='word_sync_completed'").fetchone()
        return self.word_sync_revision() > (int(row[0]) if row else 0)

    def complete_word_sync(self, revision):
        # A word changed during an in-flight push needs another push afterward.
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('word_sync_completed',?)", (str(revision),))

    def replace_words(self, language, words):
        stamp = now()
        normalized = self._word_rows(language, words, stamp)
        with self.db:
            self.db.execute("DELETE FROM words WHERE language=?", (language,))
            self.db.executemany("INSERT OR REPLACE INTO words VALUES (?,?,?,?,?,?,?,?,?,?)", normalized)
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"words:{language}:synced_at", stamp))
        return self.words_stats(language)

    def words_stats(self, language):
        counts = {row["status"]: row["n"] for row in self.db.execute(
            "SELECT status,count(*) n FROM words WHERE language=? GROUP BY status", (language,))}
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (f"words:{language}:synced_at",)).fetchone()
        return {"language": language, "total": sum(counts.values()), "statuses": counts,
                "synced_at": row[0] if row else None, "indexed": row is not None}

    def search_words(self, language, query="", status=None, limit=50, offset=0):
        clauses, args = ["language=?"], [language]
        if query:
            clauses.append("(instr(headword,?)>0 OR instr(reading,?)>0)")
            args.extend([query, query])
        if status:
            if status == "tracking":
                clauses.append("tracked=1")
            else:
                clauses.append("status=?")
                args.append(status)
        where = " AND ".join(clauses)
        total = self.db.execute(f"SELECT count(*) FROM words WHERE {where}", args).fetchone()[0]
        rows = self.db.execute(f"SELECT * FROM words WHERE {where} ORDER BY headword,reading,part_of_speech LIMIT ? OFFSET ?", args + [limit, offset])
        items = []
        for row in rows:
            item = dict(row)
            item.pop("raw")
            item["tracked"], item["has_card"] = bool(item["tracked"]), bool(item["has_card"])
            items.append(item)
        return {"words": items, "total": total, "limit": limit, "offset": offset,
                "index": self.words_stats(language)}

    def save_draft(self, payload, draft_id=None):
        stamp, draft_id = now(), draft_id or str(uuid.uuid4())
        with self.db:
            self.db.execute("INSERT INTO drafts VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at",
                            (draft_id, canonical(payload), stamp, stamp))
        return self.get_draft(draft_id)

    def get_draft(self, draft_id):
        row = self.db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def list_drafts(self):
        return [self.get_draft(row[0]) for row in self.db.execute("SELECT id FROM drafts ORDER BY updated_at DESC LIMIT 200")]

    def enqueue(self, key, payload):
        fingerprint = hashlib.sha256(canonical(payload).encode()).hexdigest()
        row = self.db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
        if row:
            if row["fingerprint"] != fingerprint:
                raise Conflict("This Idempotency-Key has already been used for a different card.")
            return self.get_job(row["id"])
        job_id, stamp = str(uuid.uuid4()), now()
        with self.db:
            self.db.execute("INSERT INTO jobs (id,idempotency_key,fingerprint,payload,card_id,state,created_at,updated_at) VALUES (?,?,?,?,?,'queued',?,?)",
                            (job_id, key, fingerprint, canonical(payload), None, stamp, stamp))
        return self.get_job(job_id)

    def get_job(self, job_id):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item.pop("fingerprint")
        if item.get("prepared_payload"):
            item["prepared_payload"] = json.loads(item["prepared_payload"])
        item["write_attempted"] = bool(item["write_attempted"])
        return item

    def jobs(self, states=None):
        if states:
            slots = ",".join("?" for _ in states)
            rows = self.db.execute(f"SELECT id FROM jobs WHERE state IN ({slots}) ORDER BY created_at", states)
        else:
            rows = self.db.execute("SELECT id FROM jobs ORDER BY created_at DESC LIMIT 200")
        return [self.get_job(row[0]) for row in rows]

    def update_job(self, job_id, **fields):
        allowed = {"state", "write_attempted", "attempts", "error", "card_id", "prepared_payload"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Invalid job update")
        fields["updated_at"] = now()
        if "prepared_payload" in fields:
            fields["prepared_payload"] = canonical(fields["prepared_payload"])
        assignments = ",".join(f"{key}=?" for key in fields)
        with self.db:
            self.db.execute(f"UPDATE jobs SET {assignments} WHERE id=?", [*fields.values(), job_id])
        return self.get_job(job_id)
