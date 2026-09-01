#!/usr/bin/env python3
"""Scrape Pokémon Legends: Z-A learnsets from Bulbapedia.

By default this atomically replaces version-group 30 rows in
data/v2/csv/pokemon_moves.csv.  Pass --output to create a standalone CSV
instead.  A JSON manifest records the exact Bulbapedia revisions and SHA-256
digests used to produce the rows.

Examples:
    python Resources/scripts/data/scrape_legends_za_movesets.py \
        --cache-dir /tmp/bulbapedia-za
    python Resources/scripts/data/scrape_legends_za_movesets.py \
        --cache-dir /tmp/bulbapedia-za --offline
    python Resources/scripts/data/scrape_legends_za_movesets.py \
        --only Bulbasaur --output /tmp/bulbasaur.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence


API_URL = "https://bulbapedia.bulbagarden.net/w/api.php"
AVAILABILITY_PAGE = "List of Pokémon in Pokémon Legends: Z-A"
VERSION_GROUP_ID = 30
LEVEL_UP_METHOD_ID = 1
MACHINE_METHOD_ID = 4
USER_AGENT = "PokeAPI Legends Z-A data importer (https://github.com/PokeAPI/pokeapi)"
OUTPUT_FIELDS = (
    "pokemon_id",
    "version_group_id",
    "move_id",
    "pokemon_move_method_id",
    "level",
    "order",
    "mastery",
)


class ScrapeError(RuntimeError):
    """Raised when source data cannot be mapped without guessing."""


@dataclass(frozen=True)
class Page:
    title: str
    revision_id: int
    text: str


@dataclass(frozen=True)
class AvailableForm:
    national_dex: int
    species_name: str
    image_suffix: str
    form_name: str


@dataclass(frozen=True)
class Move:
    identifier: str
    method_id: int
    level: int = 0
    mastery: int | None = None


@dataclass
class Learnset:
    species_name: str
    form_heading: str
    method_id: int
    moves: list[Move]


@dataclass(frozen=True)
class Pokemon:
    pokemon_id: int
    identifier: str
    species_id: int
    is_default: bool


@dataclass(frozen=True)
class PokemonForm:
    pokemon_id: int
    identifier: str
    form_identifier: str


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def slug(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("♀", " female ").replace("♂", " male ")
    value = value.replace("’", "'").replace("'", "")
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def split_template(template: str) -> tuple[str, list[str], dict[str, str]]:
    """Split a MediaWiki template while preserving pipes in nested templates."""
    if not template.startswith("{{") or not template.endswith("}}"):
        raise ValueError(f"not a template: {template!r}")
    body = template[2:-2]
    parts: list[str] = []
    start = 0
    curly_depth = 0
    square_depth = 0
    index = 0
    while index < len(body):
        pair = body[index : index + 2]
        if pair == "{{":
            curly_depth += 1
            index += 2
            continue
        if pair == "}}" and curly_depth:
            curly_depth -= 1
            index += 2
            continue
        if pair == "[[":
            square_depth += 1
            index += 2
            continue
        if pair == "]]" and square_depth:
            square_depth -= 1
            index += 2
            continue
        if body[index] == "|" and not curly_depth and not square_depth:
            parts.append(body[start:index].strip())
            start = index + 1
        index += 1
    parts.append(body[start:].strip())

    name = parts[0]
    positional: list[str] = []
    named: dict[str, str] = {}
    for part in parts[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9 _-]*", key.strip()):
                named[key.strip()] = value.strip()
                continue
        positional.append(part)
    return name, positional, named


def iter_templates(text: str, template_name: str) -> Iterator[str]:
    """Yield balanced templates with the requested case-insensitive name."""
    pattern = re.compile(r"\{\{\s*" + re.escape(template_name) + r"(?=[|}])", re.I)
    for match in pattern.finditer(text):
        depth = 0
        index = match.start()
        while index < len(text) - 1:
            pair = text[index : index + 2]
            if pair == "{{":
                depth += 1
                index += 2
                continue
            if pair == "}}":
                depth -= 1
                index += 2
                if depth == 0:
                    yield text[match.start() : index]
                    break
                continue
            index += 1


def parse_availability(text: str) -> list[AvailableForm]:
    forms: list[AvailableForm] = []
    for template in iter_templates(text, "gdex/ZA"):
        _, positional, named = split_template(template)
        if len(positional) < 4 or not positional[0].isdigit():
            continue
        forms.append(
            AvailableForm(
                national_dex=int(positional[0]),
                species_name=positional[3],
                image_suffix=named.get("ig", "").lstrip("-"),
                form_name=re.sub(r"<br\s*/?>", " ", named.get("form", ""), flags=re.I),
            )
        )
    if not forms:
        raise ScrapeError("the availability page contained no gdex/ZA entries")
    return forms


HEADING_RE = re.compile(r"^(={2,6})(.+?)\1\s*$")


def _plain_heading(value: str) -> str:
    value = re.sub(r"\{\{(?:pkmn|OBP)\|([^}|]+).*?}}", r"\1", value, flags=re.I)
    value = re.sub(r"\[\[(?:[^]|]+\|)?([^]]+)]]", r"\1", value)
    return value.strip()


def parse_learnsets(text: str) -> list[Learnset]:
    """Parse only the Legends Z-A level-up and TM templates from a species page."""
    headings: dict[int, str] = {}
    method_level: int | None = None
    method_id: int | None = None
    current: Learnset | None = None
    output: list[Learnset] = []

    for line in text.splitlines():
        heading_match = HEADING_RE.match(line)
        if heading_match:
            level = len(heading_match.group(1))
            heading = _plain_heading(heading_match.group(2))
            headings[level] = heading
            for old_level in tuple(headings):
                if old_level > level:
                    del headings[old_level]
            heading_slug = slug(heading)
            if "leveling-up" in heading_slug:
                method_level, method_id = level, LEVEL_UP_METHOD_ID
            elif heading_slug in {"by-tm", "tm", "by-tms"}:
                method_level, method_id = level, MACHINE_METHOD_ID
            elif method_level is not None and level <= method_level:
                method_level, method_id = None, None
            current = None
            continue

        template_match = re.search(r"\{\{learnlist/([^|}]+).*}}", line, flags=re.I)
        if not template_match or method_id is None:
            continue
        try:
            name, positional, _ = split_template(template_match.group(0))
        except ValueError:
            continue
        normalized_name = name.lower()

        # Rotom's current Bulbapedia TM table is accidentally wrapped in
        # levelh/ZA; the containing section and row template remain unambiguous.
        if normalized_name in {"learnlist/levelh/za", "learnlist/tmh/za"}:
            if not positional:
                raise ScrapeError(f"malformed learnset header: {line}")
            form_heading = ""
            deeper = [level for level in headings if method_level is not None and level > method_level]
            if deeper:
                form_heading = headings[max(deeper)]
            current = Learnset(positional[0], form_heading, method_id, [])
            output.append(current)
            continue

        if normalized_name in {"learnlist/levelf/9", "learnlist/tmf/9"}:
            current = None
            continue
        if current is None:
            continue
        if normalized_name == "learnlist/levelza":
            if len(positional) < 3:
                raise ScrapeError(f"malformed levelZA row: {line}")
            level_text = positional[0]
            # Bulbapedia spells evolution moves as a tooltip rather than a number;
            # reminder-only moves use the same level-0 convention in
            # pokemon_moves.csv.
            level_markers = set(slug(level_text).split("-"))
            if level_markers & {"evo", "rem"}:
                level = 0
            else:
                number = re.match(r"\d+", level_text)
                if not number:
                    raise ScrapeError(f"unknown ZA level marker: {level_text}")
                level = int(number.group())
            mastery_number = re.match(r"\d+", positional[1])
            if not mastery_number:
                raise ScrapeError(f"unknown ZA mastery marker: {positional[1]}")
            current.moves.append(
                Move(
                    identifier=slug(positional[2]),
                    method_id=LEVEL_UP_METHOD_ID,
                    level=level,
                    mastery=int(mastery_number.group()),
                )
            )
        elif normalized_name == "learnlist/tmza":
            if len(positional) < 2:
                raise ScrapeError(f"malformed tmZA row: {line}")
            current.moves.append(Move(slug(positional[1]), MACHINE_METHOD_ID))

    return [learnset for learnset in output if learnset.moves]


def load_repository(root: Path) -> tuple[dict[int, list[Pokemon]], dict[int, list[PokemonForm]], dict[str, int]]:
    csv_dir = root / "data" / "v2" / "csv"
    pokemon_by_species: dict[int, list[Pokemon]] = defaultdict(list)
    with (csv_dir / "pokemon.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            pokemon_by_species[int(row["species_id"])].append(
                Pokemon(
                    pokemon_id=int(row["id"]),
                    identifier=row["identifier"],
                    species_id=int(row["species_id"]),
                    is_default=row["is_default"] == "1",
                )
            )

    forms_by_species: dict[int, list[PokemonForm]] = defaultdict(list)
    pokemon_species = {
        pokemon.pokemon_id: pokemon.species_id
        for values in pokemon_by_species.values()
        for pokemon in values
    }
    with (csv_dir / "pokemon_forms.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            pokemon_id = int(row["pokemon_id"])
            species_id = pokemon_species.get(pokemon_id)
            if species_id is not None:
                forms_by_species[species_id].append(
                    PokemonForm(pokemon_id, row["identifier"], row["form_identifier"])
                )

    with (csv_dir / "moves.csv").open(encoding="utf-8", newline="") as handle:
        move_ids = {row["identifier"]: int(row["id"]) for row in csv.DictReader(handle)}
    return pokemon_by_species, forms_by_species, move_ids


REGION_WORDS = {"alolan": "alola", "galarian": "galar", "hisuian": "hisui", "paldean": "paldea"}
IGNORED_FORM_WORDS = {"form", "forme", "mode", "flower", "plumage", "trim", "reversion", "pokemon"}


def form_tokens(value: str, species_name: str) -> set[str]:
    tokens = set(slug(value).split("-"))
    tokens -= set(slug(species_name).split("-"))
    tokens = {REGION_WORDS.get(token, token) for token in tokens}
    return tokens - IGNORED_FORM_WORDS - {"percent"}


def resolve_available_form_ids(
    available: AvailableForm,
    pokemon: Sequence[Pokemon],
    forms: Sequence[PokemonForm],
) -> list[int]:
    defaults = [entry for entry in pokemon if entry.is_default]
    if len(defaults) != 1:
        raise ScrapeError(f"expected one default Pokémon for #{available.national_dex:04d}")
    if not available.image_suffix:
        return [defaults[0].pokemon_id]

    wanted = form_tokens(f"{available.image_suffix} {available.form_name}", available.species_name)
    candidates: dict[int, set[str]] = {}
    for entry in pokemon:
        candidates[entry.pokemon_id] = form_tokens(entry.identifier, available.species_name)
    for entry in forms:
        candidates.setdefault(entry.pokemon_id, set()).update(
            form_tokens(f"{entry.identifier} {entry.form_identifier}", available.species_name)
        )
    scores = {
        pokemon_id: (len(wanted & tokens), -len(tokens - wanted))
        for pokemon_id, tokens in candidates.items()
    }
    best_score = max(scores.values(), default=(0, 0))
    best = [pokemon_id for pokemon_id, score in scores.items() if score == best_score]
    if best_score[0] == 0:
        # Cosmetic forms often share the default pokemon_id.
        return [defaults[0].pokemon_id]
    if len(best) != 1:
        # A source entry such as "Mega Meowstic" applies to multiple
        # sex-specific repository forms.  Returning all equally good generic
        # Mega matches is lossless; more specific Mega X/Y/Z names remain
        # uniquely matched by their additional token.
        if wanted == {"mega"} and all("mega" in candidates[pokemon_id] for pokemon_id in best):
            return sorted(best)
        names = [entry.identifier for entry in pokemon if entry.pokemon_id in best]
        raise ScrapeError(
            f"ambiguous form mapping for {available.species_name} {available.form_name or available.image_suffix}: {names}"
        )
    return [best[0]]


def resolve_available_form(
    available: AvailableForm,
    pokemon: Sequence[Pokemon],
    forms: Sequence[PokemonForm],
) -> int:
    """Resolve a source form expected to correspond to exactly one Pokémon."""
    resolved = resolve_available_form_ids(available, pokemon, forms)
    if len(resolved) != 1:
        raise ScrapeError(f"{available.species_name} maps to multiple repository forms: {resolved}")
    return resolved[0]


def choose_learnset(
    available: AvailableForm,
    learnsets: Sequence[Learnset],
    method_id: int,
) -> Learnset:
    choices = [entry for entry in learnsets if entry.method_id == method_id]
    if not choices:
        raise ScrapeError(f"no ZA method {method_id} learnset for {available.species_name}")
    if len(choices) == 1:
        return choices[0]

    wanted = form_tokens(f"{available.image_suffix} {available.form_name}", available.species_name)
    scored = []
    for choice in choices:
        tokens = form_tokens(choice.form_heading, available.species_name)
        default_bonus = int(not wanted and not tokens)
        scored.append(((len(wanted & tokens), default_bonus, -len(tokens - wanted)), choice))
    best_score = max(score for score, _ in scored)
    best = [choice for score, choice in scored if score == best_score]
    if len(best) != 1:
        labels = [choice.form_heading or "(default)" for choice in best]
        raise ScrapeError(f"ambiguous {available.species_name} learnset: {labels}")
    return best[0]


class BulbapediaClient:
    def __init__(self, cache_dir: Path | None, offline: bool, delay: float = 0.25):
        self.cache_dir = cache_dir
        self.offline = offline
        self.delay = delay
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, title: str) -> Path:
        assert self.cache_dir is not None
        return self.cache_dir / f"{sha256_text(title)}.json"

    def _request(self, parameters: dict[str, str]) -> dict:
        query = urllib.parse.urlencode({**parameters, "format": "json", "formatversion": "2"})
        request = urllib.request.Request(f"{API_URL}?{query}", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.load(response)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise ScrapeError(f"Bulbapedia API request failed: {error}") from error
        if "error" in result:
            raise ScrapeError(f"Bulbapedia API error: {result['error']}")
        time.sleep(self.delay)
        return result

    def pages(self, titles: Sequence[str]) -> dict[str, Page]:
        result: dict[str, Page] = {}
        missing: list[str] = []
        for title in titles:
            if self.cache_dir and self._cache_path(title).exists():
                payload = json.loads(self._cache_path(title).read_text(encoding="utf-8"))
                result[title] = Page(title, int(payload["revision_id"]), payload["text"])
            else:
                missing.append(title)
        if self.offline and missing:
            raise ScrapeError(f"offline cache is missing {len(missing)} page(s), including {missing[0]!r}")

        for start in range(0, len(missing), 50):
            batch = missing[start : start + 50]
            payload = self._request(
                {
                    "action": "query",
                    "prop": "revisions",
                    "rvprop": "ids|content",
                    "rvslots": "main",
                    "titles": "|".join(batch),
                }
            )
            returned = payload.get("query", {}).get("pages", [])
            by_title = {entry["title"]: entry for entry in returned}
            for requested_title in batch:
                entry = by_title.get(requested_title)
                if not entry or entry.get("missing"):
                    raise ScrapeError(f"Bulbapedia page not found: {requested_title}")
                revision = entry["revisions"][0]
                page = Page(requested_title, int(revision["revid"]), revision["slots"]["main"]["content"])
                result[requested_title] = page
                if self.cache_dir:
                    self._cache_path(requested_title).write_text(
                        json.dumps(
                            {"title": requested_title, "revision_id": page.revision_id, "text": page.text},
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
        return result


def build_rows(
    available_forms: Sequence[AvailableForm],
    pages: dict[str, Page],
    pokemon_by_species: dict[int, list[Pokemon]],
    forms_by_species: dict[int, list[PokemonForm]],
    move_ids: dict[str, int],
) -> tuple[list[dict[str, int | str]], set[str]]:
    parsed_by_species: dict[int, list[Learnset]] = {}
    rows: list[dict[str, int | str]] = []
    unresolved_moves: set[str] = set()
    seen_targets: set[int] = set()

    for available in available_forms:
        pokemon = pokemon_by_species.get(available.national_dex, [])
        if not pokemon:
            raise ScrapeError(f"national dex #{available.national_dex:04d} is missing from pokemon.csv")
        title = f"{available.species_name} (Pokémon)"
        if available.national_dex not in parsed_by_species:
            parsed_by_species[available.national_dex] = parse_learnsets(pages[title].text)
        learnsets = parsed_by_species[available.national_dex]
        target_ids = resolve_available_form_ids(
            available, pokemon, forms_by_species[available.national_dex]
        )
        for target_id in target_ids:
            if target_id in seen_targets:
                continue
            seen_targets.add(target_id)
            target = next(entry for entry in pokemon if entry.pokemon_id == target_id)
            # Repository identifiers disambiguate generic source labels such
            # as "Mega Meowstic", which applies to both male and female rows.
            repository_form_names = " ".join(
                entry.form_identifier
                for entry in forms_by_species[available.national_dex]
                if entry.pokemon_id == target_id and entry.form_identifier
            )
            learnset_form = AvailableForm(
                available.national_dex,
                available.species_name,
                available.image_suffix,
                f"{available.form_name} {target.identifier} {repository_form_names}",
            )

            for method_id in (LEVEL_UP_METHOD_ID, MACHINE_METHOD_ID):
                method_choices = [entry for entry in learnsets if entry.method_id == method_id]
                # A few species legitimately have no TMs. Level-up data is mandatory.
                if not method_choices and method_id == MACHINE_METHOD_ID:
                    continue
                chosen = choose_learnset(learnset_form, learnsets, method_id)
                initial_order = 0
                for move in chosen.moves:
                    move_id = move_ids.get(move.identifier)
                    if move_id is None:
                        unresolved_moves.add(move.identifier)
                        continue
                    order: int | str = ""
                    if method_id == LEVEL_UP_METHOD_ID and move.level == 1:
                        initial_order += 1
                        order = initial_order
                    rows.append(
                        {
                            "pokemon_id": target_id,
                            "version_group_id": VERSION_GROUP_ID,
                            "move_id": move_id,
                            "pokemon_move_method_id": method_id,
                            "level": move.level,
                            "order": order,
                            "mastery": move.mastery if move.mastery is not None else "",
                        }
                    )

    rows.sort(
        key=lambda row: (
            int(row["pokemon_id"]),
            int(row["pokemon_move_method_id"]),
            int(row["level"]),
            int(row["order"] or 0),
            int(row["move_id"]),
        )
    )
    return rows, unresolved_moves


def atomic_write_csv(path: Path, rows: Iterable[dict[str, int | str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def update_production_csv(path: Path, rows: Sequence[dict[str, int | str]]) -> int:
    """Atomically replace ZA rows while preserving every other row and its order."""
    ordered_rows = sorted(
        rows,
        key=lambda row: (int(row["pokemon_id"]), int(row["version_group_id"])),
    )
    temporary = path.with_suffix(path.suffix + ".tmp")
    replaced = 0
    new_index = 0

    with path.open("r", encoding="utf-8", newline="") as source, temporary.open(
        "w", encoding="utf-8", newline=""
    ) as destination:
        reader = csv.DictReader(source)
        if tuple(reader.fieldnames or ()) != OUTPUT_FIELDS:
            raise ScrapeError(
                f"unexpected {path.name} header: {reader.fieldnames}; expected {list(OUTPUT_FIELDS)}"
            )
        writer = csv.DictWriter(destination, fieldnames=OUTPUT_FIELDS, lineterminator="\n")
        writer.writeheader()
        for existing in reader:
            if int(existing["version_group_id"]) == VERSION_GROUP_ID:
                replaced += 1
                continue
            existing_key = (int(existing["pokemon_id"]), int(existing["version_group_id"]))
            while new_index < len(ordered_rows):
                new = ordered_rows[new_index]
                new_key = (int(new["pokemon_id"]), int(new["version_group_id"]))
                if new_key >= existing_key:
                    break
                writer.writerow(new)
                new_index += 1
            writer.writerow(existing)
        writer.writerows(ordered_rows[new_index:])

    temporary.replace(path)

    with path.open("r", encoding="utf-8", newline="") as handle:
        written_za = [row for row in csv.DictReader(handle) if int(row["version_group_id"]) == VERSION_GROUP_ID]
    expected = [{field: str(row[field]) for field in OUTPUT_FIELDS} for row in ordered_rows]
    if written_za != expected:
        raise ScrapeError(f"post-write verification failed for {path}")
    return replaced


def repository_root() -> Path:
    return Path(__file__).resolve().parents[3]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="write a standalone CSV instead of updating data/v2/csv/pokemon_moves.csv",
    )
    parser.add_argument("--manifest", type=Path, help="override the JSON provenance manifest path")
    parser.add_argument("--cache-dir", type=Path, help="optional revision cache for reproducible offline runs")
    parser.add_argument("--offline", action="store_true", help="read every source page from --cache-dir")
    parser.add_argument("--allow-unresolved", action="store_true", help="omit moves absent from moves.csv instead of failing")
    parser.add_argument("--only", action="append", default=[], metavar="SPECIES", help="scrape named species only (repeatable)")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.offline and not args.cache_dir:
        raise ScrapeError("--offline requires --cache-dir")
    if not args.output and args.only:
        raise ScrapeError("--only requires --output; a partial scrape cannot replace production ZA data")
    if not args.output and args.allow_unresolved:
        raise ScrapeError("--allow-unresolved requires --output; production data must resolve every move")

    client = BulbapediaClient(args.cache_dir, args.offline)
    availability_page = client.pages([AVAILABILITY_PAGE])[AVAILABILITY_PAGE]
    available = parse_availability(availability_page.text)
    if args.only:
        selected = {slug(value) for value in args.only}
        available = [entry for entry in available if slug(entry.species_name) in selected]
        missing = selected - {slug(entry.species_name) for entry in available}
        if missing:
            raise ScrapeError(f"unknown or unavailable species: {', '.join(sorted(missing))}")

    titles = sorted({f"{entry.species_name} (Pokémon)" for entry in available})
    pages = client.pages(titles)
    for title in titles:
        parsed = parse_learnsets(pages[title].text)
        expected = {
            LEVEL_UP_METHOD_ID: len(list(iter_templates(pages[title].text, "learnlist/levelZA"))),
            MACHINE_METHOD_ID: len(list(iter_templates(pages[title].text, "learnlist/tmZA"))),
        }
        actual = {
            method_id: sum(len(entry.moves) for entry in parsed if entry.method_id == method_id)
            for method_id in (LEVEL_UP_METHOD_ID, MACHINE_METHOD_ID)
        }
        if actual != expected:
            raise ScrapeError(f"incomplete template parse for {title}: expected {expected}, got {actual}")
    pokemon, forms, moves = load_repository(repository_root())
    rows, unresolved = build_rows(available, pages, pokemon, forms, moves)
    if unresolved and not args.allow_unresolved:
        preview = ", ".join(sorted(unresolved)[:12])
        raise ScrapeError(
            f"{len(unresolved)} move(s) are absent from moves.csv: {preview}. "
            "Add those moves first or use --allow-unresolved to inspect the remaining rows."
        )
    if not rows:
        raise ScrapeError("scrape produced no rows")

    root = repository_root()
    destination = args.output or root / "data" / "v2" / "csv" / "pokemon_moves.csv"
    replaced_rows = 0
    if args.output:
        atomic_write_csv(destination, rows)
    else:
        replaced_rows = update_production_csv(destination, rows)
    manifest_path = args.manifest
    if manifest_path is None:
        manifest_path = (
            destination.with_suffix(destination.suffix + ".manifest.json")
            if args.output
            else Path(__file__).with_name("legends_za_movesets.manifest.json")
        )
    source_pages = [availability_page, *[pages[title] for title in titles]]
    manifest = {
        "source": API_URL,
        "availability_page": AVAILABILITY_PAGE,
        "version_group_id": VERSION_GROUP_ID,
        "rows": len(rows),
        "pokemon": len({int(row["pokemon_id"]) for row in rows}),
        "unresolved_moves": sorted(unresolved),
        "output": str(destination.relative_to(root) if destination.is_relative_to(root) else destination),
        "output_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "pages": [
            {"title": page.title, "revision_id": page.revision_id, "sha256": sha256_text(page.text)}
            for page in sorted(source_pages, key=lambda page: page.title)
        ],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"wrote {len(rows)} ZA rows for {manifest['pokemon']} Pokémon to {destination} "
        f"(replaced {replaced_rows})"
    )
    print(f"wrote source manifest to {manifest_path}")
    if unresolved:
        print(f"warning: omitted {len(unresolved)} unresolved move(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ScrapeError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
