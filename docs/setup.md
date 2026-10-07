# Setup and deployment

## Choose and preserve the extension version

Install Migaku normally using your own account. Set `MIGAKU_EXTENSION_PATH` to the directory containing `manifest.json`; the worker reads these assets without modifying them. Linux Chrome Default-profile discovery is a convenience, not a cross-platform installer.

For a stable local installation, copy your installed extension into private `.runtime/extension/`, then point the variable there. Keep the snapshot private. Review/test extension updates before replacing it. General RPC discovery locates the current core factory dynamically, but sentence-card enrichment additionally requires the exact **1.31.0.16** asset hashes reviewed by this adapter.

## Login and persistent state

Follow the README's headed first-login steps using the same runtime directory that the headless service will use. Only one process may own that directory/profile. Stop the headless service before signing in again with `MIGAKU_HEADLESS=0`, and stop the headed process before restarting the service.

`.runtime/` contains the authenticated browser profile, vocabulary mirror, drafts and durable jobs. Protect/back up that directory. Passwords are not saved by the wrapper, but the profile contains signed-in session state and must stay private.

## LAN access for a pen or eReader

The default bind is loopback. Set a private bearer token before binding to your server's LAN interface:

```sh
export MIGAKU_API_TOKEN="$(openssl rand -hex 32)"
export MIGAKU_HOST=YOUR_SERVER_IP
.venv/bin/python -m migaku_api
```

Save the token securely for reuse by your clients and service; do not generate a different token on every restart. All protected client requests use `Authorization: Bearer <token>`. Prefer a trusted LAN/private network; HTTP alone does not encrypt the token. The token permits vocabulary updates and card creation in the signed-in account.

A pen configuration uses this API root (without `/ingest` appended):

```json
{"endpoint":"http://YOUR_SERVER_IP:8766","token":"YOUR_PRIVATE_API_TOKEN"}
```

## Configuration

| Variable | Purpose / default |
| --- | --- |
| `MIGAKU_EXTENSION_PATH` | Installed extension directory; Linux Chrome discovery if absent |
| `MIGAKU_RUNTIME_DIR` | Persistent private state; `.runtime` in checkout |
| `MIGAKU_HEADLESS` | `1`; set `0` for interactive login |
| `MIGAKU_HOST`, `MIGAKU_PORT` | `127.0.0.1`, `8766` |
| `MIGAKU_API_TOKEN` | Bearer token; required by entrypoint for a network bind |
| `MIGAKU_LANGUAGES` | Indexed languages; `ja` |
| `MIGAKU_REFRESH_SECONDS` | Local extension export interval; `300` |
| `MIGAKU_INGEST_DECK_ID` | Ingestion destination; otherwise extension's selected deck |
| `MIGAKU_INGEST_CARD_TYPE_ID` | Ingestion card type; otherwise extension selection/default |

The index refresh reads the extension's local database; it is not an explicit cloud-pull endpoint. Japanese text ingestion is the currently supported adapter path. See `/docs` for schema validation.

## systemd (tested deployment style)

The example `deploy/migaku-api.service` assumes a checkout at `~/repos/migaku-api`. Adjust paths for another location. Store the service's configuration as `KEY=value` lines in private `.runtime/service.env`, with mode 600. Include the extension path, stable API token, bind address and ingestion deck selection as needed.

```sh
mkdir -p ~/.config/systemd/user
cp deploy/migaku-api.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now migaku-api
```

Complete interactive login first. User lingering is a separate host choice if the service should run without a desktop login.

## Docker (experimental deployment)

Copy `.env.example` to private `.env`, fill in a strong stable token and an absolute installed-extension path, then:

```sh
docker compose up --build -d
```

Compose mounts `.runtime/` persistently and the extension read-only. It maps port 8766 to host loopback by default. For a pen, change the host bind address in the port mapping to your private LAN interface and use bearer authentication.

The container starts headlessly. The first-login workflow currently relies on a compatible headed worker with the same private state; profile/session portability into the container is **not validated**. A guided container login remains packaging work. Prefer the native Linux setup for the first preview if you do not already have a compatible authenticated profile.

The Docker build context is restricted with `.dockerignore` so browser sessions and local runtime files are not sent to the builder. Docker packaging is supplied for development; it is not yet a claim of a verified one-command setup.
