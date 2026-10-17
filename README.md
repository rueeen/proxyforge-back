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
| `CARD_TEXT_LANGUAGE` | Idioma de nombre y flavor temáticos: `en` (default) o `es`; cualquier otro valor usa `en`. |
| `ANTHROPIC_MODEL` | Modelo de Claude; predeterminado `claude-sonnet-4-6`. |
| `STALE_JOB_MINUTES` | Expira jobs pending/running sin actualización; predeterminado `10` minutos. |
| `ANTHROPIC_API_KEY` | Generación del prompt visual con Claude. |
| `GEMINI_API_KEY` | Imágenes con el proveedor predeterminado Gemini. |
| `OPENAI_API_KEY` | Imágenes si `IMAGE_PROVIDER=openai`. |
| `IMAGE_PROVIDER` | Principal: `gemini` (predeterminado), `openai` o `grok`. |
| `IMAGE_FALLBACK_PROVIDER` | Respaldo opcional: `gemini`, `openai` o `grok`; vacío o igual al principal lo desactiva. |
| `XAI_API_KEY` | Clave de xAI para Grok Imagine. |
| `AUTO_FRAMING` | Auto-encuadre de imágenes nuevas sin borde; predeterminado `true`. Usa visión con `ANTHROPIC_VERIFY_MODEL` o `ANTHROPIC_MODEL`. |
| `AUTO_REFERENCE_VERIFY` | Verifica las referencias automáticas con Claude; predeterminado `true`. |
| `ANTHROPIC_VERIFY_MODEL` | Modelo opcional de verificación; vacío usa `ANTHROPIC_MODEL`. |
| `AUTO_REFERENCES_IN_JOBS` | Busca referencias pendientes antes de generar cada carta; predeterminado `true`. |
| `XAI_EDIT_MODEL` | Modelo para edición con referencias; predeterminado `grok-imagine-image-2.0`. |
| `XAI_IMAGE_MODEL` | Modelo de Grok; predeterminado `grok-imagine-image-2.0`. |
| `XAI_IMAGE_RESOLUTION` | Resolución de Grok: `1k` (predeterminado) o `2k`. |
| `XAI_IMAGE_QUALITY` | Calidad de Grok: `medium` (predeterminado), `low` o `auto`; solo se envía a modelos `grok-imagine-image-2*`. |
| `MAX_CARDS_PER_JOB` | Máximo de cartas únicas pendientes por petición; predeterminado `5`. |
| `AUTO_REFERENCE_COUNT` | Referencias automáticas por carta (1–3); predeterminado `2`. |
| `COST_PER_REFERENCE_USD` | Costo por referencia usada en cada imagen generada; predeterminado `0.01`. |
| `WIKI_DISCOVERY_WEB_SEARCH` | Permite buscar wikis en la web si las propuestas no tienen coincidencias; predeterminado `true`. |
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
  "theme_assignment": {"element": "Espina fulminante", "category": "Fenómeno", "visual_brief": "Una espina descarga energía sobre una criatura entre flores", "edited": false, "references": []},
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
una imagen para oracle_id, tema, proveedor, asignación y formato actuales.
`theme_assignment` es null si no hay asignación o si la entrada no está resuelta. Las caras incluyen name,
mana_cost, type_line, oracle_text, flavor_text, power, toughness y loyalty; los campos
faltantes son cadenas vacías. Las cartas de una cara tienen `card_faces: []`.

La regeneración devuelve el DeckCard completo con status 200; un job activo devuelve
409 con `{"detail":"Hay una generación en curso para este mazo."}` y un fallo del
proveedor devuelve 502 con `{"detail":"<mensaje>"}`. No acepta un tema alternativo.

La estimación conserva las claves originales y agrega el coste y tamaño de la siguiente tanda:

```json
{"unique_cards": 67, "cached": 10, "to_generate": 57, "estimated_cost_usd": 2.28, "batch_limit": 5, "batch_size": 5, "batch_cost_usd": 0.20, "reference_cost_usd": 0.0}
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
`full_prompt` y `fallback_prompt` se escriben en inglés; nombre y flavor usan
`CARD_TEXT_LANGUAGE` (inglés por defecto). El planner procesa lotes de 30 cartas con 8000 tokens por lote,
compartiendo los elementos ya usados. La entrevista dispone de 3000 tokens.

Si Claude informa `stop_reason=max_tokens`, se reintenta automáticamente una vez con
el doble de tokens (16000 en el planner), el mensaje original y una instrucción de
concisión. La respuesta truncada no se agrega al historial. El JSON inválido sin
truncamiento usa el reintento de corrección habitual. Si ambos intentos fallan, la
API y el job muestran un mensaje corto; las respuestas fallidas se registran solo en
logs, hasta 2000 caracteres cada una. Los errores de generación se limitan a 500
caracteres. El contrato de la API, PROMPT_VERSION v3 y las claves de caché se mantienen.


## Formato de carta: sin borde o clásico

Los mazos nuevos usan `card_layout:"borderless"` por defecto: ilustración vertical
3:4 a sangre completa, con el rostro entre el 20% y el 45% del alto y nunca en el 15% superior. El 10% superior
y el 40% inferior dejan espacio visual para las placas translúcidas del frontend;
la imagen generada sigue sin incluir texto ni marcos. Los prompts describen los
rasgos anatómicos y variantes de época de cada especie para evitar confusiones
como troll/orco; el planner pide comprobar facción, tribu y raza canónicas.

Los mazos existentes conservan `card_layout:"classic"` mediante la migración y
mantienen su arte horizontal 16:9. El campo aparece en listado y detalle. Al crear
un mazo se puede elegir explícitamente:

```bash
curl -X POST http://127.0.0.1:8000/api/decks/ \
  -H 'Content-Type: application/json' \
  -d '{"name":"Warcraft","raw_list":"1 Sol Ring","theme":"Warcraft","card_layout":"borderless"}'
```

Cambiar el formato de un mazo:

```bash
curl -X PATCH http://127.0.0.1:8000/api/decks/1/ \
  -H 'Content-Type: application/json' -d '{"card_layout":"borderless"}'
```

El PATCH acepta `card_layout` (`borderless` o `classic`) y `auto_reference_count` (entero de 1 a 3), y devuelve el detalle.
Otros campos responden 400;
un formato inválido devuelve 400 y un job activo devuelve 409.

Cambiar a sin borde deja pendientes las cartas que no tengan arte vertical. Genera
las tandas para ese formato; volver a clásico recupera las imágenes horizontales
cacheadas. Se conservan las claves de caché clásicas y PROMPT_VERSION sigue en v3.

Grok y Gemini reciben 3:4 o 16:9; OpenAI usa respectivamente 1024x1536 o 1536x1024,
sus tamaños vertical/horizontal disponibles. Todos los intentos, incluido el
proveedor de respaldo, usan el mismo formato. Con google-genai 1.38 se envía
imageConfig mediante HttpOptions.extra_body, sin actualizar dependencias.


## Referencias de identidad por carta

Una asignación temática puede tener hasta **3 imágenes de referencia**, para conservar
el parecido del personaje o elemento en una ilustración nueva. Primero genera el plan
o define la asignación de esa carta. Puedes cargar una imagen por archivo o por URL:

```bash
curl -X POST http://127.0.0.1:8000/api/cards/12/theme-assignment/references/ \
  -F 'file=@voljin.png'
curl -X POST http://127.0.0.1:8000/api/cards/12/theme-assignment/references/ \
  -H 'Content-Type: application/json' -d '{"url":"https://example.com/voljin.jpg"}'
curl -X DELETE http://127.0.0.1:8000/api/cards/12/theme-assignment/references/4/
```

El POST devuelve 201 y el DELETE 200, ambos con el DeckCard actualizado.
`theme_assignment.references` contiene objetos como:

```json
[{"id":4,"image_url":"http://127.0.0.1:8000/media/references/archivo.jpg","source_url":"https://example.com/voljin.jpg","origin":"manual"}]
```

La subida requiere una asignación y rechaza imágenes duplicadas, más de tres
referencias, URLs que no devuelven imagen y entradas inválidas (400). Un job activo
bloquea las modificaciones (409); borrar una referencia de otra carta devuelve 404.
Al agregar referencias, la asignación queda `edited:true` para conservarla al rehacer
el plan. El borrado de un registro también elimina su archivo.

Se admiten JPEG, PNG y WEBP de hasta 10 MB. Se verifican con Pillow, se aplanan sobre
blanco y se guardan como JPEG RGB, calidad 90 y lado mayor de hasta 1536 px. El hash
se calcula sobre esos bytes normalizados. Agregar o quitar referencias cambia el
caché y deja pendiente la carta si no existe una imagen para esa combinación; sin
referencias las claves anteriores se conservan.

Con referencias se recomienda **`IMAGE_PROVIDER=grok`**. Grok utiliza el endpoint
JSON `POST https://api.x.ai/v1/images/edits` con `XAI_EDIT_MODEL` (default
`grok-imagine-image-2.0`), referencias como data URI y el aspecto del mazo. Admite
respuestas base64 o URL, y reintenta con `image` para una referencia o sin
`response_format` si la API rechaza esos parámetros. Sin referencias sigue usando
el SDK de OpenAI para generar imágenes como antes.

Gemini envía las referencias como partes JPEG junto al prompt. **OpenAI las ignora**,
lo registra en logs y genera solo desde texto. El runner pasa las mismas referencias,
en orden de posición, al intento completo, al fallback y al proveedor de respaldo.
El prompt pide conservar identidad, rostro, especie y equipo característico, creando
una composición nueva en el formato clásico o sin borde seleccionado.


## Referencias automáticas desde wikis

El plan temático marca con `needs_reference:true` los personajes, jefes, objetos o
lugares icónicos cuyo parecido visual importa, e indica `reference_wiki` (dominio)
y `reference_page` (título de página). Los conceptos, enemigos genéricos y temas
puramente estéticos no necesitan estas referencias.

Flujo completo: **tema → entrevista opcional → confirmar wiki → plan → referencias →
tandas**. Puedes buscar todas las referencias pendientes o limitarte a entradas
DeckCard del mazo:

```bash
curl -X POST http://127.0.0.1:8000/api/decks/1/auto-references/ \
  -H 'Content-Type: application/json' -d '{"force":false,"deck_card_ids":[12,13]}'
curl -X POST http://127.0.0.1:8000/api/cards/12/theme-assignment/auto-reference/ \
  -H 'Content-Type: application/json' -d '{"force":true}'
```

El endpoint del mazo devuelve su detalle más
`auto_reference_summary: {"found":0,"not_found":0,"rejected":0,"error":0,"blocked":0,"skipped":0}`.
Omitir `deck_card_ids` procesa todas las asignaciones; una lista vacía no procesa
ninguna. El endpoint por carta devuelve el DeckCard y usa `force:true` por defecto;
responde 400 si faltan asignación, wiki o página. Ambos son síncronos y responden
409 si hay un job activo. `force:true` reemplaza todas las referencias automáticas anteriores y sus archivos, y
reintenta aunque needs_reference sea false. Conserva las referencias manuales y las elegidas de la wiki.

Puedes corregir la wiki o página desde el PATCH de asignación:

```bash
curl -X PATCH http://127.0.0.1:8000/api/cards/12/theme-assignment/ \
  -H 'Content-Type: application/json' \
  -d '{"element":"Vol\u0027jin","category":"Troll","visual_brief":"Vol\u0027jin al mando de sus tropas","needs_reference":true,"reference_wiki":"warcraft.wiki.gg","reference_page":"Vol\u0027jin"}'
```

En JSON, `\u0027` representa el apóstrofo. El dominio debe estar en minúsculas, sin
esquema ni rutas. Cambiar wiki o página limpia el estado del intento y elimina
solo las referencias automáticas. Si el planner cambia el elemento, también limpia
el estado y las automáticas. Las referencias manuales se conservan; las asignaciones
editadas siguen protegidas salvo `overwrite_edited:true`.

Cada `theme_assignment` expone `needs_reference`, `reference_wiki`, `reference_page`,
`reference_wiki_alt`, `reference_page_status`, `auto_reference_status` y `auto_reference_message`. Los estados son `""` (sin intentar),
`found`, `not_found`, `rejected`, `error` y `blocked`. Cada referencia incluye `origin:"manual"`,
`origin:"wiki"` o `origin:"auto"`. El detalle del mazo agrega, sin consultas por carta:

```json
{"auto_reference_counts":{"needed":18,"with_reference":12,"pending":6}}
```

`needed` cuenta las asignaciones que necesitan referencia; `with_reference`, las que
ya tienen alguna; `pending`, las que no tienen referencias ni un intento previo.
Se guardan hasta `auto_reference_count` automáticas por asignación (dos por defecto),
limitadas por las plazas disponibles tras contar manuales y elegidas de la wiki. El límite total es tres. La carga automática no marca la asignación como editada.

El cliente detecta `/api.php` o `/w/api.php`, resuelve la página y lista sus imágenes,
excluyendo logos, iconos, mapas, SVG y GIF. Prioriza candidatas sin indicios de fan art
y con al menos 600 px en su lado mayor; intenta descargar hasta `target + 3` candidatas. Pausa al menos 0,3 segundos
entre peticiones al mismo dominio y valida tipo de imagen y límite de 10 MB.
Las imágenes pasan por la misma normalización JPEG que las referencias manuales.

`AUTO_REFERENCE_VERIFY=true` verifica todas las imágenes candidatas en una sola llamada a Claude y ordena las aceptadas,
con `ANTHROPIC_VERIFY_MODEL` o el modelo habitual si está vacío. Un rechazo visual
queda como `rejected` si todas se rechazan; un fallo técnico de Claude acepta las imágenes en el orden original.
Con `AUTO_REFERENCES_IN_JOBS=true`, cada carta intenta buscar su referencia pendiente
antes de generar y calcular su caché. Una búsqueda fallida no bloquea la ilustración.

Diagnóstico local de una wiki:

```bash
python manage.py find_reference "Vol'jin" --wiki warcraft.wiki.gg --verify
```

Imprime la ruta de API, el paso que encontró la imagen, su URL y dimensiones, y
opcionalmente la verificación de Claude. Guarda el JPEG normalizado en
`media/cache/find_reference/<slug>.jpg`. wiki.gg y otras wikis MediaWiki usan este flujo. Fandom permite consultas a la API,
pero bloquea las descargas automáticas de imágenes; en ese caso se usa la carga manual. Las imágenes
se descargan **solo para uso local como referencia**; `media/references/` está ignorada
y no se sube al repositorio, salvo su `.gitkeep` vacío.


## Encuadre manual y automático del arte

Las imágenes nuevas sin borde piden más aire sobre la cabeza, incluidos casco,
corona, cuernos o armas alzadas. Claude debe terminar los prompts de imagen en inglés
con una instrucción explícita de composición vertical, franja superior libre y 40%
inferior más tranquilo para superponer texto. PROMPT_VERSION sigue en v3 y las
imágenes ya cacheadas se conservan.

Cada `generated_image` del DeckCard incluye:

```json
{"framing_offset_y":22.0,"framing_zoom":100.0,"framing_source":"auto","face_center_y":0.1}
```

`framing_offset_y` desplaza la imagen verticalmente en porcentaje del alto de la
carta (positivo hacia abajo), entre −20 y +35. `framing_zoom` va de 70 a 160, con 100
como tamaño cover normal. `framing_source` es `default`, `auto` o `manual`.
`face_center_y` es la posición detectada en la imagen original, entre 0 y 1, o null.
El frontend aplica las transformaciones CSS y rellena los huecos con una copia
difuminada de la misma imagen; el backend conserva el archivo de arte original.

Ajustar una imagen actual, con ambos campos opcionales:

```bash
curl -X PATCH http://127.0.0.1:8000/api/cards/12/framing/ \
  -H 'Content-Type: application/json' -d '{"offset_y":12.5,"zoom":105}'
curl -X POST http://127.0.0.1:8000/api/cards/12/framing/auto/
curl -X POST http://127.0.0.1:8000/api/decks/1/framing/auto/
```

El PATCH recorta los números a sus límites, marca `manual` y devuelve el DeckCard.
Un valor no numérico devuelve 400; si la entrada no tiene arte actual, los endpoints
por carta devuelven 404 con `{"detail":"La carta no tiene arte generado."}`.
El POST por carta recalcula incluso un ajuste manual; sin rostro restaura `(0,100)`
y `default`. El POST por mazo omite los ajustes manuales, procesa cada imagen actual
una sola vez y devuelve el detalle más
`framing_summary: {"adjusted":0,"unchanged":0,"no_face":0}`. Un job activo devuelve 409.

Con `AUTO_FRAMING=true`, después de guardar una imagen nueva borderless se realiza
una llamada corta a Claude con visión (200 tokens, modelo `ANTHROPIC_VERIFY_MODEL`
o `ANTHROPIC_MODEL` si el primero está vacío). La copia enviada es JPEG con lado
mayor de 768 px. Las imágenes clásicas mantienen el encuadre por defecto y no usan
esta llamada. Un error de detección o persistencia del encuadre no elimina el arte.

Si el rostro está por encima del 26% del alto, el auto-encuadre lo desplaza hacia el
32%, con zoom 100 y tope +35; si está al 26% o más, no desplaza la imagen. Por ejemplo,
un rostro al 10% recibe +22 y uno al 0% recibe +32. El zoom 90 queda como respaldo
para posiciones defensivas fuera de rango que aún no alcancen la zona segura con
el desplazamiento máximo. Al regenerar, la imagen nueva recibe su propio encuadre,
sin heredar el manual anterior. Estos valores no cambian la clave de caché.


## Idioma de las cartas y reescritura sin regenerar arte

`CARD_TEXT_LANGUAGE=en` genera `themed_name` y `themed_flavor` en inglés, con tono de
Magic en inglés. `es` mantiene el español; cualquier otro valor usa inglés. Los
nombres propios se conservan siempre en su forma canónica en inglés: Darkspear,
Undercity, Arthas, the Lich King. Los planes nuevos o rehechos escriben `element`
en inglés, `category` y `visual_brief` en español, y `reference.page` con el título
exacto en inglés. La entrevista conserva preguntas y explicaciones en español, con
nombres propios canónicos en las opciones.

Para actualizar nombres y flavor ya generados sin volver a pagar por el arte:

```bash
curl -X POST http://127.0.0.1:8000/api/decks/1/rewrite-texts/
```

Este endpoint síncrono usa solo texto, en lotes de 20 cartas y con 4000 tokens por
llamada a Claude. Responde con el detalle más
`rewrite_summary: {"updated":24,"skipped":1}`. Solo considera imágenes actuales;
una carta omitida por Claude conserva sus textos y cuenta en skipped. Actualiza
solo las dos claves de prompt_json, truncando a 30 y 140 caracteres. Conserva
archivo, cache_key, encuadre y los elementos de asignaciones existentes. Responde
409 con un job activo y 502 con detail si Claude falla. Cambiar el idioma no invalida
el arte: usa este endpoint para textos antiguos; rehacer el plan sí puede cambiar
asignaciones y dejar imágenes pendientes.

## Búsquedas de wiki con resultado explícito

El PATCH de asignación acepta URLs completas en reference_wiki o reference_page,
incluido `index.php?title=...`. Extrae dominio y título, decodifica escapes, convierte
`_` en espacios, elimina anclas y normaliza mayúsculas del dominio, www. y rutas.
Por ejemplo, con una asignación ya definida:

```bash
curl -X PATCH http://127.0.0.1:8000/api/cards/12/theme-assignment/ \
  -H 'Content-Type: application/json' \
  -d '{"element":"Vol\u0027jin","category":"Personaje","visual_brief":"Al mando de sus tropas","reference_wiki":"","reference_page":"https://warcraft.wiki.gg/wiki/Vol%27jin"}'
curl -X POST http://127.0.0.1:8000/api/cards/12/theme-assignment/auto-reference/ \
  -H 'Content-Type: application/json' -d '{"force":true}'
```

La respuesta por carta agrega `auto_reference_result`:

```json
{"status":"skipped","message":"La búsqueda ya se intentó; usa force para reintentar.","skipped_reason":"already_attempted"}
```

status puede ser found, not_found, rejected, error, blocked o skipped. skipped_reason es vacío
si se intentó; si se omitió, indica no_reference_needed, missing_wiki_or_page,
already_has_auto_reference, already_attempted o references_full. Los errores de normalización
responden 400 con un mensaje claro.

Sin force, una referencia manual no bloquea la búsqueda: se pueden agregar automáticas
en las posiciones libres hasta el objetivo. Con force se reemplazan las automáticas;
las manuales y las elegidas de la wiki se conservan. Con tres referencias conservadas,
se omite con `skipped_reason:"references_full"`. El mensaje de éxito informa
«Se agregaron N referencias» y si se evitó la principal por posible fan art.


## Descargas bloqueadas y Fandom

El bloqueo de Fandom está confirmado: la API puede responder, pero la CDN rechaza
las descargas automatizadas. La app devuelve `blocked` y explica la acción manual:
abre la página en la wiki, haz clic derecho → **Copiar imagen** y pégala en el frontend
con **Ctrl+V**. El frontend usa el endpoint de subida manual existente. No se intenta
eludir el bloqueo ni hacerse pasar por un navegador.

Cada asignación agrega `reference_page_url`, construido con el título codificado
para el enlace «Abrir página en la wiki» (vacío si no hay wiki o página):

```json
{"reference_page_url":"https://megaman.fandom.com/wiki/Mega_Man_X_(character)","auto_reference_status":"blocked"}
```

Los bloqueos de descarga HTTP 401, 403 y 429 se reflejan en
`auto_reference_status:"blocked"`, en `auto_reference_result.status` y en el contador
`blocked` del resumen por mazo. Los errores de descarga terminan con la URL fallida
(hasta 120 caracteres), con un máximo de 300 caracteres para el mensaje completo.

Fuera de Fandom, si se bloquea la imagen original se intenta una vez su miniatura,
si existe. En Fandom o su CDN wikia.nocookie.net no se intenta la miniatura tras el
bloqueo. Para HTTP 429, un Retry-After numérico de 0 a 5 segundos permite una espera
y un único reintento de esa descarga. HTTP 404 y 500 siguen siendo errores de descarga.

El plan prefiere wikis comunitarias activas fuera de Fandom, como warcraft.wiki.gg,
cuando existen. Si la franquicia solo está en Fandom, lo mantiene como fuente y el
usuario pega la referencia manualmente. `find_reference` imprime el status final;
si hubo bloqueo, muestra el mensaje completo con la URL para diagnosticarlo.

## Elegir una imagen de la página de la wiki

El selector permite revisar las imágenes de la página y escoger una referencia canónica:

```http
GET /api/cards/12/theme-assignment/wiki-images/
GET /api/cards/12/theme-assignment/wiki-images/?wiki=warcraft.wiki.gg&page=Lich%20King
```

Usa por defecto la wiki y la página de la asignación. Devuelve `wiki`, `page` (título
resuelto), `fandom`, `page_url` e `images`. Cada candidata incluye `file`, `thumb_url`,
`full_url`, `width`, `height`, `is_page_image` y `maybe_fan_art`. La principal aparece
primero, seguida por las demás imágenes sin indicios de fan art y luego el fan art;
dentro de cada grupo se prioriza el área. Se excluyen iconos, SVG, GIF, formatos no
compatibles e imágenes de menos de 300 px en cualquiera de sus lados. Los resultados
se guardan en memoria durante diez minutos; se devuelven hasta 40 candidatas.

```http
POST /api/cards/12/theme-assignment/references/wiki/
Content-Type: application/json

{"file":"Arthas_Lich_King_WotLK.jpg","wiki":"warcraft.wiki.gg"}
```

`wiki` es opcional y usa la asignación como valor por defecto. Responde **201 con el
DeckCard completo**. La referencia queda con `origin:"wiki"`, `source_url` apuntando
a la página `File:` y la asignación con `edited:true` y `auto_reference_status:"found"`.
Se aplican el máximo de tres referencias, la detección de duplicados (400) y el
bloqueo por job activo (409). Las referencias elegidas se conservan al buscar con
`force` y al cambiar la wiki o la página; solo se reemplazan automáticamente las de
origen `auto`.

La heurística marca nombres que contienen ` by `, `fanart`, `fan art`, `deviantart`,
`artstation` o `commission` (ignorando mayúsculas y convirtiendo guiones bajos en
espacios). En el diagnóstico `find_reference`, si la principal parece fan art se elige la primera
alternativa sin esa marca y con al menos 600 px en su lado mayor; si no hay ninguna,
conserva la principal. El mensaje de éxito informa «Se evitó la imagen principal…».
La verificación visual también rechaza diseños claramente no canónicos. La heurística
orienta la selección; el usuario puede revisar las candidatas.

Fandom permite listar imágenes, pero bloquea su descarga automática. Si ocurre al
seleccionar una, el POST devuelve **400** con `detail`, `blocked:true` y `full_url`.
El front permite abrir esa imagen, copiarla y pegarla con **Ctrl+V** mediante la subida
manual existente; no se intenta eludir el bloqueo.

## Wiki confirmada por mazo y referencias múltiples

Flujo recomendado: **tema → entrevista → confirmar wiki → plan → referencias → tandas**.
La wiki confirmada es una decisión del usuario. El backend puede recomendar una,
pero nunca la confirma automáticamente.

```bash
# Proponer y comprobar wikis para el tema y los elementos del plan
curl -X POST http://127.0.0.1:8000/api/decks/1/wiki-candidates/

# Confirmar una wiki principal y, opcionalmente, una alternativa
curl -X PUT http://127.0.0.1:8000/api/decks/1/reference-wiki/ \
  -H 'Content-Type: application/json' \
  -d '{"domain":"warcraft.wiki.gg","alt_domain":"warcraft.fandom.com"}'

# Quitar la confirmación sin modificar las asignaciones existentes
curl -X DELETE http://127.0.0.1:8000/api/decks/1/reference-wiki/

# Elegir cuántas referencias automáticas debe buscar cada carta
curl -X PATCH http://127.0.0.1:8000/api/decks/1/ \
  -H 'Content-Type: application/json' -d '{"auto_reference_count":2}'
```

Los endpoints devuelven el detalle del mazo. Este agrega `reference_wiki`,
`reference_wiki_alt`, `reference_wiki_confirmed`, `wiki_candidates` y
`auto_reference_count`. Las candidatas tienen esta forma:

```json
{
  "domain":"warcraft.wiki.gg",
  "why":"Wiki comunitaria activa",
  "ok":true,
  "sitename":"Warcraft Wiki",
  "fandom":false,
  "found":5,
  "checked":6,
  "error":"",
  "recommended":true
}
```

Claude propone entre dos y cuatro dominios y hasta seis títulos de muestra. Se
comprueban el `siteinfo` de MediaWiki y la existencia de esos títulos, incluyendo
redirecciones y búsquedas. El resultado se ordena por acceso válido, más títulos
encontrados y preferencia por no-Fandom. Solo la mejor candidata, si encuentra al
menos la mitad de las muestras, lleva `recommended:true`. Las comprobaciones de
sitios, incluso las fallidas, se guardan en SQLite (`WikiSite`) durante siete días.

Si no hay una candidata accesible con alguna coincidencia, y
`WIKI_DISCOVERY_WEB_SEARCH=true`, Claude busca en la web con hasta tres usos de la
herramienta; los dominios nuevos también se comprueban. Cada búsqueda cuesta unos
USD 0,01; los usos se registran en el log. Un fallo de Claude devuelve 502, y un job
activo devuelve 409. Confirmar un dominio inaccesible devuelve 400.

La confirmación propaga los dominios a asignaciones con `needs_reference` no editadas
o con la wiki vacía. Limpia sus intentos previos y solo elimina referencias `auto`;
conserva las manuales y las elegidas de la wiki. Las editadas con wiki definida se
respetan. El plan recibe y usa obligatoriamente la wiki confirmada. Sin confirmación,
se comprueban los dominios propuestos: uno inválido se vacía y deja el estado `error`
con una indicación de confirmar la wiki del mazo.

Las páginas se verifican al guardar el plan, confirmar la wiki o cambiar wiki/página
en el PATCH de asignación. `reference_page_status` vale `ok` para una coincidencia
directa o redirección, `search` para una coincidencia por búsqueda que conviene revisar,
y `missing` si no se encontró. Se guarda el título real encontrado. La asignación
expone también `reference_wiki_alt`.

La búsqueda automática usa `target = min(auto_reference_count, plazas disponibles)`.
Descarta duplicados y verifica en **una sola llamada** las candidatas numeradas para
priorizar rostro y diseño canónico, evitando poses casi idénticas. Guarda hasta el
objetivo, con `origin:"auto"`, e informa «Se agregaron N referencias». Con tres
referencias manuales o elegidas, omite la búsqueda con `skipped_reason:"references_full"`.
Si la página no ofrece imágenes o bloquea las descargas, prueba una vez la wiki
alternativa de la asignación o, en su defecto, la del mazo. Fandom conserva el flujo
de copiar y pegar cuando no hay una alternativa que permita descargar.

`AUTO_REFERENCE_COUNT` fija el valor inicial de cada mazo (2 por defecto; rango 1–3).
Con `grok-imagine-image-2.0`, cada referencia usada suma aproximadamente USD 0,01 a
**cada imagen generada**, configurable mediante `COST_PER_REFERENCE_USD`; la
verificación en lote es una llamada corta a Claude adicional. El estimado agrega
`reference_cost_usd` y lo incluye en `estimated_cost_usd` y `batch_cost_usd`. Cuenta
las referencias actuales y, para una carta que necesita referencias, sin imágenes
ni intentos previos, prevé `auto_reference_count`.
