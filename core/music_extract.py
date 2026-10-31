"""One music lookup, in a process of its own (route C, 2.19).

`cogs/music.py` runs this file with `python -I`, writes `{"source", "options"}`
as JSON to its stdin, and reads yt-dlp's sanitised result as JSON from its
stdout. A lookup used to run on a thread, and a thread that wedged (a hanging
DNS resolution is outside yt-dlp's `socket_timeout`) could not be stopped, only
abandoned until the next restart. A process can be killed, which is the whole
reason this is a separate file.

Self-contained on purpose: `-I` keeps the project off `sys.path`, the parent
hands it an environment without the bot's secrets, and it imports nothing but
the standard library and yt-dlp. On failure it writes only the exception's
type to stderr, never its text, which can carry the member's search.
"""

import json
import sys


class _Silent:
    """yt-dlp's logger. `quiet` still prints errors to stderr, and an error
    names the video or the search, so nothing it says may leave this process."""

    def debug(self, message):
        pass

    info = warning = error = debug


def extract(request: dict) -> dict:
    import yt_dlp

    options = dict(request["options"])
    options["logger"] = _Silent()
    with yt_dlp.YoutubeDL(options) as extractor:
        info = extractor.extract_info(request["source"], download=False)
        # Generators, sets and other non-JSON values become plain data here.
        return extractor.sanitize_info(info)


def main() -> int:
    try:
        result = extract(json.load(sys.stdin))
    except Exception as exc:  # noqa: BLE001 - reported to the parent by kind only
        sys.stderr.write(type(exc).__name__)
        return 1
    json.dump(result, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
