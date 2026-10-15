import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

from playwright.async_api import async_playwright

from .credentials import CredentialsError, load_credentials

ENRICHMENT_ASSETS = {
    '1.31.0.16': {
        'player-store-bd8f4b49.js': 'e2674057579ef893eb83f82cab8d3814679085626073c9996896d62113a55b3f',
        'UiCallout-7750a374.js': '619866c53648d4e02a0b75b9355732fa9318178e4751c9f9c6614f3a62c54f3c',
    },
    '1.31.0.17': {
        'player-store-f1fc3b5f.js': 'df10166c3e4be387c0602e2f99480455fb8db34d2b166dd1038b890639746bda',
        'UiCallout-509f2f89.js': 'c762b9fa59d0528c8fd8f1a7202791f535d264a67c498925dd913f210773bf63',
    },
}
ENRICHMENT_EXPORTS = {
    '1.31.0.16': {'core': 'd', 'settings': 'u', 'format': 'ah', 'dictionary': 'n', 'translation': 'd', 'explanation': 'C'},
    '1.31.0.17': {'core': 'd', 'settings': 'u', 'format': 'ah', 'dictionary': 'n', 'translation': 'T', 'explanation': 'x'},
}
EXPERIMENTAL_VERSION = '1.31.0.17'


def experimental_profile_enabled():
    return os.environ.get('MIGAKU_ENABLE_EXPERIMENTAL_131017') == '1'


def enrichment_compatible(extension: Path, version: str) -> bool:
    assets = ENRICHMENT_ASSETS.get(version)
    return bool(assets) and all(
        (extension / 'assets' / name).is_file() and
        hashlib.sha256((extension / 'assets' / name).read_bytes()).hexdigest() == digest
        for name, digest in assets.items()
    )

class WorkerError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def find_extension() -> Path | None:
    configured = os.environ.get("MIGAKU_EXTENSION_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    root = Path.home() / ".config/google-chrome/Default/Extensions"
    candidates = []
    for manifest in root.glob("*/*/manifest.json"):
        info = json.loads(manifest.read_text())
        name = info.get("name", "")
        if name.startswith("__MSG_"):
            messages = manifest.parent / "_locales" / info.get("default_locale", "en") / "messages.json"
            if messages.is_file():
                name = json.loads(messages.read_text()).get(name[6:-2], {}).get("message", name)
        if "migaku" in name.lower():
            version = tuple(int(x) for x in info["version"].split("."))
            candidates.append((version, manifest.parent))
    return max(candidates, default=(None, None), key=lambda x: x[0])[1]


def discover_core(extension: Path) -> dict:
    """Discover the RPC factory instead of depending on a minified export name."""
    manifest = json.loads((extension / "manifest.json").read_text())
    for asset in (extension / "assets").glob("*.js"):
        text = asset.read_text(errors="replace")
        if '"WordListMgr.queryWords"' not in text:
            continue
        factory = re.search(r"([\w$]+)=\(\)=>new Proxy\(\{\},\{get:", text)
        if not factory:
            continue
        exported = text[text.rfind("export{"):]
        alias = re.search(r"(?:\{|,)" + re.escape(factory[1]) + r" as ([\w$]+)(?:,|\})", exported)
        if alias:
            return {"version": manifest["version"],
                    "module": str(asset.relative_to(extension)), "export": alias[1]}
    raise WorkerError("unsupported_extension", "Could not locate Migaku's core RPC factory in this extension version.")



class MigakuBrowser:
    def __init__(self, runtime: Path, extension: Path | None = None, headless: bool = True):
        self.runtime = runtime
        self.extension = extension or find_extension()
        self.headless = headless
        self.context = self.page = self.playwright = None
        self.core = None
        self.lock = asyncio.Lock()
        self.enrichment_lock = asyncio.Lock()
        self.translation_lock = asyncio.Lock()
        self.sync_lock = asyncio.Lock()
        self.auth_lock = asyncio.Lock()
        self.credentials_file = Path(os.environ.get(
            "MIGAKU_CREDENTIALS_FILE", Path(__file__).resolve().parent.parent / "CREDS"
        )).expanduser()
        self.auth_retry_at = 0.0
        self.auth_error = None
        self.last_error = None

    async def start(self):
        async with self.lock:
            if self.context:
                return
            if not self.extension:
                raise WorkerError("extension_missing", "Set MIGAKU_EXTENSION_PATH to an installed Migaku extension directory.")
            self.core = discover_core(self.extension)
            self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.playwright = await async_playwright().start()
            try:
                self.context = await self.playwright.chromium.launch_persistent_context(
                    str(self.runtime / "browser-profile"), channel="chromium",
                    headless=self.headless, viewport={"width": 1200, "height": 850},
                    args=[f"--disable-extensions-except={self.extension}",
                          f"--load-extension={self.extension}"],
                )
                workers = self.context.service_workers
                worker = workers[0] if workers else await self.context.wait_for_event("serviceworker", timeout=30_000)
                self.extension_id = worker.url.split("/")[2]
                self.page = await self.context.new_page()
                await self.page.goto(f"chrome-extension://{self.extension_id}/pages/app-window/index.html")
                await self.page.wait_for_load_state("domcontentloaded")
                self.last_error = None
            except Exception:
                await self._close()
                raise

    async def _close(self):
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
        self.context = self.page = self.playwright = None

    async def close(self):
        async with self.lock:
            await self._close()

    async def call(self, method: str, params: dict | None = None, timeout: float = 45):
        if not self.context:
            raise WorkerError("worker_stopped", "The Migaku browser is not running.")
        # The native RPC layer correlates each request independently. Holding a
        # shared lock while AI generation/sync waits blocks vocabulary actions.
        try:
            result = await asyncio.wait_for(self.page.evaluate("""async ({module, name, method, params}) => {
                const imported = await import(chrome.runtime.getURL(module));
                const core = imported[name]();
                try {
                    const value = await core[method](params);
                    return value === undefined ? {ok:false, code:'not_ready'} : {ok:true, value};
                } catch (error) {
                    // Core error messages can contain request fields. Do not send them to logs/API clients.
                    return {ok:false, code:'core_error', message: error.coreError?.message || 'Migaku rejected the request'};
                }
            }""", {"module": self.core["module"], "name": self.core["export"],
                    "method": method, "params": params or {}}), timeout)
        except asyncio.TimeoutError as exc:
            raise WorkerError("timeout", "Migaku did not respond before the operation timeout.") from exc
        except Exception as exc:
            raise WorkerError("browser_error", "The Migaku browser connection failed.") from exc
        if not result["ok"]:
            self.last_error = {"code": result["code"], "message": str(result.get("message", "Migaku is not ready"))[:300]}
            raise WorkerError(result["code"], str(result.get("message", "Migaku is still starting or needs login."))[:300])
        return result.get("value")

    async def recover_authentication(self):
        # Only sign in after an explicit logged-out result, never after a network
        # or browser error. Concurrent requests share one bounded attempt.
        async with self.auth_lock:
            if not await self._login_form_visible() and await self.call("Firebase_waitForAuthCheckDone", timeout=15):
                self.auth_retry_at = 0.0
                self.auth_error = None
                return True
            if time.monotonic() < self.auth_retry_at:
                return False
            self.auth_retry_at = time.monotonic() + 300
            try:
                credentials = load_credentials(self.credentials_file)
            except CredentialsError as exc:
                self.auth_error = str(exc)
                return False
            if credentials is None:
                self.auth_error = None
                return False
            self.auth_error = "login_failed"
            try:
                signed_in = await asyncio.wait_for(self._sign_in(credentials), timeout=45)
                if signed_in and await self.call("Firebase_waitForAuthCheckDone", timeout=15):
                    self.auth_retry_at = 0.0
                    self.auth_error = self.last_error = None
                    return True
            except Exception:
                # Playwright exceptions may include evaluation arguments; keep
                # both exception text and credentials out of logs/API responses.
                pass
            return False

    async def _login_form_visible(self):
        try:
            return await self.page.locator('form input[type="email"]').is_visible()
        except Exception:
            return False

    async def _sign_in(self, credentials):
        # Use Migaku's complete login form: the bare Firebase sign-in RPC omits
        # subscription checks and app/database initialization. A separate page
        # preserves in-flight RPCs on the worker's main page.
        use_main_page = await self._login_form_visible()
        page = self.page if use_main_page else await self.context.new_page()
        try:
            if not use_main_page:
                await page.goto(f"chrome-extension://{self.extension_id}/pages/app-window/index.html")
            notice = page.get_by_role('button', name='OK, got it', exact=True)
            if await notice.is_visible():
                # Fresh Chromium profiles show an informational browser notice
                # over the login form. It must be acknowledged before clicking.
                await notice.click(timeout=5_000)
            await page.locator('input[type="email"]').fill(credentials.email, timeout=15_000)
            await page.locator('input[type="password"]').fill(credentials.password, timeout=15_000)
            # The interface-language menu also has a submit button. Scope this
            # locator to the login form so it cannot select that unrelated button.
            # The fresh-profile page animates its layout in headless Chromium.
            # Activate this exact native button without waiting for a stationary
            # pointer target; Migaku still handles normal form validation/login.
            await page.locator('form button[type="submit"]').evaluate('(button) => button.click()', timeout=15_000)
            await page.locator('input[type="email"]').wait_for(state="hidden", timeout=30_000)
            return True
        finally:
            if not use_main_page:
                await page.close()

    async def status(self):
        result = {"running": bool(self.context), "headless": self.headless,
                  "version": self.core["version"] if self.core else None,
                  "authenticated": False, "ready": False, "last_error": self.last_error}
        if not self.context:
            result["error"] = self.last_error
            return result
        try:
            # A brand-new profile displays a login form before its core RPC
            # handler is ready. The visible form is an explicit logged-out state.
            form_visible = await self._login_form_visible()
            result["authenticated"] = False if form_visible else bool(await self.call("Firebase_waitForAuthCheckDone", timeout=15))
            if not result["authenticated"]:
                result["authenticated"] = await self.recover_authentication()
            if result["authenticated"]:
                result["initial_sync_done"] = bool(await self.call("Syncer_isInitialPullSyncDone", timeout=15))
                result["ready"] = result["initial_sync_done"]
            else:
                result["error"] = self.auth_error or "login_required"
        except WorkerError as exc:
            result["error"] = exc.code
        result["last_error"] = self.last_error
        return result

    async def require_ready(self):
        status = await self.status()
        if not status["ready"]:
            raise WorkerError(status.get("error", "initial_sync_pending"), "Migaku login or initial sync is pending. Check /worker for recovery status.")

    async def export_words(self, language: str):
        await self.require_ready()
        words = await self.call("WordListMgr_getAll", {"language": language})
        if not isinstance(words, list):
            raise WorkerError("schema_mismatch", "Migaku's vocabulary response is not a list; the existing index was preserved.")
        return words

    async def list_decks(self, language: str):
        await self.require_ready()
        ids = await self.call("DeckMgr_idsByLang", {"lang": language, "includeLearningMaterials": False})
        decks = []
        for deck_id in ids:
            decks.append(await self.call("DeckMgr_get", {"id": deck_id}))
        return decks

    async def list_card_types(self, language: str):
        await self.require_ready()
        ids = await self.call("CardTypeMgr_idsByLang", {"lang": language})
        return await self.call("CardTypeMgr_getMany", {"ids": ids})

    async def prepare_ingestion(self, request: dict):
        await self.require_ready()
        modules = self.check_enrichment_assets()
        try:
            async with self.enrichment_lock:
                result = await asyncio.wait_for(self.page.evaluate(
                    (Path(__file__).parent / "enrich.js").read_text(), {
                        **modules, "input": request,
                        "deckId": int(os.environ.get("MIGAKU_INGEST_DECK_ID", "0")),
                        "cardTypeId": int(os.environ.get("MIGAKU_INGEST_CARD_TYPE_ID", "0")),
                    }), timeout=180)
        except asyncio.TimeoutError as exc:
            raise WorkerError("ingestion_timeout", "Migaku's content generation timed out before card creation.") from exc
        except Exception as exc:
            raise WorkerError("ingestion_generation_failed", "Migaku could not fill the card. The sentence is retained for retry.") from exc
        if result.get("error"):
            raise WorkerError(result["error"], "Migaku could not fill the card. The sentence is retained for retry.")
        from .models import CardSubmission
        return CardSubmission.model_validate(result).model_dump()

    def check_enrichment_assets(self):
        if not enrichment_compatible(self.extension, self.core['version']):
            raise WorkerError("unsupported_enrichment_extension", "Migaku's Card Creator adapter needs a compatibility review.")
        if self.core['version'] == EXPERIMENTAL_VERSION and not experimental_profile_enabled():
            raise WorkerError('experimental_extension_disabled',
                'Migaku EA 1.31.0.17 needs explicit experimental opt-in; see the setup guide.')
        core, helper = ENRICHMENT_ASSETS[self.core['version']]
        return {'coreModule': 'assets/' + core, 'helperModule': 'assets/' + helper,
                'exports': ENRICHMENT_EXPORTS[self.core['version']]}

    async def translate_text(self, request: dict):
        await self.require_ready()
        async def generate():
            modules = self.check_enrichment_assets()
            async with self.translation_lock:
                return await self.page.evaluate((Path(__file__).parent / "translate.js").read_text(), {
                    **modules, "input": request,
                })

        try:
            result = await asyncio.wait_for(generate(), timeout=25)
        except asyncio.TimeoutError as exc:
            raise WorkerError("translation_timeout", "Migaku's translation timed out. Try again.") from exc
        except WorkerError:
            raise
        except Exception as exc:
            raise WorkerError("translation_failed", "Migaku could not translate this text. Try again.") from exc
        if not isinstance(result, dict) or result.get("error") or not result.get("translation"):
            raise WorkerError(result.get("error", "translation_failed") if isinstance(result, dict) else "translation_failed",
                              "Migaku could not translate this text. Try again.")
        return result

    async def create_card(self, job_id: str, draft: dict):
        await self.require_ready()
        # Migaku generates its own numeric ID, even when CardInfo.id is supplied.
        # Preserve a durable job marker in metadata so an interrupted response can be reconciled.
        supplied = draft["fields"]
        # CardInfo.fields is an ordered string array, exactly as Card Creator serializes it.
        fields = [supplied.get(key, "") for key in (
            "targetWord", "sentence", "translation", "definitions", "exampleSentences", "notes")]
        fields.extend("|".join(supplied.get(key, [])) for key in ("images", "sentenceAudio", "wordAudio"))
        fields.append(json.dumps({**supplied.get("metadata", {}), "migakuApiJobId": job_id}, ensure_ascii=False))
        return await self.call("CardMgr_add", {"info": {"id": None,
            "deckId": draft["deck_id"], "typeId": draft["card_type_id"], "fields": fields}})

    async def validate_card(self, draft: dict):
        decks = await self.list_decks(draft["language"])
        types = await self.list_card_types(draft["language"])
        if draft["deck_id"] not in {d["id"] for d in decks}:
            raise WorkerError("deck_missing", "The destination deck does not exist for this language.")
        if draft["card_type_id"] not in {t["id"] for t in types}:
            raise WorkerError("card_type_missing", "The card type does not exist for this language.")
        card_type = next(t for t in types if t["id"] == draft["card_type_id"])
        for index, field, code in [(7, "sentenceAudio", "missing_sentence_audio"), (8, "wordAudio", "missing_word_audio")]:
            if index in card_type.get("front", []) and not draft["fields"].get(field):
                raise WorkerError(code, "An audio card needs its front-side audio before it can be submitted.")

    async def get_card(self, card_id: int):
        await self.require_ready()
        # Migaku's get method throws a specific missing-row error rather than returning null.
        try:
            return await self.call("CardMgr_get", {"id": card_id})
        except WorkerError as exc:
            if exc.code == "core_error" and "ResultSet returned null for Card.sq:get" in str(exc):
                self.last_error = None
                return None
            raise

    async def find_job_card(self, job_id: str):
        await self.require_ready()
        cards = await self.call("CardMgr_getAll", timeout=90)
        matches = []
        for card in cards:
            for value in card.get("fields", [])[9:]:
                try:
                    metadata = json.loads(value)
                except (ValueError, TypeError):
                    continue
                if isinstance(metadata, dict) and metadata.get("migakuApiJobId") == job_id:
                    matches.append(card)
                    break
        if len(matches) > 1:
            raise WorkerError("duplicate_job_marker", "Multiple Migaku cards carry this job marker; manual inspection is required.")
        return matches[0] if matches else None

    async def push_sync(self):
        # Serialize pushes and their shared event stream, but leave other RPCs
        # available throughout the cloud round trip.
        async with self.sync_lock:
            return await self._push_sync()

    async def _push_sync(self):
        await self.require_ready()
        async with self.lock:
            await self.page.evaluate("""async ({module, name}) => {
                window.__migakuApiPushEvents = [];
                if (!window.__migakuApiSyncListener) {
                    const imported = await import(chrome.runtime.getURL(module));
                    const core = imported[name]();
                    await core.on({event:'pushSyncScheduled', callback: () => {
                        window.__migakuApiPushScheduled = true;
                        // Discard states from before this cycle began. A clean
                        // push can emit only IDLE because state observers coalesce
                        // the very brief IN_PROGRESS transition.
                        window.__migakuApiPushEvents = [];
                    }});
                    await core.on({event:'pushSyncState', callback: state => {
                        window.__migakuApiPushState = state;
                        window.__migakuApiPushEvents.push(state);
                    }});
                    window.__migakuApiSyncListener = true;
                }
                window.__migakuApiPushScheduled = false;
                window.__migakuApiPushEvents = [];
            }""", {"module": self.core["module"], "name": self.core["export"]})
        await self.call("Syncer_pushSync", timeout=90)
        for _ in range(45):
            state = await self.page.evaluate("""() => ({state: window.__migakuApiPushState,
                scheduled: window.__migakuApiPushScheduled,
                events: window.__migakuApiPushEvents || []})""")
            if state["state"] and state["state"].get("state") == "ERROR":
                self.last_error = {"code": "sync_error", "message": str(state["state"].get("msg", "Push synchronization failed"))[:300]}
                raise WorkerError("sync_error", self.last_error["message"])
            if (state["scheduled"] and state["events"]
                    and state["events"][-1].get("state") == "IDLE"):
                self.last_error = None
                return
            await asyncio.sleep(1)
        raise WorkerError("sync_unconfirmed", "Migaku did not confirm completion of push synchronization.")
