import importlib.util
import sqlite3
import sys
import types
from pathlib import Path

import pytest

ADDON_SOURCE = Path(__file__).resolve().parents[1]/'extras/anki-media-addon/__init__.py'

def test_cloud_ack_requires_real_file_clean_journal_and_enabled_sync(tmp_path, monkeypatch):
    db = sqlite3.connect(tmp_path/'collection.media.db2')
    db.execute('CREATE TABLE media(fname TEXT PRIMARY KEY,csum TEXT,dirty INTEGER)')
    name = 'pen_'+'a'*64+'.jpg'
    db.execute('INSERT INTO media VALUES(?,?,1)',(name,'hash'));db.commit()
    media = tmp_path/'collection.media';media.mkdir();(media/name).write_bytes(b'fixture')
    class Profile:
        profile = {'syncMedia':False}
        saved = False
        def profileFolder(self):return str(tmp_path)
        def media_syncing_enabled(self):return self.profile['syncMedia']
        def save(self):self.saved = True
    starts = []
    mw = types.SimpleNamespace(pm=Profile(),col=types.SimpleNamespace(media_sync_status=lambda:types.SimpleNamespace(active=False)),media_syncer=types.SimpleNamespace(last_progress='',start=lambda:starts.append(True)))
    hooks = types.SimpleNamespace(profile_did_open=[])
    monkeypatch.setitem(sys.modules,'aqt',types.SimpleNamespace(mw=mw,gui_hooks=hooks))
    connect = types.SimpleNamespace(AnkiConnect=type('AnkiConnect',(),{}),util=types.SimpleNamespace(api=lambda:lambda f:f))
    monkeypatch.setitem(sys.modules,'2055492159',connect)
    path=ADDON_SOURCE
    spec=importlib.util.spec_from_file_location('test_media_bridge',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    hooks.profile_did_open[0]()
    assert not mw.pm.profile['syncMedia'] and not mw.pm.saved
    connect.AnkiConnect().penEnsureMediaSync([name])
    assert not starts
    mw.pm.profile['syncMedia'] = True
    assert not module.status([name])['uploaded']
    db.execute('UPDATE media SET dirty=0');db.commit()
    assert module.status([name])['uploaded']
    (media/name).unlink()
    assert not module.status([name])['uploaded']
    with pytest.raises(ValueError):module.status(['../other.jpg'])
    connect.AnkiConnect().penEnsureMediaSync([name])
    assert starts == [True]
    db.close()
