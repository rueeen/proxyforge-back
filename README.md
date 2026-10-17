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
| `ANTHROPIC_CARD_MAX_TOKENS` | Presupuesto de salida del prompt por carta; predeterminado `4000`. Si Claude corta la respuesta por largo, reintenta automáticamente una vez con el doble. |
| `ANTHROPIC_MODEL` | Modelo de Claude; predeterminado `claude-sonnet-4-6`. |
| `STALE_JOB_MINUTES` | Expira jobs pending/running sin actualización; predeterminado `10` minutos. |
| `ANTHROPIC_API_KEY` | Generación del prompt visual con Claude. |
| `GEMINI_API_KEY` | Imágenes con el proveedor predeterminado Gemini. |
| `OPENAI_API_KEY` | Imágenes si `IMAGE_PROVIDER=openai`. |
| `IMAGE_PROVIDER` | Principal: `gemini` (predeterminado), `openai` o `grok`. |
| `IMAGE_FALLBACK_PROVIDER` | Respaldo opcional: `gemini`, `openai` o `grok`; vacío o igual al principal lo desactiva. |
| `XAI_API_KEY` | Clave de xAI para Grok Imagine. |
| `XAI_IMAGE_MODEL` | Modelo de Grok; predeterminado `grok-imagine-image-2.0`. |
| `XAI_IMAGE_RESOLUTION` | Resolución de Grok: `1k` (predeterminado) o `2k`. |
| `XAI_IMAGE_QUALITY` | Calidad de Grok: `medium` (predeterminado), `low` o `auto`; solo se envía a modelos `grok-imagine-image-2*`. |
| `MAX_CARDS_PER_JOB` | Máximo de cartas únicas pendientes por petición; predeterminado `5`. |
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

## Generación

El tema se aplica literalmente, incluidas franquicias, personajes y estilos de
artista. Claude produce un `full_prompt` fiel al tema y un `fallback_prompt` de
la misma escena que sustituye nombres propios por descripciones visuales. Si el
proveedor rechaza el prompt original o devuelve una imagen inválida, se intenta
el fallback una sola vez con el principal. Si también falla y hay un respaldo
configurado, se intenta ese proveedor únicamente con el fallback:
principal completo → principal fallback → respaldo fallback. Se detiene al obtener
una imagen válida y se registran el prompt y el proveedor efectivos. La ilustración
no incluye texto ni marcos; las características jugables de la carta se conservan.

Por ejemplo, `IMAGE_PROVIDER=grok` con `IMAGE_FALLBACK_PROVIDER=gemini` usa Gemini
solo después de que fallen los dos intentos de Grok. Se necesitan las claves de
ambos proveedores para esa configuración.

Ajusta `COST_PER_IMAGE_USD` al proveedor principal. Como referencia, la documentación
de xAI indica aproximadamente USD 0,02 por imagen en `grok-imagine-image` y
USD 0,04–0,08 en `grok-imagine-image-2.0`, según resolución. El estimado no incluye
posibles costes adicionales de reintentos o del respaldo.

Cambiar `IMAGE_PROVIDER` cambia las `cache_key`: las imágenes del proveedor anterior
se conservan, pero no se muestran hasta volver a seleccionarlo. Cuando se usa un
respaldo, la clave de caché sigue usando el principal y el registro guarda el
proveedor que efectivamente produjo la imagen.

Al regenerar, el arte anterior se conserva si falla el renderizado. Su archivo
solo se elimina después de guardar y confirmar el reemplazo.

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

El detalle incluye `active_job` (job pending/running o null), `card_count` (suma de cantidades)
y `has_theme_plan` (true si todas las cartas resueltas tienen asignación).
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
  "theme_assignment": {"element": "Espina fulminante", "category": "Fenómeno", "visual_brief": "Una espina descarga energía sobre una criatura entre flores", "edited": false},
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
una imagen para oracle_id, tema, proveedor y asignación actuales.
`theme_assignment` es null si no hay asignación o si la entrada no está resuelta. Las caras incluyen name,
mana_cost, type_line, oracle_text, flavor_text, power, toughness y loyalty; los campos
faltantes son cadenas vacías. Las cartas de una cara tienen `card_faces: []`.

La regeneración devuelve el DeckCard completo con status 200; un job activo devuelve
409 con `{"detail":"Hay una generación en curso para este mazo."}` y un fallo del
proveedor devuelve 502 con `{"detail":"<mensaje>"}`. No acepta un tema alternativo.

La estimación conserva las claves originales y agrega el coste y tamaño de la siguiente tanda:

```json
{"unique_cards": 67, "cached": 10, "to_generate": 57, "estimated_cost_usd": 2.28, "batch_limit": 5, "batch_size": 5, "batch_cost_usd": 0.20}
```

El prompt v3 combina el elemento asignado con la mecánica y la ilustración original; si Claude no puede descargarla, reintenta una vez sin ella.
Los cachés v1/v2 quedan invalidados; sus metadatos temáticos ausentes se serializan como
cadenas vacías y frame_style null. Las imágenes nuevas conservan su formato real
PNG, JPEG o WEBP y usan nombres únicos para evitar el caché del navegador.
Los tests mockean las APIs externas y no requieren claves.


## Tandas y plan temático

Cada petición genera como máximo `MAX_CARDS_PER_JOB` cartas únicas pendientes
(default 5), en orden commander → main → sideboard y después por orden de importación.
Las cartas ya cacheadas se omiten. El job persiste los oracle_id seleccionados y
procesa solo esa tanda. Sin pendientes responde 400. Puedes pedir menos cartas:

```bash
curl -X POST http://127.0.0.1:8000/api/decks/1/generate/ \
  -H 'Content-Type: application/json' -d '{"limit":2}'
```

`limit` debe ser un entero positivo; un valor mayor se recorta al máximo configurado.
El estimado incluye `batch_size = min(batch_limit, to_generate)` y `batch_cost_usd`.
Los costes mostrados corresponden a imágenes; no incluyen el plan ni prompts de Claude.

El plan procesa lotes de hasta 30 cartas con Claude para asignar un elemento concreto y distinto a cada
carta única según su función, incluyendo personajes y referencias canónicas si el tema
es una franquicia, o sujetos variados si es una estética. Si faltan asignaciones, hace
un único reintento solo con las cartas faltantes. Puede prepararse antes de generar:

```bash
curl -X POST http://127.0.0.1:8000/api/decks/1/theme-plan/ \
  -H 'Content-Type: application/json' -d '{"overwrite_edited":false}'
curl -X PATCH http://127.0.0.1:8000/api/cards/12/theme-assignment/ \
  -H 'Content-Type: application/json' \
  -d '{"element":"Storm Eagle","category":"Jefe Maverick","visual_brief":"Storm Eagle en picada sobre la cubierta de una aeronave, soltando ráfagas de viento."}'
```

El POST es síncrono y devuelve el detalle del mazo (502 si Claude falla). Conserva las
asignaciones editadas salvo `overwrite_edited:true`. El PATCH requiere los tres campos;
`category` admite cadena vacía, y marca `edited:true`. Devuelve el DeckCard completo;
una carta sin resolver responde 400. Ambos rechazan cambios durante un job activo (409).

El job prepara el plan si ninguna carta seleccionada tiene asignación; si falla,
termina en failed. La regeneración usa la asignación existente sin replanificar.
Modificar `element` o `visual_brief` invalida su imagen: vuelve a estar pendiente.
La versión v3 invalida imágenes de versiones anteriores. El plan se consulta en
`theme_assignment` de cada entrada y `has_theme_plan` del detalle.

## Formatos de importación

Se aceptan listas de Card Kingdom, Moxfield, Archidekt, MTGGoldfish y EDHREC en texto
o CSV. En texto: `4 Lightning Bolt`, `4x Lightning Bolt`, `4 x Lightning Bolt`,
tabuladores y nombres sin cantidad (default 1). Se reconocen `(C21) 263`, `[C21]`,
marcadores `*F*`/`*E*`/`*Foil*`, etiquetas finales `[Ramp]` y `#tags`, y `SB: 2 Negate`.
Las cabeceras main/deck/mazo/mainboard/maindeck, sideboard/banquillo y
commander/commanders/comandante admiten mayúsculas, `:` y conteos `(15)`.
Companion, Maybeboard y Considering se ignoran hasta la siguiente cabecera.
También se omiten líneas vacías, comentarios `//` o `#`, `About` y `Name ...`.

CSV reconoce nombre (`name`, `card name`, `card`, `nombre`), cantidad (`quantity`,
`qty`, `count`, `cantidad`, default 1), set (`set code`, `set`, `edition code`, solo
códigos alfanuméricos de 3–5 caracteres) y número (`collector number`, `number`,
`collector #`). Las cabeceras no distinguen mayúsculas y se ignoran filas sin nombre.

```csv
Quantity,Card Name,Set Code,Collector Number
1,Sol Ring,C21,263
4,Lightning Bolt,,
```

## Simbología de maná

```bash
curl http://127.0.0.1:8000/api/symbology/
```

Devuelve una lista `[{"symbol":"{R}","svg_uri":"https://svgs.scryfall.io/card-symbols/R.svg","english":"one red mana"}]`.
Los datos de Scryfall se cachean 7 días en `media/cache/symbology.json`. Si el servicio
falla, se usa el caché vencido; sin caché responde 502. El frontend puede usar estos
SVG para componer la capa de texto; debe tener acceso a `svgs.scryfall.io`.


## Entrevista temática opcional

Flujo recomendado: **importar → preguntas → respuestas → plan temático → tandas**.
Puedes omitir la entrevista y seguir planificando y generando como antes.
Claude prepara entre 0 y 5 preguntas a partir del comandante, identidad de color,
tipos y cartas destacadas del mazo real; las preguntas no son una plantilla fija.
Con un tema suficientemente preciso puede devolver cero preguntas.

Generar preguntas (reemplaza las anteriores, vacía las respuestas y reinicia su fecha):

```bash
curl -X POST http://127.0.0.1:8000/api/decks/1/theme-questions/ \
  -H 'Content-Type: application/json' -d '{}'
```

Cada pregunta tiene `id` (`q1`, `q2`, …), `question`, `help`, `multiple`,
`allow_other` y de 2 a 4 opciones con `id` (`a`–`d`), `label` y `detail`.
Lee esos ids del detalle antes de enviar las respuestas:

```bash
curl -X PUT http://127.0.0.1:8000/api/decks/1/theme-answers/ \
  -H 'Content-Type: application/json' \
  -d '{"answers":{"q1":{"option_ids":["a"],"other":""},"q3":{"option_ids":[],"other":"Estilo de las cinemáticas de Warcraft III"}}}'
curl -X POST http://127.0.0.1:8000/api/decks/1/theme-plan/ \
  -H 'Content-Type: application/json' -d '{"overwrite_edited":false}'
curl -X POST http://127.0.0.1:8000/api/decks/1/generate/ \
  -H 'Content-Type: application/json' -d '{"limit":2}'
```

El PUT reemplaza todas las respuestas; se pueden omitir preguntas o enviar
`{"answers":{}}` para vaciarlas. Los ids de pregunta y opción deben existir.
Cada respuesta necesita al menos una opción o texto `other` no vacío (máximo
200 caracteres y solo si `allow_other` es true). `multiple:false` admite como
máximo una opción. Un error de validación devuelve 400 con `{"answers":["mensaje"]}`.
Ambos endpoints devuelven el detalle del mazo y responden 409 ante un job activo;
la generación de preguntas responde 502 con `detail` si Claude falla.

Todas las respuestas de detalle incluyen:

```json
{
  "theme_questions": [
    {"id":"q1","question":"¿Quién representa al comandante?","help":"Sacrificio negro-rojo","multiple":false,"allow_other":true,"options":[{"id":"a","label":"Arthas","detail":"No-muertos"},{"id":"b","label":"Gul’dan","detail":"Magia vil"}]}
  ],
  "theme_answers": {"q1":{"option_ids":["a"],"other":""}},
  "theme_brief": [{"question":"¿Quién representa al comandante?","answer":"Arthas"}],
  "theme_plan_stale": false
}
```

El brief une los labels elegidos con coma y agrega `Otra: …` para el texto libre.
El planner y los prompts de cada carta lo tratan como decisiones obligatorias,
incluidos protagonista, facción, época, tierras y estilo visual. Se conservan las
asignaciones editadas al rehacer el plan salvo `overwrite_edited:true`.

Al cambiar las respuestas, las imágenes del brief anterior dejan de contar como
cacheadas. `theme_plan_stale:true` indica que hay un plan completo y las respuestas
son posteriores a la asignación no editada más reciente: vuelve a generar el plan
antes de la siguiente tanda. Si no hay asignaciones no editadas, el indicador es
false. El prompt sigue en v3: con brief vacío las claves de caché permanecen iguales
a las de los mazos que no usan entrevista.


## Respuestas de Claude y límites de salida

Los prompts por carta usan `ANTHROPIC_CARD_MAX_TOKENS=4000` y piden campos breves;
`full_prompt` y `fallback_prompt` se escriben en inglés, mientras que nombre y flavor
siguen en español. El planner procesa lotes de 30 cartas con 8000 tokens por lote,
compartiendo los elementos ya usados. La entrevista dispone de 3000 tokens.

Si Claude informa `stop_reason=max_tokens`, se reintenta automáticamente una vez con
el doble de tokens (16000 en el planner), el mensaje original y una instrucción de
concisión. La respuesta truncada no se agrega al historial. El JSON inválido sin
truncamiento usa el reintento de corrección habitual. Si ambos intentos fallan, la
API y el job muestran un mensaje corto; las respuestas fallidas se registran solo en
logs, hasta 2000 caracteres cada una. Los errores de generación se limitan a 500
caracteres. El contrato de la API, PROMPT_VERSION v3 y las claves de caché se mantienen.
