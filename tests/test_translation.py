import asyncio
import json
import subprocess
from pathlib import Path

import playwright
from fastapi.testclient import TestClient

from migaku_api.app import create_app
from migaku_api.browser import MigakuBrowser, WorkerError
from tests.test_service import FakeBrowser, run


def test_translation_endpoint_is_authenticated_and_does_not_create_cards(tmp_path, monkeypatch):
    class Translator(FakeBrowser):
        async def translate_text(self, request):
            await self.require_ready()
            return {**request, "translation": "I read a book.", "target_language": "en"}
    browser = Translator()
    monkeypatch.setenv("MIGAKU_API_TOKEN", "test-private-token")
    with TestClient(create_app(tmp_path, browser, autostart=False, background=False)) as client:
        body = {"language": "ja", "text": "本を読みました。"}
        assert client.post("/text/translate", json=body).status_code == 401
        headers = {"Authorization": "Bearer test-private-token"}
        result = client.post("/text/translate", json=body, headers=headers)
        assert result.status_code == 200
        assert result.json()["text"] == body["text"]
        assert result.json()["translation"] == "I read a book."
        assert client.get("/jobs", headers=headers).json()["jobs"] == []
        assert browser.creations == 0
        assert client.post("/text/translate", json={"text": "x" * 2001}, headers=headers).status_code == 422
        browser.ready = False
        assert client.post("/text/translate", json=body, headers=headers).status_code == 503


def test_translation_uses_configured_language_and_prompt_without_card_helpers():
    node = Path(playwright.__file__).parent / "driver" / "node"
    source = Path(__file__).parents[1] / "migaku_api" / "translate.js"
    script = r"""
import fs from 'node:fs';
const fixture = JSON.parse(fs.readFileSync(0,'utf8'));
globalThis.settings = {designatedLangs:{lang:'ja',translationLang:'es'},
    customGptPrompt:{translationSystem:'custom system',translationUser:'custom user'}};
globalThis.core = {AiMgr_promptStreamSynced:async prompt=>{
    globalThis.prompt=prompt; return {isSuccess:true,value:' Leí un libro. '}; }};
const native = 'export const d=()=>globalThis.core,u=()=>globalThis.settings;';
const helpers = 'export const d=(text,target,custom)=>({text,target,custom});';
globalThis.chrome = {runtime:{getURL:name=>'data:text/javascript,'+encodeURIComponent(name==='native'?native:helpers)}};
const translate = eval('('+fs.readFileSync(fixture.source,'utf8')+')');
const result = await translate({coreModule:'native',helperModule:'helpers',input:{language:'ja',text:'本を読みました。'}});
console.log(JSON.stringify({result,prompt:globalThis.prompt}));
"""
    result = subprocess.run([str(node), "--input-type=module", "-e", script],
                            input=json.dumps({"source": str(source)}), text=True,
                            capture_output=True, check=True, timeout=10)
    output = json.loads(result.stdout)
    assert output["result"] == {"language": "ja", "text": "本を読みました。",
                                "target_language": "es", "translation": "Leí un libro."}
    assert output["prompt"] == {"text": "本を読みました。", "target": "es",
                                "custom": {"system": "custom system", "user": "custom user"}}


def test_slow_translation_does_not_block_word_status_rpc(tmp_path):
    async def scenario():
        started, finish = asyncio.Event(), asyncio.Event()
        class Page:
            async def evaluate(self, script, args):
                if "input" in args:
                    started.set()
                    await finish.wait()
                    return {**args["input"], "translation": "A book.", "target_language": "en"}
                return {"ok": True, "value": True}
        browser = MigakuBrowser(tmp_path)
        browser.context = object()
        browser.core = {"module": "test", "export": "core", "version": "test"}
        browser.page = Page()
        browser.check_enrichment_assets = lambda: None
        translation = asyncio.create_task(browser.translate_text({"language": "ja", "text": "本"}))
        await asyncio.wait_for(started.wait(), timeout=1)
        try:
            assert await asyncio.wait_for(browser.call("WordListMgr_getMany"), timeout=0.2)
        finally:
            finish.set()
            assert (await translation)["translation"] == "A book."
    run(scenario())
