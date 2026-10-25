import base64
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from muse_weixin_bridge import protocol as p
from muse_weixin_bridge.config import Config, write_private_json
from muse_weixin_bridge.files import FileError, MAX_FILE_BYTES
from muse_weixin_bridge.runtime import WeixinBridge
from muse_weixin_bridge.transport import WeixinClient, ChannelError, CipherReader
from muse_weixin_bridge.weixin_state import WeixinState


@pytest.fixture
def state(tmp_path):
    s = WeixinState(tmp_path / 'state')
    s.initialize({'user_id': 'owner'}, 100)
    s.put('enabled_at', 1)
    yield s
    s.close()


def message(mid=1, **changes):
    return dict({'message_id': mid, 'from_user_id': 'owner', 'message_type': 1, 'message_state': 2,
        'create_time_ms': 2000, 'context_token': 'context1',
        'item_list': [{'type': 1, 'text_item': {'text': 'test'}}]}, **changes)


def test_image_batch_and_caption_are_one_durable_turn_and_cursor_replay_is_safe(state):
    page={'msgs':[message(item_list=[{'type':1,'text_item':{'text':'read both'}},
        {'type':2,'image_item':{'media':{'encrypt_query_param':'one'}}},
        {'type':2,'image_item':{'media':{'encrypt_query_param':'two'}}}])],'get_updates_buf':'images-cursor'}
    state.ingest(page,'owner');state.ingest(page,'owner')
    rows=list(state.db.execute('SELECT * FROM inbox'))
    assert len(rows)==1 and state.get('weixin_cursor')=='images-cursor'
    payload=json.loads(rows[0]['payload'])
    assert payload['caption']=='read both' and len(payload['images'])==2


def test_image_decryption_prefers_hex_key_and_allows_plain_cdn(tmp_path,state,monkeypatch):
    creds=tmp_path/'creds.json';write_private_json(creds,{'token':'fake','user_id':'owner','bot_id':'bot','base_url':p.API})
    channel=WeixinClient(creds,state)
    key=bytes(range(16));plain=b'image-content';body=[p.encrypt(plain,key)]
    class Response:
        status_code=200;headers={}
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def iter_content(self,*a):yield body[0]
    class Session:
        def get(self,*a,**kw):assert kw['allow_redirects'] is False;return Response()
        def close(self):pass
    from muse_weixin_bridge import transport
    monkeypatch.setattr(transport.requests,'Session',Session)
    media={'encrypt_query_param':'opaque','aes_key':'invalid-secondary-key'}
    assert channel.download_image('x',{'aeskey':key.hex(),'media':media})==plain
    body[0]=plain
    assert channel.download_image('x',{'media':{'encrypt_query_param':'opaque'}})==plain
    with pytest.raises(FileError):channel.download_image('x',{'aeskey':'invalid','media':media})


def test_atomic_cursor_replay_owner_and_group_isolation(state):
    page = {'msgs': [message(), message(2, from_user_id='other'), message(3, group_id='group'),
                    message(4, message_type=2), message(5, message_state=1), message(6, create_time_ms=0)],
            'get_updates_buf': 'next'}
    state.ingest(page, 'owner')
    state.ingest(page, 'owner')
    assert state.counts()['inbox'] == {'pending': 1}
    assert state.get('weixin_cursor') == 'next'
    assert state.get('weixin_context')['version'] == 2
    with pytest.raises(ValueError):
        state.ingest({'msgs': [message(99)]}, 'other')


def test_failed_page_rolls_back_cursor_and_message_together(state, monkeypatch):
    original = state._insert_incoming
    calls = []
    def fail(*args):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError('simulated_disk_error')
        original(*args)
    monkeypatch.setattr(state, '_insert_incoming', fail)
    with pytest.raises(RuntimeError):
        state.ingest({'msgs': [message(), message(2)], 'get_updates_buf': 'lost'}, 'owner')
    assert not state.counts()['inbox']
    assert state.get('weixin_cursor') == ''
    assert state.get('wx-received:1') is None


def test_crash_after_send_never_replays_either_direction(state):
    state.ingest({'msgs': [message()]}, 'owner')
    inbound = state.claim_incoming()
    state.queue_text('out', 'owner', 'reply')
    outbound = state.claim_outgoing()
    state.outgoing_submitting(outbound['id'])
    assert state.recover() == 2
    assert state.claim_incoming() is None
    assert state.claim_outgoing() is None
    assert state.counts() == {'inbox': {'unknown': 1}, 'outbox': {'unknown': 1}}


def test_preparation_can_resume_and_blocked_send_wakes_on_new_owner_message(state):
    state.queue_text('out', 'owner', 'reply')
    state.claim_outgoing()
    assert state.recover() == 0
    assert state.claim_outgoing()['id'] == 'out'
    state.outgoing_blocked('out', 1)
    assert state.claim_outgoing() is None
    state.ingest({'msgs': [message()]}, 'owner')
    assert state.claim_outgoing()['id'] == 'out'


def test_updated_context_race_retries_only_explicit_rejection(state):
    state.queue_text('out', 'owner', 'reply')
    state.claim_outgoing()
    state.ingest({'msgs': [message()]}, 'owner')
    state.outgoing_blocked('out', 1)
    assert state.claim_outgoing()['id'] == 'out'
    state.outgoing_unknown('out')
    state.ingest({'msgs': [message(2)]}, 'owner')
    assert state.claim_outgoing() is None


def test_blocked_file_gets_fresh_media_receipt_on_next_owner_message(state):
    payload = {'filename': 'test.zip', 'path': 'workspace/test.zip', 'size': 10, 'weixin_media': {'old': True}}
    with state.lock, state.db:
        state.db.execute("INSERT INTO outbox(id,chat_id,text,state,created,kind,payload) VALUES ('file','owner','','blocked',1,'file',?)",
            (json.dumps(payload),))
    state.ingest({'msgs': [message()]}, 'owner')
    row = state.claim_outgoing()
    assert row['id'] == 'file'
    assert 'weixin_media' not in json.loads(row['payload'])
    assert json.loads(row['payload'])['path'] == 'workspace/test.zip'


def test_text_file_same_message_and_status_stays_local(state):
    file = {'file_name': '../report.zip', 'len': str(MAX_FILE_BYTES), 'media': {'aes_key': 'fake'}}
    msg = message(item_list=[{'type': 1, 'text_item': {'text': 'process file'}}, {'type': 4, 'file_item': file}])
    state.ingest({'msgs': [msg]}, 'owner')
    rows = list(state.db.execute('SELECT * FROM inbox ORDER BY rowid'))
    assert len(rows) == 2
    assert rows[0]['kind'] == 'text' and rows[1]['kind'] == 'file'
    assert json.loads(rows[1]['payload'])['size'] == MAX_FILE_BYTES
    assert '/' not in json.loads(rows[1]['payload'])['filename']
    state.ingest({'msgs': [message(2, item_list=[{'type': 1, 'text_item': {'text': '/status'}}])]}, 'owner')
    assert len(list(state.db.execute('SELECT * FROM inbox'))) == 2
    assert state.counts()['outbox'] == {'pending': 1}


def test_aes_vector_both_keys_and_url_guard():
    key = bytes.fromhex('2b7e151628aed2a6abf7158809cf4f3c')
    data = bytes.fromhex('6bc1bee22e409f96e93d7e117393172a')
    encrypted = p.encrypt(data, key)
    assert encrypted[:16].hex() == '3ad77bb40d7a3660a89ecaf32466ef97'
    for encoded in (base64.b64encode(key), base64.b64encode(key.hex().encode())):
        assert p.decrypt(encrypted, encoded) == data
    for url in ('http://ilinkai.weixin.qq.com', 'https://ilinkai.weixin.qq.com.evil.test',
                'https://user:pass@ilinkai.weixin.qq.com', 'https://127.0.0.1',
                'https://ilinkai.weixin.qq.com:444'):
        with pytest.raises(p.ProbeError):
            p.checked_url(url)


def test_large_upload_body_uses_content_length_and_bounded_reads():
    from requests.utils import super_len
    data = b'x' * (3 * 1024 * 1024 + 1)
    reader = CipherReader(data)
    assert super_len(reader) == len(data)
    pieces = []
    while chunk := reader.read(1024 * 1024):
        assert len(chunk) <= 65536
        pieces.append(chunk)
    assert b''.join(pieces) == data and super_len(reader) == 0
    with pytest.raises(ChannelError):
        CipherReader(data, timeout=-1).read()


@pytest.mark.parametrize('failure,expected', [
    (ChannelError('network', retryable=True), 'unknown'),
    (ChannelError('api', code=-2), 'blocked'),
    (ValueError('ambiguous'), 'unknown')])
def test_outgoing_network_ambiguity_is_never_retried(state, tmp_path, failure, expected):
    state.ingest({'msgs': [message()]}, 'owner')
    config = Config(path=tmp_path / 'config', app_id='', secrets_file=tmp_path / 'none', data_dir=tmp_path / 'state')
    channel = SimpleNamespace()
    bridge = WeixinBridge(config, state, None, channel)
    def fail(*args):
        bridge.stop_event.set()
        raise failure
    channel.send_text = fail
    state.queue_text('out', 'owner', 'reply')
    bridge._outgoing()
    assert state.counts()['outbox'] == {expected: 1}
    state.recover()
    assert state.claim_outgoing() is None


def test_size_rejected_before_network_or_decryption(state, tmp_path, monkeypatch):
    creds = tmp_path / 'creds.json'
    write_private_json(creds, {'token': 'fake', 'user_id': 'owner', 'bot_id': 'bot', 'base_url': p.API})
    client = WeixinClient(creds, state)
    def no_network(*args, **kwargs):
        pytest.fail('must not make network call')
    monkeypatch.setattr(client, 'api', no_network)
    with pytest.raises(FileError):
        client.download_file('file', {'len': str(MAX_FILE_BYTES + 1)})
    with pytest.raises(ChannelError):
        client.send_text('other', 'secret', 'id')


def test_successful_delivery_and_restart_do_not_duplicate(state, tmp_path):
    state.ingest({'msgs': [message()]}, 'owner')
    config = Config(path=tmp_path / 'config', app_id='', secrets_file=tmp_path / 'none', data_dir=tmp_path / 'state')
    channel = SimpleNamespace()
    bridge = WeixinBridge(config, state, None, channel)
    def send(*args):
        bridge.stop_event.set()
        return 'remote-id'
    channel.send_text = send
    state.queue_text('out', 'owner', 'reply')
    bridge._outgoing()
    assert state.counts()['outbox'] == {'sent': 1}
    state.recover()
    assert state.claim_outgoing() is None


@pytest.mark.parametrize('direction', ['incoming', 'outgoing'])
def test_file_workers_share_one_memory_budget_without_blocking_polling(state, tmp_path, direction):
    config = Config(path=tmp_path / 'config', app_id='', secrets_file=tmp_path / 'none', data_dir=tmp_path / 'state')
    touched = threading.Event()
    channel = SimpleNamespace()
    muse = SimpleNamespace()
    bridge = WeixinBridge(config, state, muse, channel)
    def download(*args):
        touched.set()
        return b'data'
    def finish(*args):
        bridge.stop_event.set()
        return {'status': 'accepted'} if direction == 'incoming' else 'remote-id'
    if direction == 'incoming':
        state.enqueue_file('file', 'owner', {'filename': 'x.zip', 'file_key': {}, 'size': 4})
        channel.download_file = download
        muse.send_file = finish
        bridge.baseline_ready.set()
        target = bridge._incoming
    else:
        state.record_muse({'message_id':'file','seq':101,'kind':'file','role':'assistant','completed':True,
            'file':{'filename':'x.zip','path':'workspace/x.zip','size':4}}, [])
        state.put('weixin_context', {'token':'context','version':1})
        muse.read_file = download
        channel.upload_file = lambda *args: {'type':4}
        channel.send_file = finish
        # record_muse uses an independent attachment baseline.
        if not state.counts()['outbox']:
            state.put('attachment_baseline_seq',100)
            state.record_muse({'message_id':'file2','seq':102,'kind':'file','role':'assistant','completed':True,
                'file':{'filename':'x.zip','path':'workspace/x.zip','size':4}}, [])
        target = bridge._outgoing
    bridge.file_transfer_lock.acquire()
    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    try:
        assert not touched.wait(0.05)
        # Intake remains usable while a file worker waits for the memory slot.
        state.ingest({'msgs':[message(55)]}, 'owner')
    finally:
        bridge.file_transfer_lock.release()
    worker.join(2)
    assert touched.is_set() and not worker.is_alive()
    assert state.counts()['inbox' if direction == 'incoming' else 'outbox'].get('sent') == 1


def test_webp_upload_and_send_use_native_image_media_type(state,tmp_path,monkeypatch):
    import io
    from PIL import Image
    from muse_weixin_bridge import transport
    creds=tmp_path/'creds.json'
    write_private_json(creds,{'token':'fake','user_id':'owner','bot_id':'bot','base_url':p.API})
    channel=WeixinClient(creds,state);calls=[]
    def api(action,body,**kwargs):
        calls.append((action,body))
        return {'upload_full_url':'https://novac2c.cdn.weixin.qq.com/c2c/upload'} if action=='getuploadurl' else {'message_id':'receipt'}
    channel.api=api
    class Response:
        status_code=200
        headers={'x-encrypted-param':'private-receipt'}
        def __enter__(self):return self
        def __exit__(self,*a):pass
    class Session:
        def post(self,url,**kwargs):return Response()
        def close(self):pass
    monkeypatch.setattr(transport.requests,'Session',Session)
    raw=io.BytesIO();Image.new('RGB',(7,9),'red').save(raw,format='WEBP')
    item=channel.upload_image('avatar.webp',raw.getvalue())
    assert calls[0][1]['media_type']==1 and item['type']==2
    assert item['image_item']['mid_size']%16==0
    state.put('weixin_context',{'token':'context','version':1})
    assert channel.send_image('owner',item,'id')=='receipt'
    assert calls[1][1]['msg']['item_list']==[item]
