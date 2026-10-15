// Reviewed Migaku native Card Creator helpers. The Python adapter verifies
// the asset hashes before resolving these exports; no private alias is guessed.
async ({coreModule, helperModule, input, deckId, cardTypeId,
    exports: names = {core:'d',settings:'u',format:'ah',dictionary:'n',translation:'d',explanation:'C'}}) => {
    const native = await import(chrome.runtime.getURL(coreModule));
    const helpers = await import(chrome.runtime.getURL(helperModule));
    const core = native[names.core](), settings = native[names.settings]();
    const langs = settings.designatedLangs;
    if (langs.lang !== input.language) return {error:'ingestion_language_mismatch'};
    const targets = settings.langCardTargets[input.language] || {};
    deckId = deckId || targets.deck;
    cardTypeId = cardTypeId || targets.cardType || 4096;
    if (!deckId) return {error:'ingestion_destination_missing'};
    // Use Migaku's parsing, dictionary selection, formatting and prompt builders,
    // including the account's translation language and custom AI prompts.
    const tokens = await core.ParsingMgr_parseTokens({lang:input.language,text:input.focus_word});
    const reading = tokens.map(t=>t.terms[0]?.surfaceReading || '').join('');
    const words = [{surface:input.focus_word,reading}];
    const formatDefinitions = dictionary => dictionary.map(formatObj=>native[names.format]({lang:input.language,
        langConfig:settings.langConfig,formatObj})).filter(Boolean).join('<p></p>');
    let definitions = formatDefinitions(await helpers[names.dictionary]([{surface:input.focus_word}], settings.langResources, langs));
    if (!definitions) {
        // The pen can select an inflection or a span crossing Migaku's token
        // boundaries. Try Migaku's lemmas while preserving the selected surface.
        const lemmas = [...new Set(tokens.flatMap(token=>(token.terms || []).map(term=>term.term)))]
            .filter(term=>term && term!==input.focus_word);
        if (lemmas.length) definitions = formatDefinitions(await helpers[names.dictionary](
            lemmas.map(surface=>({surface})), settings.langResources, langs));
    }
    // Card Creator permits an empty dictionary field. A missing entry must not
    // discard an otherwise valid sentence/AI explanation before card creation.
    const prompt = settings.customGptPrompt;
    const translation = input.translation || (await core.AiMgr_promptStreamSynced(
        helpers[names.translation](input.sentence, langs.translationLang, {system:prompt.translationSystem || '',
            user:prompt.translationUser || ''})));
    const notes = await core.AiMgr_promptStreamSynced(helpers[names.explanation]({words,sentence:input.sentence,
        targetLang:input.language,interfaceLang:langs.gptLang,wordStartIndex:input.focus_start,
        customPrompt:{system:prompt.explanationSystem || '',user:prompt.explanationUser || ''}}));
    const translated = typeof translation === 'string' ? translation :
        translation.isSuccess ? translation.value : '';
    if (!translated || !notes.isSuccess || !notes.value) return {error:'ingestion_ai_failed'};
    const marked = input.sentence.slice(0,input.focus_start) + '<b>' + input.focus_word + '</b>' +
        input.sentence.slice(input.focus_end);
    const targetWord = await core.ParsingMgr_parseSyntax({lang:input.language,text:input.focus_word});
    const sentence = await core.ParsingMgr_parseSyntax({lang:input.language,text:marked});
    return {language:input.language,deck_id:deckId,card_type_id:cardTypeId,fields:{
        targetWord,sentence,translation:translated.replace(/\n/g,'<br>'),definitions,
        notes:notes.value.replace(/\n/g,'<br>'),metadata:{source:'translatepen',
            focusStart:String(input.focus_start),focusEnd:String(input.focus_end)}}};
}
