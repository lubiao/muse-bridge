# SPDX-License-Identifier: Apache-2.0
"""Validate native image bytes; decode static WebP to lossless PNG for delivery."""
import io
from pathlib import Path
import warnings
from PIL import Image,ImageOps,UnidentifiedImageError
from .files import FileError

MAX_IMAGE_BYTES=10_000_000
MAX_IMAGE_PIXELS=24_000_000


def prepare_image(data, name='image'):
    if not isinstance(data,bytes) or not data or len(data)>MAX_IMAGE_BYTES:
        raise FileError('image_size_limit')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error',Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                width,height=source.size
                if width<1 or height<1 or width*height>MAX_IMAGE_PIXELS or max(width,height)>12000:
                    raise FileError('image_pixel_limit')
                if getattr(source,'n_frames',1)!=1:
                    raise FileError('animated_image_requires_file')
                if source.format not in ('PNG','JPEG','WEBP','GIF'):
                    raise FileError('unsupported_image_format')
                source.load()
                if source.format in ('PNG','JPEG'):
                    return data,Path(name).stem+('.png' if source.format=='PNG' else '.jpg')
                image=ImageOps.exif_transpose(source)
                if image.mode not in ('RGB','RGBA'):
                    image=image.convert('RGBA' if 'transparency' in image.info else 'RGB')
                output=io.BytesIO()
                image.save(output,format='PNG')
                converted=output.getvalue()
                if len(converted)>MAX_IMAGE_BYTES:
                    raise FileError('image_size_limit')
                return converted,Path(name).stem+'.png'
    except FileError:
        raise
    except (UnidentifiedImageError,OSError,ValueError,Image.DecompressionBombWarning,Image.DecompressionBombError):
        raise FileError('invalid_image') from None
