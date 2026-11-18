import json
import subprocess
from pathlib import Path

import playwright
import pytest
from migaku_api.browser import ENRICHMENT_EXPORTS


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("version", ["1.31.0.16", "1.30.16.0"])
@pytest.mark.parametrize("destination", ["migaku", "anki"])
def test_inflected_selection_falls_back_to_migaku_lemmas_and_survives_missing_entry(missing, version, destination):
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
if (fixture.destination === 'anki') settings.langCardTargets = {};
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
const names = fixture.exports;
const helpers = `export const ${names.dictionary}=globalThis.lookup,${names.translation}=()=>({kind:'translation'}),${names.explanation}=()=>({kind:'explanation'});`;
globalThis.chrome = {runtime:{getURL:name=>'data:text/javascript,'+encodeURIComponent(name==='native'?native:helpers)}};
const enrich = eval('('+fs.readFileSync(fixture.source,'utf8')+')');
const result = await enrich({coreModule:'native',helperModule:'helpers',exports:names,destination:fixture.destination,input:{language:'ja',
    sentence:'手紙書くなんて',focus_word:'書くな',focus_start:2,focus_end:5}});
console.log(JSON.stringify({result,queries}));
"""
    result = subprocess.run([str(node), "--input-type=module", "-e", script],
                            input=json.dumps({"source": str(source), "missing": missing, "exports": ENRICHMENT_EXPORTS[version], "destination": destination}),
                            text=True, encoding="utf-8", capture_output=True, check=True, timeout=10)
    output = json.loads(result.stdout)
    fields = output["result"]["fields"]
    assert output["result"]["deck_id"] == (None if destination == "anki" else 123)
    assert output["queries"] == [["書くな"], ["書く", "な"]]
    assert fields["definitions"] == ("" if missing else "write")
    assert fields["targetWord"] == "書くな"
    assert fields["sentence"] == "手紙<b>書くな</b>んて"
    assert fields["translation"] == "Migaku explanation"
    assert fields["notes"] == ("" if destination == "anki" else "Migaku explanation")
