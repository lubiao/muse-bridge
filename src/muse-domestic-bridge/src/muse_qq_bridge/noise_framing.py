# SPDX-License-Identifier: Apache-2.0
# Copyright (c) Meta Platforms, Inc. and affiliates.
# Protocol adaptation modified for muse-cli protobuf frames and bounded buffers.
# See licenses/muse-gadget-sdk-Apache-2.0.txt.
"""Muse Noise framing, matching Meta's gadget SDK wire format.

Protocol reference: facebookincubator/muse-gadget-sdk, linux/src/musegadget/
noise/framing.py. Noise messages cap at 65535 bytes including authentication.
Keep both per-assembly and aggregate buffered bytes bounded.
"""
from __future__ import annotations
import os
import time
from muse_cli.gateway import NoiseTransportFrame

CHUNK_BYTES = 65489
MAX_BYTES = 4 * 1024 * 1024


def encode(payload: bytes) -> list[bytes]:
    if len(payload) > MAX_BYTES:
        raise ValueError('noise_payload_too_large')
    identifier = int.from_bytes(os.urandom(8), 'little', signed=True)
    count = max(1, (len(payload) + CHUNK_BYTES - 1) // CHUNK_BYTES)
    return [NoiseTransportFrame(chunk_id=identifier, chunk_index=index, total_chunks=count,
        payload=payload[index * CHUNK_BYTES:(index + 1) * CHUNK_BYTES]).SerializeToString()
        for index in range(count)]


class Decoder:
    def __init__(self):
        self.pending = {}
        self.buffered = 0

    def decode(self, data: bytes) -> bytes | None:
        if len(data) > 65519:
            raise ValueError('noise_frame_too_large')
        part = NoiseTransportFrame(); part.ParseFromString(data)
        count = part.total_chunks or 1
        if count > 256 or part.chunk_index >= count or len(part.payload) > CHUNK_BYTES:
            raise ValueError('invalid_noise_chunk')
        now = time.monotonic()
        if any(now - item['created'] > 60 for item in self.pending.values()):
            raise ValueError('noise_assembly_expired')
        if count == 1:
            return bytes(part.payload)
        current = self.pending.get(part.chunk_id)
        if current is None:
            if len(self.pending) >= 16:
                raise ValueError('too_many_noise_assemblies')
            current = self.pending[part.chunk_id] = {'count':count,'chunks':{},'bytes':0,'created':now}
        if current['count'] != count or part.chunk_index in current['chunks']:
            raise ValueError('inconsistent_noise_chunks')
        raw = bytes(part.payload)
        current['bytes'] += len(raw); self.buffered += len(raw)
        if current['bytes'] > MAX_BYTES or self.buffered > MAX_BYTES:
            raise ValueError('noise_assembly_too_large')
        current['chunks'][part.chunk_index] = raw
        if len(current['chunks']) != count:
            return None
        payload = b''.join(current['chunks'][index] for index in range(count))
        self.buffered -= current['bytes']; del self.pending[part.chunk_id]
        return payload
