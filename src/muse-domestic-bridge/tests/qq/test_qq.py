import json
import os
from pathlib import Path
from types import SimpleNamespace
import threading
import pytest

from muse_qq_bridge.config import Config,write_private_json
from muse_qq_bridge.files import FileError,MAX_FILE_BYTES
from muse_qq_bridge.qq_state import QQState
from muse_qq_bridge.runtime import QQBridge
from muse_qq_bridge.transport import QQClient,ChannelError,read_download

CREDS={'owner_id':'111','bot_id':'222','api_token':'fake'}


@pytest.fixture
def state(tmp_path):
    s=QQState(tmp_path/'state');s.initialize(CREDS,100);s.put('enabled_at',1)
    yield s
    s.close()


def message(mid=1,**changes):
    return dict({'post_type':'message','message_type':'private','message_id':mid,'self_id':'222',
        'user_id':'111','time':2,'message':[{'type':'text','data':{'text':'test'}}]},**changes)


def test_private_owner_identity_and_replay_filter(state):
    for changes in ({'post_type':'message_sent'},{'message_type':'group'},{'user_id':'333'},
        {'user_id':'222'},{'self_id':'444'},{'time':0},{'group_id':'555'}):
        assert not state.ingest(message(**changes),CREDS)
    assert state.ingest(message(),CREDS)
    assert not state.ingest(message(),CREDS)
    assert state.counts()['inbox']=={'pending':1}


def test_file_and_text_dedup_names_and_local_status(state):
    segments=[{'type':'text','data':{'text':'read '}},{'type':'text','data':{'text':'this'}},
        {'type':'file','data':{'file':'../report.zip','file_id':'opaque','file_size':str(MAX_FILE_BYTES)}}]
    state.ingest(message(message=segments),CREDS)
    rows=list(state.db.execute('SELECT * FROM inbox ORDER BY rowid'))
    assert len(rows)==2 and rows[0]['text']=='read this'
    payload=json.loads(rows[1]['payload'])
    assert payload['filename']=='_report.zip' or '/' not in payload['filename']
    assert payload['size']==MAX_FILE_BYTES and payload['file_key']['file_id']=='opaque'
    state.ingest(message(2,message=[{'type':'text','data':{'text':'/status'}}]),CREDS)
    assert state.counts()['inbox']=={'pending':2} and state.counts()['outbox']=={'pending':1}


def test_transaction_failure_rolls_back_all_message_segments(state,monkeypatch):
    original=state._insert
    def fail(key,*args):
        if key.endswith(':1'):raise RuntimeError('disk_error')
        original(key,*args)
    monkeypatch.setattr(state,'_insert',fail)
    with pytest.raises(RuntimeError):
        state.ingest(message(message=[{'type':'text','data':{'text':'x'}},
            {'type':'file','data':{'file':'x.txt','file_id':'id','file_size':4}}]),CREDS)
    assert not state.counts()['inbox'] and state.get('qq-received:1') is None


def test_crash_stages_and_restart_never_replay_ambiguous_send(state):
    state.ingest(message(),CREDS);state.claim_incoming()
    state.queue_text('out','111','reply');state.claim_outgoing();state.outgoing_submitting('out')
    assert state.recover()==2
    assert state.claim_incoming() is None and state.claim_outgoing() is None
    state.queue_text('prepare','111','safe');state.claim_outgoing();state.recover()
    assert state.claim_outgoing()['id']=='prepare'


@pytest.mark.parametrize('failure',[ChannelError('network',retryable=True),ChannelError('onebot_rejected',code=1200),ValueError('ambiguous')])
def test_outgoing_uncertainty_not_retried(state,tmp_path,failure):
    config=Config(path=tmp_path/'config',app_id='',secrets_file=tmp_path/'none',data_dir=tmp_path/'state')
    channel=SimpleNamespace();bridge=QQBridge(config,state,None,channel)
    def fail(*args):
        bridge.stop_event.set();raise failure
    channel.send_text=fail
    state.queue_text('out','111','reply');bridge._outgoing()
    assert state.counts()['outbox']=={'unknown':1}
    state.recover();assert state.claim_outgoing() is None


def client(tmp_path,state):
    creds=tmp_path/'creds.json';write_private_json(creds,CREDS)
    shared=tmp_path/'shared';shared.mkdir(exist_ok=True)
    return QQClient(creds,state,shared_dir=shared,download_roots=[str(tmp_path/'allowed')])


def test_file_send_uses_private_local_path_and_retains_file_receipt(state,tmp_path,monkeypatch):
    channel=client(tmp_path,state);data=bytes(range(256))*100
    item=channel.upload_file('../source.zip',data)
    assert Path(item['path']).read_bytes()==data
    calls=[]
    def api(action,body,**kwargs):
        calls.append((action,body));return {'file_id':'file-ack'}
    monkeypatch.setattr(channel,'api',api)
    assert channel.send_file('111',item,'id')=='file:file-ack'
    assert calls[0][0]=='upload_private_file' and calls[0][1]['file']==item['path']
    assert not calls[0][1]['file'].startswith('base64://')
    with pytest.raises(ChannelError):channel.send_text('333','private','id')
    with pytest.raises(FileError):channel.send_file('111',dict(item,path='/etc/passwd'),'id')


def test_oversize_download_rejected_before_network(state,tmp_path,monkeypatch):
    channel=client(tmp_path,state)
    monkeypatch.setattr(channel,'api',lambda *a,**k:pytest.fail('must not request oversized file'))
    with pytest.raises(FileError):channel.download_file('id',{'file_id':'opaque','size':MAX_FILE_BYTES+1})


@pytest.mark.skipif(os.name=='nt',reason='Linux openat/ACL deployment boundary')
def test_local_download_rejects_escape_symlink_and_size_change(tmp_path):
    root=tmp_path/'allowed';root.mkdir();path=root/'x.bin';path.write_bytes(b'data')
    assert read_download(str(path),[str(root)],4)==b'data'
    with pytest.raises(FileError):read_download(str(path),[str(root)],5)
    with pytest.raises(FileError):read_download('/etc/passwd',[str(root)])
    link=root/'link';link.symlink_to(path)
    with pytest.raises(FileError):read_download(str(link),[str(root)])
    folder=root/'alias';folder.symlink_to(root,target_is_directory=True)
    with pytest.raises(FileError):read_download(str(folder/'x.bin'),[str(root)])


@pytest.mark.parametrize('direction',['incoming','outgoing'])
def test_file_workers_share_memory_slot_and_accept_intake(state,tmp_path,direction):
    config=Config(path=tmp_path/'config',app_id='',secrets_file=tmp_path/'none',data_dir=tmp_path/'state')
    channel=SimpleNamespace();muse=SimpleNamespace();bridge=QQBridge(config,state,muse,channel)
    touched=threading.Event()
    def download(*args):touched.set();return b'data'
    def finish(*args):bridge.stop_event.set();return {'status':'accepted'} if direction=='incoming' else 'remote'
    if direction=='incoming':
        state.enqueue_file('file','111',{'filename':'x.zip','file_key':{},'size':4})
        channel.download_file=download;muse.send_file=finish;bridge.baseline_ready.set();target=bridge._incoming
    else:
        state.put('attachment_baseline_seq',100)
        state.record_muse({'message_id':'file','seq':101,'kind':'file','role':'assistant','completed':True,
            'file':{'filename':'x.zip','path':'workspace/x.zip','size':4}},[])
        muse.read_file=download;channel.upload_file=lambda *a:{'path':'fake'};channel.send_file=finish;target=bridge._outgoing
    bridge.file_transfer_lock.acquire();worker=threading.Thread(target=target,daemon=True);worker.start()
    try:
        assert not touched.wait(0.05)
        assert state.ingest(message(55),CREDS)
    finally:bridge.file_transfer_lock.release()
    worker.join(2)
    assert touched.is_set() and not worker.is_alive()
    assert state.counts()['inbox' if direction=='incoming' else 'outbox'].get('sent')==1


def test_history_repair_only_enqueues_bound_owner(state,tmp_path):
    config=Config(path=tmp_path/'config',app_id='',secrets_file=tmp_path/'none',data_dir=tmp_path/'state')
    items=[dict(message(),sender={'user_id':'111'}),dict(message(2),sender={'user_id':'222'}),
        dict(message(3),sender={'user_id':'333'})]
    channel=SimpleNamespace(credentials=CREDS,api=lambda *a,**k:{'messages':items})
    bridge=QQBridge(config,state,None,channel);bridge._history_repair();bridge._history_repair()
    assert state.counts()['inbox']=={'pending':1}


def test_native_image_upload_converts_webp_and_send_uses_image_segment(state,tmp_path,monkeypatch):
    import io
    from PIL import Image
    raw=io.BytesIO();Image.new('RGBA',(20,12),(10,20,30,128)).save(raw,format='WEBP',lossless=True)
    channel=client(tmp_path,state)
    item=channel.upload_image('avatar.webp',raw.getvalue())
    assert item['name']=='avatar.png' and Path(item['path']).read_bytes().startswith(b'\x89PNG')
    calls=[]
    def api(action,body,**kwargs):
        calls.append((action,body));return {'message_id':123}
    monkeypatch.setattr(channel,'api',api)
    assert channel.send_image('111',item,'image-id')=='123'
    assert calls==[('send_private_msg',{'user_id':'111','message':[
        {'type':'image','data':{'file':Path(item['path']).as_uri()}}]})]
    with pytest.raises(ChannelError):channel.send_image('333',item,'image-id')
    with pytest.raises(FileError):channel.send_image('111',dict(item,path='/etc/passwd'),'image-id')


def test_image_worker_uses_native_transport_and_persists_receipt(state,tmp_path):
    config=Config(path=tmp_path/'config',app_id='',secrets_file=tmp_path/'none',data_dir=tmp_path/'state')
    channel=SimpleNamespace();muse=SimpleNamespace(read_file=lambda *a:b'image-data')
    bridge=QQBridge(config,state,muse,channel)
    state.put('image_baseline_seq',100)
    state.record_muse({'event_id':'image:widget','message_id':'image','seq':101,'kind':'file','role':'assistant',
        'completed':True,'file':{'filename':'avatar.webp','path':'workspace/avatar.webp','size':10,'media_kind':'image'}},[])
    channel.upload_image=lambda name,data:{'path':'cached-image','name':name}
    def send(*args):bridge.stop_event.set();return 'image-receipt'
    channel.send_image=send
    bridge._outgoing()
    row=state.db.execute('SELECT * FROM outbox').fetchone()
    assert row['state']=='sent' and row['remote_id']=='image-receipt'
    assert json.loads(row['payload'])['qq_file']['path']=='cached-image'


def test_incoming_images_share_caption_and_dedup_one_job(state):
    segments=[{'type':'text','data':{'text':'read both'}},{'type':'image','data':{'file':'one'}},
        {'type':'image','data':{'file':'two'}}]
    assert state.ingest(message(message=segments),CREDS)
    assert not state.ingest(message(message=segments),CREDS)
    rows=list(state.db.execute('SELECT * FROM inbox'))
    assert len(rows)==1
    payload=json.loads(rows[0]['payload'])
    assert payload['caption']=='read both' and len(payload['images'])==2
    assert [i['file_key']['index'] for i in payload['images']]==[1,2]


def test_image_download_rechecks_owner_and_pins_native_segment(state,tmp_path,monkeypatch):
    from muse_qq_bridge import transport
    channel=client(tmp_path,state);calls=[]
    def api(action,body,**kw):
        calls.append((action,body))
        if action=='get_msg':return {'message_type':'private','sender':{'user_id':'111'},
            'message':[{'type':'image','data':{'file':'native-opaque-key'}}]}
        return {'file':'/allowed/image.png'}
    monkeypatch.setattr(channel,'api',api)
    monkeypatch.setattr(transport,'read_download',lambda path,roots,**kw:b'image' if kw['limit']==10000000 else None)
    assert channel.download_image('qq:1:images',{'message_id':'1','index':0})==b'image'
    assert calls==[('get_msg',{'message_id':'1'}),('get_image',{'file':'native-opaque-key'})]
    monkeypatch.setattr(channel,'api',lambda *a,**kw:{'message_type':'private','sender':{'user_id':'333'}})
    with pytest.raises(FileError):channel.download_image('x',{'message_id':'1','index':0})


def test_native_image_cdn_download_is_bounded_and_rejects_external_hosts(state,tmp_path,monkeypatch):
    from muse_qq_bridge import transport
    channel=client(tmp_path,state);calls=[]
    class Response:
        status_code=200;headers={}
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def iter_content(self,*a):yield b'original-image'
    class Session:
        def get(self,url,**kw):calls.append((url,kw));return Response()
        def close(self):pass
    monkeypatch.setattr(transport.requests,'Session',Session)
    url='https://multimedia.nt.qq.com.cn/download?token=placeholder'
    assert channel._download_image_url(url,100)==b'original-image'
    assert calls[0][1]['allow_redirects'] is False and 'headers' not in calls[0][1]
    with pytest.raises(FileError):channel._download_image_url(url,2)
    for bad in ('https://evil.example/image','http://multimedia.nt.qq.com.cn/image',
        'https://multimedia.nt.qq.com.cn@127.0.0.1/image','https://multimedia.nt.qq.com.cn:123/image'):
        with pytest.raises(FileError):channel._download_image_url(bad,100)
