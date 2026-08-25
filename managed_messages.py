"""Turning a stored managed message into the embeds and view Discord needs.

Its own module rather than part of `dashboard_api` because both processes need
it: the dashboard's publish worker renders a row here, and so do the bot's own
`/setup_*` and `/rules_group`. A cog cannot import `dashboard_api` — that would
pull Flask, Waitress and the OAuth environment into a bot that may be running
with the dashboard as a separate service — and two renderers is exactly the
disagreement this replaces, where `/rules_group` sent one message with the guild
icon and an accept button while the dashboard sent one bare embed per section
with no view at all.
"""

import discord

from feature_access import is_enabled

# Which feature owns each kind of managed message. A page an operator cannot
# reach is better than a Post button that queues an action the worker refuses.
MANAGED_KIND_FEATURES = {
    "role_menu": "role_menus",
    "rules": "onboarding",
    "ticket": "tickets",
    "airlock": "onboarding",
    # A plain embed is not a feature anybody would switch off — it is the bot
    # saying something in a channel. `None` means no gate, which the renderer
    # reads directly rather than needing a special case.
    "embed": None,
}


# Discord's button-label limit. discord.py does not check it — it coerces to
# `str` and lets Discord answer 400 — so an over-long label would surface as an
# opaque outbox error after the operator had already pressed Post.
BUTTON_LABEL_LIMIT = 80


def stored_button_label(options) -> str | None:
    """The operator's button text, or `None` to mean "use the shipped one".

    Absent, present-but-blank and set are three different things, and only the
    third may override: a label saved as whitespace must fall back rather than
    render an unreadable button. Truncated defensively as well as validated at
    the API, because the failure this prevents happens after the queue.
    """
    label = (options or {}).get("button_label")
    if not isinstance(label, str) or not label.strip():
        return None
    return label.strip()[:BUTTON_LABEL_LIMIT]


def render_managed_message(guild, stored):
    """Turn one stored row into the embeds and the view Discord needs.

    One function for all four kinds, so the bot's own `/setup_*` and the
    dashboard's Post cannot render the same row differently — which is the
    failure the old publisher had: `/rules_group` sent one message with the
    guild icon and an accept button, and the dashboard sent one bare embed per
    section with no view at all.

    Returns `(embeds, view)`, or `(None, error_code)` when the feature that owns
    the kind is switched off — checked here rather than at queue time because a
    feature can be turned off while an action waits in the outbox.
    """
    kind = stored["kind"]
    feature = MANAGED_KIND_FEATURES.get(kind)
    if feature and not is_enabled(guild.id, feature):
        return None, "feature_disabled_or_invalid_panel"
    options = stored.get("options") or {}
    colour = (discord.Color(stored["colour"]) if stored.get("colour") is not None
              else discord.Color(0xF5B041))

    # Both kinds that carry sections render the same way; they differ in what
    # goes *around* the embeds, which is the whole distinction between a rules
    # panel and a plain announcement.
    if kind in ("rules", "embed"):
        embeds = []
        for index, section in enumerate(options.get("sections") or []):
            embed = discord.Embed(
                title=(section.get("title") or None),
                # `\n` typed into a one-line form field is a newline, the same
                # substitution `/rules_group` has always made.
                description=str(section.get("body", "")).replace("\\n", "\n"),
                color=colour)
            if (index == 0 and kind == "rules"
                    and options.get("thumbnail", True) and guild.icon):
                embed.set_thumbnail(url=guild.icon.url)
            # A banner on the leading section, which is what `/rules_verify`
            # posts. Without it, adopting one of those messages would strip the
            # banner on the first Update — silently, which is the worst way for
            # a feature gap to show up.
            if index == 0 and options.get("image_url"):
                embed.set_image(url=options["image_url"])
            embeds.append(embed)
        if not embeds:
            return None, "feature_disabled_or_invalid_panel"
        # A plain embed has no button at all: it is the message and nothing else.
        if kind == "embed":
            return embeds, None
        view = None
        if options.get("accept_button", True):
            from cogs.admin import RuleAcceptView
            view = RuleAcceptView(label=stored_button_label(options))
        return embeds, view

    embed = discord.Embed(title=(stored.get("title") or None),
                          description=str(stored.get("body") or "").replace("\\n", "\n"),
                          color=colour)
    if kind == "role_menu":
        from cogs.roleselect import RoleMenuView
        if not stored["entries"]:
            return None, "managed_menu_empty"
        # The guild id is what makes this *this* guild's menu. Without it the
        # constructor builds the routing instance, which carries every guild's
        # labels.
        return [embed], RoleMenuView(guild.id, stored["menu_key"])
    if kind == "ticket":
        from cogs.tickets import TicketLauncher
        return [embed], TicketLauncher(label=stored_button_label(options))
    if kind == "airlock":
        from cogs.admin import EnterServerView
        return [embed], EnterServerView(label=stored_button_label(options))
    return None, "feature_disabled_or_unsupported"
