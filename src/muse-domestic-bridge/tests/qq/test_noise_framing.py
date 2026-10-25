import os,threading
from types import SimpleNamespace
import pytest
from noise.connection import NoiseConnection,Keypair
from muse_cli.gateway import ServiceRequest,ServiceResponse,ServiceFrame,NoiseTransportFrame
from muse_qq_bridge.muse import _BoundedGateway
from muse_qq_bridge.noise_framing import Decoder,encode,CHUNK_BYTES


def noise_pair():
    a,b=[NoiseConnection.from_name(b'Noise_XX_25519_AESGCM_SHA256') for _ in range(2)]
    a.set_as_initiator();b.set_as_responder()
    for peer in (a,b):
        peer.set_keypair_from_private_bytes(Keypair.STATIC,os.urandom(32));peer.start_handshake()
    b.read_message(a.write_message(b''));a.read_message(b.write_message(b''));b.read_message(a.write_message(b''))
    return a,b


def test_large_gateway_request_is_noise_encrypted_in_valid_chunks():
    initiator,responder=noise_pair();packets=[]
    gateway=object.__new__(_BoundedGateway);gateway.noise=initiator
    gateway.ws=SimpleNamespace(send_bytes=packets.append);gateway._send_lock=threading.Lock()
    raw=os.urandom(2*1024*1024)
    gateway._send_envelope(0,raw)
    assert len(packets)>30 and all(len(packet)<=65535 for packet in packets)
    decoder=Decoder();assembled=None
    for packet in packets:assembled=decoder.decode(bytes(responder.decrypt(packet)))
    request=ServiceRequest();request.ParseFromString(assembled)
    assert request.payload==raw


def test_gateway_reassembles_large_encrypted_response_before_protobuf_parse():
    initiator,responder=noise_pair();raw=os.urandom(400000)
    frame=ServiceFrame(stream_id=7);frame.response.status=200;frame.response.body=raw;frame.response.end_body=True
    outer=ServiceResponse(payload=frame.SerializeToString())
    packets=iter(bytes(responder.encrypt(part)) for part in encode(outer.SerializeToString()))
    gateway=object.__new__(_BoundedGateway);gateway.noise=initiator;gateway._recv_lock=threading.Lock();gateway._frame_decoder=Decoder()
    gateway.ws=SimpleNamespace(recv=lambda:(next(packets),0))
    output=gateway._read_frame()
    assert output.stream_id==7 and output.response.body==raw


def test_chunk_decoder_handles_interleaved_out_of_order_assemblies():
    a=encode(b'a'*(CHUNK_BYTES+40));b=encode(b'b'*(CHUNK_BYTES+80));decoder=Decoder()
    assert decoder.decode(a[1]) is None
    assert decoder.decode(b[0]) is None
    assert decoder.decode(a[0])==b'a'*(CHUNK_BYTES+40)
    assert decoder.decode(b[1])==b'b'*(CHUNK_BYTES+80)
    assert decoder.buffered==0 and decoder.pending=={}


def test_duplicate_or_inconsistent_chunks_are_rejected():
    parts=encode(b'a'*(CHUNK_BYTES+1));decoder=Decoder()
    decoder.decode(parts[0])
    with pytest.raises(ValueError,match='inconsistent'):decoder.decode(parts[0])
