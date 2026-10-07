import pytest

from migaku_api.browser import MigakuBrowser, WorkerError
from tests.test_service import run


@pytest.mark.parametrize("scheduled,events,error", [
    (True, ["IDLE"], None),  # Clean push: IN_PROGRESS can be coalesced away.
    (False, ["IDLE"], "sync_unconfirmed"),  # Prior idle is not completion.
    (True, ["IN_PROGRESS"], "sync_unconfirmed"),
    (True, ["IN_PROGRESS", "ERROR"], "sync_error"),
])
def test_push_requires_completion_of_current_cycle(tmp_path, monkeypatch, scheduled, events, error):
    async def scenario():
        class Page:
            async def evaluate(self, script, args=None):
                if args is not None:
                    return None  # Register native listeners.
                return {"scheduled": scheduled, "events": [{"state": s} for s in events],
                        "state": {"state": events[-1]}}

        async def noop(*args, **kwargs):
            pass

        browser = MigakuBrowser(tmp_path)
        browser.context = object()
        browser.page = Page()
        browser.core = {"module": "test", "export": "core"}
        browser.require_ready = browser.call = noop
        monkeypatch.setattr("migaku_api.browser.asyncio.sleep", noop)
        if error:
            with pytest.raises(WorkerError) as exc:
                await browser.push_sync()
            assert exc.value.code == error
        else:
            await browser.push_sync()
    run(scenario())
