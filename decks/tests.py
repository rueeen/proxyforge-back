from django.test import SimpleTestCase

from .parser import parse_decklist


class DecklistParserTests(SimpleTestCase):
    def test_quantities_with_and_without_x_and_default(self):
        parsed = parse_decklist("4 Lightning Bolt\n4x Counterspell\nSol Ring")
        self.assertEqual([item["quantity"] for item in parsed], [4, 4, 1])
        self.assertEqual(parsed[2]["name"], "Sol Ring")

    def test_set_and_collector_number(self):
        item = parse_decklist("1 Sol Ring (c21) 263")[0]
        self.assertEqual((item["set_code"], item["collector_number"]), ("c21", "263"))
        self.assertEqual(item["raw_name"], "Sol Ring")

    def test_double_faced_card_uses_first_face_for_lookup(self):
        full = "Fable of the Mirror-Breaker // Reflection of Kiki-Jiki"
        item = parse_decklist(f"2 {full}")[0]
        self.assertEqual(item["name"], "Fable of the Mirror-Breaker")
        self.assertEqual(item["raw_name"], full)

    def test_sections_in_english_and_spanish_case_insensitively(self):
        text = "Deck\nIsland\nBANQUILLO\nNegate\nComandante\nAtraxa\nMazo\nForest\nSideboard\nDuress\nCommander\nOmnath\nMain\nPlains"
        self.assertEqual(
            [entry["section"] for entry in parse_decklist(text)],
            ["main", "sideboard", "commander", "main", "sideboard", "commander", "main"],
        )

    def test_comments_and_blank_lines_are_ignored(self):
        self.assertEqual(len(parse_decklist("\n// comentario\n  \n1 Island")), 1)
