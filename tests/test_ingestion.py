import copy

from fastapi.testclient import TestClient

from migaku_api.app import create_app
from migaku_api.browser import WorkerError
from migaku_api.queue import CardQueue
from migaku_api.store import Store
from tests.test_service import CARD, FakeBrowser, run

INPUT = {"language": "ja", "sentence": "図書館で本を読む。", "focus_word": "図書館", "focus_start": 0, "focus_end": 3}


class EnrichingBrowser(FakeBrowser):
    def __init__(self):
        super().__init__()
        self.enrichments = 0
        self.fail_enrichment = False

    async def prepare_ingestion(self, request):
        self.enrichments += 1
        if self.fail_enrichment:
            raise WorkerError("ingestion_ai_failed", "Generation failed")
        payload = copy.deepcopy(CARD)
        payload["fields"].update(translation="Read a book at the library.", notes="A place to borrow books.")
        return payload


def test_ingestion_accepts_offline_and_reuses_key(tmp_path):
    browser = EnrichingBrowser()
    browser.ready = False
    with TestClient(create_app(tmp_path, browser, autostart=False, background=False)) as client:
        assert client.post("/ingest", json=INPUT).status_code == 400
        first = client.post("/ingest", json=INPUT, headers={"Idempotency-Key": "pen-1"})
        assert first.status_code == 202
        assert first.json()["state"] == "queued"
        second = client.post("/ingest", json=INPUT, headers={"Idempotency-Key": "pen-1"})
        assert first.json()["id"] == second.json()["id"]
        changed = {**INPUT, "translation": "A different translation."}
        assert client.post("/ingest", json=changed, headers={"Idempotency-Key": "pen-1"}).status_code == 409
        invalid = {**INPUT, "focus_start": 1}
        assert client.post("/ingest", json=invalid, headers={"Idempotency-Key": "pen-2"}).status_code == 422
        astral = {**INPUT, "sentence": "📚図書館", "focus_start": 2, "focus_end": 5}
        assert client.post("/ingest", json=astral, headers={"Idempotency-Key": "pen-3"}).status_code == 202
    assert browser.creations == browser.enrichments == 0


def test_failed_enrichment_never_attempts_write_and_can_retry(tmp_path):
    store, browser = Store(tmp_path / "jobs.sqlite"), EnrichingBrowser()
    queue = CardQueue(store, browser)
    job = store.enqueue("pen", {"kind": "ingestion", "input": INPUT})
    browser.fail_enrichment = True
    result = run(queue.process(job["id"]))
    assert result["state"] == "blocked" and not result["write_attempted"]
    assert browser.creations == 0
    browser.fail_enrichment = False
    assert run(queue.process(job["id"]))["state"] == "synced"
    assert browser.creations == 1
    store.close()


def test_prepared_card_survives_restart_and_lost_write_response(tmp_path):
    store, browser = Store(tmp_path / "jobs.sqlite"), EnrichingBrowser()
    job = store.enqueue("pen", {"kind": "ingestion", "input": INPUT})
    browser.timeout_after_commit = True
    assert run(CardQueue(store, browser).process(job["id"]))["state"] == "uncertain"
    assert browser.enrichments == 1
    store.close()
    store = Store(tmp_path / "jobs.sqlite")
    assert store.enqueue("pen", {"kind": "ingestion", "input": INPUT})["id"] == job["id"]
    assert store.get_job(job["id"])["prepared_payload"]["fields"]["translation"]
    assert run(CardQueue(store, browser).process(job["id"]))["state"] == "synced"
    assert browser.enrichments == browser.creations == 1
    store.close()


def test_analysis_spans_use_inflected_surfaces_and_authoritative_status(tmp_path):
    class Parser(FakeBrowser):
        async def call(self, method, params):
            if method == "ParsingMgr_parseWords":
                return [{"dictForm": "読む", "secondary": "よむ", "partOfSpeech": ""}]
            if method == "WordListMgr_getMany":
                return [{"dictForm": "読む", "secondary": "よむ", "partOfSpeech": "", "knownStatus": "UNKNOWN"}]
            if method == "ParsingMgr_parseTokens":
                return [{"surface": "読みました", "start": 2, "terms": [{"term": "読む", "reading": "よむ", "pos": ""}]},
                        {"surface": "。", "start": 7, "terms": []}]
    with TestClient(create_app(tmp_path, Parser(), autostart=False, background=False)) as client:
        result = client.post("/text/analyze", json={"text": "📚読みました。"}).json()
        assert result["spans"] == [{"start": 2, "end": 7, "surface": "読みました", "status": "UNKNOWN"}]
