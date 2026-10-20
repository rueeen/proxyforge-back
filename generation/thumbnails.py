from pathlib import Path
from uuid import uuid4

from PIL import Image
from django.conf import settings


def get_thumbnail(generated, width=480) -> Path:
    source = Path(generated.image.path)
    target = Path(settings.MEDIA_ROOT) / 'cache' / 'thumbs' / f'{generated.pk}-{width}.webp'
    if target.exists() and target.stat().st_mtime_ns >= source.stat().st_mtime_ns:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f'{target.stem}-{uuid4().hex}.tmp')
    try:
        with Image.open(source) as image:
            height = max(1, round(image.height * width / image.width))
            image = image.convert('RGB').resize((width, height), Image.Resampling.LANCZOS)
            image.save(temporary, 'WEBP', quality=82)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def delete_thumbnails(generated):
    directory = Path(settings.MEDIA_ROOT) / 'cache' / 'thumbs'
    for path in directory.glob(f'{generated.pk}-*.webp'):
        path.unlink(missing_ok=True)
