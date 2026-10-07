import asyncio

from migaku_api.queue import WordSyncQueue
from migaku_api.store import Store
from tests.test_service import FakeBrowser, WORD, run


def test_word_push_survives_restart_and_retries_failed_cloud(tmp_path):
    path = tmp_path / "words.sqlite"
    store = Store(path)
    store.upsert_word("ja", WORD, queue_sync=True)
    store.close()
    store = Store(path)
    browser = FakeBrowser()
    browser.sync_fail = True
    queue = WordSyncQueue(store, browser)
    assert not run(queue.process())
    assert store.word_sync_pending()
    assert store.search_words("ja", status="known")["total"] == 1
    browser.sync_fail = False
    assert run(queue.process())
    assert not store.word_sync_pending()
    store.close()


def test_word_changed_during_cloud_push_remains_pending(tmp_path):
    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()
        class SlowCloud(FakeBrowser):
            async def push_sync(self):
                started.set()
                await finish.wait()
        store = Store(tmp_path / "words.sqlite")
        store.upsert_word("ja", WORD, queue_sync=True)
        queue = WordSyncQueue(store, SlowCloud())
        task = asyncio.create_task(queue.process())
        await started.wait()
        store.upsert_word("ja", {**WORD, "secondary": "せい"}, queue_sync=True)
        finish.set()
        assert await task
        assert store.word_sync_pending()
        assert await queue.process()
        assert not store.word_sync_pending()
        store.close()
    run(scenario())
