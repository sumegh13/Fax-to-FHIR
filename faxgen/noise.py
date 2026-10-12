"""Degrade a clean page into something that looks like it came off a fax machine.

Every geometric change is also applied to the field bounding boxes so the
ground truth still points at the right pixels.
"""
import math
import random

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .layout import FONTS

LEVELS = {
    #          skew deg, blur, noise sigma, speckle rate, stamp prob
    "clean":  (0.5, 0.0, 4, 0.0000, 0.0),
    "mild":   (2.0, 0.6, 12, 0.0004, 0.5),
    "ugly":   (5.0, 1.1, 22, 0.0015, 0.9),
}


def _stamp(img: Image.Image, rng: random.Random, date: str):
    """Rubber 'RECEIVED' stamp, rotated and partly transparent, dropped anywhere on the page."""
    f = ImageFont.truetype(str(FONTS / "CourierPrime-Bold.ttf"), 56)
    text = rng.choice(["RECEIVED", "FAXED", "COPY", "SCANNED"])
    layer = Image.new("L", (560, 200), 0)
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle((6, 6, 554, 194), radius=18, outline=255, width=8)
    d.text((40, 30), text, font=f, fill=255)
    d.text((40, 110), date, font=f, fill=255)
    layer = layer.rotate(rng.uniform(-25, 25), expand=True)
    x = rng.randint(100, img.width - layer.width - 100)
    y = rng.randint(200, img.height - layer.height - 200)
    ink = Image.new("L", layer.size, rng.randint(70, 130))
    mask = layer.point(lambda v: int(v * rng.uniform(0.55, 0.85)))
    img.paste(ink, (x, y), mask)


def _rotate_box(box, angle, cx, cy):
    """Map an axis-aligned box through PIL's counter-clockwise rotate about (cx, cy)."""
    t = math.radians(angle)
    c, s = math.cos(t), math.sin(t)
    x0, y0, x1, y1 = box
    pts = [(x, y) for x in (x0, x1) for y in (y0, y1)]
    rot = [(cx + (x - cx) * c + (y - cy) * s, cy - (x - cx) * s + (y - cy) * c) for x, y in pts]
    xs, ys = zip(*rot)
    return [round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys))]


def degrade(img: Image.Image, fields: list[dict], level: str, rng: random.Random, date: str):
    skew, blur, sigma, speckle, stamp_p = LEVELS[level]
    img = img.copy()
    if rng.random() < stamp_p:
        _stamp(img, rng, date)

    angle = rng.uniform(-skew, skew)
    img = img.rotate(angle, resample=Image.BICUBIC, fillcolor=255)
    for f in fields:
        f["bbox"] = _rotate_box(f["bbox"], angle, img.width / 2, img.height / 2)

    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))

    # Fax "standard" mode is ~200x100 dpi: squash vertically, then stretch back.
    if level == "ugly":
        w, h = img.size
        img = img.resize((w, h // 2), Image.BILINEAR).resize((w, h), Image.NEAREST)

    nrng = np.random.default_rng(rng.randint(0, 2**32 - 1))
    a = np.asarray(img, dtype=np.float32)
    a += nrng.normal(0, sigma, a.shape)

    if level != "clean":
        # 1-bit fax transmission: threshold with a little per-pixel jitter.
        thresh = 150 + nrng.normal(0, 18, a.shape)
        a = np.where(a < thresh, 0, 255).astype(np.float32)
        # Speckles and dropout lines.
        mask = nrng.random(a.shape) < speckle
        a[mask] = 0
        for _ in range(rng.randint(0, 4 if level == "ugly" else 1)):
            y = rng.randint(0, a.shape[0] - 3)
            a[y:y + rng.randint(1, 3), :] = 0 if rng.random() < 0.6 else 255

    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)), fields, angle
