import os

import uvicorn


host = os.environ.get("MIGAKU_HOST", "127.0.0.1")
if host not in {"127.0.0.1", "localhost", "::1"} and not os.environ.get("MIGAKU_API_TOKEN"):
    raise SystemExit("Set MIGAKU_API_TOKEN before binding the API to a network interface.")
uvicorn.run("migaku_api.app:create_app", factory=True, host=host,
            port=int(os.environ.get("MIGAKU_PORT", "8766")), workers=1)
