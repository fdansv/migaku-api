import asyncio
import hmac
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .browser import MigakuBrowser, WorkerError
from .models import CardSubmission, DeckRequest, Draft, Ingestion, TextRequest, TranslationRequest, WordFocus, WordStatus
from .queue import CardQueue, WordSyncQueue
from .store import Conflict, Store


def create_app(runtime=None, browser=None, autostart=None, background=True):
    runtime = Path(runtime or os.environ.get("MIGAKU_RUNTIME_DIR", Path(__file__).resolve().parent.parent / ".runtime")).resolve()
    if autostart is None:
        autostart = os.environ.get("MIGAKU_AUTOSTART", "1") != "0"
    store = Store(runtime / "migaku.sqlite")
    browser = browser or MigakuBrowser(runtime, headless=os.environ.get("MIGAKU_HEADLESS", "1") != "0")
    queue = CardQueue(store, browser)
    word_sync = WordSyncQueue(store, browser)
    token = os.environ.get("MIGAKU_API_TOKEN", "")
    security = HTTPBearer(auto_error=False)
    known_lock = asyncio.Lock()

    async def authorize(request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        if request.url.path == "/":
            return  # Public shell contains no account data; its API requests still require authentication.
        if token and (not credentials or not hmac.compare_digest(credentials.credentials, token)):
            raise HTTPException(401, "A valid API bearer token is required.", headers={"WWW-Authenticate": "Bearer"})

    async def refresh_index(language):
        try:
            return store.replace_words(language, await browser.export_words(language))
        except WorkerError as exc:
            raise HTTPException(503, {"code": exc.code, "message": str(exc)}) from exc
        except ValueError as exc:
            raise HTTPException(502, {"code": "schema_mismatch", "message": str(exc)}) from exc

    async def synchronize_vocabulary():
        while True:
            if (await browser.status())["ready"]:
                for language in os.environ.get("MIGAKU_LANGUAGES", "ja").split(","):
                    try:
                        await refresh_index(language.strip())
                    except HTTPException:
                        pass  # Preserve last successful snapshot; report freshness in API responses.
                await asyncio.sleep(int(os.environ.get("MIGAKU_REFRESH_SECONDS", "300")))
            else:
                await asyncio.sleep(5)

    @asynccontextmanager
    async def lifespan(app):
        tasks = []
        if autostart:
            try:
                await browser.start()
            except Exception as exc:
                browser.last_error = exc.code if isinstance(exc, WorkerError) else "browser_start_failed"
        if background:
            tasks = [asyncio.create_task(queue.run()), asyncio.create_task(synchronize_vocabulary()),
                     asyncio.create_task(word_sync.run())]
        yield
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        await browser.close()
        store.close()

    app = FastAPI(title="Migaku Local API", version="0.1.0", lifespan=lifespan,
                  dependencies=[Depends(authorize)], description="Local vocabulary snapshots, card drafts, and a durable Migaku Memory queue.")
    app.state.store, app.state.browser, app.state.queue = store, browser, queue
    app.state.word_sync = word_sync

    @app.exception_handler(WorkerError)
    async def worker_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"detail": {"code": exc.code, "message": str(exc)}})

    @app.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(Path(__file__).parent / "dashboard.html")

    @app.get("/health")
    async def health():
        return {"service": "ok", "worker_running": bool(browser.context),
                "extension_version": browser.core["version"] if browser.core else None,
                "index": store.words_stats("ja")}

    @app.get("/worker")
    async def worker_status():
        result = await browser.status()
        result["word_sync"] = {"pending": store.word_sync_pending(), "last_error": word_sync.last_error}
        return result

    @app.post("/worker/start")
    async def start_worker():
        await browser.start()
        return await browser.status()

    @app.get("/words")
    async def words(language: str = "ja", q: str = Query("", max_length=500), status: WordStatus | None = None,
                    limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        return store.search_words(language, q, status, limit, offset)

    @app.get("/words/stats")
    async def word_stats(language: str = "ja"):
        return store.words_stats(language)

    @app.post("/words/refresh")
    async def refresh_words(language: str = "ja"):
        return await refresh_index(language)

    @app.post("/words/known")
    async def mark_known(request: WordFocus):
        async with known_lock:
            await browser.require_ready()
            tokens = await browser.call("ParsingMgr_parseTokens", {"lang": request.language, "text": request.sentence})
            selected = [token for token in tokens if token.get("start") == request.focus_start
                        and token.get("surface") == request.focus_word and token.get("terms")
                        and not token.get("unrecognized") and not token.get("html")]
            if len(selected) != 1:
                raise HTTPException(422, "Select a single word that Migaku recognizes.")
            term = selected[0]["terms"][0]
            identity = {"dictForm": term["term"], "secondary": term.get("reading", ""),
                        "partOfSpeech": term.get("pos", "")}
            query = {"lang": request.language, "words": [identity]}
            # Setting KNOWN is idempotent. Omit optional tracking/card fields so
            # the status action preserves them, including when a reply is lost.
            await browser.call("WordListMgr_setWordStatus", {**identity, "language": request.language, "knownStatus": "KNOWN"})
            updated = await browser.call("WordListMgr_getMany", query)
            word = updated[0] if updated else None
            if not word or word.get("knownStatus") != "KNOWN" or any(word.get(k) != v for k, v in identity.items()):
                raise WorkerError("known_unconfirmed", "Migaku did not confirm the word status. Try again.")
            store.upsert_word(request.language, word, queue_sync=True)
            word_sync.wake.set()
            # Return the confirmed Migaku status without waiting for cloud I/O.
            # The durable revision also resumes its push after a service restart.
            return {"word": word, "synced": False, "sync_pending": True}

    @app.get("/decks")
    async def decks(language: str = "ja"):
        return {"decks": await browser.list_decks(language)}

    @app.post("/decks", status_code=201)
    async def create_deck(request: DeckRequest):
        await browser.require_ready()
        existing = next((deck for deck in await browser.list_decks(request.language) if deck["name"] == request.name), None)
        if existing:
            return existing
        deck_id = await browser.call("DeckMgr_add", {"info": {"id": None, "name": request.name,
            "lang": request.language, "icon": request.icon}})
        deck = await browser.call("DeckMgr_get", {"id": deck_id})
        await browser.push_sync()
        return deck

    @app.get("/card-types")
    async def card_types(language: str = "ja"):
        return {"card_types": await browser.list_card_types(language)}

    @app.post("/text/translate")
    async def translate_text(request: TranslationRequest):
        return await browser.translate_text(request.model_dump())

    @app.post("/text/analyze")
    async def analyze_text(request: TextRequest):
        await browser.require_ready()
        parsed = await browser.call("ParsingMgr_parseWords", {"lang": request.language, "text": request.text})
        identities = [{key: word[key] for key in ("dictForm", "secondary", "partOfSpeech")} for word in parsed]
        statuses = await browser.call("WordListMgr_getMany", {"lang": request.language, "words": identities}) if identities else []
        tokens = await browser.call("ParsingMgr_parseTokens", {"lang": request.language, "text": request.text})
        status_map = {(word["dictForm"], word["secondary"], word["partOfSpeech"]): word["knownStatus"]
                      for word in statuses if word}
        spans = []
        for token in tokens:
            terms = token.get("terms", [])
            if not terms or token.get("unrecognized") or token.get("html"):
                continue
            term = terms[0]
            status = status_map.get((term["term"], term.get("reading", ""), term.get("pos", "")))
            if status is not None:
                spans.append({"start": token["start"],
                              "end": token["start"] + len(token["surface"].encode("utf-16-le")) // 2,
                              "surface": token["surface"], "status": status})
        return {"language": request.language, "text": request.text, "parsed": parsed, "statuses": statuses,
                "spans": spans, "index": store.words_stats(request.language)}

    @app.post("/drafts", status_code=201)
    async def create_draft(draft: Draft):
        return store.save_draft(draft.model_dump())

    @app.get("/drafts")
    async def list_drafts():
        return {"drafts": store.list_drafts()}

    @app.get("/drafts/{draft_id}")
    async def get_draft(draft_id: str):
        result = store.get_draft(draft_id)
        if result is None:
            raise HTTPException(404, "Draft not found")
        return result

    @app.put("/drafts/{draft_id}")
    async def update_draft(draft_id: str, draft: Draft):
        if not store.get_draft(draft_id):
            raise HTTPException(404, "Draft not found")
        return store.save_draft(draft.model_dump(), draft_id)

    def enqueue(key, payload):
        if not key or len(key) > 128:
            raise HTTPException(400, "An Idempotency-Key header between 1 and 128 characters is required.")
        try:
            return store.enqueue(key, payload)
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/cards", status_code=202)
    async def create_card(card: CardSubmission, idempotency_key: str | None = Header(None)):
        return enqueue(idempotency_key, card.model_dump())

    @app.post("/ingest", status_code=202)
    async def ingest(request: Ingestion, idempotency_key: str | None = Header(None)):
        # Accept durably even while Migaku is offline. Enrichment happens in the
        # worker, before the write boundary, using Migaku's native generators.
        return enqueue(idempotency_key, {"kind": "ingestion", "input": request.model_dump()})

    @app.post("/drafts/{draft_id}/submit", status_code=202)
    async def submit_draft(draft_id: str, idempotency_key: str | None = Header(None)):
        draft = store.get_draft(draft_id)
        if not draft:
            raise HTTPException(404, "Draft not found")
        from pydantic import ValidationError
        try:
            payload = CardSubmission.model_validate(draft["payload"]).model_dump()
        except ValidationError:
            raise HTTPException(422, "Choose a deck_id and card_type_id before submitting this draft.")
        return enqueue(idempotency_key, payload)

    @app.get("/jobs")
    async def jobs():
        return {"jobs": store.jobs()}

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: str):
        job = store.get_job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return job

    @app.post("/jobs/{job_id}/reconcile")
    async def reconcile_job(job_id: str):
        if not store.get_job(job_id):
            raise HTTPException(404, "Job not found")
        return await queue.process(job_id)

    @app.get("/cards/{card_id}")
    async def get_card(card_id: int):
        card = await browser.get_card(card_id)
        if card is None:
            raise HTTPException(404, "Card not found in Migaku")
        return card

    return app
