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
del mazo), usando el tema del mazo o uno nuevo:

```bash
curl -X POST http://127.0.0.1:8000/api/cards/1/regenerate/ \
  -H 'Content-Type: application/json' -d '{}'
curl -X POST http://127.0.0.1:8000/api/cards/1/regenerate/ \
  -H 'Content-Type: application/json' -d '{"theme":"cyberpunk neón"}'
```

## Tests

```bash
python manage.py test
```
