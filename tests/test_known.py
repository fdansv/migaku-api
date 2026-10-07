import copy

from fastapi.testclient import TestClient

from migaku_api.app import create_app
from migaku_api.browser import WorkerError
from tests.test_service import FakeBrowser

FOCUS = {"language": "ja", "sentence": "📚読みました。", "focus_word": "読みました", "focus_start": 2, "focus_end": 7}
WORD = {"dictForm": "読む", "secondary": "よむ", "partOfSpeech": "", "language": "ja",
        "knownStatus": "UNKNOWN", "tracked": True, "hasCard": True}


class VocabularyBrowser(FakeBrowser):
    def __init__(self):
        super().__init__()
        self.word = copy.deepcopy(WORD)
        self.writes = []
        self.confirm = True
        self.tokens = [{"start": 2, "surface": "読みました", "terms": [{"term": "読む", "reading": "よむ", "pos": ""}]}]

    async def call(self, method, params):
        if method == "ParsingMgr_parseTokens":
            return self.tokens
        if method == "WordListMgr_getMany":
            assert params["words"] == [{key: WORD[key] for key in ("dictForm", "secondary", "partOfSpeech")}]
            return [copy.deepcopy(self.word)]
        if method == "WordListMgr_setWordStatus":
            self.writes.append(params)
            if self.confirm:
                self.word["knownStatus"] = params["knownStatus"]
            return
        raise AssertionError(method)


def test_known_uses_inflected_identity_preserves_flags_and_updates_only_one_row(tmp_path):
    browser = VocabularyBrowser()
    app = create_app(tmp_path, browser, autostart=False, background=False)
    app.state.store.replace_words("ja", [WORD, {**WORD, "secondary": "よめる"}])
    stamp = app.state.store.words_stats("ja")["synced_at"]
    with TestClient(app) as client:
        result = client.post("/words/known", json=FOCUS)
        assert result.status_code == 200 and not result.json()["synced"]
        assert result.json()["sync_pending"] and app.state.store.word_sync_pending()
        word = result.json()["word"]
        assert word["dictForm"] == "読む" and word["knownStatus"] == "KNOWN"
        assert word["tracked"] and word["hasCard"]
        assert browser.writes == [{"dictForm": "読む", "secondary": "よむ", "partOfSpeech": "", "language": "ja", "knownStatus": "KNOWN"}]
        known = client.get("/words?status=known").json()
        assert known["total"] == 1 and known["words"][0]["reading"] == "よむ"
        assert client.get("/words?status=unknown").json()["total"] == 1
        assert known["index"]["synced_at"] == stamp
        assert client.post("/words/known", json=FOCUS).status_code == 200
        assert len(browser.writes) == 2 and browser.word == word


def test_known_rejects_wrong_offsets_and_multiword_selection(tmp_path):
    browser = VocabularyBrowser()
    with TestClient(create_app(tmp_path, browser, autostart=False, background=False)) as client:
        assert client.post("/words/known", json={**FOCUS, "focus_start": 1}).status_code == 422
        assert client.post("/words/known", json={**FOCUS, "focus_end": 8, "focus_word": "読みました。"}).status_code == 422
        browser.tokens = []
        assert client.post("/words/known", json=FOCUS).status_code == 422
    assert browser.writes == []


def test_known_needs_readback_and_reports_cloud_sync_separately(tmp_path):
    browser = VocabularyBrowser()
    with TestClient(create_app(tmp_path, browser, autostart=False, background=False)) as client:
        browser.confirm = False
        result = client.post("/words/known", json=FOCUS)
        assert result.status_code == 503 and result.json()["detail"]["code"] == "known_unconfirmed"
        assert client.get("/words?status=known").json()["total"] == 0
        browser.confirm = True
        browser.sync_fail = True
        result = client.post("/words/known", json=FOCUS)
        assert result.status_code == 200 and not result.json()["synced"]
        assert result.json()["word"]["knownStatus"] == "KNOWN"
        browser.ready = False
        assert client.post("/words/known", json=FOCUS).status_code == 503


def test_known_reply_never_waits_for_cloud_sync(tmp_path):
    class SlowCloud(VocabularyBrowser):
        async def push_sync(self):
            raise AssertionError("The HTTP action must not wait for the cloud")
    app = create_app(tmp_path, SlowCloud(), autostart=False, background=False)
    with TestClient(app) as client:
        result = client.post("/words/known", json=FOCUS)
        assert result.status_code == 200
        assert result.json()["word"]["knownStatus"] == "KNOWN"
        assert client.get("/worker").json()["word_sync"]["pending"]
