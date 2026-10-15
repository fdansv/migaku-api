// Uses the same translation prompt and language as Migaku's Card Creator.
async ({coreModule, helperModule, input, exports:names = {core:'d',settings:'u',translation:'d'}}) => {
    const native = await import(chrome.runtime.getURL(coreModule));
    const helpers = await import(chrome.runtime.getURL(helperModule));
    const settings = native[names.settings](), prompt = settings.customGptPrompt;
    const target = settings.designatedLangs.translationLang;
    if (settings.designatedLangs.lang !== input.language)
        return {error:'translation_language_mismatch'};
    const result = await native[names.core]().AiMgr_promptStreamSynced(helpers[names.translation](input.text, target, {
        system:prompt.translationSystem || '', user:prompt.translationUser || ''
    }));
    if (!result.isSuccess || typeof result.value !== 'string' || !result.value.trim())
        return {error:'translation_failed'};
    return {language:input.language, text:input.text, target_language:target,
        translation:result.value.trim()};
}
