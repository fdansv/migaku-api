import asyncio
import copy
import json

from fastapi.testclient import TestClient

from migaku_api.app import create_app
from migaku_api.browser import WorkerError, discover_core
from migaku_api.queue import CardQueue
from migaku_api.store import Store


WORD = {"dictForm": "生", "secondary": "なま", "partOfSpeech": "", "language": "ja",
        "knownStatus": "KNOWN", "hasCard": 0, "tracked": False}
CARD = {"language": "ja", "deck_id": 123, "card_type_id": 4096,
        "fields": {"targetWord": "図書館", "sentence": "図書館で本を読む。", "definitions": "library"}}


class FakeBrowser:
    def __init__(self):
        self.context = None
        self.core = None
        self.last_error = None
        self.ready = True
        self.cards = {}
        self.creations = 0
        self.sync_fail = False
        self.timeout_after_commit = False
        self.timeout_before_commit = False

    async def close(self):
        pass

    async def status(self):
        return {"ready": self.ready}

    async def require_ready(self):
        if not self.ready:
            raise WorkerError("login_required", "Sign in")

    async def export_words(self, language):
        await self.require_ready()
        return [WORD, {**WORD, "secondary": "せい", "knownStatus": "LEARNING", "hasCard": 1, "tracked": True}]

    async def get_card(self, card_id):
        return self.cards.get(card_id)

    async def find_job_card(self, job_id):
        return next((card for card in self.cards.values() if card["job_id"] == job_id), None)

    async def create_card(self, job_id, payload):
        self.creations += 1
        if self.timeout_before_commit:
            raise WorkerError("timeout", "Timed out")
        card_id = self.creations + 1000
        self.cards[card_id] = {"id": card_id, "job_id": job_id, "fields": payload["fields"]}
        if self.timeout_after_commit:
            raise WorkerError("timeout", "Response lost after commit")
        return card_id

    async def validate_card(self, payload):
        await self.require_ready()

    async def push_sync(self):
        if self.sync_fail:
            raise WorkerError("timeout", "Sync did not complete")


def run(coro):
    return asyncio.run(coro)


def test_word_identity_readings_tracking_and_failed_refresh_preserves_snapshot(tmp_path):
    store = Store(tmp_path / "index.sqlite")
    words = run(FakeBrowser().export_words("ja"))
    store.replace_words("ja", words)
    assert store.search_words("ja", "生")["total"] == 2
    assert store.search_words("ja", status="tracking")["words"][0]["reading"] == "せい"
    assert store.search_words("ja", status="known")["words"][0]["has_card"] is False
    try:
        store.replace_words("ja", [{"word": "broken schema"}])
        assert False, "Schema drift must be rejected"
    except ValueError:
        pass
    assert store.words_stats("ja")["total"] == 2
    store.close()


def test_http_draft_idempotency_conflicts_and_persistence(tmp_path):
    worker = FakeBrowser()
    app = create_app(tmp_path, worker, autostart=False, background=False)
    with TestClient(app) as client:
        assert client.post("/words/refresh").status_code == 200
        assert client.get("/words?q=生&status=learning").json()["total"] == 1
        draft = client.post("/drafts", json={"fields": CARD["fields"]}).json()
        assert client.post(f"/drafts/{draft['id']}/submit", headers={"Idempotency-Key": "draft"}).status_code == 422
        assert client.post("/cards", json=CARD).status_code == 400
        first = client.post("/cards", json=CARD, headers={"Idempotency-Key": "same-request"}).json()
        second = client.post("/cards", json=CARD, headers={"Idempotency-Key": "same-request"}).json()
        assert first["id"] == second["id"]
        changed = copy.deepcopy(CARD)
        changed["fields"]["sentence"] = "別の文。"
        assert client.post("/cards", json=changed, headers={"Idempotency-Key": "same-request"}).status_code == 409
        worker.ready = False
        assert client.post("/words/refresh").status_code == 503
        assert client.get("/words").json()["total"] == 2
    with TestClient(create_app(tmp_path, worker, autostart=False, background=False)) as client:
        assert client.get(f"/jobs/{first['id']}").json()["state"] == "queued"
        assert client.get(f"/drafts/{draft['id']}").status_code == 200


def test_lost_create_response_reconciles_without_duplicate(tmp_path):
    store, browser = Store(tmp_path / "jobs.sqlite"), FakeBrowser()
    queue = CardQueue(store, browser)
    job = store.enqueue("request", CARD)
    browser.timeout_after_commit = True
    assert run(queue.process(job["id"]))["state"] == "uncertain"
    store.close()
    store = Store(tmp_path / "jobs.sqlite")
    result = run(CardQueue(store, browser).process(job["id"]))
    assert result["state"] == "synced"
    assert result["card_id"] == 1001
    assert browser.creations == 1
    store.close()


def test_unknown_write_outcome_never_blindly_resubmits(tmp_path):
    store, browser = Store(tmp_path / "jobs.sqlite"), FakeBrowser()
    queue = CardQueue(store, browser)
    job = store.enqueue("request", CARD)
    browser.timeout_before_commit = True
    assert run(queue.process(job["id"]))["state"] == "uncertain"
    browser.timeout_before_commit = False
    assert run(queue.process(job["id"]))["state"] == "uncertain"
    assert browser.creations == 1
    store.close()


def test_sync_failure_retries_sync_without_recreating_card(tmp_path):
    store, browser = Store(tmp_path / "jobs.sqlite"), FakeBrowser()
    queue = CardQueue(store, browser)
    job = store.enqueue("request", CARD)
    browser.sync_fail = True
    assert run(queue.process(job["id"]))["state"] == "created"
    browser.sync_fail = False
    assert run(queue.process(job["id"]))["state"] == "synced"
    assert browser.creations == 1
    store.close()


def test_interrupted_jobs_and_login_recover_safely(tmp_path):
    store = Store(tmp_path / "jobs.sqlite")
    job = store.enqueue("request", CARD)
    store.update_job(job["id"], state="running", write_attempted=True)
    store.close()
    store, browser = Store(tmp_path / "jobs.sqlite"), FakeBrowser()
    assert store.get_job(job["id"])["state"] == "uncertain"
    other = store.enqueue("other-request", CARD)
    browser.ready = False
    queue = CardQueue(store, browser)
    blocked = run(queue.process(other["id"]))
    assert blocked["state"] == "blocked" and blocked["write_attempted"] is False
    browser.ready = True
    assert run(queue.process(other["id"]))["state"] == "synced"
    store.close()


def test_api_token_and_schema_validation(tmp_path, monkeypatch):
    monkeypatch.setenv("MIGAKU_API_TOKEN", "test-token")
    with TestClient(create_app(tmp_path, FakeBrowser(), autostart=False, background=False)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/words").status_code == 401
        assert client.get("/words", headers={"Authorization": "Bearer test-token"}).status_code == 200
        bad = {**CARD, "pen": "must not be accepted"}
        assert client.post("/cards", json=bad, headers={"Authorization": "Bearer test-token"}).status_code == 422


def test_extension_factory_discovery_does_not_depend_on_minified_alias(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "manifest.json").write_text(json.dumps({"version": "1.31.0.16"}))
    (tmp_path / "assets/core-abc.js").write_text('F("WordListMgr.queryWords");Factory=()=>new Proxy({},{get:()=>{}});export{Factory as renamed};')
    assert discover_core(tmp_path)["export"] == "renamed"
