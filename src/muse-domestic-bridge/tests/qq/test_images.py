import io
import threading
from types import SimpleNamespace
import pytest
from PIL import Image
from muse_qq_bridge.images import prepare_image
from muse_qq_bridge.files import FileError
from muse_qq_bridge.muse import _normalized_presentations
from muse_qq_bridge.state import State


def widget():
    return {'event_name':'delta.presentation','seq':3332,'payload':{'kind':'image','role':'assistant',
        'message_id':'parent','id':'widget','seq':3332,'is_thread':False,'data':{'images':[
            {'path':'sandbox://workspace/avatars/avatar.webp','label':'avatar','byte_len':20,'mime':'image/webp',
             'variants':{'original':'https://ignored.invalid/image'}}]}}}


def test_webp_to_png_preserves_decoded_pixels_and_transparency():
    source=Image.new('RGBA',(23,17),(30,50,70,120));raw=io.BytesIO();source.save(raw,format='WEBP',lossless=True)
    data,name=prepare_image(raw.getvalue(),'avatar.webp')
    assert name=='avatar.png' and data.startswith(b'\x89PNG')
    with Image.open(io.BytesIO(data)) as image:assert image.convert('RGBA').tobytes()==source.tobytes()


def test_png_remains_byte_identical_and_corrupt_or_animated_input_is_rejected():
    image=Image.new('RGB',(10,10));raw=io.BytesIO();image.save(raw,format='PNG')
    assert prepare_image(raw.getvalue(),'x.png')[0]==raw.getvalue()
    with pytest.raises(FileError):prepare_image(b'not an image')
    frames=[Image.new('RGB',(3,3),c) for c in ('red','blue')]
    raw=io.BytesIO();frames[0].save(raw,format='WEBP',save_all=True,append_images=frames[1:],duration=100)
    with pytest.raises(FileError) as raised:prepare_image(raw.getvalue())
    assert raised.value.code=='animated_image_requires_file'


def test_dimensions_checked_before_decoding(monkeypatch):
    from muse_qq_bridge import images
    class TooLarge:
        size=(9000,9000)
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def load(self):pytest.fail('must not decode excessive pixels')
    monkeypatch.setattr(images.Image,'open',lambda _:TooLarge())
    with pytest.raises(FileError):prepare_image(b'fake')


def test_native_image_multi_items_have_stable_separate_ids_and_main_chat_filter():
    record=widget();record['payload']['data']['images'].append({'path':'workspace/second.png','label':'second'})
    history=_normalized_presentations(record,source='history')
    live=_normalized_presentations({'event':'delta.presentation','payload':record['payload']},source='live')
    assert len(history)==2 and [x['event_id'] for x in history]==[x['event_id'] for x in live]
    assert history[0]['file']['media_kind']=='image'
    assert 'variants' not in history[0]['file']
    assert len({x['event_id'] for x in history})==2
    record['payload']['is_thread']=True
    assert not _normalized_presentations(record,source='history')


def test_image_baseline_is_separate_and_native_jobs_survive_restart(tmp_path):
    state=State(tmp_path);state.bind(state.new_pairing_token(),'owner','chat')
    state.put('baseline_seq',1);state.put('attachment_baseline_seq',1);state.put('image_baseline_seq',3332)
    row=_normalized_presentations(widget(),source='history')[0]
    assert not state.record_muse(row,[])
    row=dict(row,event_id='image:new',seq=3333)
    assert state.record_muse(row,[]) and not state.record_muse(row,[])
    job=state.claim_outgoing();assert job['kind']=='file' and 'image' in job['payload']
    state.outgoing_sent(job['id'],'receipt');state.recover()
    assert state.claim_outgoing() is None
    state.close()
