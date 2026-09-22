import time

import requests

from decks.models import Card

COLLECTION_URL = "https://api.scryfall.com/cards/collection"
HEADERS = {"User-Agent": "ProxyForge/1.0", "Accept": "application/json"}


def _entry_key(entry):
    if entry.get("set_code") and entry.get("collector_number"):
        return ("printing", entry["set_code"].lower(), str(entry["collector_number"]).lower())
    return ("name", entry["name"].casefold())


def _payload_key(payload):
    return ("printing", payload["set"].lower(), str(payload["collector_number"]).lower())


def _save_card(payload):
    faces = payload.get("card_faces") or []
    first_face = faces[0] if faces else {}
    image_uris = payload.get("image_uris") or first_face.get("image_uris") or {}
    defaults = {
        "oracle_id": payload["oracle_id"],
        "name": payload["name"],
        "mana_cost": payload.get("mana_cost", first_face.get("mana_cost", "")),
        "cmc": payload.get("cmc", 0),
        "type_line": payload.get("type_line", first_face.get("type_line", "")),
        "oracle_text": payload.get("oracle_text", first_face.get("oracle_text", "")),
        "flavor_text": payload.get("flavor_text", first_face.get("flavor_text", "")),
        "colors": payload.get("colors", first_face.get("colors", [])),
        "color_identity": payload.get("color_identity", []),
        "rarity": payload.get("rarity", ""),
        "set_code": payload["set"],
        "collector_number": payload["collector_number"],
        "original_art_url": image_uris.get("art_crop", ""),
        "original_artist": payload.get("artist", ""),
        "layout": payload.get("layout", ""),
        "scryfall_payload": payload,
    }
    card, _ = Card.objects.update_or_create(scryfall_id=payload["id"], defaults=defaults)
    return card


def _post_collection(identifiers):
    last_error = None
    for attempt in range(4):
        try:
            response = requests.post(
                COLLECTION_URL, json={"identifiers": identifiers}, headers=HEADERS, timeout=30
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(2 ** attempt)
    raise last_error


def resolve_cards(entries: list[dict]) -> dict:
    """Resolve entries from the local cache first, then Scryfall in batches."""
    resolved = {}
    missing_by_key = {}
    for entry in entries:
        key = _entry_key(entry)
        if key in resolved or key in missing_by_key:
            continue
        if key[0] == "printing":
            card = Card.objects.filter(set_code__iexact=key[1], collector_number__iexact=key[2]).first()
        else:
            card = Card.objects.filter(name=entry["name"]).first()
        if card:
            resolved[key] = card
        else:
            missing_by_key[key] = entry

    pending = list(missing_by_key.items())
    not_found_keys = set()
    for offset in range(0, len(pending), 75):
        batch = pending[offset:offset + 75]
        identifiers = []
        for _, entry in batch:
            if entry.get("set_code") and entry.get("collector_number"):
                identifiers.append({"set": entry["set_code"], "collector_number": entry["collector_number"]})
            else:
                identifiers.append({"name": entry["name"]})
        payload = _post_collection(identifiers)
        cards = [_save_card(item) for item in payload.get("data", [])]
        by_printing = {_payload_key(card.scryfall_payload): card for card in cards}
        by_name = {}
        for card in cards:
            by_name[card.name.casefold()] = card
            by_name[card.name.split("//", 1)[0].strip().casefold()] = card
        for key, entry in batch:
            card = by_printing.get(key) if key[0] == "printing" else by_name.get(entry["name"].casefold())
            if card:
                resolved[key] = card
            else:
                not_found_keys.add(key)
        if offset + 75 < len(pending):
            time.sleep(0.1)

    return {
        "resolved": [resolved.get(_entry_key(entry)) for entry in entries],
        "not_found": [entry for entry in entries if _entry_key(entry) in not_found_keys],
    }
