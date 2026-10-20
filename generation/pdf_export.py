import logging
import time
from io import BytesIO

from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)
PAPER_SIZES = {'a4': (2480, 3508), 'letter': (2550, 3300)}
CARD_SIZE = (744, 1039)
MAX_IMAGE_BYTES = 4 * 1024 * 1024


def _prepare_image(raw):
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError('Cada imagen debe pesar como máximo 4 MB.')
    try:
        with Image.open(BytesIO(raw)) as image:
            if image.format not in ('JPEG', 'PNG'):
                raise ValueError('Las imágenes deben ser JPEG o PNG.')
            image.verify()
        with Image.open(BytesIO(raw)) as image:
            rgba = image.convert('RGBA')
            rgb = Image.new('RGB', rgba.size, 'white')
            rgb.paste(rgba, mask=rgba.getchannel('A'))
            resized = rgb.resize(CARD_SIZE, Image.Resampling.LANCZOS)
            output = BytesIO()
            resized.save(output, 'JPEG', quality=95)
        return output.getvalue()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError('Imagen inválida: usa JPEG o PNG de hasta 4 MB.') from exc


def _page(images, keys, paper, cut_marks):
    width, height = PAPER_SIZES[paper]
    card_w, card_h = CARD_SIZE
    left, top = (width - 3 * card_w) // 2, (height - 3 * card_h) // 2
    page = Image.new('RGB', (width, height), 'white')
    for index, key in enumerate(keys):
        with Image.open(BytesIO(images[key])) as card:
            page.paste(card, (left + (index % 3) * card_w, top + (index // 3) * card_h))
    if cut_marks:
        draw = ImageDraw.Draw(page)
        # With no gutters, project every cut into the outer margins; never mark a neighbor's art.
        right, bottom = left + 3 * card_w, top + 3 * card_h
        for column in range(4):
            x = left + column * card_w
            draw.line((x, top - 43, x, top - 4), fill='#888888', width=3)
            draw.line((x, bottom + 3, x, bottom + 42), fill='#888888', width=3)
        for row in range(4):
            y = top + row * card_h
            draw.line((left - 43, y, left - 4, y), fill='#888888', width=3)
            draw.line((right + 3, y, right + 42, y), fill='#888888', width=3)
    return page


def build_pdf(images, layout, paper='a4', cut_marks=True, bleed_mm=0):
    started = time.monotonic()
    if paper not in PAPER_SIZES:
        raise ValueError('paper debe ser a4 o letter.')
    if bleed_mm != 0:
        raise ValueError('Por ahora bleed_mm debe ser 0.')
    if not isinstance(layout, list) or not 1 <= len(layout) <= 400:
        raise ValueError('layout debe contener entre 1 y 400 claves.')
    if not 1 <= len(images) <= 150:
        raise ValueError('Envía entre 1 y 150 imágenes.')
    if any(not isinstance(key, str) or key not in images for key in layout):
        raise ValueError('Cada clave de layout debe existir en keys.')
    prepared = {key: _prepare_image(raw) for key, raw in images.items()}
    pages = []
    try:
        for offset in range(0, len(layout), 9):
            with _page(prepared, layout[offset:offset + 9], paper, cut_marks) as page:
                jpeg = BytesIO()
                page.save(jpeg, 'JPEG', quality=88)
            jpeg.seek(0)
            # Keep pages compressed and lazy until Pillow writes each PDF page.
            pages.append(Image.open(jpeg))
        buffer = BytesIO()
        pages[0].save(buffer, 'PDF', save_all=True, append_images=pages[1:], resolution=300.0, quality=88)
        result = buffer.getvalue()
        logger.info('PDF: %s cartas, %s páginas, %.2f s, %s bytes', len(layout), len(pages), time.monotonic() - started, len(result))
        return result
    finally:
        for page in pages:
            page.close()
