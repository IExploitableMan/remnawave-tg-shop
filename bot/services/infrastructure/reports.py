"""Local PNG/PDF rendering. No upload service or sponsor content."""
import html
import statistics
import textwrap
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, PageBreak, Spacer, Image as PDFImage
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
MONO = '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'


def fit_text(draw, text, font, width):
    text = str(text)
    while text and draw.textlength(text, font=font) > width:
        text = text[:-2] + '…'
    return text


def summary_pages(directory, manifest, result, index, title_font, label_font):
    from .report_data import parse_result
    data = parse_result(result)
    if not any(data.values()):
        return []
    sections = [('services', data['services'][i:i+30]) for i in range(0, len(data['services']), 30)]
    sections += [('bars', data['bars'][i:i+20]) for i in range(0, len(data['bars']), 20)]
    if not sections:
        sections = [('facts', [])]
    files = []
    small = ImageFont.truetype(FONT, 18)
    status_names = {'ok': 'выполнен', 'partial': 'частично', 'failed': 'ошибка', 'timeout': 'таймаут'}
    for page, (section, rows) in enumerate(sections, 1):
        facts = ([('Пропущенные проверки', warning) for warning in result.get('warnings', [])] + data['facts'])[:12]
        facts_height = ((len(facts)+3)//4)*82
        content_height = ((len(rows)+5)//6)*132 if section == 'services' else ((len(rows)+1)//2)*64
        height = max(480, 278 + facts_height + content_height)
        canvas = Image.new('RGB', (1280, height), '#0b0b0b')
        draw = ImageDraw.Draw(canvas)
        draw.text((38, 30), manifest['node'], font=title_font, fill='#eeeeee')
        draw.text((38, 78), f"{manifest['address']} · {manifest['created_at'][:19]} · {index}/{len(manifest['results'])}",
                  font=label_font, fill='#888888')
        draw.line((38, 120, 1242, 120), fill='#303030')
        draw.rounded_rectangle((38, 142, 1242, height-30), radius=8, fill='#121212', outline='#303030')
        draw.text((62, 164), fit_text(draw, result['title'], label_font, 860), font=label_font, fill='#eeeeee')
        draw.text((1000, 164), status_names.get(result['status'], result['status']), font=small, fill='#888888')
        draw.text((62, 195), f"Проход {result['round']} · {result.get('seconds', 0):.0f} сек", font=small, fill='#888888')
        top = 232
        for offset, (label, value) in enumerate(facts):
            x, y = 62+(offset%4)*288, top+(offset//4)*82
            draw.rounded_rectangle((x,y,x+272,y+68), radius=5, fill='#202020')
            draw.text((x+12,y+8), fit_text(draw,label,small,248), font=small, fill='#888888')
            draw.text((x+12,y+35), fit_text(draw,value,label_font,248), font=label_font, fill='#eeeeee')
        top += facts_height
        if section == 'services':
            for offset, row in enumerate(rows):
                x,y = 62+(offset%6)*192, top+(offset//6)*132
                fill = '#e5e5e5' if row['status']=='bad' else '#202020'
                ink = '#191919' if row['status']=='bad' else '#eeeeee'
                draw.rounded_rectangle((x,y,x+180,y+118), radius=5, fill=fill)
                name_lines = textwrap.wrap(row['label'], 18)[:2]
                for number, line in enumerate(name_lines):
                    draw.text((x+12,y+14+number*23), fit_text(draw,line,small,156), font=small, fill=ink)
                value = row['value'] or 'N/A'
                draw.text((x+12,y+76), fit_text(draw,value,label_font,156), font=label_font, fill=ink)
        elif section == 'bars':
            maximum = max((b['value'] for b in data['bars']), default=1) or 1
            for offset, row in enumerate(rows):
                x,y = 62+(offset%2)*576, top+(offset//2)*64
                draw.text((x,y), fit_text(draw,row['label'],small,310), font=small, fill='#eeeeee')
                value = f"↓ {row['value']:.1f}"
                if row['upload'] is not None:
                    value += f"  ↑ {row['upload']:.1f}"
                draw.text((x+330,y), value, font=small, fill='#aaaaaa')
                draw.rectangle((x,y+33,x+550,y+38), fill='#303030')
                draw.rectangle((x,y+33,x+max(1,int(550*row['value']/maximum)),y+38), fill='#dddddd')
        filename = f'{index:02d}-{page:02d}.png'
        canvas.save(directory/filename)
        files.append(filename)
    return files


def aggregate(results):
    grouped = {}
    for result in results:
        for label, value in result.get('metrics', {}).items():
            grouped.setdefault((result['title'], label), []).append(value)
    return [{'test': test, 'metric': metric, 'count': len(values), 'min': min(values),
             'median': statistics.median(values), 'max': max(values)}
            for (test, metric), values in grouped.items() if len(values) > 1]


def render_reports(directory, manifest):
    title_font = ImageFont.truetype(FONT, 30)
    label_font = ImageFont.truetype(FONT, 20)
    mono_font = ImageFont.truetype(MONO, 18)
    files = []
    for index, result in enumerate(manifest['results'], 1):
        summaries = summary_pages(directory, manifest, result, index, title_font, label_font)
        if summaries:
            files.extend(summaries)
            continue
        lines = [wrapped for line in result['output'].splitlines()
                 for wrapped in (textwrap.wrap(line, 95, replace_whitespace=False) or [''])]
        chunks = [lines[offset:offset + 48] for offset in range(0, len(lines), 48)] or [['Нет данных']]
        for page, chunk in enumerate(chunks, 1):
            height = max(480, 220 + len(chunk) * 26)
            image = Image.new('RGB', (1280, height), '#0b0b0b')
            draw = ImageDraw.Draw(image)
            draw.text((38, 30), manifest['node'], font=title_font, fill='#f1f1f1')
            draw.text((38, 78), f"{manifest['address']} · {manifest['created_at'][:19]} · {index}/{len(manifest['results'])}",
                      font=label_font, fill='#888888')
            draw.line((38, 120, 1242, 120), fill='#2c2c2c')
            draw.rounded_rectangle((38, 142, 1242, height - 30), radius=8, fill='#121212', outline='#2c2c2c')
            draw.text((62, 160), f"{result['title']} · {result['status']} · проход {result['round']}",
                      font=label_font, fill='#ededed')
            for line_number, line in enumerate(chunk):
                draw.text((62, 204 + line_number * 26), line, font=mono_font, fill='#bbbbbb')
            filename = f'{index:02d}-{page:02d}.png'
            image.save(directory / filename)
            files.append(filename)
    if manifest['level'] == 'large':
        pdfmetrics.registerFont(TTFont('InfraSans', FONT))
        pdfmetrics.registerFont(TTFont('InfraMono', MONO))
        styles = getSampleStyleSheet()
        for style in styles.byName.values():
            style.fontName = 'InfraSans'
        styles['Code'].fontName = 'InfraMono'
        styles['Code'].fontSize = 7
        styles['Code'].leading = 9
        story = [Paragraph(html.escape(manifest['node']), styles['Title']),
                 Paragraph(html.escape(f"{manifest['address']} — {manifest['created_at']}"), styles['Normal']),
                 Spacer(1, 16)]
        for item in aggregate(manifest['results']):
            story.append(Paragraph(html.escape(f"{item['test']} / {item['metric']}: "
                f"min {item['min']:.2f}, median {item['median']:.2f}, max {item['max']:.2f} ({item['count']} замеров)"), styles['Normal']))
        for filename in files:
            with Image.open(directory/filename) as picture:
                width, height = picture.size
            scale = min(490/width, 690/height)
            story += [PageBreak(), PDFImage(str(directory/filename), width=width*scale, height=height*scale)]
        filename = 'report.pdf'
        SimpleDocTemplate(str(directory / filename)).build(story)
        files.append(filename)
    return files
