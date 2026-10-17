import hashlib
from io import BytesIO

from PIL import Image

MAX_REFERENCE_BYTES = 10 * 1024 * 1024


def normalize_reference(raw_bytes):
    if len(raw_bytes) > MAX_REFERENCE_BYTES:
        raise ValueError('La imagen supera 10 MB')
    try:
        with Image.open(BytesIO(raw_bytes)) as image:
            if image.format not in ('JPEG', 'PNG', 'WEBP'):
                raise ValueError('Formato no compatible')
            image.verify()
        with Image.open(BytesIO(raw_bytes)) as image:
            rgba = image.convert('RGBA')
            rgb = Image.new('RGB', rgba.size, 'white')
            rgb.paste(rgba, mask=rgba.getchannel('A'))
            rgb.thumbnail((1536, 1536), Image.Resampling.LANCZOS)
            output = BytesIO()
            rgb.save(output, format='JPEG', quality=90)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError('Imagen de referencia inválida') from exc
    normalized = output.getvalue()
    return normalized, hashlib.sha256(normalized).hexdigest()


def reference_hashes(assignment):
    return [reference.sha256 for reference in assignment.references.all()] if assignment is not None else []
