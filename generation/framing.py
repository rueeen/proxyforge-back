import base64
import logging
import math
import os
from io import BytesIO

from PIL import Image
from django.db import transaction

from .claude_client import call_claude_json
from .models import GeneratedImage

logger = logging.getLogger(__name__)
SYSTEM_PROMPT = '''Localiza el rostro (o la cabeza, si no se ve el rostro) del sujeto principal de la ilustración. Responde solo JSON: {"face_center_x": número entre 0 y 1 medido desde el borde izquierdo, "face_center_y": número entre 0 y 1 medido desde el borde superior, "face_height": número entre 0 y 1, "found": true|false}. Si no hay un sujeto con cabeza (paisaje, objeto, hechizo abstracto), responde found=false.'''


def _validate_face(data):
    if type(data.get('found')) is not bool:
        raise ValueError('found debe ser booleano')
    if data['found']:
        for field in ('face_center_x', 'face_center_y', 'face_height'):
            value = data.get(field)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError('Posición o tamaño de rostro inválidos')
    return data


def detect_face_center(image_bytes):
    try:
        with Image.open(BytesIO(image_bytes)) as image:
            rgba = image.convert('RGBA')
            resized = Image.new('RGB', rgba.size, 'white')
            resized.paste(rgba, mask=rgba.getchannel('A'))
            resized.thumbnail((768, 768), Image.Resampling.LANCZOS)
            output = BytesIO()
            resized.save(output, format='JPEG')
        data = call_claude_json(
            system=SYSTEM_PROMPT, max_tokens=200,
            model=os.getenv('ANTHROPIC_VERIFY_MODEL') or os.getenv('ANTHROPIC_MODEL', 'claude-sonnet-4-6'),
            messages=[{'role': 'user', 'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg',
                                           'data': base64.b64encode(output.getvalue()).decode()}},
                {'type': 'text', 'text': SYSTEM_PROMPT},
            ]}], validate=_validate_face,
        )
        return (float(data['face_center_x']), float(data['face_center_y'])) if data['found'] else None
    except Exception as exc:
        logger.warning('No se pudo detectar el rostro: %s', str(exc)[:200])
        return None


def auto_framing(face_x, face_y, img_w, img_h, layout):
    if layout != 'borderless':
        return 0, 0, 100
    card_ratio, img_ratio = 63 / 88, img_w / img_h
    if img_ratio > card_ratio:
        h, w = 100, img_ratio / card_ratio * 100
    else:
        w, h = 100, card_ratio / img_ratio * 100
    offset_y = 32 - 50 + h / 2 - face_y * h
    if offset_y < 0 and 50 - h / 2 + face_y * h >= 26:
        offset_y = 0
    if h > 100 and 50 + offset_y - h / 2 > 0:
        # Prioritize no top gap when a very high face cannot reach the target band.
        offset_y = math.floor((h / 2 - 50) * 10) / 10
    lateral_limit = (w - 100) / 2
    offset_x = max(-lateral_limit, min(lateral_limit, (0.5 - face_x) * w))
    return round(max(-60, min(60, offset_x)), 1), round(max(-60, min(60, offset_y)), 1), 100


def apply_auto_framing(generated, layout, image_bytes=None):
    """Update only framing; technical failures must never discard the saved artwork."""
    face = None
    offset_x, offset_y, zoom = 0, 0, 100
    width, height = generated.width, generated.height
    try:
        if image_bytes is None and (layout == 'borderless' or not width or not height):
            with generated.image.open('rb') as image:
                image_bytes = image.read()
        if not width or not height:
            with Image.open(BytesIO(image_bytes)) as image:
                width, height = image.size
        if layout == 'borderless':
            face = detect_face_center(image_bytes)
            if face is not None:
                offset_x, offset_y, zoom = auto_framing(*face, width, height, layout)
    except Exception as exc:
        logger.warning('No se pudo calcular el encuadre: %s', str(exc)[:200])
        face = None
        offset_x, offset_y, zoom = 0, 0, 100
    values = dict(face_center_x=face[0] if face else None, face_center_y=face[1] if face else None,
                  framing_offset_x=offset_x, framing_offset_y=offset_y, framing_zoom=zoom,
                  width=width, height=height, framing_source='auto' if face is not None else 'default')
    try:
        # Use a savepoint when regeneration already holds a transaction.
        with transaction.atomic():
            GeneratedImage.objects.filter(pk=generated.pk).update(**values)
        for field, value in values.items():
            setattr(generated, field, value)
    except Exception as exc:
        logger.warning('No se pudo guardar el encuadre: %s', str(exc)[:200])
        return 'no_face'
    if face is None:
        return 'no_face'
    return 'adjusted' if (offset_x, offset_y, zoom) != (0, 0, 100) else 'unchanged'
