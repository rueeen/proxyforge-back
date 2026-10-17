import os


def card_text_language():
    value = os.getenv('CARD_TEXT_LANGUAGE', 'en')
    return value if value in ('en', 'es') else 'en'


def card_text_instructions():
    canonical = 'Los nombres propios de la franquicia siempre van en su forma canónica en inglés: Darkspear, no Lanza Negra; Undercity, no Entrañas.'
    if card_text_language() == 'es':
        return ('themed_name y themed_flavor se escriben en ESPAÑOL. themed_name es un nombre alternativo de 30 caracteres o menos, '
                'coherente con el tema y reconocible respecto del original. themed_flavor tiene 140 caracteres o menos, ambientado en el tema y sin comillas envolventes. ' + canonical)
    return ('themed_name y themed_flavor se escriben en INGLÉS, con el tono de las cartas de Magic en inglés. '
            'themed_name usa el nombre canónico en inglés del elemento asignado tal como aparece en la franquicia '
            '(por ejemplo Vol\'jin, Shadow Hunter; Sylvanas, the Dark Lady), con 30 caracteres o menos. '
            'themed_flavor tiene 140 caracteres o menos y puede ser una cita breve atribuida a un personaje (— Sylvanas Windrunner). ' + canonical)
