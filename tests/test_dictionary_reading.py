import json
import subprocess
from pathlib import Path

import playwright

from migaku_api.browser import ENRICHMENT_EXPORTS


def test_obsolete_reading_is_replaced_by_matching_entry_not_relabelled():
    node = Path(playwright.__file__).parent / "driver" / "node"
    source = Path(__file__).parents[1] / "migaku_api" / "enrich.js"
    script = r"""
import fs from 'node:fs';
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
const entry = (reading, gloss) => ({enhTokens:[{tokenData:{surface:'図書館',terms:[{surfaceReading:reading}]}}],definitions:[gloss]});
globalThis.settings={designatedLangs:{lang:'ja',translationLang:'en',gptLang:'en'},langCardTargets:{},customGptPrompt:{},langResources:{},langConfig:{}};
globalThis.core={
 ParsingMgr_parseTokens:async()=>[{terms:[{term:'図書館',reading:'としょかん',surfaceReading:'としょかん'}]}],
 LangResourceMgr_searchByProfile:async(query)=>{globalThis.query=query; return [entry('ずしょかん','old entry'),entry('としょかん','modern library entry')];},
 AiMgr_promptStreamSynced:async()=>({isSuccess:true,value:'Explanation'}),
 ParsingMgr_parseSyntax:async({text})=>text
};
globalThis.bulk=async()=>[entry('ずしょかん','old entry')];
globalThis.lookupReading=async(info,langs,config,resources,a11y,search)=>search({termQuery:info.terms[0],readingQuery:info.readings[0]});
const native='export const d=()=>globalThis.core,u=()=>globalThis.settings,ah=({formatObj})=>formatObj.enhTokens[0].tokenData.terms[0].surfaceReading+":"+formatObj.definitions[0];';
const helpers='export const k=globalThis.bulk,C=globalThis.lookupReading,D=()=>({stars:()=>""}),u=()=>({}),y=()=>({});';
globalThis.chrome={runtime:{getURL:name=>'data:text/javascript,'+encodeURIComponent(name==='native'?native:helpers)}};
const enrich=eval('('+fs.readFileSync(fixture.source,'utf8')+')');
const result=await enrich({coreModule:'native',helperModule:'helpers',exports:fixture.exports,destination:'anki',input:{language:'ja',sentence:'図書館です。',focus_word:'図書館',focus_start:0,focus_end:3,translation:'It is a library.'}});
console.log(JSON.stringify({result,query:globalThis.query}));
"""
    result = subprocess.run([str(node), "--input-type=module", "-e", script],
        input=json.dumps({"source": str(source), "exports": ENRICHMENT_EXPORTS["1.30.16.0"]}),
        encoding="utf-8", capture_output=True, check=True, timeout=10)
    output = json.loads(result.stdout)
    assert output["query"] == {"termQuery": "図書館", "readingQuery": "としょかん"}
    assert output["result"]["fields"]["definitions"] == "としょかん:modern library entry"
