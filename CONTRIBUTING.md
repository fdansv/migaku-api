# Contributing

This wrapper uses private Migaku extension interfaces. Include the extension version and operation when reporting compatibility failures. Do not upload the extension bundle, browser profile, vocabulary database, account tokens or raw private RPC output.

Run `.venv/bin/python -m pytest -q` after code changes. Tests must use temporary state and mocked account operations. Live account writes require a deliberate manual check; do not add them to CI.

Preserve stable ingestion IDs, immutable request fingerprints, durable job state, authoritative word statuses and non-blind reconciliation of uncertain writes. Keep vocabulary operations responsive while AI enrichment or cloud sync waits.

Useful contributions include guided first login, verified Docker onboarding, additional extension compatibility adapters and example clients. Pen/firmware work belongs in [TranslatePen](https://github.com/fdansv/translatepen).

Wrapper contributions use the MIT license. Keep all Migaku/vendor assets and authenticated state outside this repository.
