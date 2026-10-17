import json
import logging
import os
import re
from copy import deepcopy

from anthropic import Anthropic

logger = logging.getLogger(__name__)
CONCISION_MESSAGE = 'IMPORTANTE: tu respuesta anterior se cortó por largo. Sé más conciso y respeta los límites de palabras de cada campo.'


class ClaudeTruncatedError(ValueError):
    pass


def parse_json(raw):
    cleaned = re.sub(r'^```(?:json)?\s*', '', raw.strip(), flags=re.I)
    cleaned = re.sub(r'\s*```$', '', cleaned)
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError('Se esperaba un objeto JSON.')
    return data


def call_claude_json(*, system, messages, max_tokens, validate, retry_max_tokens=None):
    client = Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
    original = deepcopy(messages)
    current = deepcopy(original)
    budget = max_tokens
    for attempt in range(2):
        response = client.messages.create(
            model=os.getenv('ANTHROPIC_MODEL', 'claude-sonnet-4-6'), max_tokens=budget,
            system=system, messages=current,
        )
        raw = ''.join(block.text for block in response.content if getattr(block, 'type', '') == 'text')
        if getattr(response, 'stop_reason', None) == 'max_tokens':
            logger.warning('Respuesta de Claude truncada: %s', raw[:2000])
            if attempt:
                raise ClaudeTruncatedError('La respuesta de Claude se cortó por largo incluso tras reintentar.')
            current = deepcopy(original)
            for message in reversed(current):
                if message['role'] == 'user':
                    if isinstance(message['content'], list):
                        message['content'].append({'type': 'text', 'text': CONCISION_MESSAGE})
                    else:
                        message['content'] += '\n\n' + CONCISION_MESSAGE
                    break
            budget = retry_max_tokens if retry_max_tokens is not None else max_tokens * 2
            continue
        try:
            return validate(parse_json(raw))
        except (ValueError, TypeError):
            logger.warning('Respuesta JSON inválida de Claude: %s', raw[:2000])
            if attempt:
                raise ValueError('Claude devolvió JSON inválido tras dos intentos.') from None
            current = [*current, {'role': 'assistant', 'content': raw},
                       {'role': 'user', 'content': 'Devuelve únicamente JSON válido con todas las claves solicitadas, sin fences Markdown.'}]
