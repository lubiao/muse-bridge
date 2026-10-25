# SPDX-License-Identifier: Apache-2.0
"""Prepare bounded native images before committing a Muse chat turn."""
from .files import FileError
from .images import prepare_image, MAX_IMAGE_BYTES


def prepare_images(bridge, row, payload):
    images = payload.get('images')
    caption = payload.get('caption', '')
    if not isinstance(images, list) or not 1 <= len(images) <= 10 or not isinstance(caption, str) or len(caption) > 20000:
        raise FileError('invalid_image_reference')
    refs = payload.setdefault('muse_image_refs', [])
    if not isinstance(refs, list) or len(refs) > len(images):
        raise FileError('invalid_image_reference')
    for index in range(len(refs), len(images)):
        image = images[index]
        size = image.get('size')
        if size is not None and (not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_IMAGE_BYTES):
            raise FileError('image_size_limit')
        data = bridge.feishu.download_image(row['id'], image['file_key'])
        data, name = prepare_image(data, image.get('filename', 'image'))
        if sum(ref['bytes_len'] for ref in refs) + len(data) > 30_000_000:
            raise FileError('image_batch_size_limit')
        mime = 'image/png' if name.lower().endswith('.png') else 'image/jpeg'
        ref = bridge.muse.prepare_file_reference(name, mime, data, row['request_id'] + ':image:' + str(index))
        refs.append(ref)
        bridge.state.file_payload('inbox', row['id'], payload)
    return refs
