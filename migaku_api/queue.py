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
    def __init__(self, store, browser):
        self.store, self.browser = store, browser
        self.lock = asyncio.Lock()

    async def process(self, job_id):
        async with self.lock:
            job = self.store.get_job(job_id)
            if not job or job["state"] == "synced":
                return job
            try:
                await self.browser.require_ready()
                card = await self.browser.get_card(job["card_id"]) if job["card_id"] else (
                    await self.browser.find_job_card(job_id) if job["write_attempted"] else None)
                if card:
                    self.store.update_job(job_id, state="created", card_id=card["id"], error=None)
                elif job["write_attempted"]:
                    # A timed-out RPC may still complete inside the extension. Never blindly resubmit.
                    return self.store.update_job(job_id, state="uncertain", error="card_not_found_after_write_attempt")
                else:
                    payload = job.get("prepared_payload") or job["payload"]
                    if payload.get("kind") == "ingestion":
                        self.store.update_job(job_id, state="running", write_attempted=False, error=None)
                        payload = await self.browser.prepare_ingestion(payload["input"])
                        self.store.update_job(job_id, prepared_payload=payload)
                    await self.browser.validate_card(payload)
                    self.store.update_job(job_id, state="running", attempts=job["attempts"] + 1,
                                          write_attempted=True, error=None)
                    created_id = await self.browser.create_card(job_id, payload)
                    if not isinstance(created_id, int) or created_id <= 0:
                        return self.store.update_job(job_id, state="uncertain", error="creation_returned_no_card_id")
                    self.store.update_job(job_id, card_id=created_id)
                    card = await self.browser.get_card(created_id)
                    if not card:
                        return self.store.update_job(job_id, state="uncertain", error="creation_not_verified")
                    self.store.update_job(job_id, state="created", error=None)
                await self.browser.push_sync()
                return self.store.update_job(job_id, state="synced", error=None)
            except WorkerError as exc:
                current = self.store.get_job(job_id)
                state = "created" if current["state"] == "created" else "uncertain" if current["write_attempted"] else (
                    "failed" if exc.code in {"deck_missing", "card_type_missing", "missing_sentence_audio", "missing_word_audio",
                        "unsupported_enrichment_extension", "ingestion_destination_missing", "ingestion_language_mismatch",
                        "ingestion_dictionary_empty"} else "blocked")
                return self.store.update_job(job_id, state=state, error=exc.code)
            except Exception:
                current = self.store.get_job(job_id)
                state = "uncertain" if current["write_attempted"] else "failed"
                return self.store.update_job(job_id, state=state, error="unexpected_worker_error")

    async def run(self):
        while True:
            # Uncertain jobs need explicit reconciliation; synced jobs are terminal.
            jobs = self.store.jobs(["queued", "blocked", "created"])
            if jobs:
                status = await self.browser.status()
                if status["ready"]:
                    for job in jobs:
                        await self.process(job["id"])
            await asyncio.sleep(5)
