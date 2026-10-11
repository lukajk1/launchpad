"""Draws the app icon: assets/icon.ico (window, taskbar, shortcut) and ui/icon.png (favicon).

    python assets/make_icon.py

A little rocket heading up-right on the same dark rounded square as the Ideas icon: a cream body
with a blue window and fins, and an amber flame. The rocket is drawn upright on its own layer, then
tilted 45 degrees. Drawn large and scaled down, so the small sizes stay crisp.
"""
import os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = 1024
INK = (35, 32, 27, 255)          # matches Ideas' background
BODY = (255, 250, 240, 255)
ACCENT = (74, 134, 196, 255)     # a brighter take on the UI's #2f5d8a
NOZZLE = (90, 84, 74, 255)
FLAME = (232, 146, 58, 255)
FLAME_CORE = (255, 214, 102, 255)


def rocket():
    """The upright rocket on a transparent layer, centered on the canvas."""
    layer = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = S // 2, S // 2 - 40   # nose at -330, flame tip at +430: centre the whole length
    at = lambda pts: [(cx + x, cy + y) for x, y in pts]  # noqa: E731

    # Flame first, so the nozzle overlaps its top edge
    d.polygon(at([(-72, 270), (72, 270), (40, 360), (0, 440), (-40, 360)]), fill=FLAME)
    d.polygon(at([(-38, 270), (38, 270), (18, 340), (0, 385), (-18, 340)]), fill=FLAME_CORE)

    # Fins behind the body
    for side in (-1, 1):
        d.polygon(at([(side * 100, 40), (side * 215, 230), (side * 215, 315),
                      (side * 92, 238)]), fill=ACCENT)

    # Body: a pointed ogive nose easing into straight sides, slightly tapered at the base
    half, shoulder, nose = 112, -40, -330
    left = []
    for i in range(41):
        u = i / 40                       # 0 at the shoulder, 1 at the tip
        left.append((-half * (1 - u ** 1.7), shoulder + (nose - shoulder) * u))
    right = [(-x, y) for x, y in left]
    body = [(-96, 262), (-half, 230)] + left[:-1] + [(0, nose)] + right[-2::-1] + [(half, 230), (96, 262)]
    d.polygon(at(body), fill=BODY)

    d.polygon(at([(-80, 258), (80, 258), (90, 290), (-90, 290)]), fill=NOZZLE)

    # Window: a dark rim around a blue porthole
    for r, color in ((74, INK), (52, ACCENT)):
        d.ellipse((cx - r, cy - 70 - r, cx + r, cy - 70 + r), fill=color)
    return layer


def draw():
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((40, 40, S - 40, S - 40), radius=210, fill=INK)
    tilted = rocket().rotate(-45, resample=Image.BICUBIC, center=(S // 2, S // 2))
    img.alpha_composite(tilted)
    return img


def main():
    img = draw()
    png = os.path.join(ROOT, 'ui', 'icon.png')
    img.resize((256, 256), Image.LANCZOS).save(png)
    ico = os.path.join(ROOT, 'assets', 'icon.ico')
    img.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print('wrote', png, 'and', ico)


if __name__ == '__main__':
    main()
