import io,json,threading
from types import SimpleNamespace
import pytest
from PIL import Image
from muse_qq_bridge.config import Config
from muse_qq_bridge.state import State
from muse_qq_bridge.bridge import Bridge
from muse_qq_bridge.files import FileError
from muse_qq_bridge.incoming_images import prepare_images
from muse_qq_bridge.muse import MainChatClient,DeliveryUnknown


def sample():
    data=io.BytesIO();Image.new('RGB',(30,20),'red').save(data,format='WEBP',lossless=True)
    return data.getvalue()


def test_incoming_image_worker_converts_uploads_and_submits_once(tmp_path):
    state=State(tmp_path/'state');state.bind(state.new_pairing_token(),'owner','chat')
    config=Config(path=tmp_path/'config',app_id='',secrets_file=tmp_path/'none',data_dir=tmp_path/'state')
    channel=SimpleNamespace(download_image=lambda *a:sample());calls=[]
    bridge=Bridge(config,state,SimpleNamespace(),channel)
    def prepare(name,mime,data,request_id):
        assert data.startswith(b'\x89PNG') and name.endswith('.png') and mime=='image/png'
        calls.append(request_id)
        return {'type':'file_ref','path':'workspace/user/image.png','filename':name,'mime_type':mime,
            'bytes_len':len(data),'sha256':'a'*64}
    def send(refs,text,request_id):
        assert len(refs)==2 and text=='describe both'
        bridge.stop_event.set();return {'status':'accepted','message_id':'muse-ack'}
    bridge.muse.prepare_file_reference=prepare;bridge.muse.send_image_references=send
    state.enqueue_file('native','chat',{'media_kind':'image','images':[
        {'file_key':'one','filename':'a.webp'},{'file_key':'two','filename':'b.webp'}],'caption':'describe both'})
    bridge.baseline_ready.set();bridge._incoming()
    row=state.db.execute('SELECT * FROM inbox').fetchone()
    assert row['state']=='sent' and row['attempts']==1 and len(set(calls))==2
    assert len(json.loads(row['payload'])['muse_image_refs'])==2
    assert json.loads(row['payload'])['muse_image_ack']['message_id']=='muse-ack'
    state.recover();assert state.claim_incoming() is None
    state.close()


def test_prepared_image_references_resume_without_redownload_or_second_upload():
    ref={'type':'file_ref','path':'workspace/image.png','filename':'image.png','mime_type':'image/png','bytes_len':1,'sha256':'a'*64}
    def forbidden(*a):pytest.fail('completed preparation must not repeat')
    bridge=SimpleNamespace(feishu=SimpleNamespace(download_image=forbidden),muse=SimpleNamespace(prepare_file_reference=forbidden))
    assert prepare_images(bridge,{'id':'x'}, {'images':[{'file_key':'image'}],'muse_image_refs':[ref]})==[ref]


def test_image_limit_checked_before_download_and_corrupt_content_before_upload():
    def forbidden(*a):pytest.fail('must reject before network or upload')
    bridge=SimpleNamespace(feishu=SimpleNamespace(download_image=forbidden),muse=SimpleNamespace(prepare_file_reference=forbidden))
    with pytest.raises(FileError):prepare_images(bridge,{}, {'images':[{'file_key':'x','size':10000001}]})
    with pytest.raises(FileError):prepare_images(bridge,{}, {'images':[{}]*11})
    bridge.feishu.download_image=lambda *a:b'corrupt'
    with pytest.raises(FileError):prepare_images(bridge,{'id':'x'}, {'images':[{'file_key':'x'}]})


def test_image_reference_and_caption_share_one_native_muse_turn(monkeypatch):
    muse=MainChatClient(lambda:'not-used',lambda e:None);calls=[]
    monkeypatch.setattr(muse,'_send_items',lambda items,key:calls.append((items,key)) or {'status':'accepted'})
    ref={'type':'file_ref','path':'workspace/image.png','filename':'image.png','mime_type':'image/png','bytes_len':500000,'sha256':'a'*64}
    muse.send_image_references([ref],'read this','same-turn')
    assert calls==[([ref,{'type':'text','text':'read this'}],'same-turn')]
    with pytest.raises(FileError):muse.send_image_references([dict(ref,mime_type='text/plain')],'','x')
    with pytest.raises(FileError):muse.send_image_references([dict(ref,path='/etc/passwd')],'','x')
