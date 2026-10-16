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


class ExpandedParserTests(SimpleTestCase):
    def test_quantity_formats(self):
        for line in ('4 Lightning Bolt', '4x Lightning Bolt', '4 x Lightning Bolt',
                     '4X Lightning Bolt', '4\tLightning Bolt'):
            with self.subTest(line=line):
                item = parse_decklist(line)[0]
                self.assertEqual((item['quantity'], item['name']), (4, 'Lightning Bolt'))
        self.assertEqual(parse_decklist('Lightning Bolt')[0]['quantity'], 1)

    def test_printing_tags_and_foil(self):
        for suffix in ('', ' *F*', ' *E*', ' *Foil*', ' [Ramp]'):
            with self.subTest(suffix=suffix):
                item = parse_decklist('1x Sol Ring (C21) 263' + suffix)[0]
                self.assertEqual((item['name'], item['set_code'], item['collector_number']), ('Sol Ring', 'c21', '263'))
        self.assertEqual(parse_decklist('1 Sol Ring #ramp #artifact')[0]['name'], 'Sol Ring')
        self.assertEqual(parse_decklist('1 Sol Ring [C21]')[0]['set_code'], 'c21')
        item = parse_decklist('SB: 2 Negate')[0]
        self.assertEqual((item['section'], item['quantity'], item['name']), ('sideboard', 2, 'Negate'))

    def test_all_headers(self):
        from .parser import SECTION_HEADERS
        for header, section in SECTION_HEADERS.items():
            for suffix in ('', ':', ' (15)', ' (1):'):
                with self.subTest(header=header, suffix=suffix):
                    entries = parse_decklist(header.upper() + suffix + '\n1 Island')
                    if section == 'ignored':
                        self.assertEqual(entries, [])
                    else:
                        self.assertEqual(entries[0]['section'], section)

    def test_maybeboard_and_comments(self):
        entries = parse_decklist('About\nName My Deck\n# comment\n// comment\n\n1 Island\nMaybeboard\nSol Ring\nMain\nForest')
        self.assertEqual([entry['name'] for entry in entries], ['Island', 'Forest'])

    def test_csv_with_and_without_set(self):
        entries = parse_decklist('Quantity,Card Name,Set Code,Collector Number\n2,"Card, With Comma",C21,263\n1,,abc,4')
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]['quantity'], entries[0]['name'], entries[0]['set_code'], entries[0]['collector_number']),
                         (2, 'Card, With Comma', 'c21', '263'))
        for name in ('name', 'card name', 'card', 'nombre'):
            for quantity in ('quantity', 'qty', 'count', 'cantidad'):
                with self.subTest(name=name, quantity=quantity):
                    self.assertEqual(parse_decklist(f'{name},{quantity}\nIsland,3')[0]['quantity'], 3)
        self.assertEqual(parse_decklist('name,other\nIsland,anything')[0]['quantity'], 1)
        for code in ('set code', 'set', 'edition code'):
            for number in ('collector number', 'number', 'collector #'):
                with self.subTest(code=code, number=number):
                    entry = parse_decklist(f'name,{code},{number}\nSol Ring,C21,263')[0]
                    self.assertEqual((entry['set_code'], entry['collector_number']), ('c21', '263'))
        self.assertIsNone(parse_decklist('name,set\nIsland,Commander 2021')[0]['set_code'])
