import csv
import io
import re

SECTION_HEADERS = {
    **dict.fromkeys(('deck', 'mazo', 'main', 'mainboard', 'maindeck'), 'main'),
    **dict.fromkeys(('sideboard', 'banquillo'), 'sideboard'),
    **dict.fromkeys(('commander', 'commanders', 'comandante'), 'commander'),
    **dict.fromkeys(('companion', 'maybeboard', 'considering'), 'ignored'),
}
CSV_HEADERS = {
    'name': ('name', 'card name', 'card', 'nombre'),
    'quantity': ('quantity', 'qty', 'count', 'cantidad'),
    'set_code': ('set code', 'set', 'edition code'),
    'collector_number': ('collector number', 'number', 'collector #'),
}


def _entry(raw_name, quantity, section, set_code=None, collector_number=None):
    return dict(raw_name=raw_name, name=raw_name.split('//', 1)[0].strip(),
                quantity=quantity, section=section, set_code=set_code, collector_number=collector_number)


def parse_decklist(text: str) -> list[dict]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    headers = [field.strip().casefold() for field in next(csv.reader([lines[0]]))]
    if ',' in lines[0] and set(headers) & {h for values in CSV_HEADERS.values() for h in values}:
        entries = []
        for row in csv.DictReader(io.StringIO('\n'.join(lines))):
            row = {key.strip().casefold(): (value or '').strip() for key, value in row.items() if key}
            values = {key: next((row[h] for h in aliases if row.get(h)), '') for key, aliases in CSV_HEADERS.items()}
            if not values['name']:
                continue
            code = values['set_code']
            entries.append(_entry(values['name'], int(values['quantity'] or 1), 'main',
                                  code.lower() if re.fullmatch(r'[A-Za-z0-9]{3,5}', code) else None,
                                  values['collector_number'] or None))
        return entries
    entries = []
    section = 'main'
    for line in lines:
        if line.startswith(('//', '#')) or line.casefold() == 'about' or line.casefold().startswith('name '):
            continue
        header = re.sub(r'\s*\(\d+\)\s*$', '', line.rstrip(':').strip()).casefold()
        if header in SECTION_HEADERS:
            section = SECTION_HEADERS[header]
            continue
        if section == 'ignored':
            continue
        entry_section = section
        if line.upper().startswith('SB:'):
            entry_section, line = 'sideboard', line[3:].strip()
        line = re.sub(r'\s+(?:\*(?:F|E|Foil)\*|#[^\s]+)\s*$', '', line, flags=re.I)
        # Strip multiple trailing tags, keeping an uppercase bracketed set code.
        while re.search(r'\s+(?:#[^\s]+|\[(?![A-Z0-9]{3,5}\]|[A-Za-z0-9]*[0-9][A-Za-z0-9]*\])[^\]]*\])$', line):
            line = re.sub(r'\s+(?:#[^\s]+|\[(?![A-Z0-9]{3,5}\]|[A-Za-z0-9]*[0-9][A-Za-z0-9]*\])[^\]]*\])$', '', line)
        match = re.match(r'^(?:(\d+)\s*[xX]?\s+)?(.+)$', line)
        quantity, raw_name = int(match[1] or 1), match[2].strip()
        code = number = None
        printing = re.match(r'^(.*?)\s+\(([A-Za-z0-9]{3,5})\)(?:\s+(\S+))?$', raw_name)
        bracket = re.match(r'^(.*?)\s+\[([A-Za-z0-9]{3,5})\]$', raw_name)
        if printing:
            raw_name, code, number = printing[1].strip(), printing[2].lower(), printing[3]
        elif bracket:
            raw_name, code = bracket[1].strip(), bracket[2].lower()
        entries.append(_entry(raw_name, quantity, entry_section, code, number))
    return entries
