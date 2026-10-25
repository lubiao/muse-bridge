import os
import sqlite3
import time
import pytest
from muse_feishu_bridge.files import FileCache,FileError,MAX_FILE_BYTES,filename
from muse_feishu_bridge.state import State


def test_cache_preserves_binary_and_cleanup_respects_active_queue_and_other_files(tmp_path):
    cache=FileCache(tmp_path)
    data=bytes(range(256))
    cache.write('pending',data);cache.write('sent',data)
    other=cache.directory/'unrelated.txt';other.write_text('keep')
    for path in cache.directory.iterdir():os.utime(path,(100,100))
    assert cache.read('pending')==data
    assert cache.cleanup({'pending'},now=10000)==1
    assert cache.path('pending').exists() and other.exists()
    assert not cache.path('sent').exists()


def test_oversize_cache_file_is_rejected_without_publishing_partial_file(tmp_path):
    cache=FileCache(tmp_path)
    with pytest.raises(FileError,match='file_too_large'):cache.write('huge',b'x'*(MAX_FILE_BYTES+1))
    assert list(cache.directory.iterdir())==[]
    assert '/' not in filename('../../报告.docx') and '\\' not in filename('a\\b.docx')


def test_restart_retries_preparation_but_not_uncertain_file_submission(tmp_path):
    state=State(tmp_path)
    try:
        state.enqueue_file('preparing','private',{'file_key':'f','filename':'x'})
        state.claim_incoming();assert state.recover()==0
        assert state.claim_incoming()['id']=='preparing'
        state.incoming_submitting('preparing');assert state.recover()==1
        assert state.claim_incoming() is None
    finally:state.close()


def test_additive_migration_keeps_old_text_queue_payloads_and_owner(tmp_path):
    path=tmp_path/'bridge.sqlite3';db=sqlite3.connect(path)
    db.executescript("""CREATE TABLE inbox(id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,text TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',request_id TEXT NOT NULL,created REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,error TEXT);
        INSERT INTO inbox(id,chat_id,text,request_id,created) VALUES ('old','private','original text','stable-request',1);
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        INSERT INTO meta VALUES ('owner','{"open_id":"owner","chat_id":"private"}');""")
    db.commit();db.close()
    state=State(tmp_path)
    try:
        row=state.claim_incoming()
        assert row['kind']=='text' and row['payload']=='{}' and row['text']=='original text'
        assert row['request_id']=='stable-request' and state.owner()['open_id']=='owner'
    finally:state.close()
