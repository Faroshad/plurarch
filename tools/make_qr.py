"""Make a high-resolution QR code PNG for the participant app, with the short URL underneath.

Usage:
    python tools/make_qr.py <url> [--out path] [--label text] [--width px]

    python tools/make_qr.py https://faroshad.github.io/plurarch/
        -> <state_dir>/qr.png, labelled "faroshad.github.io/plurarch"

The PNG is at least 1600 px wide (default 2000), error correction Q (survives about 25 %
damage, e.g. a projector hot spot or a slide logo), a 6-module quiet zone, crisp
integer-pixel modules, and the label in a large bold font (Inter, Segoe UI, Arial, or
Pillow's built-in font).

Default output: <state_dir>/qr.png, where state_dir comes from config/local.json
("state_dir") when present, else <repo>/state.

For the orchestrator:
    from make_qr import make_qr        # importing is cheap; qrcode/Pillow load on first call
    path = make_qr(url, out_path, label=None)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

MIN_WIDTH = 1600
DEFAULT_WIDTH = 2000
QUIET_MODULES = 6          # the QR spec asks for >= 4; more is safer on slides
INK = (17, 17, 17)         # --text #111111
PAPER = (255, 255, 255)

_FONT_CANDIDATES = (
    # Inter (as in the UI), installed per user or system-wide
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts\Inter_24pt-Bold.ttf"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts\Inter_18pt-Bold.ttf"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Windows\Fonts\Inter-Bold.ttf"),
    r"C:\Windows\Fonts\Inter_24pt-Bold.ttf",
    r"C:\Windows\Fonts\Inter-Bold.ttf",
    r"C:\Windows\Fonts\segoeuib.ttf",          # Segoe UI Bold
    r"C:\Windows\Fonts\arialbd.ttf",           # Arial Bold
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
)


def default_out_path() -> Path:
    """<state_dir>/qr.png from config/local.json, else <repo>/state/qr.png."""
    local = REPO / "config" / "local.json"
    try:
        state_dir = json.loads(local.read_text(encoding="utf-8")).get("state_dir")
        if state_dir:
            return Path(state_dir) / "qr.png"
    except (OSError, ValueError, AttributeError):
        pass
    return REPO / "state" / "qr.png"


def short_label(url: str) -> str:
    """'https://faroshad.github.io/plurarch/' -> 'faroshad.github.io/plurarch'."""
    text = url.strip()
    for prefix in ("https://", "http://"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
    if text.lower().startswith("www."):
        text = text[4:]
    return text.rstrip("/")


def _load_font(size: int):
    from PIL import ImageFont

    for candidate in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)   # Pillow >= 10.1: scalable built-in font
    except TypeError:
        return ImageFont.load_default()


def _fit_font(draw, text: str, max_width: int, start_size: int):
    size = start_size
    while True:
        font = _load_font(size)
        left, _, right, _ = draw.textbbox((0, 0), text, font=font)
        if right - left <= max_width or size <= 24:
            return font
        size = int(size * 0.92)


def make_qr(url: str, out_path: str | os.PathLike | None = None, label: str | None = None,
            width: int = DEFAULT_WIDTH) -> str:
    """Write the QR PNG and return its absolute path. `label=None` uses the short URL;
    `label=""` draws no label."""
    import qrcode
    from qrcode.constants import ERROR_CORRECT_Q
    from PIL import Image, ImageDraw

    if not url or not url.strip():
        raise ValueError("url is empty")
    url = url.strip()
    width = max(int(width), MIN_WIDTH)
    out = Path(out_path) if out_path else default_out_path()
    out.parent.mkdir(parents=True, exist_ok=True)

    qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_Q, box_size=1, border=0)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()                  # border=0 -> just the modules
    n = len(matrix)
    total_modules = n + 2 * QUIET_MODULES
    module_px = max(1, width // total_modules)
    qr_px = module_px * total_modules
    width = max(width, qr_px)

    text = short_label(url) if label is None else label.strip()
    label_area = int(width * 0.16) if text else 0
    height = qr_px + label_area

    img = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(img)
    x0 = (width - qr_px) // 2 + QUIET_MODULES * module_px
    y0 = QUIET_MODULES * module_px
    for r, row in enumerate(matrix):
        for c, dark in enumerate(row):
            if dark:
                x = x0 + c * module_px
                y = y0 + r * module_px
                draw.rectangle([x, y, x + module_px - 1, y + module_px - 1], fill=(0, 0, 0))

    if text:
        font = _fit_font(draw, text, int(width * 0.9), int(width * 0.075))
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        tx = (width - (right - left)) // 2 - left
        # the label sits in the lower quiet zone + label area, optically centred
        area_top = qr_px - QUIET_MODULES * module_px // 2
        ty = area_top + (height - area_top - (bottom - top)) // 2 - top
        draw.text((tx, ty), text, font=font, fill=INK)

    img.save(out, "PNG", dpi=(300, 300), optimize=True)
    return str(out.resolve())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QR code PNG for the Plurarch participant app")
    parser.add_argument("url", help="the participant app URL, e.g. https://faroshad.github.io/plurarch/")
    parser.add_argument("--out", help=f"output PNG (default: {default_out_path()})")
    parser.add_argument("--label", help="text under the code (default: the URL without https://; '' for none)")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH,
                        help=f"image width in px (min {MIN_WIDTH}, default {DEFAULT_WIDTH})")
    args = parser.parse_args(argv)
    if not args.url.lower().startswith(("http://", "https://")):
        print(f"warning: {args.url!r} has no http(s):// scheme; phones may treat it as text", file=sys.stderr)
    path = make_qr(args.url, args.out, args.label, args.width)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
