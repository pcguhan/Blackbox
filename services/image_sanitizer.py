"""Decode and re-encode uploaded images without copying source metadata."""

import io
from pathlib import Path
import warnings

from PIL import Image, ImageOps


FORMATS = {'PNG': 'png', 'JPEG': 'jpg', 'WEBP': 'webp'}
IMAGE_MIMETYPES = {'png': 'image/png', 'jpg': 'image/jpeg', 'webp': 'image/webp'}
MAX_SOURCE_SIDE = 4096


class InvalidImage(ValueError):
    pass


class ImageTooLarge(InvalidImage):
    pass


def sanitize_upload(upload, *, max_bytes, max_side=512):
    """Return clean encoded bytes and their canonical extension."""
    extension = Path(upload.filename).suffix.lower().removeprefix('.')
    if extension == 'jpeg':
        extension = 'jpg'
    if extension not in IMAGE_MIMETYPES:
        raise InvalidImage('Choose a PNG, JPEG, or WebP image.')
    data = upload.stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ImageTooLarge(f'The image must be {max_bytes / (1024 * 1024):g} MB or smaller.')

    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=tuple(FORMATS)) as source:
                image_format = source.format
                if FORMATS[image_format] != extension:
                    raise InvalidImage('The file contents do not match the image format.')
                if min(source.size) < 1 or max(source.size) > MAX_SOURCE_SIDE:
                    raise InvalidImage('The image dimensions are too large.')
                if getattr(source, 'n_frames', 1) != 1:
                    raise InvalidImage('Animated images are not supported.')
                image = ImageOps.exif_transpose(source)
    except InvalidImage:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning,
            OSError, SyntaxError, ValueError, TypeError, OverflowError) as error:
        raise InvalidImage('The file is not a valid image.') from error

    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    has_alpha = any(band.lower() == 'a' for band in image.getbands()) or 'transparency' in image.info
    mode = 'RGBA' if has_alpha and image_format != 'JPEG' else 'RGB'
    pixels = Image.new(mode, image.size)
    pixels.paste(image.convert(mode))
    output = io.BytesIO()
    pixels.save(output, image_format)
    return output.getvalue(), extension
