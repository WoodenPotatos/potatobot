"""The one place the running version comes from.

`pyproject.toml` holds the version and nothing else does. It used to live in
`config.json` as well, as an operator-editable setting, and the two drifted
exactly as you would expect: the file said 1.9 while the packaging metadata said
2.0.0rc1. A version an operator can type is a version that lies, so this reads
the packaging metadata and offers no way to override it.

Installed distributions answer from `importlib.metadata`. A source checkout —
which is how the bot is actually deployed — has no distribution to ask, so the
`pyproject.toml` beside this file is parsed instead. Both paths are tried before
giving up, and the result is cached because the answer cannot change while the
process runs.

The release channel is *derived* from the version rather than configured, so a
build cannot claim to be stable while carrying a prerelease suffix.
"""

import re
import tomllib
from functools import lru_cache
from pathlib import Path

# The public repository. `/version` shows it so an operator can find the source,
# the issue tracker and the changelog of the build they are actually running.
REPOSITORY_URL = "https://github.com/WoodenPotatos/potatobot"

CHANNEL_ALPHA = "alpha"
CHANNEL_BETA = "beta"
CHANNEL_STABLE = "stable"

# PEP 440 spells a prerelease `2.1.0a4`, `2.1.0b2`, `2.1.0rc1`. A release
# candidate is a beta as far as an operator is concerned: it is public, it is
# not stable, and calling it a fourth channel would only be a channel nobody
# subscribes to.
_PRERELEASE = re.compile(r"^(?P<release>\d+\.\d+\.\d+)"
                         r"(?:(?P<kind>a|b|rc)(?P<serial>\d+))?$")

_KIND_CHANNEL = {"a": CHANNEL_ALPHA, "b": CHANNEL_BETA, "rc": CHANNEL_BETA}
_KIND_LABEL = {"a": "alpha", "b": "beta", "rc": "rc"}

_PYPROJECT = Path(__file__).resolve().parent / "pyproject.toml"

# Used only when neither source can be read, which means a broken deployment
# rather than an unreleased one. It is deliberately not a plausible version.
UNKNOWN_VERSION = "0.0.0"


def _from_metadata() -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError, version as installed
    except ImportError:
        return None
    try:
        return installed("potatobot")
    except PackageNotFoundError:
        return None
    except Exception:
        # Metadata can be malformed on a half-installed environment. A version
        # lookup must never be the reason the bot fails to start.
        return None


def _from_pyproject() -> str | None:
    try:
        with open(_PYPROJECT, "rb") as handle:
            return tomllib.load(handle).get("project", {}).get("version")
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        return None


@lru_cache(maxsize=1)
def raw_version() -> str:
    """The version as PEP 440 spells it, e.g. `2.1.0b2`."""
    return _from_metadata() or _from_pyproject() or UNKNOWN_VERSION


def channel_for(raw: str) -> str:
    """Which release channel a version string belongs to.

    Derived, never declared: a build carrying `b2` is a beta whatever anybody
    labelled it, and an unparseable version is treated as alpha because the
    cautious answer is the one that does not claim stability.
    """
    match = _PRERELEASE.match(raw or "")
    if match is None:
        return CHANNEL_ALPHA
    kind = match.group("kind")
    return _KIND_CHANNEL.get(kind, CHANNEL_STABLE) if kind else CHANNEL_STABLE


def display_for(raw: str) -> str:
    """A human-facing version: `2.1.0b2` reads as `2.1.0-beta.2`.

    PEP 440 is what packaging tools need; it is not what an operator wants to
    read in an embed or quote in a bug report.
    """
    match = _PRERELEASE.match(raw or "")
    if match is None:
        return raw or UNKNOWN_VERSION
    kind = match.group("kind")
    if not kind:
        return match.group("release")
    return f"{match.group('release')}-{_KIND_LABEL[kind]}.{int(match.group('serial'))}"


def version_display() -> str:
    """The version to show a person."""
    return display_for(raw_version())


def release_channel() -> str:
    """The channel this build belongs to."""
    return channel_for(raw_version())
