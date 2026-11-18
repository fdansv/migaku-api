import asyncio

from .browser import WorkerError


class WordSyncQueue:
    """Confirm vocabulary immediately; retry its durable cloud push separately."""

    def __init__(self, store, browser):
        self.store, self.browser = store, browser
        self.wake = asyncio.Event()
        self.last_error = None

    async def process(self):
        if not self.store.word_sync_pending():
            return True
        revision = self.store.word_sync_revision()
        try:
            await self.browser.push_sync()
        except Exception as exc:
            self.last_error = exc.code if isinstance(exc, WorkerError) else "word_sync_failed"
            return False
        self.store.complete_word_sync(revision)
        self.last_error = None
        return True

    async def run(self):
        while True:
            self.wake.clear()
            if self.store.word_sync_pending():
                if not await self.process():
                    await asyncio.sleep(5)
            else:
                await self.wake.wait()


class CardQueue:
    def __init__(self, store, browser, destination=None):
        self.store, self.browser = store, browser
        self.lock = asyncio.Lock()
        self.destination = destination or browser

    async def process(self, job_id):
        async with self.lock:
            job = self.store.get_job(job_id)
            if not job or job["state"] == "synced":
                return job
            photo = (job.get('prepared_payload') or {}).get('pen_photo') or job['payload'].get('photo')
            try:
                await self.browser.require_ready()
                card = await self.destination.get_card(job["card_id"]) if job["card_id"] else (
                    await self.destination.find_job_card(job_id) if job["write_attempted"] else None)
                if card:
                    if job["payload"].get("photo"):
                        await self.destination.verify_photo(card, photo)
                    self.store.update_job(job_id, state="created", card_id=card["id"], error=None)
                elif job["write_attempted"]:
                    # A timed-out RPC may still complete inside the extension. Never blindly resubmit.
                    return self.store.update_job(job_id, state="uncertain", error="card_not_found_after_write_attempt")
                else:
                    payload = job.get("prepared_payload") or job["payload"]
                    if payload.get("kind") == "ingestion":
                        self.store.update_job(job_id, state="running", write_attempted=False, error=None)
                        payload = await self.browser.prepare_ingestion(payload["input"])
                        if job["payload"].get("photo"):
                            photo = job['payload']['photo']
                            payload["pen_photo"] = photo
                        self.store.update_job(job_id, prepared_payload=payload)
                    await self.destination.validate_card(payload)
                    if payload.get("pen_photo"):
                        await self.destination.ensure_photo(payload["pen_photo"])
                    self.store.update_job(job_id, state="running", attempts=job["attempts"] + 1,
                                          write_attempted=True, error=None)
                    created_id = await self.destination.create_card(job_id, payload)
                    if not isinstance(created_id, int) or created_id <= 0:
                        return self.store.update_job(job_id, state="uncertain", error="creation_returned_no_card_id")
                    self.store.update_job(job_id, card_id=created_id)
                    card = await self.destination.get_card(created_id)
                    if not card:
                        return self.store.update_job(job_id, state="uncertain", error="creation_not_verified")
                    if job["payload"].get("photo"):
                        await self.destination.verify_photo(card, photo)
                    self.store.update_job(job_id, state="created", error=None)
                if job["payload"].get("photo"):
                    prepared = self.store.get_job(job_id).get("prepared_payload") or {}
                    if not prepared.get("pen_collection_synced"):
                        await self.destination.push_sync()
                        prepared["pen_collection_synced"] = True
                        self.store.update_job(job_id, prepared_payload=prepared)
                    await self.destination.sync_photo(photo)
                else:
                    await self.destination.push_sync()
                return self.store.update_job(job_id, state="synced", error=None)
            except WorkerError as exc:
                current = self.store.get_job(job_id)
                state = "created" if current["state"] == "created" else "uncertain" if current["write_attempted"] else (
                    "failed" if exc.code in {"deck_missing", "card_type_missing", "missing_sentence_audio", "missing_word_audio",
                        "unsupported_enrichment_extension", "ingestion_destination_missing", "ingestion_language_mismatch",
                        "ingestion_dictionary_empty", "ingestion_ai_failed", "ingestion_ai_timeout",
                        "ingestion_timeout", "ingestion_generation_failed", "anki_field_mismatch", "anki_empty_text",
                        "anki_duplicate_or_invalid", "anki_media_unsupported"} else "blocked")
                return self.store.update_job(job_id, state=state, error=exc.code)
            except Exception:
                current = self.store.get_job(job_id)
                state = "uncertain" if current["write_attempted"] else "failed"
                return self.store.update_job(job_id, state=state, error="unexpected_worker_error")

    async def run(self):
        while True:
            # Uncertain and failed jobs need explicit investigation/reconciliation.
            # In particular, never repeat charged generation automatically after
            # an AI failure or timeout. Synced jobs are terminal.
            jobs = self.store.jobs(["queued", "blocked", "created"])
            if jobs:
                status = await self.browser.status()
                if status["ready"]:
                    for job in jobs:
                        await self.process(job["id"])
            await asyncio.sleep(5)
