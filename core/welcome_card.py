"""Image welcome cards (2.19): a member's avatar and name on a background.

Rules: `docs/subsystems/announcements.md`, "Image cards".

One module for both processes: the dashboard checks and stores an uploaded
background and draws the preview, and the bot draws the card it posts. Two
renderers would be the disagreement `core/managed_messages.py` exists to avoid.

A background is a file in the data directory, beside the database, because a
guild's image is runtime data like its rows, and the data directory is the one
place both processes share. Nothing here fetches anything from the network:
an operator uploads the image, the bot already holds the avatar.
"""

import io
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from core import database

# The events that may carry a card (the maintainer, 2026-10-30).
CARD_EVENTS = ("join", "leave")
# Each card's own background. A returning member's welcome has one of its own
# and falls back to the join card's, so a single upload still covers both.
CARD_BACKGROUNDS = ("join", "rejoin", "leave")
BACKGROUND_FALLBACK = {"rejoin": "join"}

CARD_SIZE = (1024, 320)
AVATAR_SIZE = 224
MARGIN = 48
FONT_PATH = Path(__file__).resolve().parents[1] / "data" / "fonts" / "NotoSans-Bold.ttf"

# A decoded upload larger than this is refused before it is opened. The client
# scales an image to the card's size before sending it, so a real one is far
# smaller; this bounds what a hand-made request can make Pillow read.
MAX_UPLOAD_BYTES = 1_000_000
# Pillow's decompression-bomb guard, lowered from its 89-megapixel default:
# nothing a card needs is larger than this, and it is checked before decoding.
MAX_SOURCE_PIXELS = 4096 * 4096
Image.MAX_IMAGE_PIXELS = MAX_SOURCE_PIXELS
ACCEPTED_FORMATS = {"PNG", "JPEG", "WEBP", "GIF"}

# Shipped background colours, top and bottom of a gradient, so a card works
# with nothing uploaded. Drawn from the dashboard's own warm palette.
DEFAULT_GRADIENT = ((0x2A, 0x1F, 0x18), (0x6B, 0x4A, 0x1E))


class CardImageError(ValueError):
    """An upload refused, with a locale key under `dashboard.errors.` as its reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --- storage ----------------------------------------------------------------

def background_path(guild_id: int, event: str) -> Path:
    """Where a guild's background for one card lives: `cards/` in the data
    directory, read at call time from `database.data_dir()`, so a test or a
    copy points it elsewhere by construction."""
    if event not in CARD_BACKGROUNDS:
        raise ValueError("no card for this event")
    return Path(database.data_dir()) / "cards" / f"{int(guild_id)}-{event}.png"


def load_background(guild_id: int, event: str) -> bytes | None:
    """The card's own background, else the one it falls back to, else None."""
    for key in (event, BACKGROUND_FALLBACK.get(event)):
        if key is None:
            continue
        try:
            return background_path(guild_id, key).read_bytes()
        except FileNotFoundError:
            continue
    return None


def save_background(guild_id: int, event: str, png: bytes) -> None:
    """Write atomically, owner and group only, as the database's backups are."""
    path = background_path(guild_id, event)
    path.parent.mkdir(mode=0o750, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o640)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(png)
    os.replace(temporary, path)


def delete_background(guild_id: int, event: str) -> bool:
    try:
        background_path(guild_id, event).unlink()
    except FileNotFoundError:
        return False
    return True


# --- an upload --------------------------------------------------------------

def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale to fill `size` and crop the overflow from the centre."""
    return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS)


def normalise_background(data: bytes) -> bytes:
    """Check an uploaded image and re-encode it as the card-sized PNG to store.

    Re-encoding is the point: what is kept is pixels this module drew, never
    the uploaded file, so metadata, trailing data and a second format inside
    it are all dropped. Refused rather than repaired: an unreadable image, a
    format outside `ACCEPTED_FORMATS`, too many bytes or too many pixels.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise CardImageError("card_image_too_large")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format not in ACCEPTED_FORMATS:
                raise CardImageError("card_image_unsupported")
            width, height = probe.size
            if width * height > MAX_SOURCE_PIXELS:
                raise CardImageError("card_image_too_large")
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.seek(0)  # a GIF's first frame
            card = _cover(ImageOps.exif_transpose(image).convert("RGB"), CARD_SIZE)
    except CardImageError:
        raise
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise CardImageError("card_image_unreadable") from exc
    output = io.BytesIO()
    card.save(output, format="PNG", optimize=True)
    return output.getvalue()


# --- drawing ----------------------------------------------------------------

def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_PATH), size)


def drawable(text: str, font: ImageFont.FreeTypeFont) -> str:
    """`text` without the characters this font has no glyph for.

    Emoji and most non-Latin scripts would otherwise draw as empty boxes; a
    name missing a symbol reads better than one with a box in it.
    """
    notdef = bytes(font.getmask("\U0010FFFD"))
    kept = "".join(
        character for character in text
        if character.isspace() or bytes(font.getmask(character)) != notdef)
    # A dropped symbol between two words would leave a double space.
    return " ".join(kept.split())


def _fit(draw, text: str, largest: int, smallest: int, width: int):
    """The largest font that fits `width`, the text cut with an ellipsis if
    even the smallest does not."""
    for size in range(largest, smallest - 1, -2):
        font = _font(size)
        shown = drawable(text, font)
        if draw.textlength(shown, font=font) <= width:
            return font, shown
    font = _font(smallest)
    shown = drawable(text, font)
    while shown and draw.textlength(shown + "…", font=font) > width:
        shown = shown[:-1]
    return font, (shown + "…") if shown else ""


def _default_background() -> Image.Image:
    (top_r, top_g, top_b), (bottom_r, bottom_g, bottom_b) = DEFAULT_GRADIENT
    width, height = CARD_SIZE
    background = Image.new("RGB", CARD_SIZE)
    draw = ImageDraw.Draw(background)
    for y in range(height):
        share = y / (height - 1)
        draw.line([(0, y), (width, y)], fill=(
            round(top_r + (bottom_r - top_r) * share),
            round(top_g + (bottom_g - top_g) * share),
            round(top_b + (bottom_b - top_b) * share)))
    return background


def _avatar(avatar: bytes | None, initial: str) -> Image.Image:
    """The avatar as a circle with a light ring; an initial on grey without one."""
    size = AVATAR_SIZE
    if avatar:
        try:
            with Image.open(io.BytesIO(avatar)) as image:
                image.seek(0)
                face = _cover(image.convert("RGB"), (size, size))
        except (OSError, ValueError, Image.DecompressionBombError):
            face = None
    else:
        face = None
    if face is None:
        face = Image.new("RGB", (size, size), (0x55, 0x4A, 0x40))
        draw = ImageDraw.Draw(face)
        font = _font(size // 2)
        letter = drawable(initial[:1].upper(), font) or "?"
        draw.text((size / 2, size / 2), letter, font=font, fill="white", anchor="mm")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    ring = Image.new("RGBA", (size + 12, size + 12), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((0, 0, size + 11, size + 11), fill=(255, 255, 255, 230))
    ring.paste(face, (6, 6), mask)
    return ring


def render_card(background: bytes | None, avatar: bytes | None,
                name: str, line: str) -> bytes:
    """The card as PNG bytes: background darkened, avatar left, name and line.

    Pure and synchronous, so the bot runs it in an executor and the dashboard
    calls it directly for the preview.
    """
    if background:
        with Image.open(io.BytesIO(background)) as image:
            base = _cover(image.convert("RGB"), CARD_SIZE)
    else:
        base = _default_background()
    card = base.convert("RGBA")
    # Darkened so white text reads on any image.
    card.alpha_composite(Image.new("RGBA", CARD_SIZE, (0, 0, 0, 110)))

    width, height = CARD_SIZE
    ring = _avatar(avatar, name)
    card.alpha_composite(ring, (MARGIN, (height - ring.height) // 2))

    draw = ImageDraw.Draw(card)
    text_x = MARGIN + ring.width + 40
    text_width = width - text_x - MARGIN
    name_font, name_text = _fit(draw, name, 64, 32, text_width)
    line_font, line_text = _fit(draw, line, 38, 22, text_width)
    # A thin dark outline keeps the text readable on a busy background.
    draw.text((text_x, height / 2 - 6), name_text, font=name_font,
              fill="white", anchor="ls", stroke_width=2, stroke_fill=(0, 0, 0))
    draw.text((text_x, height / 2 + 18), line_text, font=line_font,
              fill=(235, 225, 210), anchor="lt", stroke_width=2, stroke_fill=(0, 0, 0))

    output = io.BytesIO()
    card.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()
