import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("scrape_legends_za_movesets.py")
SPEC = importlib.util.spec_from_file_location("scrape_legends_za_movesets", SCRIPT)
scraper = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = scraper
SPEC.loader.exec_module(scraper)


class TemplateTests(unittest.TestCase):
    def test_slug_matches_pokeapi_apostrophe_convention(self):
        self.assertEqual(scraper.slug("King's Shield"), "kings-shield")

    def test_split_template_preserves_nested_pipes(self):
        template = "{{gdex/ZA|0254||041|Sceptile|ig=-Mega|form=Mega Sceptile|Event<ref>{{x|a|b}}</ref>}}"
        name, positional, named = scraper.split_template(template)
        self.assertEqual(name, "gdex/ZA")
        self.assertEqual(positional[:4], ["0254", "", "041", "Sceptile"])
        self.assertEqual(named["ig"], "-Mega")

    def test_parse_availability_includes_forms(self):
        text = """
{{gdex/ZA|0026|054||Raichu|1|Electric}}
{{gdex/ZA|0026|054||Raichu|2|Electric|Psychic|ig=-Alola|form=Alolan Form}}
"""
        forms = scraper.parse_availability(text)
        self.assertEqual(len(forms), 2)
        self.assertEqual(forms[1].image_suffix, "Alola")
        self.assertEqual(forms[1].form_name, "Alolan Form")


class LearnsetTests(unittest.TestCase):
    SOURCE = """
===Learnset===
====By [[Level|leveling up]]====
{{gameabbrev9|ZA}}
{{learnlist/levelh/ZA|Bulbasaur|Grass|Poison|1}}
{{learnlist/levelZA|1|10|Tackle|Normal|Physical|40|4}}
{{learnlist/levelZA|3|6|Vine Whip|Grass|Physical|45|6}}
{{learnlist/levelf/9|Bulbasaur|Grass|Poison|1}}
====By [[TM]]====
{{gameabbrev9|ZA}}
{{learnlist/tmh/ZA|Bulbasaur|Grass|Poison|1}}
{{learnlist/tmZA|TM007|Toxic|Poison|Status|—|10}}
{{learnlist/tmf/9|Bulbasaur|Grass|Poison|1}}
"""

    def test_parse_za_rows_only(self):
        learnsets = scraper.parse_learnsets(self.SOURCE)
        self.assertEqual(len(learnsets), 2)
        self.assertEqual(learnsets[0].moves[0], scraper.Move("tackle", 1, 1, 10))
        self.assertEqual(learnsets[1].moves[0], scraper.Move("toxic", 4))

    def test_form_heading_is_retained(self):
        source = self.SOURCE.replace(
            "{{gameabbrev9|ZA}}\n{{learnlist/levelh/ZA",
            "=====Alolan Bulbasaur=====\n{{gameabbrev9|ZA}}\n{{learnlist/levelh/ZA",
            1,
        )
        self.assertEqual(scraper.parse_learnsets(source)[0].form_heading, "Alolan Bulbasaur")

    def test_evolution_level_is_zero(self):
        source = self.SOURCE.replace(
            "{{learnlist/levelZA|1|10|Tackle",
            "{{learnlist/levelZA|{{tt|Evo.|Learned upon evolving}}|10|Tackle",
        )
        self.assertEqual(scraper.parse_learnsets(source)[0].moves[0].level, 0)


class ResolutionTests(unittest.TestCase):
    def test_regional_form_maps_to_repository_id(self):
        available = scraper.AvailableForm(26, "Raichu", "Alola", "Alolan Form")
        pokemon = [
            scraper.Pokemon(26, "raichu", 26, True),
            scraper.Pokemon(10100, "raichu-alola", 26, False),
        ]
        forms = [scraper.PokemonForm(10100, "raichu-alola", "alola")]
        self.assertEqual(scraper.resolve_available_form(available, pokemon, forms), 10100)

    def test_mega_uses_base_learnset_when_no_separate_table_exists(self):
        available = scraper.AvailableForm(3, "Venusaur", "Mega", "Mega Venusaur")
        base = scraper.Learnset("Venusaur", "", 1, [scraper.Move("tackle", 1, 1, 10)])
        self.assertIs(scraper.choose_learnset(available, [base], 1), base)


class ProductionCsvTests(unittest.TestCase):
    def test_update_replaces_only_za_rows(self):
        old_rows = [
            "pokemon_id,version_group_id,move_id,pokemon_move_method_id,level,order,mastery",
            "1,29,33,1,1,1,",
            "1,30,45,1,1,1,10",
            "2,1,33,1,1,1,",
        ]
        replacement = {
            "pokemon_id": 1,
            "version_group_id": 30,
            "move_id": 22,
            "pokemon_move_method_id": 1,
            "level": 3,
            "order": "",
            "mastery": 6,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pokemon_moves.csv"
            path.write_text("\n".join(old_rows) + "\n", encoding="utf-8")
            replaced = scraper.update_production_csv(path, [replacement])
            self.assertEqual(replaced, 1)
            self.assertEqual(
                path.read_text(encoding="utf-8").splitlines()[1:],
                ["1,29,33,1,1,1,", "1,30,22,1,3,,6", "2,1,33,1,1,1,"],
            )


if __name__ == "__main__":
    unittest.main()
