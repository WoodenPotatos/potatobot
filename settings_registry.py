"""Typed definitions used by bot gates, dashboard forms, and documentation."""

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

import item_catalog


class ApplyBehavior(str, Enum):
    LIVE = "live"
    SUBSYSTEM_RELOAD = "subsystem_reload"
    RESTART = "restart"


class SettingScope(str, Enum):
    INSTANCE = "instance"
    GUILD = "guild"


class SettingValueType(str, Enum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    STRING = "string"
    CHANNEL = "channel"
    CHANNEL_LIST = "channel_list"
    ROLE = "role"
    ROLE_LIST = "role_list"
    STRING_LIST = "string_list"
    JSON = "json"


class DataCategory(str, Enum):
    ECONOMY = "economy"
    PROFILE = "profile"
    GAME_STATS = "game_stats"
    MODERATION = "moderation"


class DataScopeType(str, Enum):
    GUILD = "guild"
    REALM = "realm"
    INSTANCE = "instance"


@dataclass(frozen=True)
class DataContext:
    """Resolved storage identity for one guild-originated data operation."""

    origin_guild_id: int
    category: DataCategory
    scope_type: DataScopeType
    scope_id: int


@dataclass(frozen=True)
class FeatureDefinition:
    key: str
    locale_key: str
    default: bool = True
    scope: SettingScope = SettingScope.GUILD
    dependencies: tuple[str, ...] = ()
    apply_behavior: ApplyBehavior = ApplyBehavior.LIVE
    value_type: type = bool
    sensitive: bool = False
    required_discord_permissions: tuple[str, ...] = ()
    # Presentation grouping for the dashboard switcher. It is declared rather
    # than inferred from `dependencies`, because every casino game and every
    # Everydle game depends on `economy` and would otherwise collapse into one
    # undifferentiated block. Each value needs a
    # `dashboard.feature_groups.<group>` locale key.
    group: str = "other"


@dataclass(frozen=True)
class SettingDefinition:
    """One safe dashboard-editable setting and its runtime contract."""

    key: str
    locale_key: str
    category: str
    page: str
    value_type: SettingValueType
    default: Any
    scope: SettingScope = SettingScope.GUILD
    owner_feature: str | None = None
    dependencies: tuple[str, ...] = ()
    apply_behavior: ApplyBehavior = ApplyBehavior.LIVE
    sensitive: bool = False
    required_discord_permissions: tuple[str, ...] = ("manage_guild",)
    minimum: int | None = None
    maximum: int | None = None
    legacy_path: tuple[str, ...] | None = None
    # Allowed values for a STRING setting. Empty means free text. A setting with
    # choices renders as a dropdown and is rejected on save if it is anything
    # else, so an operator cannot select a value the installation cannot honour.
    choices: tuple[str, ...] = ()
    # Which Discord channel kinds a CHANNEL/CHANNEL_LIST setting may name.
    # Empty means any. This is a presentation constraint that narrows the
    # dashboard's selector; it is deliberately not enforced on save, because
    # deciding a channel's type needs live Discord state and a settings save
    # must not start failing while Discord is unreachable.
    channel_types: tuple[str, ...] = ()
    # True when the bot has to *grant* this ROLE/ROLE_LIST, false when it only
    # recognises membership. The distinction decides whether a role above the
    # bot may be named: `premium_roles` and `admin_roles` are recognition, and
    # those roles are deliberately above the bot so it can never hand them out,
    # so filtering the selector by grantability made them unselectable and any
    # save silently dropped them. `permission_audit` derives its assignable set
    # from this field, so grantability is declared once.
    role_must_be_assignable: bool = False
    # What the *bot* must be able to do in the channel this setting names.
    # Empty falls back to the channel-kind default, which is right for an
    # ordinary destination and wrong for anything the bot manages: a ticket
    # category needs `manage_channels` and `manage_roles`, and asking every
    # category for them would report a false problem on an announcement parent.
    bot_channel_permissions: tuple[str, ...] = ()
    # What a *member* must be able to do there. This is the half no diagnostic
    # covered: the bot can post into a ticket channel while the member who
    # opened it cannot attach the screenshot the ticket exists for, and nothing
    # reported it because every check asked only about the bot. Checked against
    # `@everyone`, which is the floor every member stands on.
    member_channel_permissions: tuple[str, ...] = ()

    def public_dict(self) -> dict:
        data = asdict(self)
        data["value_type"] = self.value_type.value
        data["scope"] = self.scope.value
        data["apply_behavior"] = self.apply_behavior.value
        data["legacy_path"] = list(self.legacy_path) if self.legacy_path else None
        data["channel_types"] = list(self.channel_types)
        data["choices"] = list(self.choices)
        data["bot_channel_permissions"] = list(self.bot_channel_permissions)
        data["member_channel_permissions"] = list(self.member_channel_permissions)
        return data


def _feature(key: str, group: str, *, dependencies: tuple[str, ...] = (),
             default: bool = True,
             permissions: tuple[str, ...] = ()) -> FeatureDefinition:
    return FeatureDefinition(
        key=key,
        locale_key=f"dashboard.features.{key}",
        dependencies=dependencies,
        default=default,
        group=group,
        required_discord_permissions=permissions,
    )


# The order a dashboard renders groups in. Anything not listed sorts last, so
# adding a feature with a new group is visible rather than silently hidden.
FEATURE_GROUP_ORDER = (
    "core", "community", "onboarding", "economy", "casino", "everydle",
    "rewards", "moderation", "media", "socials", "other",
)


FEATURE_DEFINITIONS = {
    definition.key: definition
    for definition in (
        _feature("general", "core",
                 permissions=("send_messages", "embed_links",
                              "read_message_history")),
        _feature("lfg", "community",
                 permissions=("send_messages", "embed_links",
                              "mention_everyone")),
        _feature("profiles", "community", permissions=("embed_links",)),
        _feature("levels", "community", dependencies=("profiles",),
                 permissions=("embed_links", "manage_roles")),
        _feature("economy", "economy",
                 permissions=("send_messages", "embed_links",
                              "external_emojis")),
        _feature("shop", "economy", dependencies=("economy",),
                 permissions=("manage_roles",)),
        _feature("shop_gacha", "economy", dependencies=("economy", "shop"),
                 default=False,
                 permissions=("manage_roles", "manage_expressions")),
        _feature("rentals", "economy",
                 dependencies=("economy", "shop", "tickets"),
                 permissions=("manage_expressions",)),
        _feature("casino_blackjack", "casino", dependencies=("economy",),
                 permissions=("embed_links",)),
        _feature("casino_dice", "casino", dependencies=("economy",),
                 permissions=("embed_links",)),
        _feature("casino_roulette", "casino", dependencies=("economy",),
                 permissions=("embed_links",)),
        _feature("casino_slots", "casino", dependencies=("economy",),
                 permissions=("embed_links",)),
        _feature("casino_mines", "casino", dependencies=("economy",),
                 permissions=("embed_links",)),
        _feature("everydle_loldle", "everydle", dependencies=("economy",),
                 permissions=("embed_links", "attach_files")),
        _feature("everydle_valdle", "everydle", dependencies=("economy",),
                 permissions=("embed_links", "attach_files")),
        _feature("everydle_dbdle", "everydle", dependencies=("economy",),
                 permissions=("embed_links", "attach_files")),
        _feature("music", "media",
                 permissions=("connect", "speak", "embed_links")),
        _feature("moderation", "moderation",
                 permissions=("kick_members", "ban_members",
                              "moderate_members", "manage_messages")),
        _feature("factions", "moderation", dependencies=("moderation",),
                 permissions=("manage_roles",)),
        _feature("tickets", "community",
                 permissions=("manage_channels", "manage_messages",
                              "embed_links")),
        _feature("role_menus", "community",
                 permissions=("manage_roles", "embed_links",
                              "external_emojis")),
        _feature("onboarding", "onboarding",
                 permissions=("manage_roles", "embed_links")),
        _feature("temporary_voice", "community",
                 permissions=("manage_channels", "move_members", "connect")),
        _feature("chat_rewards", "rewards",
                 dependencies=("economy", "levels")),
        _feature("voice_rewards", "rewards",
                 dependencies=("economy", "levels")),
        _feature("inactivity", "moderation",
                 permissions=("kick_members", "manage_roles")),
        _feature("member_announcements", "community",
                 permissions=("send_messages", "embed_links")),
        _feature("social_twitch", "socials",
                 permissions=("send_messages", "embed_links",
                              "mention_everyone")),
        _feature("social_youtube", "socials",
                 permissions=("send_messages", "embed_links",
                              "mention_everyone")),
    )
}


# Languages an operator may select. A language belongs here only once its
# general catalog and the minigame catalogs it needs are complete; the ones that
# are not are still shipped as files so a translator has somewhere to work.
SUPPORTED_LANGUAGES = ("hu", "en")

# Channel kinds a message can be posted into, as reported by both resource
# sources. `news` is an announcement channel, which behaves as text for us.
TEXT_CHANNEL_TYPES = ("text", "news")
VOICE_CHANNEL_TYPES = ("voice", "stage_voice")
CATEGORY_CHANNEL_TYPES = ("category",)


def _setting(key: str, category: str, page: str, value_type: SettingValueType,
             default, *, feature: str | None = None, legacy_path=None,
             minimum=None, maximum=None, apply=ApplyBehavior.LIVE,
             channel_types: tuple[str, ...] = (), choices: tuple[str, ...] = (),
             assignable_role: bool = False,
             bot_permissions: tuple[str, ...] = (),
             member_permissions: tuple[str, ...] = ()):
    return SettingDefinition(
        key=key,
        locale_key=f"dashboard.settings.{key}",
        category=category,
        page=page,
        value_type=value_type,
        default=default,
        owner_feature=feature,
        legacy_path=tuple(legacy_path) if legacy_path else None,
        minimum=minimum,
        maximum=maximum,
        apply_behavior=apply,
        channel_types=channel_types,
        choices=choices,
        role_must_be_assignable=assignable_role,
        bot_channel_permissions=bot_permissions,
        member_channel_permissions=member_permissions,
    )


# Deployment credentials, bind addresses, secrets, database paths, and hard
# security limits are deliberately absent. Complex role-menu/faction definitions
# use constrained JSON until their dedicated builders replace the legacy shape.
SETTING_DEFINITIONS = {
    definition.key: definition
    for definition in (
        # Only languages this installation can actually speak. A blank catalog
        # value is not a fallback — `t()` treats it as a miss and degrades to
        # Hungarian — but a blank *minigame* entity name disables that minigame
        # outright, so an incomplete language is a broken installation rather
        # than a partly translated one. `tests/test_locale_coverage.py` fails if
        # a language listed here is not complete enough to select.
        _setting("language", "administration", "instance", SettingValueType.STRING,
                 "hu", legacy_path=("bot_settings", "language"),
                 apply=ApplyBehavior.SUBSYSTEM_RELOAD,
                 choices=SUPPORTED_LANGUAGES),
        # The symbol every balance, price and payout is printed with. It was
        # hard-coded as one guild's custom emoji in 105 places, which meant every
        # other installation rendered the raw `<:potatocoins:1489…>` text. The
        # default has to be a Unicode emoji: a custom one cannot exist in a guild
        # the bot has never joined. `t()` substitutes it into `{coin}`, so this is
        # read per message and applies live.
        _setting("currency_emoji", "administration", "instance",
                 SettingValueType.STRING, "🥔",
                 legacy_path=("bot_settings", "currency_emoji")),
        _setting("maintenance", "administration", "instance", SettingValueType.BOOLEAN,
                 False, legacy_path=("bot_settings", "maintenance")),
        # discord.py binds the prefix when the bot object is constructed, so this
        # takes effect on restart. Every prefix-only operator command moves with
        # it, which is why it is declared rather than left hard-coded.
        _setting("command_prefix", "administration", "instance",
                 SettingValueType.STRING, "?",
                 legacy_path=("bot_settings", "prefix"),
                 apply=ApplyBehavior.RESTART),
        # Days of inactivity after which a departed member's data is erased.
        # 0 retains indefinitely, so upgrading changes nothing until an operator
        # opts in. No legacy_path: retention has never lived in config.json.
        _setting("data_retention_days", "administration", "instance",
                 SettingValueType.INTEGER, 0, minimum=0, maximum=3650),
        _setting("economy_channels", "economy", "rewards", SettingValueType.CHANNEL_LIST,
                 [], feature="economy", legacy_path=("channels", "economy"),
                 channel_types=TEXT_CHANNEL_TYPES,
                 member_permissions=('view_channel', 'send_messages', 'use_application_commands')),
        _setting("levels_channels", "community", "levels", SettingValueType.CHANNEL_LIST,
                 [], feature="levels", legacy_path=("channels", "levels"),
                 channel_types=TEXT_CHANNEL_TYPES,
                 member_permissions=('view_channel',)),
        # The per-guild `No. 1` role. It resolved only through a config.json key
        # that nothing could write, so it was the one role a dashboard operator
        # could not set at all.
        _setting("top_ranker_role", "community", "levels", SettingValueType.ROLE,
                 None, feature="levels", legacy_path=("roles", "top_ranker"), assignable_role=True),
        _setting("general_channels", "community", "general", SettingValueType.CHANNEL_LIST,
                 [], feature="general", legacy_path=("channels", "general"),
                 channel_types=TEXT_CHANNEL_TYPES,
                 member_permissions=('view_channel', 'send_messages', 'use_application_commands')),
        _setting("join_channel", "community", "announcements", SettingValueType.CHANNEL,
                 None, feature="member_announcements", legacy_path=("channels", "join"),
                 channel_types=TEXT_CHANNEL_TYPES,
                 member_permissions=('view_channel',)),
        _setting("leave_channel", "community", "announcements", SettingValueType.CHANNEL,
                 None, feature="member_announcements", legacy_path=("channels", "leave"),
                 channel_types=TEXT_CHANNEL_TYPES,
                 member_permissions=('view_channel',)),
        _setting("booster_channel", "community", "announcements", SettingValueType.CHANNEL,
                 None, feature="member_announcements", legacy_path=("channels", "booster"),
                 channel_types=TEXT_CHANNEL_TYPES,
                 member_permissions=('view_channel',)),
        _setting("everydle_channel", "games", "everydle", SettingValueType.CHANNEL,
                 None, legacy_path=("channels", "everydle"),
                 channel_types=TEXT_CHANNEL_TYPES,
                 member_permissions=('view_channel', 'send_messages', 'use_application_commands')),
        _setting("other_games_channel", "games", "general", SettingValueType.CHANNEL,
                 None, legacy_path=("channels", "other_games_channel"),
                 channel_types=TEXT_CHANNEL_TYPES,
                 member_permissions=('view_channel', 'send_messages', 'use_application_commands')),
        _setting("ticket_category", "community", "tickets", SettingValueType.CHANNEL,
                 None, feature="tickets", legacy_path=("channels", "ticket_category"),
                 channel_types=CATEGORY_CHANNEL_TYPES,
                 bot_permissions=('view_channel', 'manage_channels', 'manage_roles', 'attach_files', 'read_message_history')),
        _setting("ticket_logs", "community", "tickets", SettingValueType.CHANNEL,
                 None, feature="tickets", legacy_path=("channels", "ticket_logs"),
                 channel_types=TEXT_CHANNEL_TYPES,
                 bot_permissions=('view_channel', 'send_messages', 'embed_links', 'attach_files')),
        _setting("admin_category", "administration", "logging", SettingValueType.CHANNEL,
                 None, legacy_path=("channels", "admin_category"),
                 channel_types=CATEGORY_CHANNEL_TYPES),
        _setting("bot_log_channel", "administration", "logging", SettingValueType.CHANNEL,
                 None, legacy_path=("channels", "bot_log"),
                 channel_types=TEXT_CHANNEL_TYPES),
        _setting("temporary_voice_lobbies", "community", "voice", SettingValueType.CHANNEL_LIST,
                 [], feature="temporary_voice", legacy_path=("channels", "join_to_create"),
                 channel_types=VOICE_CHANNEL_TYPES,
                 bot_permissions=('view_channel', 'connect', 'manage_channels', 'manage_roles', 'move_members')),
        _setting("admin_roles", "administration", "permissions", SettingValueType.ROLE_LIST,
                 [], legacy_path=("roles", "admin")),
        _setting("premium_roles", "economy", "premium", SettingValueType.ROLE_LIST,
                 [], feature="economy", legacy_path=("roles", "premium")),
        _setting("premium_role", "economy", "premium", SettingValueType.ROLE,
                 None, feature="shop", legacy_path=("roles", "premium_role"), assignable_role=True),
        _setting("member_role", "community", "onboarding", SettingValueType.ROLE,
                 None, feature="onboarding", legacy_path=("roles", "member"), assignable_role=True),
        _setting("onboarding_role", "community", "onboarding", SettingValueType.ROLE,
                 None, feature="onboarding", legacy_path=("roles", "onboarding"), assignable_role=True),
        _setting("autoroles", "community", "onboarding", SettingValueType.ROLE_LIST,
                 [], feature="onboarding", legacy_path=("roles", "autoroles"), assignable_role=True),
        _setting("ignored_users", "moderation", "inactivity", SettingValueType.STRING_LIST,
                 [], feature="inactivity", legacy_path=("roles", "ignored_users")),
        # Level milestone -> the role to grant. A value may be a role id or a
        # role name: `check_level_roles` has always accepted both, and the name
        # path is retained for an installation that configured neither. Ids are
        # what an operator should use, because a renamed role breaks a name and
        # does not break an id.
        #
        # The default is empty and must stay empty. It used to hold the private
        # deployment's own nine role ids, which meant every copy of the bot
        # shipped one guild's snowflakes — the same leak the `/work` responses
        # had. A role id cannot be guessed for somebody else's guild, so there is
        # no honest default; `docs/level_setup.md` documents the ladder instead.
        _setting("level_roles", "community", "levels", SettingValueType.JSON,
                 {}, feature="levels", legacy_path=("level_roles",)),
        _setting("game_roles", "community", "role_menus", SettingValueType.JSON,
                 {}, feature="role_menus", legacy_path=("game_roles",)),
        _setting("news_roles", "community", "role_menus", SettingValueType.JSON,
                 {}, feature="role_menus", legacy_path=("news_roles",)),
        _setting("theme_roles", "community", "role_menus", SettingValueType.JSON,
                 {}, feature="role_menus", legacy_path=("themes_roles",)),
        _setting("factions", "moderation", "factions", SettingValueType.JSON,
                 {}, feature="factions", legacy_path=("factions",)),
        _setting("lfg_channels", "community", "lfg", SettingValueType.JSON,
                 {}, feature="lfg", legacy_path=("lfg_channels",)),
        _setting("social_notification_channel", "community", "socials", SettingValueType.CHANNEL,
                 None, legacy_path=("socials", "notification_channel"),
                 channel_types=TEXT_CHANNEL_TYPES,
                 member_permissions=('view_channel',)),
        _setting("twitch_role", "community", "socials", SettingValueType.ROLE,
                 None, feature="social_twitch", legacy_path=("socials", "twitch_role_id")),
        _setting("youtube_role", "community", "socials", SettingValueType.ROLE,
                 None, feature="social_youtube", legacy_path=("socials", "youtube_role_id")),
        _setting("twitch_streamers", "community", "socials", SettingValueType.STRING_LIST,
                 [], feature="social_twitch", legacy_path=("socials", "twitch_streamers")),
        # `/work` outcome rarity and payouts. The three tier weights are drawn
        # against each other, so the shipped 998/1/1 reproduces the previous
        # hard-coded one-in-a-thousand chances exactly.
        _setting("work_tier_normal_weight", "economy", "work",
                 SettingValueType.INTEGER, 998, feature="economy",
                 minimum=0, maximum=1000000),
        _setting("work_tier_free_weight", "economy", "work",
                 SettingValueType.INTEGER, 1, feature="economy",
                 minimum=0, maximum=1000000),
        _setting("work_tier_high_weight", "economy", "work",
                 SettingValueType.INTEGER, 1, feature="economy",
                 minimum=0, maximum=1000000),
        _setting("work_payout_min", "economy", "work", SettingValueType.INTEGER,
                 500, feature="economy", minimum=0, maximum=1000000000),
        _setting("work_payout_max", "economy", "work", SettingValueType.INTEGER,
                 3000, feature="economy", minimum=0, maximum=1000000000),
        _setting("work_high_payout_min", "economy", "work",
                 SettingValueType.INTEGER, 10000, feature="economy",
                 minimum=0, maximum=1000000000),
        _setting("work_high_payout_max", "economy", "work",
                 SettingValueType.INTEGER, 30000, feature="economy",
                 minimum=0, maximum=1000000000),
        _setting("work_xp_normal", "economy", "work", SettingValueType.INTEGER,
                 25, feature="economy", minimum=0, maximum=1000000),
        _setting("work_xp_free", "economy", "work", SettingValueType.INTEGER,
                 50, feature="economy", minimum=0, maximum=1000000),
        _setting("work_xp_high", "economy", "work", SettingValueType.INTEGER,
                 5, feature="economy", minimum=0, maximum=1000000),
        _setting("gacha_roll_cost", "economy", "gacha", SettingValueType.INTEGER,
                 5000, feature="shop_gacha", minimum=1, maximum=100000000),
        _setting("gacha_hard_pity", "economy", "gacha", SettingValueType.INTEGER,
                 100, feature="shop_gacha", minimum=1, maximum=1000),
        _setting("gacha_soft_pity_start", "economy", "gacha", SettingValueType.INTEGER,
                 75, feature="shop_gacha", minimum=0, maximum=999),
        _setting("gacha_soft_pity_multiplier", "economy", "gacha", SettingValueType.INTEGER,
                 3, feature="shop_gacha", minimum=1, maximum=20),
        _setting("gacha_four_star_guarantee_interval", "economy", "gacha",
                 SettingValueType.INTEGER, 10, feature="shop_gacha",
                 minimum=1, maximum=1000),
        _setting("gacha_duplicate_percent", "economy", "gacha", SettingValueType.INTEGER,
                 10, feature="shop_gacha", minimum=0, maximum=100),
    )
}

# Prices are registered from the shared item catalog, so adding a built-in item
# there gives it a dashboard price field without a second list to keep in step.
for _item_key, _default_price in item_catalog.shop_default_prices().items():
    definition = _setting(
        f"shop_price_{_item_key}", "economy", "shop", SettingValueType.INTEGER,
        _default_price, feature="shop", minimum=0, maximum=1000000000,
    )
    SETTING_DEFINITIONS[definition.key] = definition

for _activity_key, (_coin, _xp) in {
    "loldle_easy": (2500, 100), "loldle_medium": (5000, 100),
    "loldle_hard": (7500, 150), "valdle": (5000, 100), "dbdle": (5000, 100),
    "daily_normal": (5000, 50), "daily_premium": (10000, 50),
    "chat_message": (5, 2), "voice_minute_normal": (5, 5),
    "voice_minute_premium": (10, 10),
}.items():
    _category = "games" if _activity_key.startswith(("loldle", "valdle", "dbdle")) else "economy"
    for _reward_type, _default in (("coin", _coin), ("xp", _xp)):
        definition = _setting(
            f"reward_{_activity_key}_{_reward_type}", _category, "rewards",
            SettingValueType.INTEGER, _default, feature="economy", minimum=0,
            maximum=1000000000,
        )
        SETTING_DEFINITIONS[definition.key] = definition


def _snowflake(value) -> int:
    """Accept a Discord id as an integer or as a decimal string, return an integer.

    A string has to be accepted because the browser cannot hold one exactly: a
    snowflake is 64-bit and a JavaScript number carries 53 bits, so
    `Number("1420070400000000001")` silently becomes ...200. The dashboard
    therefore sends ids as strings, the way Discord's own API does, and this is
    where they become integers again — storage stays integer, so nothing the bot
    reads changes shape.
    """
    if isinstance(value, bool):
        raise ValueError("setting must be a Discord snowflake")
    if isinstance(value, str):
        if not value.isdigit():
            raise ValueError("setting must be a Discord snowflake")
        value = int(value)
    if not isinstance(value, int) or value <= 0:
        raise ValueError("setting must be a Discord snowflake")
    return value


def legacy_config_value(definition: SettingDefinition, config: dict):
    """What `config.json` holds for a setting, else the registry default.

    The walk lived in three places — the runtime resolver, the dashboard's
    settings read, and nowhere at all in the permission audit, which is why the
    audit silently checked no channels: it fell back to the registry default,
    and the default for a channel is empty. One copy, so a fourth caller cannot
    disagree with the other three about where a value comes from.
    """
    if not definition.legacy_path:
        return definition.default
    node = config
    for part in definition.legacy_path:
        if not isinstance(node, dict) or part not in node:
            return definition.default
        node = node[part]
    return node


def validate_setting_value(definition: SettingDefinition, value):
    kind = definition.value_type
    if kind is SettingValueType.BOOLEAN and not isinstance(value, bool):
        raise ValueError("setting must be boolean")
    if kind is SettingValueType.INTEGER:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("setting must be integer")
        if definition.minimum is not None and value < definition.minimum:
            raise ValueError("setting is below minimum")
        if definition.maximum is not None and value > definition.maximum:
            raise ValueError("setting is above maximum")
    if kind is SettingValueType.STRING:
        if not isinstance(value, str):
            raise ValueError("setting must be string")
        if definition.choices and value not in definition.choices:
            raise ValueError("setting must be one of the allowed values")
    if kind in {SettingValueType.CHANNEL, SettingValueType.ROLE}:
        value = None if value is None else _snowflake(value)
    if kind in {SettingValueType.CHANNEL_LIST, SettingValueType.ROLE_LIST}:
        if not isinstance(value, list):
            raise ValueError("setting must be a list of Discord snowflakes")
        value = [_snowflake(item) for item in value]
    if kind is SettingValueType.STRING_LIST and (
        not isinstance(value, list) or any(not isinstance(item, str) for item in value)
    ):
        raise ValueError("setting must be a string list")
    if kind is SettingValueType.JSON and not isinstance(value, (dict, list)):
        raise ValueError("setting must be JSON object or list")
    return value


def validate_feature_key(feature_key: str) -> FeatureDefinition:
    """Return a known definition or reject unregistered external input."""
    try:
        return FEATURE_DEFINITIONS[feature_key]
    except KeyError as exc:
        raise ValueError(f"unknown feature key: {feature_key}") from exc


def validate_feature_state(feature_key: str, enabled: bool, states: dict[str, bool]):
    """Reject enabling a feature while one of its dependencies is disabled."""
    definition = validate_feature_key(feature_key)
    if not isinstance(enabled, bool):
        raise ValueError("feature state must be a boolean")
    if not enabled:
        return
    missing = [key for key in definition.dependencies if states.get(key) is False]
    if missing:
        raise ValueError(
            f"feature {feature_key} requires enabled dependencies: {', '.join(missing)}"
        )
