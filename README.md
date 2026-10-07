# Migaku Local API

An unofficial local HTTP API for **Migaku vocabulary search, Japanese text analysis, known-word updates and sentence-card creation**. Use it from a pen, eReader, script or another application. [TranslatePen](https://github.com/fdansv/translatepen) is one client; the API does not require or contact the pen.

The service runs the actual Migaku extension in a separate persistent Chromium profile using Playwright. Migaku handles account authentication, dictionaries and cloud synchronization. This is a local adapter around Migaku's private extension interfaces.

**Developer preview.** Verified against Migaku Early Access **1.31.0.16**, Playwright **1.63.0**, Chromium **153** and Python **3.12**. The card-enrichment adapter checks exact extension asset hashes and rejects unsupported versions. Other extension versions need compatibility review. Docker files are supplied, but the complete container/login workflow has not yet been verified.

## Features

- Search a local synchronized vocabulary index by headword, reading and learning status.
- Analyze Japanese text with original surfaces and authoritative word-status spans.
- Mark a selected word known, with readback confirmation and durable background sync.
- Queue prepared cards or send a sentence/focus span for Migaku-native dictionary, translation and AI enrichment.
- Retain drafts/jobs across restarts and reconcile uncertain writes without blindly duplicating cards.
- Browse a small dashboard at `/` and interactive API documentation at `/docs`.

## Quick start (Linux)

You need your own Migaku account and installed Migaku extension. The code/containers do not include the extension or a signed-in profile. The primary tested host is Linux; other host platforms and profile migration are unverified.

```sh
git clone https://github.com/fdansv/migaku-api.git
cd migaku-api
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install --with-deps chromium
```

Set `MIGAKU_EXTENSION_PATH` to your installed extension directory containing `manifest.json`. The service can discover Migaku in Linux Chrome's Default profile, but an explicit path makes the chosen version clear:

```sh
export MIGAKU_EXTENSION_PATH=/absolute/path/to/your/installed/migaku-extension
MIGAKU_HEADLESS=0 .venv/bin/python -m migaku_api
```

Sign in in the separate Chromium window. The extension may open onboarding in another tab; complete it and choose your language/deck there. The service retains the session under private `.runtime/browser-profile/`. Stop the foreground service with Ctrl-C when done, then start headlessly:

```sh
.venv/bin/python -m migaku_api
```

Open `http://127.0.0.1:8766/` or `/docs`. Check `/worker` for `authenticated`, `ready` and `initial_sync_done`. Keep one API process per runtime directory and never use your active personal Chrome profile as the worker profile. Review [setup and deployment](docs/setup.md) for extension snapshots, LAN access and Docker.

## Example: analyze a sentence

With the default loopback-only service:

```sh
curl --fail --silent --show-error \
  -H 'Content-Type: application/json' \
  -d '{"language":"ja","text":"今日は図書館で本を読みました。"}' \
  http://127.0.0.1:8766/text/analyze
```

If a bearer token is configured, add `-H "Authorization: Bearer $MIGAKU_API_TOKEN"`. Analysis combines Migaku parsing and vocabulary status; punctuation and unrecognized fragments receive no guessed status. Offsets are UTF-16 code units.

## API and integration

See [API reference](docs/api.md) for routes, request examples, idempotency, job outcomes and known-word updates. For the full physical-book setup, see [TranslatePen's combined guide](https://github.com/fdansv/translatepen/blob/main/docs/setup.md).

This adapter currently enriches text fields. It does not automatically generate audio/images or implement a self-hosted Migaku SRS. Migaku AI enrichment and cloud synchronization require their corresponding online account services.

## Tests

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Tests use isolated temporary state and fake browser/RPC responses. They cover HTTP authentication, vocabulary snapshots, text spans, durable jobs, idempotency, enrichment, concurrency, uncertain writes and synchronization recovery. They do not log in to your account or create live cards. GitHub Actions runs the same suite.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md). The wrapper is MIT-licensed; see [LICENSE](LICENSE) and [NOTICE](NOTICE). Migaku's extension, dictionaries and services retain their own licenses. This independent project is not affiliated with or endorsed by Migaku.
