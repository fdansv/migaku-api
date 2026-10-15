from migaku_api.doctor import inspect_extension
from migaku_api.browser import MigakuBrowser, WorkerError
import pytest


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


def test_experimental_profile_is_opt_in_and_keeps_integrity_checks(tmp_path, monkeypatch):
    browser = MigakuBrowser(tmp_path, extension=tmp_path)
    browser.core = {'version':'1.31.0.17'}
    monkeypatch.setattr('migaku_api.browser.enrichment_compatible', lambda *args: True)
    monkeypatch.delenv('MIGAKU_ENABLE_EXPERIMENTAL_131017', raising=False)
    with pytest.raises(WorkerError, match='experimental opt-in'):
        browser.check_enrichment_assets()
    monkeypatch.setenv('MIGAKU_ENABLE_EXPERIMENTAL_131017', '1')
    profile = browser.check_enrichment_assets()
    assert profile['exports']['translation'] == 'T'
    assert profile['exports']['explanation'] == 'x'
    monkeypatch.setattr('migaku_api.browser.enrichment_compatible', lambda *args: False)
    with pytest.raises(WorkerError, match='compatibility review'):
        browser.check_enrichment_assets()
