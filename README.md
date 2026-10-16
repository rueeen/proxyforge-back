# ProxyForge Backend

Backend local para importar una lista de Magic: The Gathering, resolver sus cartas
con Scryfall y crear ilustraciones alternativas ajustadas a un tema. No incluye
autenticación ni frontend.

## Instalación

Requiere Python 3.11 o posterior.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py runserver
```

La API estará disponible en `http://127.0.0.1:8000/api/`; durante desarrollo,
el frontend permitido por CORS es `http://localhost:5173`.

## Variables de entorno

| Variable | Uso |
| --- | --- |
| `SECRET_KEY` | Clave de Django; cámbiala fuera del desarrollo local. |
| `DEBUG` | Activa depuración y servicio local de archivos media. |
| `ANTHROPIC_MODEL` | Modelo de Claude; predeterminado `claude-sonnet-4-6`. |
| `STALE_JOB_MINUTES` | Expira jobs pending/running sin actualización; predeterminado `10` minutos. |
| `ANTHROPIC_API_KEY` | Generación del prompt visual con Claude. |
| `GEMINI_API_KEY` | Imágenes con el proveedor predeterminado Gemini. |
| `OPENAI_API_KEY` | Imágenes si `IMAGE_PROVIDER=openai`. |
| `IMAGE_PROVIDER` | `gemini` (predeterminado) u `openai`. |
| `COST_PER_IMAGE_USD` | Coste usado por la estimación; predeterminado `0.04`. |

SQLite se configura automáticamente. Los jobs usan un thread del proceso web, por
lo que están pensados para el uso local con un único proceso; su progreso sí queda
persistido en la base de datos.

## Ejemplos de API

Crear un mazo (la llamada resuelve las cartas en Scryfall):

```bash
curl -X POST http://127.0.0.1:8000/api/decks/ \
  -H 'Content-Type: application/json' \
  -d '{"name":"Burn","raw_list":"4 Lightning Bolt\n1 Sol Ring (c21) 263","theme":"acuarela botánica"}'
```

Listar mazos:

```bash
curl http://127.0.0.1:8000/api/decks/
```

Obtener o eliminar el mazo 1:

```bash
curl http://127.0.0.1:8000/api/decks/1/
curl -X DELETE http://127.0.0.1:8000/api/decks/1/
```

Estimar y lanzar una generación:

```bash
curl http://127.0.0.1:8000/api/decks/1/estimate/
curl -X POST http://127.0.0.1:8000/api/decks/1/generate/
```

Consultar por polling el job 1:

```bash
curl http://127.0.0.1:8000/api/jobs/1/
```

Regenerar de forma síncrona una entrada `DeckCard` (el `id` aparece en el detalle
del mazo), siempre usando `deck.theme`:

```bash
curl -X POST http://127.0.0.1:8000/api/cards/1/regenerate/ \
  -H 'Content-Type: application/json' -d '{}'
```

## Tests

```bash
python manage.py test
```

El detalle incluye `active_job` (job pending/running o null) y `card_count` (suma de cantidades).
Tanto creación como detalle y regeneración usan esta forma de `DeckCard`:

```json
{
  "id": 12,
  "raw_name": "Lightning Bolt",
  "quantity": 4,
  "section": "main",
  "resolution_error": "",
  "card": {
    "id": 7, "scryfall_id": "00000000-0000-0000-0000-000000000007",
    "oracle_id": "00000000-0000-0000-0000-000000000008",
    "name": "Lightning Bolt", "mana_cost": "{R}", "cmc": 1.0,
    "type_line": "Instant", "oracle_text": "Lightning Bolt deals 3 damage to any target.",
    "flavor_text": "", "colors": ["R"], "color_identity": ["R"],
    "rarity": "common", "set_code": "lea", "collector_number": "161",
    "original_art_url": "https://cards.scryfall.io/art_crop/example.jpg",
    "original_artist": "Christopher Rush", "layout": "normal",
    "power": "", "toughness": "", "loyalty": "", "keywords": [], "card_faces": []
  },
  "generated_image": {
    "id": 3,
    "image_url": "http://127.0.0.1:8000/media/generated/0123456789abcdef-ab123456.jpg",
    "theme": "acuarela botánica", "concept_source": "Descarga súbita de energía",
    "themed_name": "Espina fulminante", "themed_flavor": "El jardín también sabe defenderse.",
    "frame_style": {"border": "#994433", "background": "#EEDDCC", "text_box": "#FFF8EE", "accent": "#CC3322", "texture": "papel acuarela"},
    "created_at": "2026-10-16T12:00:00Z"
  }
}
```

`card` es null si la entrada no se resolvió; `generated_image` es null si no existe
una imagen para oracle_id, tema y proveedor actuales. Las caras incluyen name,
mana_cost, type_line, oracle_text, flavor_text, power, toughness y loyalty; los campos
faltantes son cadenas vacías. Las cartas de una cara tienen `card_faces: []`.

La regeneración devuelve el DeckCard completo con status 200; un job activo devuelve
409 con `{"detail":"Hay una generación en curso para este mazo."}` y un fallo del
proveedor devuelve 502 con `{"detail":"<mensaje>"}`. No acepta un tema alternativo.

La estimación conserva exactamente estas claves:

```json
{"unique_cards": 30, "cached": 10, "to_generate": 20, "estimated_cost_usd": 0.8}
```

El prompt v2 usa la ilustración original como referencia conceptual, sin copiar
composición ni estilo; si Claude no puede descargarla, reintenta una vez sin ella.
El caché v1 queda invalidado; sus metadatos temáticos ausentes se serializan como
cadenas vacías y frame_style null. Las imágenes nuevas conservan su formato real
PNG, JPEG o WEBP y usan nombres únicos para evitar el caché del navegador.
Los tests mockean las APIs externas y no requieren claves.
