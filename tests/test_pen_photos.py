import base64
import asyncio
import hashlib
import io
import os
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from migaku_api.app import create_app
from migaku_api.browser import WorkerError
from migaku_api.media import MediaStore
from migaku_api.models import PenPhoto
from migaku_api.queue import CardQueue
from migaku_api.store import Store
from tests.test_anki import FakeAnki
from tests.test_ingestion import INPUT, EnrichingBrowser
from tests.test_service import run


def photo(color=180):
    stream = io.BytesIO()
    Image.new("L", (264, 180), color).save(stream, "JPEG", quality=85)
    data = stream.getvalue()
    return {"sha256": hashlib.sha256(data).hexdigest(), "data": base64.b64encode(data).decode()}


class MediaAnki(FakeAnki):
    def __init__(self, media):
        super().__init__()
        self.media = media
        self.files = {}
        self.stores = 0
        self.fail_media = False

    async def call(self, action, **params):
        if action == "retrieveMediaFile":
            return self.files.get(params["filename"], False)
        if action == "storeMediaFile":
            if self.fail_media:
                raise WorkerError("anki_unavailable", "Media failed")
            self.stores += 1
            self.files[params["filename"]] = params["data"]
            return params["filename"]
        return await super().call(action, **params)


def test_ingest_photo_is_durable_bounded_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("MIGAKU_CARD_DESTINATION", "anki")
    browser = EnrichingBrowser()
    browser.ready = False
    app = create_app(tmp_path, browser, autostart=False, background=False)
    with TestClient(app) as client:
        request = {**INPUT, "photo": photo()}
        first = client.post("/ingest", json=request, headers={"Idempotency-Key": "manga-1"})
        assert first.status_code == 202
        saved = first.json()
        assert "data" not in str(saved["payload"])
        assert "photo" not in saved["payload"]["input"]
        reference = saved["payload"]["photo"]
        assert MediaStore(tmp_path / "pen-photos").read(reference)
        assert client.post("/ingest", json=request, headers={"Idempotency-Key": "manga-1"}).json()["id"] == saved["id"]
        assert client.post("/ingest", json={**INPUT, "photo": photo(50)}, headers={"Idempotency-Key": "manga-1"}).status_code == 409
        assert browser.enrichments == 0


@pytest.mark.parametrize("damage", ["digest", "base64", "truncated", "png", "oversized"])
def test_invalid_photos_never_reach_queue(tmp_path, monkeypatch, damage):
    monkeypatch.setenv("MIGAKU_CARD_DESTINATION", "anki")
    item = photo()
    data = base64.b64decode(item["data"])
    if damage == "digest":
        item["sha256"] = "0" * 64
    elif damage == "base64":
        item["data"] = "!!!!"
    else:
        if damage == "truncated": data = data[:-40]
        if damage == "png":
            buffer = io.BytesIO(); Image.new("L", (10, 10)).save(buffer, "PNG"); data = buffer.getvalue()
        if damage == "oversized": data = b"a" * 400001
        item = {"data": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest()}
    app = create_app(tmp_path, EnrichingBrowser(), autostart=False, background=False)
    with TestClient(app) as client:
        response = client.post("/ingest", json={**INPUT, "photo": item}, headers={"Idempotency-Key": "bad-photo"})
        assert response.status_code in (422, 503)
        assert app.state.store.jobs() == []


def test_lost_note_reply_recovers_same_note_and_repairs_missing_media(tmp_path):
    store, browser = Store(tmp_path / "queue.sqlite"), EnrichingBrowser()
    media = MediaStore(tmp_path / "photos")
    reference = media.save(PenPhoto(**photo()))
    anki = MediaAnki(media)
    job = store.enqueue("manga-1", {"kind": "ingestion", "input": INPUT, "photo": reference})
    queue = CardQueue(store, browser, anki)
    anki.lose_reply = True
    assert run(queue.process(job["id"]))["state"] == "uncertain"
    assert anki.adds == browser.enrichments == anki.stores == 1
    assert anki.saved[100]["fields"]["Screenshot"] == '<img src="' + reference["filename"] + '">'
    anki.files.clear()
    store.close()
    store = Store(tmp_path / "queue.sqlite")
    assert run(CardQueue(store, browser, anki).process(job["id"]))["state"] == "synced"
    assert anki.stores == 2 and anki.adds == browser.enrichments == 1
    assert store.get_job(job["id"])["prepared_payload"]["pen_photo"] == reference
    store.close()


def test_media_failure_is_before_write_and_keeps_prepared_card(tmp_path):
    store, browser = Store(tmp_path / "queue.sqlite"), EnrichingBrowser()
    media = MediaStore(tmp_path / "photos")
    reference = media.save(PenPhoto(**photo()))
    anki = MediaAnki(media); anki.fail_media = True
    queue = CardQueue(store, browser, anki)
    job = store.enqueue("manga-1", {"kind": "ingestion", "input": INPUT, "photo": reference})
    result = run(queue.process(job["id"]))
    assert not result["write_attempted"] and anki.adds == 0
    anki.fail_media = False
    assert run(queue.process(job["id"]))["state"] == "synced"
    assert anki.adds == browser.enrichments == 1
    store.close()


def test_existing_note_with_wrong_photo_requires_attention_without_second_add(tmp_path):
    store, browser = Store(tmp_path / "queue.sqlite"), EnrichingBrowser()
    media = MediaStore(tmp_path / "photos")
    reference = media.save(PenPhoto(**photo()))
    anki = MediaAnki(media); anki.lose_reply = True
    queue = CardQueue(store, browser, anki)
    job = store.enqueue("manga-1", {"kind": "ingestion", "input": INPUT, "photo": reference})
    run(queue.process(job["id"]))
    anki.saved[100]["fields"]["Screenshot"] = ""
    result = run(queue.process(job["id"]))
    assert result["state"] == "uncertain" and result["error"] == "anki_photo_field_mismatch"
    assert anki.adds == 1
    store.close()


def test_staging_cleanup_preserves_every_unfinished_photo_and_anki_copy(tmp_path):
    media = MediaStore(tmp_path / "photos")
    store = Store(tmp_path / "queue.sqlite")
    active = media.save(PenPhoto(**photo(10)))
    completed = media.save(PenPhoto(**photo(20)))
    orphan = media.save(PenPhoto(**photo(30)))
    pending = store.enqueue("pending", {"kind": "ingestion", "input": INPUT, "photo": active})
    done = store.enqueue("done", {"kind": "ingestion", "input": INPUT, "photo": completed})
    store.update_job(pending["id"], state="uncertain")
    store.update_job(done["id"], state="synced")
    for path in media.directory.iterdir():os.utime(path, (time.time()-8*86400,)*2)
    media.prune(store)
    assert media.read(active)
    assert not (media.directory / completed["filename"]).exists()
    assert not (media.directory / orphan["filename"]).exists()
    anki = MediaAnki(media)
    anki.files[completed["filename"]] = photo(20)["data"]
    run(anki.ensure_photo(completed))  # Anki copy remains usable after staging cleanup.
    assert anki.stores == 0
    store.close()


def test_hosted_photo_waits_for_media_ack_without_readding_note(tmp_path, monkeypatch):
    class HostedMediaAnki(MediaAnki):
        uploaded = False
        enabled = True
        enables = 0
        async def call(self, action, **params):
            if action == 'penEnsureMediaSync':
                self.enables += 1
                return {'enabled': True}
            if action == 'penMediaSyncStatus':
                return {'enabled': self.enabled, 'uploaded': self.uploaded}
            return await super().call(action, **params)
    store, browser = Store(tmp_path / 'jobs.sqlite'), EnrichingBrowser()
    media = MediaStore(tmp_path / 'photos')
    reference = media.save(PenPhoto(**photo()))
    anki = HostedMediaAnki(media);anki.sync_after_add = True
    queue = CardQueue(store, browser, anki)
    job = store.enqueue('await-media', {'kind':'ingestion', 'input':INPUT, 'photo':reference})
    result = run(queue.process(job['id']))
    assert result['state'] == 'created' and result['error'] == 'anki_media_sync_pending'
    assert anki.adds == browser.enrichments == 1
    anki.uploaded = True;anki.enabled = False
    disabled = run(queue.process(job['id']))
    assert disabled['state'] == 'created' and disabled['error'] == 'anki_media_sync_disabled'
    anki.enabled = True
    assert run(queue.process(job['id']))['state'] == 'synced'
    assert anki.adds == browser.enrichments == 1 and anki.enables == 1
    assert anki.syncs == 1  # Collection sync is not repeated on every media poll.
    store.close()


def test_waiting_photo_does_not_hold_up_next_text_card(tmp_path):
    class PendingMediaAnki(MediaAnki):
        async def call(self, action, **params):
            if action == 'penMediaSyncStatus':return {'enabled':True,'uploaded':False,'active':True}
            if action == 'addNote':
                self.adds += 1
                ident=100+self.adds
                self.saved[ident]={'noteId':ident,**params['note']}
                return ident
            return await super().call(action, **params)
    store,browser=Store(tmp_path/'queue.sqlite'),EnrichingBrowser()
    media=MediaStore(tmp_path/'photos');reference=media.save(PenPhoto(**photo()))
    anki=PendingMediaAnki(media);anki.sync_after_add=True
    queue=CardQueue(store,browser,anki)
    image=store.enqueue('waiting-photo',{'kind':'ingestion','input':INPUT,'photo':reference})
    text=store.enqueue('following-text',{'kind':'ingestion','input':{**INPUT,'sentence':INPUT['sentence']+'TEXT'}})
    async def process_both():
        first=await asyncio.wait_for(queue.process(image['id']),timeout=1)
        second=await asyncio.wait_for(queue.process(text['id']),timeout=1)
        return first,second
    first,second=run(process_both())
    assert first['state']=='created' and first['error']=='anki_media_sync_pending'
    assert second['state']=='synced' and anki.adds==2
    store.close()
