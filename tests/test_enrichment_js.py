import json
import subprocess
from pathlib import Path

import playwright
import pytest


@pytest.mark.parametrize("missing", [False, True])
def test_inflected_selection_falls_back_to_migaku_lemmas_and_survives_missing_entry(missing):
    # Run the production JS, mocking only the installed extension's boundaries.
    # No AI requests or cards reach the user's account from this test.
    node = Path(playwright.__file__).parent / "driver" / "node"
    source = Path(__file__).parents[1] / "migaku_api" / "enrich.js"
    script = r"""
import fs from 'node:fs';
const fixture = JSON.parse(fs.readFileSync(0,'utf8'));
globalThis.queries = [];
globalThis.settings = {designatedLangs:{lang:'ja',translationLang:'en',gptLang:'en'},
    langCardTargets:{ja:{deck:123,cardType:4096}},customGptPrompt:{},langResources:{},langConfig:{}};
globalThis.core = {
    ParsingMgr_parseTokens:async()=>[{terms:[{term:'書く',surfaceReading:'かく'}]},{terms:[{term:'な',surfaceReading:'な'}]}],
    AiMgr_promptStreamSynced:async()=>({isSuccess:true,value:'Migaku explanation'}),
    ParsingMgr_parseSyntax:async({text})=>text
};
globalThis.lookup = async words => {
    queries.push(words.map(word=>word.surface));
    return !fixture.missing && words.some(word=>word.surface==='書く') ? ['write'] : [];
};
const native = 'export const d=()=>globalThis.core,u=()=>globalThis.settings,ah=({formatObj})=>formatObj;';
const helpers = 'export const n=globalThis.lookup,d=()=>({}),C=()=>({});';
globalThis.chrome = {runtime:{getURL:name=>'data:text/javascript,'+encodeURIComponent(name==='native'?native:helpers)}};
const enrich = eval('('+fs.readFileSync(fixture.source,'utf8')+')');
const result = await enrich({coreModule:'native',helperModule:'helpers',input:{language:'ja',
    sentence:'手紙書くなんて',focus_word:'書くな',focus_start:2,focus_end:5,translation:'Existing translation'}});
console.log(JSON.stringify({result,queries}));
"""
    result = subprocess.run([str(node), "--input-type=module", "-e", script],
                            input=json.dumps({"source": str(source), "missing": missing}),
                            text=True, capture_output=True, check=True, timeout=10)
    output = json.loads(result.stdout)
    fields = output["result"]["fields"]
    assert output["queries"] == [["書くな"], ["書く", "な"]]
    assert fields["definitions"] == ("" if missing else "write")
    assert fields["targetWord"] == "書くな"
    assert fields["sentence"] == "手紙<b>書くな</b>んて"
    assert fields["translation"] == "Existing translation"
    assert fields["notes"] == "Migaku explanation"
