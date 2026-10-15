"""Upstream roster adapters for the Everydle datasets. Read-only, by design.

The attributes these puzzles run on split into two kinds. **Game data** is
published by the game or datamined from it, and a machine can fetch it. **Lore**
is a fact about a character that no source carries — nationality, species,
gender for a non-human — and only a person can supply it. An adapter therefore
normalises what upstream actually knows and says nothing about the rest, so the
report downstream can tell an operator what still needs a human.

Nothing here writes to `data/`. See `docs/everydle_data_updates.md`.

**League of Legends joined the managed set 2026-09-27**, once LoLdle's
external maintainer left the project — see `fetch_loldle` for what Data
Dragon can and cannot supply for it. `hard_mode` (the redacted-lore guessing
mode) stays unmanaged: it has no plausible upstream source, being entirely
hand-authored lore, the same category as Valdle's own `gender`/`species`/
`origin` fields.
"""

import hashlib
import json
import re
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"

# The English catalog is the pivot for comparison: it is complete, and it is the
# vocabulary upstream already speaks, so a diff never has to guess an opaque
# attribute value id.
PIVOT_LANGUAGE = "en"

# Datasets an adapter may touch, as (game, dataset). `dbdle`'s survivors, perks
# and roster datasets are unfinished scaffolding that no command loads.
# `loldle`'s `hard_mode` is deliberately absent too — see the module docstring.
MANAGED_DATASETS = (("valdle", "agents"), ("dbdle", "killers"),
                    ("genshindle", "characters"), ("loldle", "champions"))
# No longer refuses any game by name. `loldle` was locked here until
# 2026-09-27, at its then-external maintainer's request; nothing takes this
# set's place, so a future hand-maintained dataset would need its own,
# separately-decided entry rather than reusing this one.
LOCKED_GAMES = frozenset()

# Fields the game itself publishes and changes with a balance patch. A killer
# buffed or nerfed has to move with the game, so upstream is the authority here
# and a disagreement is something to *apply* rather than something to decide.
# Everything not listed is lore or a local editorial choice.
SOURCE_AUTHORITATIVE = {
    ("valdle", "agents"): ("role",),
    ("dbdle", "killers"): ("movement_speed", "terror_radius", "height"),
    # Everything Genshindle runs on is published by the game and changes only
    # when the game changes it — there is no lore attribute in the set, which is
    # why this dataset needs no person at all beyond the aliases.
    # `body_type` was here and was **removed deliberately** (2026-08-27): the
    # game publishes it and it sounded like a good clue, but in play it narrows
    # almost nothing — five values across 120 characters, most of them in two of
    # them — so it filled a column without earning it. Upstream still carries it;
    # this dataset chooses not to, which is why it is absent here rather than an
    # `ACCEPTED_DIVERGENCES` entry.
    ("genshindle", "characters"): (
        "element", "weapon", "region", "rarity", "gender",
        "weekly_boss", "version",
    ),
    # `range` alone. Verified live 2026-09-27 against every champion Data
    # Dragon returns: `partype` (the resource-bar name) is coarser than the
    # local catalog and cannot be trusted — it reports "Fury" for both
    # Renekton and Briar, who have distinct named resources locally, and
    # "None" for both truly resourceless champions and health-cost casters —
    # so a direct mapping would silently blur a distinction a person already
    # made correctly. Lane role, species, region, gender and release year are
    # not in Data Dragon's `champion.json` at all.
    ("loldle", "champions"): ("range",),
}

# Entities upstream carries that this puzzle deliberately does not, with a
# reason each. Without somewhere to record the decision they are reported as
# "new upstream" every single run, which is how a weekly report teaches its
# reader to skim — the same reason ACCEPTED_DIVERGENCES exists. Removing an
# entry makes the entity a finding again.
#
# Note what is *not* here: a character who has not been released yet. Whether a
# character is out is the one fact this source does not carry — `version` is when
# they entered the data, so an upcoming character already has one — which makes
# it a genuine finding for a person to answer rather than something to record
# once. Alyosha was briefly listed here and should not have been: 7.0 shipped
# both her and Odette.
EXCLUDED_ENTITIES = {
    # Neither has a talent record upstream — the roster lists them, the talents
    # endpoint does not — which is what says they are not playable characters.
    ("genshindle", "characters", "manekin"):
        "No talent record upstream, so not a playable character.",
    ("genshindle", "characters", "manekina"):
        "No talent record upstream, so not a playable character.",
    ("loldle", "champions", "romeo"):
        "Not a League of Legends champion — a local easter-egg entity with no "
        "upstream record.",
}


def is_excluded(game: str, dataset: str, entity_id: str) -> str | None:
    """The reason this entity is deliberately absent, or None."""
    return EXCLUDED_ENTITIES.get((game, dataset, entity_id))


# Divergences the maintainer has looked at and decided to keep. Reporting these
# every run would teach an operator to skim past the report, which is the one way
# a drift check stops working. Each entry needs a reason, and deleting an entry
# makes it a finding again.
ACCEPTED_DIVERGENCES = {
    ("dbdle", "killers", "the_singularity", "gender"):
        "A machine rather than a person; the local Indeterminate is deliberate.",
    ("dbdle", "killers", "the_twins", "gender"):
        "Charlotte and Victor are one killer with two genders; upstream records "
        "only Charlotte.",
    ("dbdle", "killers", "the_unknown", "gender"):
        "Deliberately unknowable in the game's own lore.",
    ("loldle", "champions", "gnar", "range"):
        "Data Dragon's base attack-range figure (175) reads as melee, but "
        "Gnar's default Mini form plays at range; the derivation cannot see "
        "his two-form kit. Verified live 2026-09-27.",
}


def is_accepted(game: str, dataset: str, entity_id: str, field_name: str) -> bool:
    return (game, dataset, entity_id, field_name) in ACCEPTED_DIVERGENCES


def merge_base_value(local_text, upstream_text):
    """Replace only the leading component of a multi-value label.

    A local movement speed reads `4.6 m/s, 9.2 m/s`: the base speed followed by
    the power-state speeds a person maintains. Upstream knows only the base, so
    applying a balance change has to overwrite the first component and keep the
    rest — otherwise tracking the game would throw away the part upstream cannot
    see. Single-valued labels are simply replaced.
    """
    if not isinstance(local_text, str) or not isinstance(upstream_text, str):
        return upstream_text
    parts = [part.strip() for part in local_text.split(",")]
    if len(parts) <= 1:
        return upstream_text
    return ", ".join([upstream_text.strip(), *parts[1:]])


USER_AGENT = "potatobot-everydle-drift"
TIMEOUT_SECONDS = 20


class SourceError(RuntimeError):
    """Upstream was unreachable, or returned something unusable.

    A community API changing its schema has to surface as an error rather than
    as a proposal to delete forty killers, so every adapter raises this instead
    of returning a partial roster.
    """


@dataclass(frozen=True)
class UpstreamEntity:
    """One entity as upstream describes it, in the local vocabulary."""

    entity_id: str
    display_name: str
    aliases: tuple[str, ...] = ()
    # Local field name -> the English display text or the number upstream gives.
    fields: dict = field(default_factory=dict)
    # Local fields this source structurally cannot supply. These are the lore
    # attributes a person has to fill in, and naming them is most of the point.
    unknown_fields: tuple[str, ...] = ()


def slugify(name: str) -> str:
    """Derive a local entity id from a display name.

    Deliberately lossy and ASCII-only, which is what makes `The Onryō` collapse
    onto the existing `the_onryo` instead of looking like a new killer.
    """
    folded = (
        name.replace("ō", "o").replace("ō".upper(), "O")
        .replace("ū", "u").replace("ā", "a").replace("ī", "i").replace("ē", "e")
    )
    return re.sub(r"[^a-z0-9]+", "_", folded.lower()).strip("_")


def mint_value_id(dataset: str, field_name: str, english_value: str,
                  existing: set[str]) -> str:
    """Create a new attribute value id, or raise on an unresolvable collision.

    The ids already in the data are opaque and dataset-scoped — the same word has
    a different id in `killers` than in `survivors` — so they cannot be
    re-derived. New ones therefore only have to be unique and stable, and this
    keeps the established `<field>_<10 hex>` shape. An existing value's id must
    always be reused rather than minted again: the mechanics file references it,
    and a fresh id would orphan every entity that pointed at the old one.
    """
    seed = f"{dataset}:{field_name}:{english_value}"
    for attempt in range(16):
        digest = hashlib.blake2s(
            f"{seed}:{attempt}".encode("utf-8"), digest_size=5
        ).hexdigest()
        candidate = f"{field_name}_{digest}"
        if candidate not in existing:
            return candidate
    raise SourceError(f"could not mint a unique id for {seed}")


# ------------------------------------------------------------------ local data

@dataclass
class LocalDataset:
    """The three files that together define one dataset."""

    game: str
    dataset: str
    mechanics: dict
    catalogs: dict  # language -> parsed locale catalog

    @property
    def entities(self) -> dict:
        return self.mechanics.get("entities", {})

    def attribute_text(self, language: str, field_name: str, value_id: str):
        section = self.catalogs[language]["datasets"][self.dataset]
        return (section.get("attributes") or {}).get(field_name, {}).get(value_id)

    def value_ids(self, field_name: str) -> dict:
        """English display text -> value id, for one attribute field."""
        section = self.catalogs[PIVOT_LANGUAGE]["datasets"][self.dataset]
        return {
            text: value_id
            for value_id, text in (section.get("attributes") or {})
            .get(field_name, {}).items()
        }

    def fields(self) -> tuple[str, ...]:
        """Every attribute field any entity in this dataset carries."""
        seen = []
        for mechanics in self.entities.values():
            for name in mechanics:
                if name not in seen:
                    seen.append(name)
        return tuple(seen)

    def resolved(self, entity_id: str) -> dict:
        """One entity's attributes as English text, or a number when numeric.

        LoLdle is the first managed dataset where a field can hold more than
        one value (a champion can have two species) — every mechanics value
        there is a list even when there is only one, unlike Valdle/DbDle/
        Genshindle's bare scalars. A single-item list resolves to that one
        value, matching the bare-scalar shape upstream supplies for a
        single-valued field; a genuine multi-item list resolves to a list of
        text, since nothing here compares that shape against upstream today.
        """
        resolved = {}
        for name, value in self.entities.get(entity_id, {}).items():
            if isinstance(value, list):
                texts = [self.attribute_text(PIVOT_LANGUAGE, name, item)
                        for item in value]
                resolved[name] = texts[0] if len(texts) == 1 else texts
            elif isinstance(value, str):
                resolved[name] = self.attribute_text(PIVOT_LANGUAGE, name, value)
            else:
                resolved[name] = value
        return resolved


MECHANICS_FILE = {("genshindle", "characters"): "genshindle.json",
                  ("valdle", "agents"): "valdle.json",
                  ("dbdle", "killers"): "killers.json",
                  ("loldle", "champions"): "champions.json"}


def load_local(game: str, dataset: str) -> LocalDataset:
    if game in LOCKED_GAMES:
        raise SourceError(
            f"{game} is maintained by hand at its owner's request and must not "
            "be read or written by this tool"
        )
    if (game, dataset) not in MANAGED_DATASETS:
        raise SourceError(f"unmanaged dataset: {game}.{dataset}")
    game_dir = DATA_DIR / game
    mechanics = json.loads(
        (game_dir / MECHANICS_FILE[(game, dataset)]).read_text(encoding="utf-8")
    )
    catalogs = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((game_dir / "locales").glob("*.json"))
    }
    return LocalDataset(game, dataset, mechanics, catalogs)


# --------------------------------------------------------------------- fetching

def fetch_json(url: str, opener=None):
    """Read one JSON document, or raise SourceError.

    `opener` exists so tests can drive an adapter from a recorded fixture; the
    fragile part of this tool is the shape of a third-party payload, and that is
    exactly what a fixture pins down.
    """
    if opener is not None:
        return opener(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except Exception as exc:  # urllib raises a wide family; all are the same here
        raise SourceError(f"{url} could not be read: {type(exc).__name__}") from exc


def _require_rows(rows, url: str, minimum: int):
    """Refuse a payload too small to be real.

    Without this, an upstream outage that returns an empty list would read as
    "every entity was removed", which is the one outcome this tool must never
    report.
    """
    if not isinstance(rows, (list, dict)) or len(rows) < minimum:
        raise SourceError(
            f"{url} returned {len(rows) if hasattr(rows, '__len__') else '?'} "
            f"rows, fewer than the {minimum} a real response has"
        )
    return rows


# ----------------------------------------------------------------------- valdle

VALORANT_AGENTS_URL = (
    "https://valorant-api.com/v1/agents?isPlayableCharacter=true"
)
# `releaseDate` is the Unix epoch for every agent that predates the field, so a
# 1970 date is "unset" rather than "released in 1970".
EPOCH_YEAR = 1970


def fetch_valdle(opener=None) -> dict[str, UpstreamEntity]:
    """Playable Valorant agents.

    Upstream supplies the roster and the agent's role, and a real release date
    only for recent additions. Gender, species and origin are lore and are
    reported as unknown.
    """
    payload = fetch_json(VALORANT_AGENTS_URL, opener)
    rows = _require_rows((payload or {}).get("data") or [], VALORANT_AGENTS_URL, 20)

    entities = {}
    for row in rows:
        name = (row.get("displayName") or "").strip()
        role = ((row.get("role") or {}).get("displayName") or "").strip()
        if not name or not role:
            raise SourceError("a Valorant agent row has no name or no role")
        supplied = {"role": role}
        released = str(row.get("releaseDate") or "")[:4]
        if released.isdigit() and int(released) != EPOCH_YEAR:
            supplied["year"] = int(released)
        entities[slugify(name)] = UpstreamEntity(
            entity_id=slugify(name),
            display_name=name,
            aliases=(name,),
            fields=supplied,
            # `year` is listed as unknown when upstream only had the placeholder.
            unknown_fields=tuple(
                name for name in ("gender", "species", "origin", "year")
                if name not in supplied
            ),
        )
    return entities


# ------------------------------------------------------------------------ dbdle

DBD_CHARACTERS_URL = "https://dbd.tricky.lol/api/characters"
DBD_DLC_URL = "https://dbd.tricky.lol/api/dlc"

# Upstream vocabulary -> the English display text the local catalog uses.
DBD_GENDER = {"male": "Male", "female": "Female", "multiple": "Both",
              "nothuman": "Indeterminate"}
DBD_HEIGHT = {"tall": "Tall", "average": "Average", "short": "Short"}


def fetch_dbdle(opener=None) -> dict[str, UpstreamEntity]:
    """Dead by Daylight killers.

    Upstream supplies the roster, gender, height, the base movement speed and
    terror radius from the datamined tunables, and the release year from the
    chapter's store timestamp. It does not supply nationality, and its single
    base tunable cannot express the power-state speeds the local data lists
    alongside the base one — so both are surfaced rather than proposed.
    """
    characters = fetch_json(DBD_CHARACTERS_URL, opener)
    _require_rows(characters or {}, DBD_CHARACTERS_URL, 40)
    chapters = fetch_json(DBD_DLC_URL, opener) or {}

    entities = {}
    for row in characters.values():
        if row.get("role") != "killer":
            continue
        name = (row.get("name") or "").strip()
        if not name:
            raise SourceError("a Dead by Daylight killer row has no name")
        tunables = row.get("tunables") or {}
        supplied = {}
        if row.get("gender") in DBD_GENDER:
            supplied["gender"] = DBD_GENDER[row["gender"]]
        if row.get("height") in DBD_HEIGHT:
            supplied["height"] = DBD_HEIGHT[row["height"]]
        speed = tunables.get("MaxWalkSpeed")
        if isinstance(speed, (int, float)) and speed > 0:
            supplied["movement_speed"] = f"{speed / 100:g} m/s"
        radius = tunables.get("TerrorRadius")
        if isinstance(radius, (int, float)) and radius > 0:
            supplied["terror_radius"] = f"{radius / 100:g}m"
        # A base-game killer has no chapter, so no timestamp to read a year from.
        stamp = (chapters.get(row.get("dlc")) or {}).get("time") or 0
        if stamp:
            supplied["release"] = datetime.fromtimestamp(stamp, UTC).year

        entity_id = slugify(name)
        entities[entity_id] = UpstreamEntity(
            entity_id=entity_id,
            display_name=name,
            aliases=(name,),
            fields=supplied,
            unknown_fields=tuple(
                field_name for field_name in
                ("gender", "height", "movement_speed", "terror_radius",
                 "country", "release")
                if field_name not in supplied
            ),
        )
    return entities


# ------------------------------------------------------------------ genshindle

GENSHIN_CHARACTERS_URL = (
    "https://genshin-db-api.vercel.app/api/v5/characters"
    "?query=names&matchCategories=true&verboseCategories=true"
)
GENSHIN_TALENTS_URL = (
    "https://genshin-db-api.vercel.app/api/v5/talents"
    "?query=names&matchCategories=true&verboseCategories=true"
)

# Upstream vocabulary -> the English display text the local catalog uses.
GENSHIN_WEAPON = {
    "WEAPON_SWORD_ONE_HAND": "Sword", "WEAPON_CLAYMORE": "Claymore",
    "WEAPON_POLE": "Polearm", "WEAPON_BOW": "Bow", "WEAPON_CATALYST": "Catalyst",
}
# A weekly boss drops three talent materials, and **the API does not say which
# boss a material comes from**: the material record's description only alludes to
# it in prose, and the domain list does not carry weekly-boss drops at all. So
# the value is the *material*, which upstream does publish and which a player
# recognises just as well — rather than a boss name invented here, which is the
# one thing this tool must never do.
#
# Adding a row collapses that material into a boss, and because all three of a
# boss's materials map to the same name they stay one value. It is an attribute
# change, so `everydle_propose.py` will hold it to a day boundary.
GENSHIN_WEEKLY_BOSS: dict[str, str] = {}

# Upstream leaves `region` blank for eleven characters but still states an
# `associationType` for every one of them, so the region is published — just
# under another name. These are the associations no character with a filled-in
# region uses, checked against the live roster:
#
#   ASSOC_SNEZHNAYA / _STAR   Alyosha, Odette, Sandrone   -> Snezhnaya
#   ASSOC_NODKRAI_ZIBAI       Zibai                       -> Nod-Krai
#   ASSOC_MAINACTOR           the Traveller and its forms -> no nation
#   ASSOC_RANGER              Aloy, a Horizon guest       -> no nation
#   ASSOC_HVISION             Nicole, of the Hexenzirkel  -> no nation
#   ASSOC_OMNI_SCOURGE        Skirk, of the Abyss         -> no nation
#
# The four that name no nation share one value rather than four of their own:
# "not from any of Teyvat's nations" is the fact, and it is a real clue.
#
# An association that is *not* here and whose row has no region leaves the field
# absent, which the drift report then names — so a nation added to the game
# becomes a finding rather than a silently wrong label.
GENSHIN_ASSOCIATION_REGION = {
    "ASSOC_SNEZHNAYA": "Snezhnaya",
    "ASSOC_SNEZHNAYA_STAR": "Snezhnaya",
    "ASSOC_NODKRAI_ZIBAI": "Nod-Krai",
    "ASSOC_MAINACTOR": "Outsider",
    "ASSOC_RANGER": "Outsider",
    "ASSOC_HVISION": "Outsider",
    "ASSOC_OMNI_SCOURGE": "Outsider",
}

# Upstream writes "None" for a character with no element of their own, which is
# the Traveller and any future form of them. They wield every element — the
# talents endpoint carries a separate record per element, seven of them — so no
# single element or weekly boss is true. "All" is the honest value for both, and
# it plays as a clue rather than as a hole.
GENSHIN_ALL = "All"


def _genshin_region(row) -> str | None:
    """This character's nation, from `region` or from the association."""
    region = (row.get("region") or "").strip()
    if region:
        return region
    return GENSHIN_ASSOCIATION_REGION.get(row.get("associationType") or "")

# The talent-material id range a weekly boss drop falls in. Verified against the
# live roster: 125 of 125 characters have exactly one cost in this range at
# talent level 10, so this identifies the drop without matching on its name.
GENSHIN_BOSS_MATERIAL_IDS = range(113000, 114000)


def _genshin_weekly_boss(costs: dict) -> str | None:
    """The weekly-boss talent material for one character, or None."""
    hits = [
        entry for entry in (costs or {}).get("lvl10") or []
        if isinstance(entry, dict)
        and entry.get("id") in GENSHIN_BOSS_MATERIAL_IDS
        and (entry.get("name") or "").strip()
    ]
    if len(hits) != 1:
        return None
    material = hits[0]["name"].strip()
    return GENSHIN_WEEKLY_BOSS.get(material, material)


def fetch_genshindle(opener=None) -> dict[str, UpstreamEntity]:
    """Playable Genshin Impact characters.

    Every attribute this puzzle uses is published: element, weapon, region,
    rarity, gender, body type, the release version, and the weekly-boss talent
    material derived from the character's level-10 talent cost. There is no lore
    attribute, so unlike Valdle and DbDle this adapter reports nothing as
    needing a person — the only local knowledge is the aliases, which
    `everydle_drift.py` already surfaces separately.

    Two payloads rather than one per character: both endpoints answer in bulk, so
    a run is two requests rather than a hundred and twenty-five.
    """
    characters = _require_rows(
        fetch_json(GENSHIN_CHARACTERS_URL, opener) or [],
        GENSHIN_CHARACTERS_URL, 60)
    talents = _require_rows(
        fetch_json(GENSHIN_TALENTS_URL, opener) or [],
        GENSHIN_TALENTS_URL, 60)
    boss_by_name = {}
    for row in talents:
        name = (row.get("name") or "").strip()
        if name:
            boss_by_name[name] = _genshin_weekly_boss(row.get("costs") or {})

    entities = {}
    for row in characters:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        supplied = {}
        element = (row.get("elementText") or "").strip()
        # "None" is the Traveller, who has every element rather than none.
        traveller = element == "None"
        if traveller:
            supplied["element"] = GENSHIN_ALL
        elif element:
            supplied["element"] = element
        weapon = GENSHIN_WEAPON.get(row.get("weaponType"))
        if weapon:
            supplied["weapon"] = weapon
        region = _genshin_region(row)
        if region:
            supplied["region"] = region
        rarity = row.get("rarity")
        if isinstance(rarity, int) and rarity in (4, 5):
            supplied["rarity"] = f"{rarity}-star"
        gender = (row.get("gender") or "").strip()
        if gender:
            supplied["gender"] = gender
        # The Traveller's talents are recorded per element, so there are seven
        # boss materials rather than one — "All" for the same reason the element
        # is. Everyone else takes theirs from the talent cost.
        boss = GENSHIN_ALL if traveller else boss_by_name.get(name)
        if boss:
            supplied["weekly_boss"] = boss
        version = (str(row.get("version") or "")).strip()
        if version:
            # Numeric, so it compares as higher/lower the way Valdle's year does.
            # "1.0" and "5.3" sort wrong as text and 1.10 would collide with 1.1
            # as a float, so it is stored as major*100 + minor.
            parts = version.split(".")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                supplied["version"] = int(parts[0]) * 100 + int(parts[1])

        entities[slugify(name)] = UpstreamEntity(
            entity_id=slugify(name),
            display_name=name,
            aliases=(name,),
            fields=supplied,
            unknown_fields=tuple(
                field_name for field_name in
                ("element", "weapon", "region", "rarity", "gender",
                 "weekly_boss", "version")
                if field_name not in supplied
            ),
        )
    return entities


# ----------------------------------------------------------------------- loldle

LOLDLE_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
# Community-standard threshold: a champion whose base attack range is at least
# this many units plays as ranged. Verified live 2026-09-27 against every one
# of 173 champions Data Dragon returned: exactly one exception, Gnar, recorded
# in `ACCEPTED_DIVERGENCES` rather than special-cased here.
LOLDLE_RANGED_THRESHOLD = 350


def _loldle_champion_url(version: str) -> str:
    return f"https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/champion.json"


def fetch_loldle(opener=None) -> dict[str, UpstreamEntity]:
    """League of Legends champions.

    Data Dragon supplies the roster and a base attack range, from which melee
    vs. ranged is derived (see `LOLDLE_RANGED_THRESHOLD`). Everything else the
    puzzle uses is either lore or structurally coarser upstream than the local
    catalog: lane role, species, region and gender are not in Data Dragon at
    all; release year is not in `champion.json` either; and the resource-bar
    type (`partype`) is too coarse to trust — see `SOURCE_AUTHORITATIVE`'s
    comment for the specific champions that showed this live. All five are
    reported as unknown rather than guessed.
    """
    versions = _require_rows(
        fetch_json(LOLDLE_VERSIONS_URL, opener) or [], LOLDLE_VERSIONS_URL, 1
    )
    version = versions[0]
    champion_url = _loldle_champion_url(version)
    payload = fetch_json(champion_url, opener)
    rows = _require_rows((payload or {}).get("data") or {}, champion_url, 100)

    entities = {}
    for row in rows.values():
        name = (row.get("name") or "").strip()
        attack_range = (row.get("stats") or {}).get("attackrange")
        if not name or attack_range is None:
            raise SourceError(
                "a League of Legends champion row has no name or no attack range"
            )
        supplied = {
            "range": "Ranged" if attack_range >= LOLDLE_RANGED_THRESHOLD
            else "Melee",
        }
        entities[slugify(name)] = UpstreamEntity(
            entity_id=slugify(name),
            display_name=name,
            aliases=(name,),
            fields=supplied,
            unknown_fields=(
                "gender", "role", "species", "resource", "region", "year",
            ),
        )
    return entities


ADAPTERS = {("valdle", "agents"): fetch_valdle,
            ("dbdle", "killers"): fetch_dbdle,
            ("genshindle", "characters"): fetch_genshindle,
            ("loldle", "champions"): fetch_loldle}


def fetch(game: str, dataset: str, opener=None) -> dict[str, UpstreamEntity]:
    if game in LOCKED_GAMES:
        raise SourceError(f"{game} is excluded from automatic updates")
    try:
        adapter = ADAPTERS[(game, dataset)]
    except KeyError as exc:
        raise SourceError(f"no adapter for {game}.{dataset}") from exc
    return adapter(opener)


def fixture_opener(responses: dict):
    """An opener that serves recorded payloads instead of making requests."""
    def open_url(url):
        try:
            return responses[url]
        except KeyError as exc:
            raise SourceError(f"no fixture recorded for {url}") from exc
    return open_url


def record_fixtures(destination: Path) -> list[Path]:
    """Save the current upstream payloads, for the tests to run against."""
    written = []
    for url, name in (
        (VALORANT_AGENTS_URL, "valorant_agents.json"),
        (DBD_CHARACTERS_URL, "dbd_characters.json"),
        (DBD_DLC_URL, "dbd_dlc.json"),
        (GENSHIN_CHARACTERS_URL, "genshin_characters.json"),
        (GENSHIN_TALENTS_URL, "genshin_talents.json"),
    ):
        path = destination / name
        path.write_text(
            json.dumps(fetch_json(url), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        written.append(path)

    # The champion payload's URL is version-templated, so its own recorded
    # file name carries the version rather than a fixed constant — the test
    # re-derives the same URL from the recorded `loldle_versions.json`.
    versions = fetch_json(LOLDLE_VERSIONS_URL)
    versions_path = destination / "loldle_versions.json"
    versions_path.write_text(
        json.dumps(versions, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    written.append(versions_path)
    champion_path = destination / "loldle_champions.json"
    champion_path.write_text(
        json.dumps(
            fetch_json(_loldle_champion_url(versions[0])),
            ensure_ascii=False, sort_keys=True,
        ),
        encoding="utf-8",
    )
    written.append(champion_path)
    return written


if __name__ == "__main__":
    # Re-record the test fixtures. Everything else here is a library.
    if len(sys.argv) != 3 or sys.argv[1] != "record":
        raise SystemExit(
            "usage: python scripts/everydle_sources.py record <directory>"
        )
    target = Path(sys.argv[2])
    target.mkdir(parents=True, exist_ok=True)
    for written in record_fixtures(target):
        print(f"recorded {written}")
