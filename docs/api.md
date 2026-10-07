# API reference

## API

| Route | Behavior |
|---|---|
| `GET /health` | API availability and vocabulary snapshot freshness |
| `GET /worker` | Browser, authentication, initial-sync readiness |
| `GET /words?language=ja&q=図書館&status=known` | Search headwords/readings; status filter and pagination |
| `GET /words/stats?language=ja` | Counts and last successful refresh |
| `POST /words/refresh?language=ja` | Replace a language's index after a successful full export |
| `POST /words/known` | Mark the selected sentence span known, using Migaku's parsed identity and verified readback |
| `POST /text/analyze` | Migaku parsing plus current word statuses |
| `GET /decks?language=ja` | Actual destination decks and IDs |
| `POST /decks` | Create/find a destination deck by name and language; uses Migaku's study defaults |
| `GET /card-types?language=ja` | Actual card types, IDs, and field descriptions |
| `POST /drafts` | Save populated fields locally without creating a Migaku card |
| `GET /drafts`, `GET /drafts/{id}`, `PUT /drafts/{id}` | Retrieve/edit drafts |
| `POST /drafts/{id}/submit` | Queue an existing draft with a chosen deck/type |
| `POST /cards` | Queue a populated card directly |
| `POST /ingest` | Queue a Japanese sentence and selected focus span; Migaku fills text fields before creating the card |
| `GET /jobs`, `GET /jobs/{id}` | Creation, verification, synchronization outcomes |
| `POST /jobs/{id}/reconcile` | Look for a previously attempted write and retry sync safely |
| `GET /cards/{id}` | Retrieve a real card from Migaku |

Card submission requires an `Idempotency-Key` header. Reusing a key with identical content returns the same job; different content returns HTTP 409. Numeric `deck_id` and `card_type_id` come from the endpoints above. Example body:

```json
{
  "language": "ja",
  "deck_id": 1234567890123,
  "card_type_id": 4096,
  "fields": {
    "targetWord": "図書館",
    "sentence": "今日は図書館で本を読みました。",
    "translation": "Today I read a book at the library.",
    "definitions": "library",
    "notes": "Source: my reading notes",
    "metadata": {"source": "api"}
  }
}
```

Fields also support `exampleSentences`, `images`, `sentenceAudio`, and `wordAudio`. Media lists are existing Migaku media references, not local filenames. `/cards` creates supplied fields using Migaku's native card method and templates. `/ingest` additionally uses Migaku's dictionary selection and formatting, translation and explanation prompt builders, account language/custom prompts, and syntax parser. Automatic media generation is not part of this ingestion adapter.

The pen uses `POST /ingest` with the same bearer authentication and required `Idempotency-Key` header:

```json
{
  "language": "ja",
  "sentence": "今日は図書館で本を読みました。",
  "focus_word": "図書館",
  "focus_start": 3,
  "focus_end": 6
}
```

Offsets use UTF-16 code units, matching Qt and Migaku, and must select the exact focus word. An optional `translation` is retained instead of generating one. Configure `MIGAKU_INGEST_DECK_ID` and optionally `MIGAKU_INGEST_CARD_TYPE_ID`, or use the isolated Migaku profile's selected targets. The API accepts requests durably while Migaku is offline. Generated fields are saved separately from the original request, so enrichment does not change its idempotency fingerprint. No card write starts until required text fields are filled. Generation and synchronization outcomes are reported through `/jobs/{id}`.

Dictionary enrichment tries the selected surface first, then Migaku's parsed dictionary forms. The selected surface and original offsets are preserved even when the focus spans multiple Migaku tokens. If neither lookup returns an entry, the dictionary field may remain empty, as in Migaku's native card creator; translation and AI explanation are still required.

`POST /text/analyze` now also returns `spans`, with the original surface text, UTF-16 start/end offsets and authoritative `KNOWN`, `UNKNOWN`, `LEARNING` or `IGNORED` status. Punctuation and unrecognized parser fragments receive no guessed status.

`POST /words/known` accepts `language`, `sentence`, `focus_word`, `focus_start` and `focus_end`, using the same UTF-16 span validation as ingestion. The selected span must match one recognized Migaku token. The route uses that token's dictionary form, reading and part of speech, so conjugated surfaces update the underlying word. It sets KNOWN without changing tracking/card fields, reads the status back and updates only that identity in the local index. It immediately returns the confirmed `word`, `synced: false` and `sync_pending: true`. A background worker retries cloud push; its pending revision survives service restarts and new updates during a push remain pending for the next push. `/worker` reports `word_sync.pending` and its last failure code. Vocabulary RPCs can run while card enrichment waits for AI; enrichment and cloud pushes each retain their own serialization. Retrying KNOWN is idempotent.

```bash
curl -H "Authorization: Bearer $MIGAKU_API_TOKEN" \
  'http://127.0.0.1:8766/words?q=図書館'
curl -H "Authorization: Bearer $MIGAKU_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"language":"ja","text":"図書館で本を読む。"}' \
  http://127.0.0.1:8766/text/analyze
```

## Data and write guarantees

The vocabulary mirror preserves language, dictionary form, reading/secondary form, part of speech, learning status, tracking flag, and whether a card exists. Tracking and having a card are independent of knowing a word. A missing index is reported as `indexed: false`, not as an empty known-word list. A failed refresh/schema mismatch retains the last snapshot. Automatic refresh defaults to every 300 seconds; override `MIGAKU_REFRESH_SECONDS` and `MIGAKU_LANGUAGES`.

Jobs progress through `queued`, `running`, `created`, and `synced`. Login/readiness failures become `blocked`. A write with an unknown outcome becomes `uncertain` and is never blindly repeated. A durable job UUID in card metadata allows reconciliation if Migaku saved the card but the RPC response was lost. Migaku assigns card IDs itself. Reconciliation scans card metadata only when needed. `synced` means the card was retrieved locally and Migaku emitted a completed push synchronization state; returning from the push RPC alone is insufficient. It is not a guarantee that every other client has downloaded it yet.

SQLite transactions preserve draft/job state across restarts. Browser or sync failures do not delete vocabulary. The API exposes specific operations, not unrestricted access to Migaku's internal RPC or database.
