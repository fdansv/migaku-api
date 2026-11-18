# Save TranslatePen cards to Anki

The Anki destination sends the selected word and example sentence to AnkiConnect.
The Migaku extension still fills the definition, Japanese reading, and
translation. Migaku Memory remains the default destination.

## Set up a local test

1. Follow the [native setup guide](setup.md) and confirm that `/worker` reports a
   ready Migaku worker with a supported extension. For a first test use a
   disposable Anki profile.
2. In desktop Anki on the API host, install [AnkiConnect](https://ankiweb.net/shared/info/2055492159)
   and restart Anki. Keep AnkiConnect on loopback. Create a deck and a note type
   with the fields below. Leave Anki open.
3. Start Migaku Local API with a fresh runtime directory for the Anki destination.
   A fresh directory has its own browser profile, so complete Migaku sign-in
   there. Use your normal private configuration for the API and extension.

   ```sh
   export MIGAKU_CARD_DESTINATION=anki
   export MIGAKU_RUNTIME_DIR=/absolute/path/to/new-anki-runtime
   export ANKICONNECT_URL=http://127.0.0.1:8765
   export ANKI_DECK=Mining
   export ANKI_MODEL='TranslatePen Japanese'
   python -m migaku_api
   ```

   Set `ANKICONNECT_API_KEY` privately if enabled in AnkiConnect. Use
   `ANKI_EXTRA_TAGS` for optional space-separated tags. Existing queues with
   jobs cannot be switched between destinations because their IDs refer to
   different collections. This example runs the API directly on the host.

4. Point the pen to the API as usual. Check that `GET /health` returns
   `card_destination: anki` and `pen_photos: true`. Scan a short sentence,
   choose a word, and double-press B. Confirm that exactly one note appears in
   your Anki deck. Find it with `tag:pen`.

## Note fields

Create the following fields with these exact names. Put **Sentence** first
so Anki's duplicate check works consistently. For the card template, include
`{{Sentence}}` on the front and `{{Definitions}}`, `{{Translation}}` and
`{{Screenshot}}` where you want them on the back.

| Field | What the adapter writes |
| --- | --- |
| Sentence | Original Japanese sentence with the selection marked |
| Target Word [jpdb_furigana] | Selected word, retaining its Migaku reading markup |
| Translation | Sentence translation |
| Definitions | Dictionary entries |
| Example Sentences | Examples, if supplied |
| Screenshot | Optional pen JPEG |
| Sentence Audio, Word Audio, Images | Empty |
| Is Vocabulary Card, Is Audio Card | Empty |

Create each name in the last two rows as a separate field. Some markup may
need a compatible Migaku-style template to render as furigana.

## Photos and sync

A compatible client can attach a JPEG of up to 400 KB and one million pixels.
The API verifies the SHA-256, stages it privately, then verifies Anki's copy
before adding the note. It keeps one job ID across retries and checks whether
Anki already saved the note if the response was lost.

By default, sync from Anki normally. The job confirms that the note and any
photo are in local Anki. For a hosted Anki profile, opt in with
`ANKI_SYNC_AFTER_ADD=1`. For photo jobs in this mode, copy
`extras/anki-media-addon` into Anki's add-ons folder, leave media sync enabled
in Anki, and restart it. The add-on reports whether each photo has uploaded.
The job waits for that acknowledgement separately from card sync. Open
AnkiMobile and sync to receive both. Test with a disposable profile before
configuring hosted sync for a real collection.
