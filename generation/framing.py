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
SYSTEM_PROMPT = '''Localiza el rostro (o la cabeza, si no se ve el rostro) del sujeto principal de la ilustración. Responde solo JSON: {"face_center_y": número entre 0 y 1 medido desde el borde superior, "face_height": número entre 0 y 1, "found": true|false}. Si no hay un sujeto con cabeza (paisaje, objeto, hechizo abstracto), responde found=false.'''


def _validate_face(data):
    if type(data.get('found')) is not bool:
        raise ValueError('found debe ser booleano')
    if data['found']:
        for field in ('face_center_y', 'face_height'):
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
        return float(data['face_center_y']) if data['found'] else None
    except Exception as exc:
        logger.warning('No se pudo detectar el rostro: %s', str(exc)[:200])
        return None


def auto_framing(face_center_y, image_aspect, layout):
    if layout != 'borderless':
        return 0, 100
    # In the specified vertical cover composition, image height equals card height.
    y_actual = face_center_y * 100
    if y_actual >= 26:
        return 0, 100
    offset = min(35, 32 - y_actual)
    zoom = 90 if offset == 35 and y_actual + offset < 26 else 100
    return round(offset, 1), round(zoom, 1)


def apply_auto_framing(generated, layout, image_bytes=None):
    """Update only framing; technical failures must never discard the saved artwork."""
    face = None
    offset, zoom = 0, 100
    if layout == 'borderless':
        try:
            if image_bytes is None:
                with generated.image.open('rb') as image:
                    image_bytes = image.read()
            face = detect_face_center(image_bytes)
            if face is not None:
                with Image.open(BytesIO(image_bytes)) as image:
                    aspect = image.width / image.height
                offset, zoom = auto_framing(face, aspect, layout)
        except Exception as exc:
            logger.warning('No se pudo calcular el encuadre: %s', str(exc)[:200])
            face = None
            offset, zoom = 0, 100
    values = dict(face_center_y=face, framing_offset_y=offset, framing_zoom=zoom,
                  framing_source='auto' if face is not None else 'default')
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
    return 'adjusted' if (offset, zoom) != (0, 100) else 'unchanged'
