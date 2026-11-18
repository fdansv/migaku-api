"""Anki destination for prepared Migaku text cards; pen protocol stays unchanged."""
import asyncio
import base64
import hashlib
import json
import os
from urllib.request import Request, urlopen

from .browser import WorkerError
from .media import MediaStore

class AnkiDestination:
    def __init__(self, endpoint=None, deck=None, model=None, api_key=None, sync_after_add=None, media=None):
        self.media = media
        self.endpoint = endpoint or os.environ.get("ANKICONNECT_URL", "http://127.0.0.1:8765")
        self.deck = deck or os.environ.get("ANKI_DECK", "Mining")
        self.model = model or os.environ.get("ANKI_MODEL", "TranslatePen Japanese")
        self.api_key = api_key or os.environ.get("ANKICONNECT_API_KEY", "")
        self.extra_tags = os.environ.get("ANKI_EXTRA_TAGS", "").split()
        # Opt-in for a future hosted Anki profile. Existing laptop sessions keep
        # their manual sync behavior until this is explicitly enabled.
        self.sync_after_add = (os.environ.get("ANKI_SYNC_AFTER_ADD", "0") == "1"
                               if sync_after_add is None else sync_after_add)

    async def call(self, action, **params):
        payload = {"action": action, "version": 6, "params": params}
        if self.api_key:
            payload["key"] = self.api_key

        def request():
            req = Request(self.endpoint, json.dumps(payload).encode("utf-8"),
                          {"Content-Type": "application/json"})
            try:
                with urlopen(req, timeout=90 if action == "sync" else 10) as response:
                    result = json.load(response)
            except Exception as exc:
                raise WorkerError("anki_unavailable", "AnkiConnect request failed; check Anki is running.") from exc
            if not isinstance(result, dict) or "error" not in result or "result" not in result:
                raise WorkerError("anki_invalid_response", "Unexpected AnkiConnect response.")
            if result["error"]:
                raise WorkerError("anki_error", str(result["error"]))
            return result["result"]

        return await asyncio.to_thread(request)

    @staticmethod
    def marker(job_id):
        return "translatepen_job_" + hashlib.sha256(job_id.encode()).hexdigest()

    def note(self, draft, job_id):
        fields = draft["fields"]
        # URLs in Migaku media fields are not yet Anki media files. Reject them
        # instead of creating a card that appears to have working audio/images.
        if any(fields.get(name) for name in ("images", "wordAudio", "sentenceAudio")):
            raise WorkerError("anki_media_unsupported", "This first adapter supports text sentence cards only.")
        return {
            "deckName": self.deck, "modelName": self.model,
            "fields": {
                "Sentence": fields.get("sentence", ""),
                "Translation": fields.get("translation", ""),
                "Target Word [jpdb_furigana]": fields.get("targetWord", ""),
                "Definitions": fields.get("definitions", ""),
                "Example Sentences": fields.get("exampleSentences", ""),
                "Screenshot": ('<img src="' + MediaStore.filename(draft["pen_photo"]) + '">' if draft.get("pen_photo") else ""), "Sentence Audio": "", "Word Audio": "", "Images": "",
                "Is Vocabulary Card": "", "Is Audio Card": "",
            },
            "options": {"allowDuplicate": False},
            "tags": list(dict.fromkeys(["pen", "translatepen", self.marker(job_id), *self.extra_tags])),
        }

    async def validate_card(self, draft):
        if self.deck not in await self.call("deckNames"):
            raise WorkerError("deck_missing", "The configured Anki deck does not exist.")
        if self.model not in await self.call("modelNames"):
            raise WorkerError("card_type_missing", "The configured Anki note type does not exist.")
        note = self.note(draft, "validation")
        actual = await self.call("modelFieldNames", modelName=self.model)
        if set(note["fields"]) != set(actual):
            raise WorkerError("anki_field_mismatch", "Anki note fields differ from the configured mapping.")
        if not note["fields"]["Sentence"] or not note["fields"]["Target Word [jpdb_furigana]"]:
            raise WorkerError("anki_empty_text", "Sentence and target word must both be present.")
        if await self.call("canAddNotes", notes=[note]) != [True]:
            raise WorkerError("anki_duplicate_or_invalid", "Anki rejected the draft, possibly because its sentence already exists.")

    async def create_card(self, job_id, draft):
        # The queue records the write boundary first. A lost reply is reconciled
        # by the job tag; no automatic second addNote is attempted.
        return await self.call("addNote", note=self.note(draft, job_id))

    async def ensure_photo(self, reference):
        filename = MediaStore.filename(reference)
        existing = await self.call("retrieveMediaFile", filename=filename)
        try:
            good = existing and hashlib.sha256(base64.b64decode(existing, validate=True)).hexdigest() == reference["sha256"]
        except Exception:
            good = False
        if good:
            return
        if self.media is None:
            raise WorkerError("anki_photo_missing", "Photo storage unavailable.")
        encoded = base64.b64encode(self.media.read(reference)).decode("ascii")
        result = await self.call("storeMediaFile", filename=filename, data=encoded)
        check = await self.call("retrieveMediaFile", filename=filename)
        try:
            verified = result == filename and hashlib.sha256(base64.b64decode(check, validate=True)).hexdigest() == reference["sha256"]
        except Exception:
            verified = False
        if not verified:
            raise WorkerError("anki_photo_unverified", "Anki did not confirm the photo; request saved.")

    async def verify_photo(self, card, reference):
        expected = '<img src="' + MediaStore.filename(reference) + '">'
        field = card.get("fields", {}).get("Screenshot", "")
        actual = field.get("value", "") if isinstance(field, dict) else field
        if actual != expected:
            raise WorkerError("anki_photo_field_mismatch", "Anki note exists but its photo field needs attention.")
        await self.ensure_photo(reference)

    async def get_card(self, note_id):
        notes = await self.call("notesInfo", notes=[note_id])
        if not notes or not notes[0] or notes[0].get("noteId") != note_id:
            return None
        return {"id": note_id, "fields": notes[0]["fields"], "tags": notes[0]["tags"]}

    async def find_job_card(self, job_id):
        ids = await self.call("findNotes", query="tag:" + self.marker(job_id))
        if len(ids) > 1:
            raise WorkerError("duplicate_job_marker", "Multiple Anki notes carry this job marker.")
        return await self.get_card(ids[0]) if ids else None

    async def push_sync(self):
        # The queue has already saved and verified the note. If sync fails, its
        # state remains 'created'; retry sync without adding the note again.
        # AnkiConnect/Anki handle normal sync. Never force a full upload/download
        # or alter note types to get past a conflict. Phone receipt is separate.
        if self.sync_after_add:
            await self.call("sync")

    async def sync_photo(self, reference):
        if not self.sync_after_add:
            return  # Laptop mode still uses its normal manual sync.
        filenames = [MediaStore.filename(reference)]
        result = await self.call("penMediaSyncStatus", filenames=filenames)
        if result.get("enabled") is True and result.get("uploaded") is True:
            return
        if result.get("enabled") is False:
            raise WorkerError("anki_media_sync_disabled", "Enable media sync in Anki to upload the saved photo.")
        if not result.get("active"):
            await self.call("penEnsureMediaSync", filenames=filenames)
        # Media syncing continues in Anki's background worker. Do not hold the
        # card queue lock for a minute per photo while older media downloads.
        raise WorkerError("anki_media_sync_pending", "Card saved; its image is still waiting for AnkiWeb upload.")
