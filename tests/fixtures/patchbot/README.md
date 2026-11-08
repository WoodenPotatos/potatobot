# Patchbot source recordings

Recorded live on 2026-11-06 from the endpoints `cogs/patchbot.py` reads
(`docs/patchbot_data_sources.md` has the probe notes). Trimmed by item count
only, every kept item whole: Steam to the first 5 news items, HoYoLAB to the
first 4 entries (Genshin) and all 10 the adapter asks for (HSR, re-recorded
the same day so its real notes sit behind the store notice), and each Riot page to its `__NEXT_DATA__` script with the
article grid cut to 3 cards and nothing
else kept from it: the rest of that script is the site's runtime
configuration, which carries Riot's public browser-monitoring token, and
gitleaks rightly refused it (2026-11-06). Re-record deliberately, never from memory, and
keep the tests' item-count floors honest.
