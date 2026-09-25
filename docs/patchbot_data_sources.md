# Patchbot data sources

Measured state, not reference material — re-probe before trusting a row
here, the same rule `docs/localization_status.md` and
`docs/everydle_data_updates.md` follow. Everything below was fetched live
against the real endpoint with plain `curl`/`urllib` requests, no
authentication beyond a `User-Agent` header where the target required
one — **2026-09-17** for every source except Minecraft's article link,
re-probed **2026-09-24** (the sitemap finding below). This document exists
because this repo's rule is to probe an external source before planning
against it and record what was seen, not to reason about what an API
"should" look like.

Governs: `cogs/patchbot.py`.

- **Never trust a cached version string across a redesign.** Two of these
  sources (`minecraft.net`'s article grid, both Riot news pages) are
  server templates that can change shape without notice; the adapter must
  fail loud on an unexpected response shape rather than silently reporting
  "no patch."
- **A game's Steam page is not always its patch-notes channel.** Rainbow
  Six Siege and Wuthering Waves both mix official update posts with
  marketing/event posts and (for Wuthering Waves) third-party syndicated
  posts in the same feed — the adapter filters to `feedname ==
  "steam_community_announcements"` and still cannot guarantee every post
  is a patch rather than an event.
- **HoYoLAB's API is the same kind of source `everydle_sources.py` already
  relies on for Genshin/DbD/Valorant character data** — a real, first-party
  backend with no public contract, reverse-engineered by the community and
  used because no alternative official API exists. Treat a breaking change
  there as an outage of that adapter alone, not of patchbot as a whole.

## Steam News API (`ISteamNews/GetNewsForApp/v2`, official, no key)

One generic adapter, parameterized by `appid`. Verified live for six of the
eleven games. Response is `{"appnews": {"newsitems": [{"gid", "title",
"url", "author", "contents", "feedlabel", "date", "feedname"}, ...]}}`,
newest first. The adapter takes the first item where `feedname ==
"steam_community_announcements"`.

| Game | App ID | Verified | Notes |
|---|---|---|---|
| Dead by Daylight | 381210 | Yes | Clean official patch-notes titles (e.g. "10.2.0 \| PTB Patch Notes"). |
| Counter-Strike 2 | 730 | Yes | Clean official update posts. |
| Phasmophobia | 739630 | Yes | Titles literally contain "Patch Notes". |
| Rainbow Six Siege | 359550 | Yes | Top post at probe time was a limited-time event ("3v3 Arcade"), not a version bump — Ubisoft posts both event and patch content to the same feed; treated as "an official update," not strictly "a patch." |
| Wuthering Waves | 3513350 | Yes | Feed mixes official `steam_community_announcements` version posts ("New Content in Version 3.6: ...") with third-party syndicated posts (GamingOnLinux, CGMagazine) — **must** filter by `feedname`. |
| Zenless Zone Zero | 4162040 | Yes | Clean official version-update posts. |
| Honkai: Star Rail | — | **Not on Steam** | Steam store search (`storesearch` API) returns zero results for "Honkai Star Rail", "Star Rail", or "Honkai". Only "Honkai Impact 3rd" (appid 1671200, a different game) exists on Steam. This game needs the HoYoLAB adapter below, not Steam — the plan's original "Steam-backed, medium confidence" assumption was wrong and only caught by actually probing it. |

## Riot Games news pages (LoL, Valorant) — scraped, no official API

Riot has no public patch-notes API. Both `leagueoflegends.com` and
`playvalorant.com` (not `valorant.com` — that domain 404s) serve their
`/en-us/news/tags/patch-notes/` listing **server-rendered**, with a JSON
blob embedded in the HTML containing clean `{"title": ..., "url": ...}`
pairs for every article, newest first. This is more reliable than expected
— no headless browser needed, a single GET plus a JSON/regex extraction of
the embedded blob is enough.

| Game | URL | Verified | Title pattern | URL pattern |
|---|---|---|---|---|
| League of Legends | `https://www.leagueoflegends.com/en-us/news/tags/patch-notes/` | Yes | `League of Legends Patch {X}.{Y} Notes` | `/en-us/news/game-updates/league-of-legends-patch-{x}-{y}-notes` |
| Valorant | `https://playvalorant.com/en-us/news/tags/patch-notes/` | Yes | `VALORANT Patch Notes {X}.{Y}` | `/en-us/news/game-updates/valorant-patch-notes-{x}-{y}` |

Data Dragon's `versions.json` and `valorant-api.com/v1/version` were both
live and confirmed, but their version-numbering schemes **do not match**
what the news pages use (Data Dragon showed `16.18.1`-style versions while
the LoL news page's newest articles were `26.18`-style — a client/data
patch split, not the same axis), so building a URL by concatenating a
version number onto the slug pattern is unreliable. **The adapter reads
the newest article's title+URL directly off the scraped listing instead of
constructing one from a version API** — simpler and doesn't depend on two
numbering schemes staying in sync.

## HoYoLAB community API (Genshin Impact, Honkai: Star Rail)

Neither game has an official public API and neither is on Steam.
`https://bbs-api-os.hoyolab.com/community/post/wapi/getNewsList` (`gids`
game id, `type=1`, `page_size`) is the real backend HoYoLAB's own site and
app use — unofficial in the sense of undocumented, but first-party data,
same trust level as `genshin-db-api.vercel.app`/`dbd.tricky.lol` already in
use by `scripts/everydle_sources.py`. Requires a `User-Agent` and
`Origin`/`Referer` header; no API key. Verified live for both games.

| Game | `gids` | Verified | Article title heuristic | Permalink |
|---|---|---|---|---|
| Genshin Impact | 2 | Yes | Contains `"Version"` and (`"Update Details"` or `"What's New"`) — e.g. `"Everwinter Without Mercy" Version 7.0 Update Details` | `https://www.hoyolab.com/article/{post_id}` (confirmed HTTP 200) |
| Honkai: Star Rail | 6 | Yes | Same heuristic — e.g. `Version 4.5 "To Roll the Stars in Astropolis" Update Details` | `https://www.hoyolab.com/article/{post_id}` |

The title heuristic is not perfectly consistent across every historical
post (older Genshin posts used a different phrasing, `Version "Luna VIII"
Version Details - What's New`) — the adapter matches on the substring
`"Version"` plus one of a small set of known suffixes, and skips (does not
crash) a feed item it can't confidently classify as a version-update post,
scanning further down the `page_size` results rather than falling back to
the very first item unconditionally.

## Minecraft — the article slug, found via the site's own sitemap

`launchermeta.mojang.com/mc/game/version_manifest_v2.json` is live,
official, and gives an exact version id + `releaseTime` the moment a
release ships (confirmed: current versioning has moved to a date-based
scheme, e.g. `"26.3"`, not the old `1.21.x` style — another thing this
probe corrected rather than assumed). It has **no notes URL**.

The previously-assumed `minecraft.net` articles JSON endpoint
(`_jcr_content.articles.grid`) still 404s — confirmed still dead
2026-09-24, and it turned out to be genuinely legacy: the current site
bundle (`mc-components.min.js`) still defines it as `aemAPIs().legacyArticles`
but nothing calls it. `minecraft.net/en-us/articles` itself still returns
HTTP 200 with only four hero-section links in the raw HTML, its full grid
still loading client-side.

**Found instead: `www.minecraft.net/sitemap.xml` lists every article URL on
the site directly, with no JavaScript execution needed.** Probed
2026-09-24. It is not an index of sub-sitemaps, it is one flat 8.5 MB
`<urlset>`, and `/en-us/article/<slug>` (singular "article") appears
**3,011** times in it. A full release's announcement article is at:

```
https://www.minecraft.net/en-us/article/minecraft-java-edition-<id>
```

with every `.` in the manifest's `id` replaced by `-` — `26.3` →
`minecraft-java-edition-26-3`, `26.1.1` → `minecraft-java-edition-26-1-1`.
Confirmed live (HTTP 200) against five different releases spanning both
the current date-based scheme and the legacy one: `26.1`, `26.1.1`,
`26.1.2`, `26.2`, `26.3`, and `1.21.4`. The `java-edition` segment matters —
`minecraft-26-3` (no `java-edition`) 404s, and it is specific to full
releases: a snapshot/pre-release/release-candidate id needs a different
prefix (no `java-edition`) and, for pre-releases and release candidates,
its `-pre-`/`-rc-` segment expanded to `-pre-release-`/`-release-candidate-`
respectively (`26.3-rc-3` → `minecraft-26-3-release-candidate-3`, not
`minecraft-26-3-rc-3`) — but `minecraft_adapter` only ever tracks
`latest.release`, so the adapter itself never needs that expansion.

**This is a pattern read off the sitemap, not a contract Mojang documents,
so the adapter verifies before trusting it.** `_minecraft_article_exists`
sends one `HEAD` request to the guessed URL each tick and falls back to the
general `https://www.minecraft.net/en-us/updates` hub page if it does not
resolve — a wrong guess must degrade, never post a 404 to a guild. This
also covers the case where a release ships an instant before its article
does: that tick links the hub, and the next tick (the version has not
changed, so nothing re-posts) is moot since the adapter only fires once per
new version id — a late article means that one announcement is
lower-fidelity, not that it retries.
