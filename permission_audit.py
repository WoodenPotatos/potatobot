"""One permission diagnostic shared by `/checkperms` and the dashboard.

The old check compared the bot's guild permissions against five hand-written
groups. That answered a question nobody has: it reported permissions this
installation may not use, said nothing about the features a guild actually has
enabled, and — most misleadingly — passed while a configured channel's own
overwrites denied the bot the very permission the group said it had.

This module answers the operational question instead: for this guild, as it is
configured right now, can the bot do what its enabled features need? It is pure
data. Every finding carries stable English keys and identifiers, and each caller
maps them to its own locale catalog, so the bot embed and the dashboard page
cannot drift apart.
"""

from dataclasses import dataclass, field

import discord

from settings_registry import (
    FEATURE_DEFINITIONS,
    SETTING_DEFINITIONS,
    SettingValueType,
    legacy_config_value,
)

# A finding's weight. `blocking` means an enabled feature cannot work at all;
# `degraded` means it works but something configured for it does not.
SEVERITY_BLOCKING = "blocking"
SEVERITY_DEGRADED = "degraded"

# The floor for a channel of each kind, used when a setting declares nothing
# more specific. It is deliberately blunt: it is what *any* destination of that
# kind needs, and a setting that needs more says so on its own definition via
# `bot_channel_permissions`. Asking every category for `manage_roles` because
# the ticket category needs it would report a false problem on every other one.
CHANNEL_KIND_REQUIREMENTS = {
    "text": ("view_channel", "send_messages", "embed_links"),
    "news": ("view_channel", "send_messages", "embed_links"),
    "voice": ("view_channel", "connect"),
    "stage_voice": ("view_channel", "connect"),
    "category": ("view_channel", "manage_channels"),
}

# Settings whose roles the bot has to be able to grant or take away. A role menu
# or an autorole the bot sits below is the single most common silent failure, and
# no permission flag reports it — only the role hierarchy does.
# Derived, not listed: `SettingDefinition.role_must_be_assignable` is where
# grantability is declared, so the audit and the dashboard selector cannot drift
# apart. A recognition-only role such as `premium_roles` is deliberately absent —
# those roles sit above the bot on purpose and never need to be grantable.
ASSIGNABLE_ROLE_SETTINGS = tuple(
    key for key, definition in sorted(SETTING_DEFINITIONS.items())
    if definition.role_must_be_assignable
)


@dataclass
class Finding:
    """One problem, named by stable keys rather than prose."""

    code: str
    severity: str
    subject: str = ""
    permissions: tuple[str, ...] = ()
    feature: str = ""
    identifier: str = ""

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "subject": self.subject,
            "permissions": list(self.permissions),
            "feature": self.feature,
            "identifier": self.identifier,
        }


@dataclass
class PermissionReport:
    administrator: bool = False
    top_role_position: int = 0
    features: list[dict] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [item for item in self.findings if item.severity == SEVERITY_BLOCKING]

    def as_dict(self) -> dict:
        return {
            "administrator": self.administrator,
            "top_role_position": self.top_role_position,
            "features": self.features,
            "findings": [finding.as_dict() for finding in self.findings],
            "blocking_count": len(self.blocking),
            "degraded_count": len(self.findings) - len(self.blocking),
        }


def _missing(permissions: discord.Permissions, required) -> tuple[str, ...]:
    """Permissions out of `required` that this set does not grant.

    Administrator is checked explicitly. Discord treats it as every permission,
    and discord.py's resolved `guild_permissions` and `permissions_for` already
    return a full set for an administrator, but a bare `Permissions` object does
    not — so relying on that would make the report depend on where its input
    came from.
    """
    if permissions.administrator:
        return ()
    return tuple(name for name in required if not getattr(permissions, name, False))


def _setting_ids(settings: dict, key: str) -> list[int]:
    """The snowflakes one setting names, whether it holds one or a list."""
    definition = SETTING_DEFINITIONS.get(key)
    if definition is None:
        return []
    value = settings.get(key, definition.default)
    if value is None:
        return []
    if isinstance(value, list):
        return [int(item) for item in value if isinstance(item, int)]
    return [int(value)] if isinstance(value, int) and not isinstance(value, bool) else []


def _feature_is_enabled(feature_states: dict, feature_key: str) -> bool:
    """A missing row means the registry default, matching the runtime gate."""
    definition = FEATURE_DEFINITIONS.get(feature_key)
    if definition is None:
        return False
    state = feature_states.get(feature_key)
    if isinstance(state, dict):
        state = state.get("enabled")
    return definition.default if state is None else bool(state)


def _audit_features(guild, feature_states, report: PermissionReport) -> None:
    """Compare each feature's declared permissions with what the bot holds."""
    granted = guild.me.guild_permissions
    for key, definition in sorted(FEATURE_DEFINITIONS.items()):
        enabled = _feature_is_enabled(feature_states, key)
        missing = _missing(granted, definition.required_discord_permissions)
        report.features.append({
            "key": key,
            "group": definition.group,
            "enabled": enabled,
            "missing": list(missing),
            "required": list(definition.required_discord_permissions),
        })
        if missing and enabled:
            # A disabled feature's missing permission is not a problem yet, so
            # it is reported as state rather than as a finding.
            report.findings.append(Finding(
                code="feature_missing_permission",
                severity=SEVERITY_BLOCKING,
                subject=key,
                feature=key,
                permissions=missing,
            ))


def _bot_requirements(definition, channel) -> tuple[str, ...]:
    """What the bot needs here: the setting's own declaration, else the kind."""
    if definition.bot_channel_permissions:
        return definition.bot_channel_permissions
    return CHANNEL_KIND_REQUIREMENTS.get(str(channel.type), ())


def _member_reference(guild, settings):
    """Who "a member" is in this guild, for the member half of the audit.

    `@everyone` is the wrong reference wherever an airlock is used: access is
    denied to `@everyone` deliberately and granted through a member role, so
    checking `@everyone` reports every channel as broken. Measured against the
    private deployment, that mistake produced nine findings and not one of them
    was real — the kind of report that teaches an operator to ignore it.

    `permissions_for` resolves `@everyone` plus the role it is given, so the
    configured member role is the complete answer where one exists, and
    `@everyone` remains correct for a guild with no airlock.
    """
    for role_id in _setting_ids(settings, "member_role"):
        role = guild.get_role(role_id)
        if role is not None:
            return role
    return guild.default_role


def _visible_to_any_role(guild, channel) -> bool:
    """Whether some ordinary role can see this channel at all."""
    for role in getattr(guild, "roles", ()):
        if role.managed or role.is_default():
            continue
        if channel.permissions_for(role).view_channel:
            return True
    return False


def _missing_for_members(guild, channel, reference, required) -> tuple[str, ...]:
    """What members cannot do here, ignoring what is gated on purpose.

    A channel an opt-in role unlocks — a per-game chat, a creator channel — is
    invisible to the ordinary member role by design, and reporting that as a
    problem is how a diagnostic teaches people to ignore it. Measured against
    the private deployment, treating every invisible channel as broken produced
    eleven findings of which none were real.

    So invisibility is only a finding when *no* ordinary role can see the
    channel, which is the case an operator genuinely wants to know about: the
    announcements land somewhere nobody will ever read. Where the channel is
    gated, the rest of the member checks are moot and are skipped — you cannot
    send a message in a channel you cannot see.
    """
    if not required:
        return ()
    effective = channel.permissions_for(reference)
    if not effective.view_channel and not effective.administrator:
        if _visible_to_any_role(guild, channel):
            return ()
        return ("view_channel",) if "view_channel" in required else ()
    return _missing(effective, [name for name in required if name != "view_channel"])


def _audit_channels(guild, feature_states, settings, report: PermissionReport) -> None:
    """Check each configured channel, including its own overwrites.

    Guild-wide permissions are not the effective ones: a log channel that denies
    the bot `send_messages` reports fine at guild level and then silently drops
    every message. `permissions_for` is the only answer that matches runtime.

    Both sides are checked. Every diagnostic here used to ask only what the *bot*
    needs, which meant a channel could pass while the members it exists for could
    not use it — a slash command does not appear at all where
    `use_application_commands` is denied, and nothing said so. A member finding is
    reported as degraded rather than blocking: the installation works, this guild's
    configuration defeats the point of it, and over-claiming would make the report
    the kind nobody reads.
    """
    member_reference = _member_reference(guild, settings)
    for key, definition in sorted(SETTING_DEFINITIONS.items()):
        if definition.value_type not in {
            SettingValueType.CHANNEL, SettingValueType.CHANNEL_LIST
        }:
            continue
        owner = definition.owner_feature
        if owner and not _feature_is_enabled(feature_states, owner):
            continue
        for channel_id in _setting_ids(settings, key):
            channel = guild.get_channel(channel_id)
            if channel is None:
                report.findings.append(Finding(
                    code="channel_missing",
                    severity=SEVERITY_DEGRADED,
                    subject=key,
                    feature=owner or "",
                    identifier=str(channel_id),
                ))
                continue

            required = _bot_requirements(definition, channel)
            if required:
                missing = _missing(channel.permissions_for(guild.me), required)
                if missing:
                    report.findings.append(Finding(
                        code="channel_missing_permission",
                        severity=SEVERITY_BLOCKING,
                        subject=key,
                        feature=owner or "",
                        identifier=channel.name,
                        permissions=missing,
                    ))

            member_missing = _missing_for_members(
                guild, channel, member_reference,
                definition.member_channel_permissions)
            if member_missing:
                report.findings.append(Finding(
                    code="channel_member_missing_permission",
                    severity=SEVERITY_DEGRADED,
                    subject=key,
                    feature=owner or "",
                    identifier=channel.name,
                    permissions=member_missing,
                ))


def _audit_roles(guild, feature_states, settings, report: PermissionReport) -> None:
    """Check that every role the bot is expected to grant is actually grantable.

    Nothing in Discord's permission model reports this: `manage_roles` is held
    guild-wide while the specific role sits above the bot's own top role, and the
    grant then fails one member at a time.
    """
    can_manage = guild.me.guild_permissions.manage_roles
    for key in ASSIGNABLE_ROLE_SETTINGS:
        definition = SETTING_DEFINITIONS.get(key)
        if definition is None:
            continue
        owner = definition.owner_feature
        if owner and not _feature_is_enabled(feature_states, owner):
            continue
        for role_id in _setting_ids(settings, key):
            role = guild.get_role(role_id)
            if role is None:
                report.findings.append(Finding(
                    code="role_missing",
                    severity=SEVERITY_DEGRADED,
                    subject=key,
                    feature=owner or "",
                    identifier=str(role_id),
                ))
                continue
            if role.managed or role.is_default():
                # An integration role cannot be granted by anyone.
                report.findings.append(Finding(
                    code="role_unmanageable",
                    severity=SEVERITY_BLOCKING,
                    subject=key,
                    feature=owner or "",
                    identifier=role.name,
                ))
                continue
            if not can_manage or role >= guild.me.top_role:
                report.findings.append(Finding(
                    code="role_above_bot",
                    severity=SEVERITY_BLOCKING,
                    subject=key,
                    feature=owner or "",
                    identifier=role.name,
                    permissions=() if can_manage else ("manage_roles",),
                ))


def build_report(guild, feature_states: dict, settings: dict) -> PermissionReport:
    """Audit one guild's live Discord state against its enabled configuration.

    `feature_states` accepts either the dashboard's `{key: {"enabled": bool}}`
    rows or a plain `{key: bool}` map, and `settings` maps a setting key to its
    resolved value. Both are passed in rather than read here, so this stays a
    pure function that a test can drive with a fake guild.
    """
    report = PermissionReport(
        administrator=bool(guild.me.guild_permissions.administrator),
        top_role_position=int(guild.me.top_role.position),
    )
    _audit_features(guild, feature_states, report)
    _audit_channels(guild, feature_states, settings, report)
    _audit_roles(guild, feature_states, settings, report)
    if report.administrator:
        # Not a failure: it works, and it is how many installations are set up.
        # It is reported because it grants far more than anything declared here,
        # so a later permission mistake would be invisible until it is removed.
        report.findings.append(Finding(
            code="administrator_granted", severity=SEVERITY_DEGRADED,
        ))
    return report


def resolved_settings(stored: dict, config: dict | None = None) -> dict:
    """Flatten stored settings rows to the values the bot actually uses.

    `config` is the legacy `config.json` mirror, and passing it is not optional
    in practice: `guild_settings` is sparse — empty on the private deployment —
    so defaulting to the registry meant every channel and role resolved to
    nothing and the audit reported clean while checking no configured channel at
    all. The fallback order matches `cogs.utils.guild_setting`: stored row,
    then the legacy path, then the registry default.
    """
    config = config or {}
    return {
        key: (stored[key]["value"] if key in stored
              else legacy_config_value(definition, config))
        for key, definition in SETTING_DEFINITIONS.items()
    }
