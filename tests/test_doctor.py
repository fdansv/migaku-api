from migaku_api.doctor import inspect_extension


def test_missing_or_unsupported_extension_is_detected_before_login(tmp_path):
    assert not inspect_extension(None)['card_enrichment_and_translation_compatible']
    assert not inspect_extension(tmp_path)['rpc_factory_found']
    (tmp_path / 'manifest.json').write_text('{"version":"9.0.0"}')
    (tmp_path / 'assets').mkdir()
    (tmp_path / 'assets/core.js').write_text(
        'F("WordListMgr.queryWords");Factory=()=>new Proxy({},{get:()=>{}});export{Factory as d};')
    report = inspect_extension(tmp_path)
    assert report['installed_version'] == '9.0.0'
    assert report['rpc_factory_found']
    assert not report['card_enrichment_and_translation_compatible']


def test_version_label_alone_does_not_approve_enrichment(tmp_path):
    (tmp_path / 'manifest.json').write_text('{"version":"1.31.0.16"}')
    (tmp_path / 'assets').mkdir()
    (tmp_path / 'assets/core.js').write_text(
        'F("WordListMgr.queryWords");Factory=()=>new Proxy({},{get:()=>{}});export{Factory as d};')
    assert not inspect_extension(tmp_path)['card_enrichment_and_translation_compatible']
