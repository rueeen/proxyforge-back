import os
import re


def card_text_language():
    value = os.getenv('CARD_TEXT_LANGUAGE', 'en')
    return value if value in ('en', 'es') else 'en'


def card_text_instructions():
    canonical = ('Prefiere nombres cortos y potentes, por ejemplo "Sliver Mother" o "Yandere Hive Queen", antes que "The First Sliver, Yandere Queen". '
                 'Los nombres propios de la franquicia siempre van en su forma canónica en inglés: Darkspear, no Lanza Negra; Undercity, no Entrañas.')
    if card_text_language() == 'es':
        return ('themed_name y themed_flavor se escriben en ESPAÑOL. themed_name es un nombre alternativo de como máximo max_name_chars caracteres (cuenta espacios y comas), '
                'coherente con el tema y reconocible respecto del original. themed_flavor tiene 140 caracteres o menos, ambientado en el tema y sin comillas envolventes. ' + canonical)
    return ('themed_name y themed_flavor se escriben en INGLÉS, con el tono de las cartas de Magic en inglés. '
            'themed_name usa el nombre canónico en inglés del elemento asignado tal como aparece en la franquicia '
            '(por ejemplo Vol\'jin, Shadow Hunter; Sylvanas, the Dark Lady), con como máximo max_name_chars caracteres (cuenta espacios y comas). '
            'themed_flavor tiene 140 caracteres o menos y puede ser una cita breve atribuida a un personaje (— Sylvanas Windrunner). ' + canonical)



def themed_name_limit(card):
    faces = card.card_faces or []
    mana_cost = faces[0].get('mana_cost', '') if faces else card.mana_cost
    symbols = len(re.findall(r'\{[^}]*\}', mana_cost or ''))
    return max(14, 28 - 2 * symbols)


def shorten_themed_name(name, limit):
    name = name.strip()
    if len(name) <= limit:
        return name
    if ',' in name:
        name = name.rsplit(',', 1)[0].rstrip()
    if len(name) > limit:
        prefix = name[:limit]
        if not name[limit].isspace() and re.search(r'\s', prefix):
            prefix = re.sub(r'\s+\S*$', '', prefix)
        name = prefix
    return name.rstrip(' ,;:.!?-–—/|')
