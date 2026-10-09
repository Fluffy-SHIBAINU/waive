"""Normalize uploaded photos before extraction (spec §9 step 1). Photos stay in memory."""

import io
from dataclasses import dataclass

from PIL import Image, ImageFilter, ImageOps, ImageStat, UnidentifiedImageError

MIN_SIDE = 600
# Edge variance of the downscaled grayscale. Measured on test images: a 6 px Gaussian blur scores
# about 310, while crisp synthetic bills score 1200 or more; 600 sits between them.
MIN_SHARPNESS = 600.0
# Above any phone camera and well under Pillow's 89.5 MP warning line. Checked from the header
# before decoding, so a kilobyte-sized PNG cannot inflate into hundreds of megabytes.
MAX_PIXELS = 40_000_000


class ImageError(ValueError):
    """The upload is not a usable image."""


@dataclass(frozen=True)
class PreparedImage:
    jpeg: bytes
    width: int
    height: int
    sharpness: float
    warnings: tuple[str, ...]


def _sharpness(image: Image.Image) -> float:
    gray = image.convert("L")
    if max(gray.size) > 1000:
        gray = gray.resize(
            (gray.width * 1000 // max(gray.size), gray.height * 1000 // max(gray.size))
        )
    edges = gray.filter(ImageFilter.FIND_EDGES)
    return float(ImageStat.Stat(edges).var[0])


def prepare_image(data: bytes, max_side: int = 2000) -> PreparedImage:
    try:
        image = Image.open(io.BytesIO(data))  # lazy: the header only, nothing decoded yet
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as error:
        raise ImageError("could not read the photo") from error
    if image.width * image.height > MAX_PIXELS:
        raise ImageError("the photo is too large")
    if image.format == "JPEG":
        image.draft("RGB", (max_side, max_side))  # the decoder downsamples before load()
    try:
        image.load()
    except (OSError, ValueError) as error:
        raise ImageError("could not read the photo") from error
    image = ImageOps.exif_transpose(image).convert("RGB")
    if max(image.size) > max_side:
        scale = max_side / max(image.size)
        image = image.resize((round(image.width * scale), round(image.height * scale)))
    warnings: list[str] = []
    if min(image.size) < MIN_SIDE:
        warnings.append("too_small")
    sharpness = _sharpness(image)
    if sharpness < MIN_SHARPNESS:
        warnings.append("blurry")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)  # no exif argument: metadata is dropped
    return PreparedImage(buffer.getvalue(), image.width, image.height, sharpness, tuple(warnings))
