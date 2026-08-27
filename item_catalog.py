"""One definition of what a built-in economy item *is*.

The Shop and Potato Gacha are two ways to obtain the same goods, so the item's
identity — its stable key and the effect it applies — must live in exactly one
place. Each system keeps its own acquisition rules on top of that shared
identity: a duplicate vault refuses a shop purchase and charges nothing, while
the same duplicate from a pull pays the banner's configured compensation.

Before this module the item list was written out four times (``SHOP_DEFAULTS``,
the shop cog's catalog builder, the settings registry's price loop, and the
dashboard's consumable validator), which is how the gacha ended up with a
``lockpick`` backed by a different column than the shop's and with vault tiers
under keys the shop had never heard of.

Stdlib only and no project imports, so ``database``, ``settings_registry``,
``dashboard_api`` and the cogs can all depend on it without a cycle.
"""

from dataclasses import dataclass
from enum import Enum


class ItemEffect(str, Enum):
    """What an item does once a member owns it, independent of how it arrived."""

    # A stackable, guild-local ``user_inventory`` row consumed by a later action.
    INVENTORY = "inventory"
    # A fixed protected reserve on ``users.protected_reserve``.
    VAULT = "vault"
    # A permanent Discord role named by a ``config.json`` roles key.
    ROLE = "role"
    # Timed robbery defence on ``users.rob_defense``/``bodyguard_until``.
    BODYGUARD = "bodyguard"
    # Opens a staff fulfillment ticket for a rented Discord asset.
    RENT_TICKET = "rent_ticket"


class ItemCategory(str, Enum):
    """Which shelf of the shop an item sits on.

    The shop menu is one Discord select and a select holds 25 options, so a flat
    menu made the whole shop 25 items — 17 of them built in, leaving an operator
    eight. Splitting the menu by category makes the ceiling 25 *per shelf*, so a
    built-in we add only ever costs a slot in its own section.

    **Declaration order is display order**, so there is no second list to keep in
    step — the property `FEATURE_GROUP_ORDER` exists to provide for features.

    A category is **declared per item and never derived from `ItemEffect`**. Two
    of the assignments prove why: `bodyguard` carries BODYGUARD and belongs with
    the vaults, because to a member buying one it answers the same question; and
    `streak_freeze` carries INVENTORY but is spent by an Everydle claim, not by a
    wager, so grouping by effect would file the streak protector next to the
    blackjack helper. That is the `FeatureDefinition.group` lesson — grouping by
    a related field collapses it wrongly — one module over.
    """

    # A standing benefit rather than a single use: membership, a streak.
    PERKS = "perks"
    # Spent by one paid casino round, win or lose.
    CASINO = "casino"
    # Offence in a robbery.
    HEIST = "heist"
    # Defence against one: the bodyguard and the vaults.
    PROTECTION = "protection"
    # Opens a staff ticket for a rented Discord asset.
    RENTALS = "rentals"


#: Display order, and the order the section menu is built in.
SHOP_CATEGORY_ORDER: tuple[ItemCategory, ...] = tuple(ItemCategory)

#: Stored value -> member, for validating what a browser sent.
_CATEGORY_VALUES: dict[str, ItemCategory] = {c.value: c for c in ItemCategory}

#: A Discord select holds this many options, and that binds twice over: the
#: section menu cannot offer more sections than this, and a section cannot offer
#: more items. Derived from the platform, not chosen. `cogs/shop.py` and
#: `dashboard_api.py` both read it from here, because the per-section cap is now
#: computed in two processes that have to agree exactly.
SELECT_OPTION_LIMIT = 25


@dataclass(frozen=True)
class ItemDefinition:
    """One built-in item.

    ``shop_price`` of ``None`` means the item exists but is not sold, and
    ``gacha_kind`` of ``None`` means it cannot appear in a banner. The two are
    independent on purpose: availability is a per-system decision, identity is
    not.
    """

    key: str
    effect: ItemEffect
    # Which shelf this sits on. Required and undefaulted on purpose: every
    # definition passes `key` and `effect` positionally, so adding a field here
    # without a default means all of them must name a category or the module
    # fails to import. That is "every built-in has a category" enforced before
    # any test runs; a default would let the next item land silently in a bucket.
    category: ItemCategory
    # Reserve size for a vault, roles key for a role, bonus for a consumable.
    value: int | float | str | None = None
    shop_price: int | None = None
    # The reward kind the gacha config uses for this item, when it is drawable.
    gacha_kind: str | None = None
    ticket_prefix: str | None = None
    limit_check: str | None = None

    @property
    def sold_in_shop(self) -> bool:
        return self.shop_price is not None

    @property
    def drawable_in_gacha(self) -> bool:
        return self.gacha_kind is not None


def _definitions() -> tuple[ItemDefinition, ...]:
    return (
        ItemDefinition("premium", ItemEffect.ROLE, ItemCategory.PERKS, value="premium_role",
                       shop_price=300000),
        # A lockpick is one inventory row in both systems. The legacy
        # ``users.rob_bonus`` column is no longer written by any purchase; it is
        # only read out and cleared for members who bought one before this
        # change, and has no guild dimension to migrate into.
        ItemDefinition("lockpick", ItemEffect.INVENTORY, ItemCategory.HEIST, value=0.15,
                       shop_price=5000, gacha_kind="item"),
        ItemDefinition("loaded_die", ItemEffect.INVENTORY, ItemCategory.CASINO,
                       shop_price=10000, gacha_kind="item"),
        ItemDefinition("vault_glove", ItemEffect.INVENTORY, ItemCategory.HEIST, value=0.25,
                       shop_price=75000, gacha_kind="item"),
        # One extra forgiven day on an Everydle streak, spent retroactively by
        # the next claim rather than by a scheduled job. `value` is that number
        # of days, so the mechanic reads from the catalog like every other one.
        ItemDefinition("streak_freeze", ItemEffect.INVENTORY, ItemCategory.PERKS, value=1,
                       shop_price=60000, gacha_kind="item"),
        # Casino helpers. Each one is the loaded die's bargain in a different
        # game: it improves one paid round and is spent by it, win or lose. They
        # are plain INVENTORY items, so the shop purchase path, the gacha "item"
        # grant and the dashboard's consumable validation all pick them up from
        # this declaration with no further wiring. `value` is the mechanic's own
        # number where it has one.
        ItemDefinition("stacked_deck", ItemEffect.INVENTORY, ItemCategory.CASINO,
                       shop_price=12000, gacha_kind="item"),
        ItemDefinition("lucky_charm", ItemEffect.INVENTORY, ItemCategory.CASINO,
                       shop_price=10000, gacha_kind="item"),
        # Reveals this many safe tiles before a minesweeper round starts.
        ItemDefinition("metal_detector", ItemEffect.INVENTORY, ItemCategory.CASINO, value=1,
                       shop_price=14000, gacha_kind="item"),
        # One wrong call in higher-or-lower is redrawn instead of ending the run.
        ItemDefinition("marked_card", ItemEffect.INVENTORY, ItemCategory.CASINO,
                       shop_price=13000, gacha_kind="item"),
        # `value` is the multiplier in hundredths that a crash cannot burst
        # before, so the mechanic reads from the catalog like every other one.
        # 195 rather than a round 200 because it is exactly the third step of
        # the 1.25x ladder: a floor between two steps would promise 2.00x and
        # always pay 2.44x, and a promise that is quietly generous is still a
        # promise that does not describe what happens.
        ItemDefinition("parachute", ItemEffect.INVENTORY, ItemCategory.CASINO, value=195,
                       shop_price=16000, gacha_kind="item"),
        ItemDefinition("bodyguard", ItemEffect.BODYGUARD, ItemCategory.PROTECTION, value=0.7,
                       shop_price=15000),
        # Fixed reserves, not percentages. A higher tier replaces a lower one.
        ItemDefinition("small_vault", ItemEffect.VAULT, ItemCategory.PROTECTION, value=25000,
                       shop_price=500000, gacha_kind="vault"),
        ItemDefinition("med_vault", ItemEffect.VAULT, ItemCategory.PROTECTION, value=100000,
                       shop_price=1500000, gacha_kind="vault"),
        ItemDefinition("big_vault", ItemEffect.VAULT, ItemCategory.PROTECTION, value=500000,
                       shop_price=3000000, gacha_kind="vault"),
        ItemDefinition("rent_emoji", ItemEffect.RENT_TICKET, ItemCategory.RENTALS, shop_price=100000,
                       ticket_prefix="emoji", limit_check="static"),
        ItemDefinition("rent_anim_emoji", ItemEffect.RENT_TICKET, ItemCategory.RENTALS, shop_price=250000,
                       ticket_prefix="anim", limit_check="animated"),
        ItemDefinition("rent_sound", ItemEffect.RENT_TICKET, ItemCategory.RENTALS, shop_price=314000,
                       ticket_prefix="sound", limit_check="soundboard"),
    )


ITEM_DEFINITIONS: dict[str, ItemDefinition] = {
    definition.key: definition for definition in _definitions()
}

# Insertion order is the shop's display order, so keep the tuple above ordered
# the way the select menu should read.
SHOP_ITEMS: dict[str, ItemDefinition] = {
    key: definition for key, definition in ITEM_DEFINITIONS.items()
    if definition.sold_in_shop
}

INVENTORY_ITEM_KEYS: frozenset[str] = frozenset(
    key for key, definition in ITEM_DEFINITIONS.items()
    if definition.effect is ItemEffect.INVENTORY
)

VAULT_ITEMS: dict[str, ItemDefinition] = {
    key: definition for key, definition in ITEM_DEFINITIONS.items()
    if definition.effect is ItemEffect.VAULT
}

# Items a banner may award. The gacha can also award coins and vouchers, which
# are not owned goods and therefore not catalog entries.
GACHA_ELIGIBLE_ITEMS: dict[str, ItemDefinition] = {
    key: definition for key, definition in ITEM_DEFINITIONS.items()
    if definition.drawable_in_gacha
}


#: A custom item's shelf when its operator has not chosen one. `consumable`
#: resolves through the item it wraps rather than guessing, so a custom
#: "Burglar's Kit" that grants lockpicks lands in `heist` and a "Casino Pass"
#: granting a lucky charm lands in `casino`. `resolve_custom_category` owns that
#: branch, so `database` and the cogs cannot disagree about where a row belongs.
SHOP_TEMPLATE_CATEGORIES: dict[str, ItemCategory | None] = {
    "fixed_role": ItemCategory.PERKS,
    "timed_role": ItemCategory.PERKS,
    "coin_bundle": ItemCategory.PERKS,
    "vault": ItemCategory.PROTECTION,
    "fulfillment_voucher": ItemCategory.RENTALS,
    # None means "ask the wrapped catalog item".
    "consumable": None,
}


def resolve_custom_category(template_type: str, config: dict,
                            stored: str | None = None) -> str:
    """Which shelf a custom item sits on.

    Three states, and the middle one is the point. `stored` is ``None`` when the
    operator has not chosen — a row written before categories existed, or one
    left on "Automatic" — and then the template decides. A stored value always
    wins. An empty string never reaches here; the API refuses it, because
    "not chosen" and "chosen as nothing" must not collapse.
    """
    if stored:
        category = _CATEGORY_VALUES.get(stored)
        if category is not None:
            return category.value
    default = SHOP_TEMPLATE_CATEGORIES.get(template_type, ItemCategory.PERKS)
    if default is None:
        wrapped = ITEM_DEFINITIONS.get((config or {}).get("item_key"))
        return (wrapped.category if wrapped else ItemCategory.CASINO).value
    return default.value


def shop_items_in(category: str, hidden=()) -> dict[str, ItemDefinition]:
    """The built-in items on one shelf, minus the ones this guild has hidden."""
    skip = set(hidden or ())
    return {
        key: definition for key, definition in SHOP_ITEMS.items()
        if definition.category.value == category and key not in skip
    }


def visible_shop_items(hidden=()) -> dict[str, ItemDefinition]:
    """Every built-in the shop should offer this guild.

    Hiding is **per guild** while the catalog is installation-wide, which is why
    this is a function taking the hidden list rather than a filtered module
    global: a module-level filtered view could not be correct for two guilds at
    once. It filters `SHOP_ITEMS` only, so the gacha — which reads
    `GACHA_ELIGIBLE_ITEMS` — never sees a hidden item, and `BUILTIN_SHOP_KEYS`
    stays the full reserved set so a hidden key cannot be reused by a custom
    item and shadow rows that already reference it.
    """
    skip = set(hidden or ())
    return {key: definition for key, definition in SHOP_ITEMS.items()
            if key not in skip}


def custom_item_capacity(category: str, hidden=()) -> int:
    """How many custom items one shelf can still hold for this guild.

    Per guild, because hiding is: dropping a built-in this guild does not sell
    genuinely gives its slot back, which is what makes the cap unrepresentable
    as a module constant.
    """
    return SELECT_OPTION_LIMIT - len(shop_items_in(category, hidden))


def shop_default_prices() -> dict[str, int]:
    """Installation-default price per built-in shop item."""
    return {key: definition.shop_price for key, definition in SHOP_ITEMS.items()}


def catalog_payload() -> list[dict]:
    """Serialize the catalog for the dashboard's item pickers.

    Only identity and defaults travel: names and descriptions stay in the
    language catalogs and a guild's live price stays in ``shop_prices``.
    """
    return [
        {
            "key": definition.key,
            "effect": definition.effect.value,
            "value": definition.value,
            "default_price": definition.shop_price,
            "sold_in_shop": definition.sold_in_shop,
            "gacha_kind": definition.gacha_kind,
            "category": definition.category.value,
        }
        for definition in ITEM_DEFINITIONS.values()
    ]
