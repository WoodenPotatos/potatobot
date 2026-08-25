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
        ItemDefinition("premium", ItemEffect.ROLE, value="premium_role",
                       shop_price=300000),
        # A lockpick is one inventory row in both systems. The legacy
        # ``users.rob_bonus`` column is no longer written by any purchase; it is
        # only read out and cleared for members who bought one before this
        # change, and has no guild dimension to migrate into.
        ItemDefinition("lockpick", ItemEffect.INVENTORY, value=0.15,
                       shop_price=5000, gacha_kind="item"),
        ItemDefinition("loaded_die", ItemEffect.INVENTORY,
                       shop_price=10000, gacha_kind="item"),
        ItemDefinition("vault_glove", ItemEffect.INVENTORY, value=0.25,
                       shop_price=75000, gacha_kind="item"),
        # One extra forgiven day on an Everydle streak, spent retroactively by
        # the next claim rather than by a scheduled job. `value` is that number
        # of days, so the mechanic reads from the catalog like every other one.
        ItemDefinition("streak_freeze", ItemEffect.INVENTORY, value=1,
                       shop_price=60000, gacha_kind="item"),
        ItemDefinition("bodyguard", ItemEffect.BODYGUARD, value=0.7,
                       shop_price=15000),
        # Fixed reserves, not percentages. A higher tier replaces a lower one.
        ItemDefinition("small_vault", ItemEffect.VAULT, value=25000,
                       shop_price=500000, gacha_kind="vault"),
        ItemDefinition("med_vault", ItemEffect.VAULT, value=100000,
                       shop_price=1500000, gacha_kind="vault"),
        ItemDefinition("big_vault", ItemEffect.VAULT, value=500000,
                       shop_price=3000000, gacha_kind="vault"),
        ItemDefinition("rent_emoji", ItemEffect.RENT_TICKET, shop_price=100000,
                       ticket_prefix="emoji", limit_check="static"),
        ItemDefinition("rent_anim_emoji", ItemEffect.RENT_TICKET, shop_price=250000,
                       ticket_prefix="anim", limit_check="animated"),
        ItemDefinition("rent_sound", ItemEffect.RENT_TICKET, shop_price=314000,
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
        }
        for definition in ITEM_DEFINITIONS.values()
    ]
