import io
import struct
import zlib

import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from waive.cases.images import MAX_PIXELS, MIN_SHARPNESS, ImageError, prepare_image


def text_image(size=(1600, 2200), blur=0.0, fmt="PNG", exif_rotate=False):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=40)
    for i in range(20):
        draw.text(
            (80, 80 + i * 90), f"STATEMENT LINE {i} AMOUNT DUE $1,850.00", fill="black", font=font
        )
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    buffer = io.BytesIO()
    if exif_rotate:
        exif = Image.Exif()
        exif[0x0112] = 6  # orientation: rotated 90 degrees clockwise
        # Everything a camera app or editor might leave behind, all of it to be stripped.
        image.save(
            buffer,
            format="JPEG",
            exif=exif.tobytes(),
            comment=b"secret GPS 42.3601,-71.0589 shot by Rosa Alvarez",
            xmp=b"<x:xmpmeta>secret</x:xmpmeta>",
            icc_profile=b"secret-icc" * 8,
        )
    else:
        image.save(buffer, format=fmt)
    return buffer.getvalue()


def test_prepare_resizes_to_max_side_and_reencodes_jpeg():
    prepared = prepare_image(text_image(size=(3000, 4000)))
    assert prepared.jpeg[:3] == b"\xff\xd8\xff"
    assert max(prepared.width, prepared.height) == 2000
    assert prepared.warnings == ()
    assert prepared.sharpness >= MIN_SHARPNESS


def test_prepare_strips_metadata_and_applies_orientation():
    source = text_image(size=(1200, 1600), exif_rotate=True)
    assert b"secret" in source and b"Exif" in source
    prepared = prepare_image(source)
    assert (prepared.width, prepared.height) == (1600, 1200)
    assert b"Exif" not in prepared.jpeg[:64]
    # Pillow re-encodes a JPEG COM marker from im.info["comment"] unless told otherwise: the
    # output must carry nothing but the JFIF header (README: "metadata stripped").
    assert b"secret" not in prepared.jpeg
    out = Image.open(io.BytesIO(prepared.jpeg))
    assert set(out.info) <= {"jfif", "jfif_version", "jfif_unit", "jfif_density", "dpi"}, sorted(
        out.info
    )


def test_blurry_and_small_images_get_warnings():
    blurry = prepare_image(text_image(blur=6.0))
    assert "blurry" in blurry.warnings
    small = prepare_image(text_image(size=(400, 500)))
    assert "too_small" in small.warnings


def test_garbage_raises_image_error():
    with pytest.raises(ImageError):
        prepare_image(b"not an image")


def png_header_only(width: int, height: int) -> bytes:
    """A valid PNG whose header claims any size: Pillow learns the size without decoding."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", width, height, 1, 0, 0, 0, 0)  # 1-bit grayscale
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b"\x00"))
        + chunk(b"IEND", b"")
    )


def test_decompression_bombs_are_refused_as_image_errors():
    assert MAX_PIXELS == 40_000_000
    with pytest.raises(ImageError):  # over Pillow's hard limit: DecompressionBombError
        prepare_image(png_header_only(20000, 20000))
    with pytest.raises(ImageError, match="too large"):  # under Pillow's limit, over ours
        prepare_image(png_header_only(9000, 9000))
    buffer = io.BytesIO()
    Image.new("1", (7000, 7000), 1).save(buffer, format="PNG")  # a few KB that inflate to 49 MP
    assert len(buffer.getvalue()) < 100_000
    with pytest.raises(ImageError, match="too large"):
        prepare_image(buffer.getvalue())


def test_large_jpeg_is_downsampled_by_the_decoder_and_still_fits_max_side():
    buffer = io.BytesIO()
    Image.new("RGB", (4800, 4000), "white").save(buffer, format="JPEG")
    prepared = prepare_image(buffer.getvalue())
    assert (prepared.width, prepared.height) == (2000, 1667)
