from django.db import migrations


def backfill_cards(apps, schema_editor):
    Card = apps.get_model("decks", "Card")
    for card in Card.objects.using(schema_editor.connection.alias).iterator():
        payload = card.scryfall_payload or {}
        faces = payload.get("card_faces") or []
        first = faces[0] if faces else {}
        for key in ("power", "toughness", "loyalty"):
            setattr(card, key, payload.get(key, first.get(key, "")))
        card.keywords = payload.get("keywords", [])
        card.card_faces = [
            {key: face.get(key, "") for key in (
                "name", "mana_cost", "type_line", "oracle_text", "flavor_text",
                "power", "toughness", "loyalty",
            )} for face in faces
        ]
        card.save(using=schema_editor.connection.alias,
                  update_fields=["power", "toughness", "loyalty", "keywords", "card_faces"])


class Migration(migrations.Migration):
    dependencies = [("decks", "0002_card_card_faces_card_keywords_card_loyalty_and_more")]
    operations = [migrations.RunPython(backfill_cards, migrations.RunPython.noop)]
