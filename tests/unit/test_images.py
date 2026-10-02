import io

import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from waive.cases.images import MIN_SHARPNESS, ImageError, prepare_image


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
        image.save(buffer, format="JPEG", exif=exif.tobytes())
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
    prepared = prepare_image(text_image(size=(1200, 1600), exif_rotate=True))
    assert (prepared.width, prepared.height) == (1600, 1200)
    assert b"Exif" not in prepared.jpeg[:64]


def test_blurry_and_small_images_get_warnings():
    blurry = prepare_image(text_image(blur=6.0))
    assert "blurry" in blurry.warnings
    small = prepare_image(text_image(size=(400, 500)))
    assert "too_small" in small.warnings


def test_garbage_raises_image_error():
    with pytest.raises(ImageError):
        prepare_image(b"not an image")
