import asyncio

from migaku_api.browser import MigakuBrowser
from tests.test_service import run


def test_vocabulary_rpc_completes_while_slow_rpc_is_running(tmp_path):
    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()
        class Page:
            async def evaluate(self, script, args):
                if args["method"] == "AiMgr_promptStreamSynced":
                    started.set()
                    await finish.wait()
                return {"ok": True, "value": True}
        browser = MigakuBrowser(tmp_path)
        browser.context = object()
        browser.core = {"module": "test", "export": "core"}
        browser.page = Page()
        slow = asyncio.create_task(browser.call("AiMgr_promptStreamSynced"))
        await started.wait()
        try:
            assert await asyncio.wait_for(browser.call("WordListMgr_getMany"), timeout=0.2)
        finally:
            finish.set()
            await slow
    run(scenario())
