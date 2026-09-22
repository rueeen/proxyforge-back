import re

SECTION_HEADERS = {
    "deck": "main", "mazo": "main", "main": "main",
    "sideboard": "sideboard", "banquillo": "sideboard",
    "commander": "commander", "comandante": "commander",
}
QUANTITY_RE = re.compile(r"^(?:(\d+)\s*[xX]?\s+)?(.+)$")
PRINTING_RE = re.compile(r"^(.*?)\s+\(([A-Za-z0-9]{3,5})\)(?:\s+(\S+))?\s*$")


def parse_decklist(text: str) -> list[dict]:
    """Parse common deck-list formats while retaining the user's original name."""
    entries = []
    section = "main"
    for source_line in text.splitlines():
        line = source_line.strip()
        if not line or line.startswith("//"):
            continue
        header = SECTION_HEADERS.get(line.casefold())
        if header:
            section = header
            continue
        match = QUANTITY_RE.match(line)
        quantity = int(match.group(1) or 1)
        raw_name = match.group(2).strip()
        set_code = collector_number = None
        printing = PRINTING_RE.match(raw_name)
        if printing:
            raw_name = printing.group(1).strip()
            set_code = printing.group(2).lower()
            collector_number = printing.group(3)
        name = raw_name.split("//", 1)[0].strip()
        entries.append({
            "quantity": quantity, "name": name, "set_code": set_code,
            "collector_number": collector_number, "section": section,
            "raw_name": raw_name,
        })
    return entries
