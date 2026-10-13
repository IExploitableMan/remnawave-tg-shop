"""Selectable console text in a dark PDF; no screenshots or upload services."""
import re
from dataclasses import dataclass, replace
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ESCAPE = re.compile(r'\x1b(?:\[([0-?]*)([ -/]*)([@-~])|\][^\x07]*(?:\x07|\x1b\\))')
PALETTE = ['#20242c', '#ef5350', '#43c98a', '#e5ba63', '#5caaff', '#ce8cf4', '#42c7cf', '#e7e9ef',
           '#89909e', '#ff7771', '#75e0a9', '#f4d78b', '#91c5ff', '#e4b6ff', '#85e4ea', '#ffffff']
BACKGROUND = '#101218'
PROMOTIONS = re.compile(
    r'sponsor|powered by|follow[: ]|t\.me/|tracerlab|stencloud|bedolaga|github\.com/|'
    r'bench\.sh \| bash|yabs\.sh \| bash|forked by|made with|author:|script by|'
    r'thanks for running|from the community, for the community|teddysun|'
    r'^\s*(?:website|blog|channel|telegram|advertisement|donate)\s*:', re.I)


def clean_console(raw, preserve_ansi=False):
    # Progress updates replace the current row. Keep only its final contents.
    rows = []
    for row in raw.split('\n'):
        row = row.split('\r')[-1]
        plain = ESCAPE.sub('', row)
        if PROMOTIONS.search(plain):
            continue
        row = re.sub(r'[\u2800-\u28ff]\x08?|\x08', '', row)
        row = ''.join(c for c in row if c in '\t\x1b' or ord(c) >= 32)
        rows.append(row if preserve_ansi else ESCAPE.sub('', row))
    return '\n'.join(rows)


@dataclass(frozen=True)
class Style:
    foreground: str = '#e7e9ef'
    background: str | None = None
    bold: bool = False
    italic: bool = False
    underline: bool = False


def indexed_color(index):
    if index < 16:
        return PALETTE[max(0, index)]
    if index < 232:
        n = index - 16
        levels = (0, 95, 135, 175, 215, 255)
        return '#%02x%02x%02x' % (levels[n // 36], levels[(n // 6) % 6], levels[n % 6])
    grey = min(255, 8 + (index - 232) * 10)
    return '#%02x%02x%02x' % (grey, grey, grey)


def sgr(style, parameters):
    try:
        codes = [int(v or 0) for v in parameters.split(';')]
    except ValueError:
        return style
    i = 0
    while i < len(codes):
        code = codes[i]
        if code == 0: style = Style()
        elif code in (1, 22): style = replace(style, bold=code == 1)
        elif code in (3, 23): style = replace(style, italic=code == 3)
        elif code in (4, 24): style = replace(style, underline=code == 4)
        elif code == 39: style = replace(style, foreground=Style().foreground)
        elif code == 49: style = replace(style, background=None)
        elif 30 <= code <= 37: style = replace(style, foreground=PALETTE[code - 30])
        elif 90 <= code <= 97: style = replace(style, foreground=PALETTE[code - 90 + 8])
        elif 40 <= code <= 47: style = replace(style, background=PALETTE[code - 40])
        elif 100 <= code <= 107: style = replace(style, background=PALETTE[code - 100 + 8])
        elif code in (38, 48) and i + 2 < len(codes):
            color = None
            if codes[i + 1] == 5:
                color = indexed_color(max(0, min(255, codes[i + 2])))
                i += 2
            elif codes[i + 1] == 2 and i + 4 < len(codes):
                color = '#%02x%02x%02x' % tuple(max(0, min(255, v)) for v in codes[i + 2:i + 5])
                i += 4
            if color:
                style = replace(style, **{'foreground' if code == 38 else 'background': color})
        i += 1
    return style


def console_rows(raw):
    """Return colored spans, retaining spaces and styles across terminal rows."""
    style = Style()
    rows = []
    for row in clean_console(raw, preserve_ansi=True).split('\n'):
        spans, cursor, column = [], 0, 0
        for match in list(ESCAPE.finditer(row)) + [None]:
            end = match.start() if match else len(row)
            text = row[cursor:end]
            # Tabs have terminal tab stops, not a fixed four-space replacement.
            expanded = ''
            for character in text:
                piece = ' ' * (8 - column % 8) if character == '\t' else character
                expanded += piece
                column += len(piece)
            if expanded:
                spans.append((expanded, style))
            if match:
                if match[3] == 'm':
                    style = sgr(style, match[1])
                cursor = match.end()
        rows.append(spans)
    if '\x1b[' not in raw:
        # Older checkpoints were captured without ANSI. Preserve their text and
        # emphasize statuses conservatively; do not invent measurements.
        highlighted = []
        pattern = re.compile(r'(?i)download failed|upload failed|\bfailed\b|\bblocked?\b|\bbusy\b|\bN/A\b|Rate-limit|\bYes\b|\bNo\b')
        for spans in rows:
            line = ''.join(text for text, _ in spans)
            result, cursor = [], 0
            for match in pattern.finditer(line):
                if match.start() > cursor: result.append((line[cursor:match.start()], Style()))
                token = match.group().lower()
                color = PALETTE[2] if token == 'yes' else (PALETTE[8] if token == 'n/a' else (PALETTE[3] if token in ('busy', 'rate-limit') else PALETTE[1]))
                result.append((match.group(), Style(foreground=color)))
                cursor = match.end()
            if cursor < len(line): result.append((line[cursor:], Style()))
            highlighted.append(result)
        return highlighted
    return rows


def report_results(results):
    # System/Russian network overview requested first; collection order is unchanged.
    priorities = {'bench': 0, 'tlab_speed': 1, 'yabs': 2, 'ru_speed': 3}
    return sorted(results, key=lambda r: (priorities.get(r.get('id'), 4), r.get('round', 1)))


def register_fonts():
    root = Path('/usr/share/fonts/truetype/dejavu')
    for suffix, file in (('', 'DejaVuSansMono.ttf'), ('Bold', 'DejaVuSansMono-Bold.ttf'),
                         ('Italic', 'DejaVuSansMono-Oblique.ttf'), ('BoldItalic', 'DejaVuSansMono-BoldOblique.ttf')):
        name = 'InfraConsole' + suffix
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(root / file)))


def render_console_pdf(directory, manifest, aggregates):
    register_fonts()
    width, height = landscape(A4)
    pdf = canvas.Canvas(str(directory / 'report.pdf'), pagesize=(width, height))
    pdf.setTitle(f"{manifest['node']} — diagnostics")
    margin, top, bottom = 28, height - 78, 35
    page = 0

    def new_page(title):
        nonlocal page
        if page:
            pdf.showPage()
        page += 1
        pdf.setFillColor(HexColor(BACKGROUND))
        pdf.rect(0, 0, width, height, fill=1, stroke=0)
        pdf.setFillColor(HexColor('#e7e9ef'))
        pdf.setFont('InfraConsoleBold', 11)
        pdf.drawString(margin, height - 27, str(manifest['node']))
        pdf.setFont('InfraConsole', 8)
        pdf.setFillColor(HexColor('#89909e'))
        pdf.drawString(margin, height - 43, f"{manifest['address']}  |  {manifest['created_at'][:19]}  |  {title}")
        pdf.drawRightString(width - margin, 18, str(page))
        return top

    for result in report_results(manifest['results']):
        title = f"{result['title']} | {result['status']} | round {result['round']}"
        rows = console_rows(result.get('ansi_output') or result.get('output') or 'No output')
        # Uniform font size for the whole table preserves column alignment.
        longest = max((sum(len(text) for text, _ in row) for row in rows), default=1)
        size = max(6, min(10, (width - margin * 2) / (max(1, longest) * .603)))
        cell, leading = pdfmetrics.stringWidth('M', 'InfraConsole', size), size * 1.4
        columns = max(1, int((width - margin * 2) / cell))
        y = new_page(title)
        warnings = result.get('warnings', [])
        if warnings:
            rows = console_rows('\n'.join('Warning: ' + str(v) for v in warnings)) + [[]] + rows
        for spans in rows:
            # Only unusually long rows wrap; ordinary console tables remain intact.
            chunks, current, length = [], [], 0
            for text, style in spans:
                while text:
                    take = min(len(text), columns - length)
                    current.append((text[:take], style)); text = text[take:]; length += take
                    if length == columns:
                        chunks.append(current); current, length = [], 0
            if current or not chunks:
                chunks.append(current)
            for chunk in chunks:
                if y < bottom:
                    y = new_page(title + ' (continued)')
                x = margin
                for text, style in chunk:
                    span_width = len(text) * cell
                    if style.background:
                        pdf.setFillColor(HexColor(style.background))
                        pdf.rect(x, y - size * .24, span_width, leading, fill=1, stroke=0)
                    pdf.setFillColor(HexColor(style.foreground))
                    font = 'InfraConsole' + ('Bold' if style.bold else '') + ('Italic' if style.italic else '')
                    pdf.setFont(font, size)
                    pdf.drawString(x, y, text)
                    if style.underline:
                        pdf.setStrokeColor(HexColor(style.foreground));pdf.line(x, y - 1, x + span_width, y - 1)
                    x += span_width
                y -= leading
    if aggregates:
        y = new_page('Repeated measurements: min / median / max')
        for item in aggregates:
            if y < bottom: y = new_page('Repeated measurements (continued)')
            pdf.setFillColor(HexColor('#e7e9ef'));pdf.setFont('InfraConsole', 8)
            pdf.drawString(margin, y, f"{item['test']} / {item['metric']}: {item['min']:.2f} / {item['median']:.2f} / {item['max']:.2f} ({item['count']})")
            y -= 15
    if not page: new_page('No results')
    pdf.save()
    return 'report.pdf'
