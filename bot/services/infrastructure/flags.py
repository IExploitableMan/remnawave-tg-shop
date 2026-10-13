"""Local decorative country flags; no network access during report rendering."""
import re
from pathlib import Path
from functools import lru_cache
from PIL import Image

ROOT = Path(__file__).resolve().parents[3] / 'infra' / 'flags'
CODE = re.compile(r'\[([A-Z]{2})\]|(?<![\w])([A-Z]{2})(?![\w])')


def flag_path(code):
    code = {'UK': 'GB'}.get(code, code)
    if not re.fullmatch('[A-Z]{2}', code):
        return None
    path = ROOT / (code.lower() + '.png')
    return path if path.is_file() else None


def flag_positions(line, kind):
    if kind not in ('ipregion', 'ipcheck', 'ipquality'):
        return []
    positions = []
    for match in CODE.finditer(line):
        path = flag_path(match[1] or match[2])
        # Use existing column padding, preserving every text column and header.
        if path and (match.start() == 0 or line[max(0, match.start()-3):match.start()] == '   '):
            positions.append((match.start(), path))
    return positions


@lru_cache(maxsize=256)
def icon(path):
    with Image.open(path) as image:
        return image.convert('RGBA').resize((24, 18), Image.Resampling.LANCZOS)


def draw_value(canvas, draw, position, text, font, fill):
    x, y = position
    cursor = 0
    for match in CODE.finditer(text):
        path = flag_path(match[1] or match[2])
        if not path:
            continue
        prefix = text[cursor:match.start()]
        draw.text((x, y), prefix, font=font, fill=fill)
        x += draw.textlength(prefix, font=font)
        image = icon(str(path))
        canvas.paste(image, (int(x), int(y+3)), image)
        x += 29
        draw.text((x, y), match[0], font=font, fill=fill)
        x += draw.textlength(match[0], font=font)
        cursor = match.end()
    draw.text((x, y), text[cursor:], font=font, fill=fill)
