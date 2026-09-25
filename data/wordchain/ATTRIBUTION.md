# Word chain dictionary: source and licence

`hu.txt` and `en.txt` are plain word lists, one word per line, derived from the
Hunspell dictionaries published by the LibreOffice dictionaries project
(https://github.com/LibreOffice/dictionaries, fetched 2026-09-18 from the
`master` branch). They are read by `core/wordchain_dictionary.py` to check
whether a word-chain submission is a real word.

## What was kept, and what was not

Each dictionary's `.dic` file lists one entry per line as `stem/affix-flags`
(plus, for Hungarian, a tab-separated morphological field). Only the bare
stem was kept — Hunspell's affix rules, which expand a stem into every
inflected form, were **not** applied. An entry was dropped if, once split from
its flags, it did not match `[^\W\d_]{2,32}` (letters only, 2-32 characters) —
the same shape `cogs/minigames.WORD_PATTERN` requires of a chain submission,
so a form nothing could ever type is not worth keeping.

The surviving stems were folded with the exact function
`cogs.minigames.fold` uses (Unicode NFD normalisation, casefold, strip
combining marks) and stored as that folded form, not the original spelling.
This is deliberate, not an oversight: every other comparison in word chain —
the chain-letter rule, the same-word check, the used-word ledger — already
treats an accented letter and its bare form as identical, and a dictionary
keyed on exact spelling would be the one comparison in the game that did not.
The consequence is the same one those other comparisons already accept: two
words that differ only by accent (e.g. Hungarian "kar" and "kár") are
indistinguishable to this check.

**Because affixes are not expanded, an uncommon inflected Hungarian form may
be refused as "not a word" even though it is real** — Hungarian is
agglutinative, and the source dictionary lists a stem once rather than every
case-and-suffix combination. A word actually shipped in the list (a base form,
which is how most nouns and infinitives are naturally played) is unaffected.

## Licence

- `hu.txt`: derived from `hu_HU.dic`, part of the Magyar Ispell / LibreOffice
  Hungarian dictionaries, © László Németh & Ferenc Godó, 2025. Dual-licensed
  MPLv2 or LGPLv3+; either licence's terms apply to this derived word list.
- `en.txt`: derived from `en_US.dic`, the LibreOffice/OpenOffice.org English
  (US) Hunspell dictionary, itself based on Kevin Atkinson's SCOWL word list
  (LGPL).

Neither list carries per-word attribution; this file is that notice for the
list as a whole, kept beside the data it describes.
