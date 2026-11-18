import copy

import pytest

from migaku_api.anki import AnkiDestination
from migaku_api.app import create_app
from migaku_api.browser import WorkerError
from migaku_api.queue import CardQueue
from migaku_api.store import Store
from tests.test_ingestion import EnrichingBrowser, INPUT
from tests.test_service import CARD, run


class FakeAnki(AnkiDestination):
    def __init__(self):
        super().__init__()
        self.saved = {}
        self.adds = 0
        self.lose_reply = False
        self.duplicate = False
        self.syncs = 0
        self.sync_failures = 0

    async def call(self, action, **params):
        if action == "sync":
            self.syncs += 1
            if self.sync_failures:
                self.sync_failures -= 1
                raise WorkerError("anki_unavailable", "Sync temporarily unavailable")
            return None
        if action == "deckNames":
            return [self.deck]
        if action == "modelNames":
            return [self.model]
        if action == "modelFieldNames":
            return list(self.note(CARD, "sample")["fields"])
        if action == "canAddNotes":
            return [not self.duplicate]
        if action == "addNote":
            self.adds += 1
            self.saved[100] = {"noteId": 100, **params["note"]}
            if self.lose_reply:
                raise WorkerError("anki_unavailable", "Lost reply")
            return 100
        if action == "notesInfo":
            return [self.saved.get(params["notes"][0], {})]
        if action == "findNotes":
            tag = params["query"].removeprefix("tag:")
            return [key for key, note in self.saved.items() if tag in note["tags"]]
        raise AssertionError(action)


def test_syntax_mapping_and_sentence_flags(monkeypatch):
    monkeypatch.setenv("ANKI_EXTRA_TAGS", "source::books pen")
    draft = copy.deepcopy(CARD)
    draft["fields"]["targetWord"] = "扉[とびら;h]"
    note = FakeAnki().note(draft, "pen-1")
    assert note["fields"]["Target Word [jpdb_furigana]"] == "扉[とびら;h]"
    assert note["fields"]["Is Vocabulary Card"] == note["fields"]["Is Audio Card"] == ""
    assert note["deckName"] == "Mining"
    assert "source::books" in note["tags"] and note["tags"].count("pen") == 1


def test_ingestion_writes_only_to_anki_and_recovers_lost_reply(tmp_path):
    store, browser, anki = Store(tmp_path / "queue.sqlite"), EnrichingBrowser(), FakeAnki()
    queue = CardQueue(store, browser, anki)
    job = store.enqueue("pen-1", {"kind": "ingestion", "input": INPUT})
    anki.lose_reply = True
    assert run(queue.process(job["id"]))["state"] == "uncertain"
    assert run(queue.process(job["id"]))["state"] == "synced"
    assert browser.creations == 0 and browser.enrichments == anki.adds == 1
    store.close()


def test_duplicate_is_rejected_before_write(tmp_path):
    store, browser, anki = Store(tmp_path / "queue.sqlite"), EnrichingBrowser(), FakeAnki()
    anki.duplicate = True
    job = store.enqueue("pen-1", CARD)
    result = run(CardQueue(store, browser, anki).process(job["id"]))
    assert not result["write_attempted"] and anki.adds == browser.creations == 0
    assert result["error"] == "anki_duplicate_or_invalid"
    store.close()


def test_media_requires_explicit_support():
    draft = copy.deepcopy(CARD)
    draft["fields"]["wordAudio"] = ["https://example.com/word.mp3"]
    with pytest.raises(WorkerError):
        FakeAnki().note(draft, "job")


def test_app_routes_to_anki_and_rejects_mixed_destination_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("MIGAKU_CARD_DESTINATION", "anki")
    app = create_app(tmp_path, EnrichingBrowser(), autostart=False, background=False)
    assert isinstance(app.state.queue.destination, AnkiDestination)
    app.state.store.enqueue("pen-1", CARD)
    app.state.store.close()
    monkeypatch.setenv("MIGAKU_CARD_DESTINATION", "migaku")
    with pytest.raises(ValueError, match="fresh MIGAKU_RUNTIME_DIR"):
        create_app(tmp_path, EnrichingBrowser(), autostart=False, background=False)


def test_hosted_sync_is_opt_in(monkeypatch):
    monkeypatch.delenv("ANKI_SYNC_AFTER_ADD", raising=False)
    anki = FakeAnki()
    run(anki.push_sync())
    assert not anki.sync_after_add and anki.syncs == 0
    monkeypatch.setenv("ANKI_SYNC_AFTER_ADD", "1")
    enabled = FakeAnki()
    run(enabled.push_sync())
    assert enabled.syncs == 1


def test_hosted_sync_failure_retries_sync_without_adding_or_preparing_again(tmp_path, monkeypatch):
    monkeypatch.setenv("ANKI_SYNC_AFTER_ADD", "1")
    store, browser, anki = Store(tmp_path / "queue.sqlite"), EnrichingBrowser(), FakeAnki()
    queue = CardQueue(store, browser, anki)
    job = store.enqueue("hosted-pen-1", {"kind": "ingestion", "input": INPUT})
    anki.sync_failures = 1
    first = run(queue.process(job["id"]))
    assert first["state"] == "created" and first["card_id"] == 100
    assert anki.adds == browser.enrichments == anki.syncs == 1
    assert run(queue.process(job["id"]))["state"] == "synced"
    assert anki.syncs == 2 and anki.adds == browser.enrichments == 1
    assert run(queue.process(job["id"]))["state"] == "synced"
    assert anki.syncs == 2
    store.close()
