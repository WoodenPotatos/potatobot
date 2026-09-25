"""Word chain's real-word check: a per-language wordlist loaded at import.

`cogs/minigames.py`'s `judge_word` only ever checked the chaining rule and the
used-word ledger, so any string satisfying `WORD_PATTERN` was accepted whether
or not it was a real word. This module loads a bundled dictionary per language
the way Everydle's datasets load, so a missing or unreadable wordlist disables
the check for that language only -- word chain keeps working on chaining and
uniqueness alone, the same `load_or_disable` philosophy `core/minigame_data.py`
uses, rather than breaking the whole game over one file.

The bundled data, what was kept from the source dictionaries and why words are
indexed by their folded form rather than their exact spelling:
data/wordchain/ATTRIBUTION.md.

Rules that bind changes here: docs/subsystems/minigames.md
"""

import logging
import unicodedata
from pathlib import Path

wordchain_logger = logging.getLogger("PotatoBot.WordChainDictionary")

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "wordchain"

# Which `language` setting value maps to which bundled file. A language with
# no entry here -- or whose file is missing or empty -- simply has no
# dictionary check: chaining and uniqueness alone still gate it.
WORDLIST_FILES = {"hu": "hu.txt", "en": "en.txt"}


def _fold(word: str) -> str:
    """The exact fold `cogs.minigames.fold` applies, duplicated rather than
    imported: `core/` is what cogs import, never the reverse, and the
    dictionary is keyed on this folded form so it agrees with every other
    accent-insensitive comparison word chain already makes (the chain-letter
    rule, the same-word check, the used-word ledger).
    """
    decomposed = unicodedata.normalize("NFD", word.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _load_wordlist(path: Path) -> frozenset[str]:
    with path.open("r", encoding="utf-8") as source:
        words = frozenset(_fold(line.strip()) for line in source if line.strip())
    if not words:
        raise ValueError(f"Empty word list: {path}")
    return words


def _load_all() -> dict[str, frozenset[str]]:
    loaded = {}
    for language, filename in WORDLIST_FILES.items():
        path = DATA_DIR / filename
        try:
            loaded[language] = _load_wordlist(path)
        except (OSError, ValueError) as error:
            wordchain_logger.error(
                "Word chain dictionary disabled (language=%s, reason=%s)",
                language, error,
            )
    return loaded


WORDLISTS = _load_all()


def is_known_word(language: str, folded_word: str) -> bool:
    """Whether `folded_word` (already folded by the caller) is in the
    dictionary for `language`.

    Answers True -- does not refuse -- when the language has no loaded
    wordlist at all, since a missing dictionary must disable the check rather
    than break word chain for every language.
    """
    wordlist = WORDLISTS.get(language)
    if wordlist is None:
        return True
    return folded_word in wordlist
