/* PotatoBot control plane front-end.
 *
 * Every visible string comes from the server locale catalogue: this file must
 * contain no Hungarian prose, which tests/test_localization_policy.py enforces.
 * DOM construction stays on createElement/textContent - never innerHTML - so
 * Discord-supplied names cannot become markup.
 */

const API = '/api';

let locale = {};
let csrf = '';
let isHost = false;
let guildId = null;
let featureState = {};
let registry = {};
let featureGroupOrder = [];
let settings = {};
// The banner currently being edited, and the guild's full banner list. A guild
// may run several banners, so the page edits one at a time and the picker in
// the card head decides which.
let gacha = null;
let gachaBanners = [];
let activeBannerKey = null;
// The shared built-in item catalog. Identical for every guild, so it is fetched
// once and reused by the gacha reward picker and the shop item builder.
let itemCatalog = [];
let activePage = 'overview';
let builderType = 'embed';
let resources = {channels: [], roles: []};
// The setup report, cached per guild: findings indexed by the setting they
// concern, plus the counts the overview quotes. null means not loaded yet.
let permissionFindings = null;
// The session's idle deadline, as a timestamp. The cookie's lifetime is the
// idle timeout and every authenticated request slides it forward, so the
// countdown restarts on each successful call rather than ticking down from login.
let sessionIdleSeconds = 0;
let sessionDeadline = 0;
let sessionTicker = null;
let builderDocuments = [];
let fulfillmentRequests = [];
let languages = [];
let activeLanguage = '';
let guilds = [];
let account = null;

const CATEGORY_ICONS = {
    community: 'ic-community',
    economy: 'ic-economy',
    games: 'ic-games',
    moderation: 'ic-moderation',
    factions: 'ic-factions',
    music: 'ic-music',
    builders: 'ic-builders',
    administration: 'ic-administration',
};

/* ------------------------------------------------------------ localization */

const tr = (path) => {
    const value = path.split('.').reduce((current, key) => current?.[key], locale);
    return typeof value === 'string' && value ? value : `[${path}]`;
};

const format = (path, values) => {
    let text = tr(path);
    Object.entries(values || {}).forEach(([key, value]) => {
        text = text.split(`{${key}}`).join(String(value));
    });
    return text;
};

function localize(root = document) {
    root.querySelectorAll('[data-i18n]').forEach((node) => { node.textContent = tr(node.dataset.i18n); });
    root.querySelectorAll('[data-i18n-alt]').forEach((node) => { node.alt = tr(node.dataset.i18nAlt); });
    root.querySelectorAll('[data-i18n-aria-label]').forEach((node) => node.setAttribute('aria-label', tr(node.dataset.i18nAriaLabel)));
    root.querySelectorAll('[data-i18n-placeholder]').forEach((node) => { node.placeholder = tr(node.dataset.i18nPlaceholder); });
    root.querySelectorAll('[data-i18n-title]').forEach((node) => { node.title = tr(node.dataset.i18nTitle); });
}

/* ------------------------------------------------------------ DOM builders */

function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
}

function icon(symbolId, className = 'ic') {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', className);
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', `#${symbolId}`);
    svg.appendChild(use);
    return svg;
}

function pill(labelKey, variant) {
    return element('span', `pill ${variant}`, tr(labelKey));
}

function emptyState(messageKey, symbolId = 'ic-inbox') {
    const wrap = element('div', 'empty-state');
    wrap.append(icon(symbolId), element('p', null, tr(messageKey)));
    return wrap;
}

function table(headerKeys) {
    const node = element('table', 'data');
    const head = element('thead');
    const row = element('tr');
    headerKeys.forEach((key) => row.appendChild(element('th', null, tr(key))));
    head.appendChild(row);
    node.append(head, element('tbody'));
    return node;
}

function emptyRow(container, columns, messageKey) {
    const row = element('tr');
    const cell = element('td', 'cell-empty', tr(messageKey));
    cell.colSpan = columns;
    row.appendChild(cell);
    container.appendChild(row);
}

function renderSkeleton(container, rows = 3) {
    container.replaceChildren();
    for (let index = 0; index < rows; index += 1) container.appendChild(element('div', 'skeleton-row'));
}

/* --------------------------------------------------------------- transport */

class ApiError extends Error {
    constructor(message, status) {
        super(message);
        this.status = status;
    }
}

async function api(path, options = {}) {
    let response;
    try {
        response = await fetch(`${API}${path}`, options);
    } catch (error) {
        throw new ApiError(tr('dashboard.network_error'), 0);
    }

    // A reverse proxy error page is not JSON, so never assume a parsable body.
    let result = null;
    const body = await response.text();
    if (body) {
        try {
            result = JSON.parse(body);
        } catch (error) {
            result = null;
        }
    }

    if (!response.ok || result?.status === 'error') {
        const message = result?.message || format('dashboard.http_error', {status: response.status});
        throw new ApiError(message, response.status);
    }
    slideSessionDeadline();
    return result ?? {};
}

/* --------------------------------------------------------- session countdown */

// When the last authenticated request went out. Navigation between pages that
// render from loaded state makes none, so without a keepalive the cookie really
// does expire while somebody is using the interface — the countdown was not
// wrong, the session was genuinely ending.
let lastServerContact = 0;
// Short, because `/api/session/touch` only refreshes the cookie — no database
// read, no guild decoration. The guard exists to collapse a double click, not to
// ration requests: reads allow 300 a minute and this is one per navigation.
const KEEPALIVE_SECONDS = 5;

/** Touch the server if it has been a while, so navigating counts as activity.
 *
 *  Throttled because a click that already fetches has refreshed the cookie, and
 *  a second request would tell us nothing. `/auth/status` is the cheapest
 *  authenticated endpoint and is already the one the countdown is calibrated
 *  from. Failures are ignored: the next real call will surface a dead session
 *  through `handleApiError`, and a keepalive is not the place to end one.
 */
async function keepSessionAlive() {
    if (!sessionIdleSeconds) return;
    if ((Date.now() - lastServerContact) / 1000 < KEEPALIVE_SECONDS) return;
    try {
        await api('/session/touch');
    } catch (error) {
        // Deliberately silent: the next real call surfaces a dead session
        // through `handleApiError`, and a keepalive is not the place to end one.
    }
}

/** Restart the idle countdown, mirroring what the request just did to the cookie. */
function slideSessionDeadline() {
    lastServerContact = Date.now();
    if (!sessionIdleSeconds) return;
    sessionDeadline = Date.now() + sessionIdleSeconds * 1000;
    renderSessionTimer();
}

function startSessionTimer(idleSeconds) {
    sessionIdleSeconds = Number(idleSeconds) || 0;
    if (!sessionIdleSeconds) return;
    document.getElementById('session-timer').classList.remove('hidden');
    slideSessionDeadline();
    if (sessionTicker) clearInterval(sessionTicker);
    sessionTicker = setInterval(renderSessionTimer, 1000);
}

function stopSessionTimer() {
    if (sessionTicker) clearInterval(sessionTicker);
    sessionTicker = null;
    sessionDeadline = 0;
    document.getElementById('session-timer').classList.add('hidden');
}

/** Draw mm:ss, warning under two minutes.
 *  At zero it says so and stops, but does not force a logout: a static asset
 *  request also refreshes the cookie without passing through `api()`, so the
 *  countdown can run slightly ahead of the real deadline. The next call gets a
 *  401 and `handleApiError` shows the login screen properly. */
function renderSessionTimer() {
    const node = document.getElementById('session-timer');
    if (!node || !sessionDeadline) return;
    const left = Math.max(0, Math.round((sessionDeadline - Date.now()) / 1000));
    const minutes = Math.floor(left / 60);
    const seconds = String(left % 60).padStart(2, '0');
    node.textContent = left
        ? format('dashboard.session_remaining', {time: `${minutes}:${seconds}`})
        : tr('dashboard.session_expired_short');
    node.classList.toggle('warning', left > 0 && left <= 120);
    node.classList.toggle('expired', left === 0);
}

/** Route an expired session back to the login card instead of looping toasts. */
function handleApiError(error) {
    if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
        showLogin();
        toast(tr('dashboard.session_expired'), true);
        return;
    }
    toast(error.message, true);
}

/* -------------------------------------------------------- toasts and modals */

function toast(message, isError = false) {
    const node = element('div', `toast${isError ? ' error' : ''}`);
    const close = element('button', 'toast-close');
    close.type = 'button';
    close.setAttribute('aria-label', tr('dashboard.dismiss'));
    close.appendChild(icon('ic-close', 'ic ic-sm'));

    const dismiss = () => {
        node.classList.add('fade-out');
        node.addEventListener('animationend', () => node.remove(), {once: true});
    };
    close.addEventListener('click', dismiss);

    node.append(icon(isError ? 'ic-alert' : 'ic-check'), element('span', 'toast-text', message), close);
    document.getElementById('toast-container').appendChild(node);
    setTimeout(dismiss, 6000);
}

/** Resolve to true only when the operator explicitly confirms. */
function confirmAction(message) {
    return new Promise((resolve) => {
        const root = document.getElementById('modal-root');
        const backdrop = element('div', 'modal-backdrop');
        const card = element('div', 'modal-card');
        const heading = element('h2', 'display', tr('dashboard.confirm_title'));
        const body = element('p', null, message);
        const footer = element('div', 'form-footer');

        const finish = (value) => { backdrop.remove(); resolve(value); };

        const cancel = element('button', 'btn btn-ghost', tr('dashboard.confirm_no'));
        cancel.type = 'button';
        cancel.addEventListener('click', () => finish(false));

        const accept = element('button', 'btn btn-primary', tr('dashboard.confirm_yes'));
        accept.type = 'button';
        accept.addEventListener('click', () => finish(true));

        backdrop.addEventListener('click', (event) => { if (event.target === backdrop) finish(false); });
        document.addEventListener('keydown', function escape(event) {
            if (event.key !== 'Escape') return;
            document.removeEventListener('keydown', escape);
            finish(false);
        });

        footer.append(cancel, accept);
        card.append(heading, body, footer);
        backdrop.appendChild(card);
        root.appendChild(backdrop);
        accept.focus();
    });
}

/* ------------------------------------------------------- anchored popovers */

let closeOpenPopover = null;

/** Attach an anchored menu to a trigger button.
 *
 * Both the account menu and the guild switcher are the same widget, so they
 * share one implementation: outside click and Escape close it, focus returns to
 * the trigger, and the arrow keys walk the items.
 */
function popover(trigger, buildMenu, options = {}) {
    trigger.addEventListener('click', (event) => {
        event.stopPropagation();
        if (trigger.getAttribute('aria-expanded') === 'true') {
            closeOpenPopover();
            return;
        }
        if (closeOpenPopover) closeOpenPopover();
        openPopover(trigger, buildMenu, options);
    });
}

/** `options.role` is the ARIA role of the surface, `menu` for the topbar
 *  switchers and `listbox` for the resource picker. `options.align` anchors to
 *  the trigger's left edge instead of its right, and `options.matchWidth` makes
 *  the surface as wide as the trigger, which is what a form control needs and a
 *  topbar menu does not. */
function openPopover(trigger, buildMenu, options = {}) {
    const itemRole = options.itemRole || 'menuitem';
    const root = document.getElementById('popover-root');
    const surface = element('div', 'popover');
    const menu = element('div', `popover-menu${options.className ? ` ${options.className}` : ''}`);
    menu.setAttribute('role', options.role || 'menu');
    if (options.ariaLabel) menu.setAttribute('aria-label', options.ariaLabel);

    // Filled in below; `close` must be defined before the handlers that call it.
    const closers = [];

    const close = () => {
        document.removeEventListener('click', onOutsideClick);
        document.removeEventListener('keydown', onKeyDown, true);
        closers.forEach((undo) => undo());
        surface.remove();
        trigger.setAttribute('aria-expanded', 'false');
        closeOpenPopover = null;
    };

    function onOutsideClick(event) {
        if (!surface.contains(event.target) && event.target !== trigger) close();
    }

    function onKeyDown(event) {
        if (event.key === 'Escape') {
            event.preventDefault();
            close();
            trigger.focus();
            return;
        }
        if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
        const items = [...menu.querySelectorAll(`[role="${itemRole}"]:not(:disabled)`)];
        if (!items.length) return;
        event.preventDefault();
        const step = event.key === 'ArrowDown' ? 1 : -1;
        const index = items.indexOf(document.activeElement);
        items[(index + step + items.length) % items.length].focus();
    }

    buildMenu(menu, close);

    // The surface is fixed so the sticky topbar's stacking context cannot clip
    // it, which means it does not move with the page: scrolling left the menu
    // hanging in mid-air while its trigger slid away behind it. Position is
    // therefore recomputed on scroll rather than only at open time.
    if (options.align === 'left') surface.classList.add('popover-left');
    if (options.matchWidth) surface.classList.add('popover-matched');

    // A trigger in the sticky header never moves, so a fixed surface is right
    // there. Anything else is positioned in document coordinates and carried by
    // the page itself — the only way to avoid trailing, because a JS-repositioned
    // fixed element always lags a compositor-thread scroll.
    const header = document.querySelector('.topbar');
    const inHeader = Boolean(header && header.contains(trigger));
    if (!inHeader) surface.classList.add('popover-in-page');

    const place = () => {
        const box = trigger.getBoundingClientRect();
        // Document coordinates for an in-page menu, viewport for a header one.
        // Both are scroll-invariant, so this writes the same values on every
        // call and the surface never chases the scroll.
        const offsetY = inHeader ? 0 : window.scrollY;
        const offsetX = inHeader ? 0 : window.scrollX;
        // The topbar is sticky, so a field scrolled up behind it is invisible
        // while the fixed surface below is not: the menu went on floating over
        // the header, anchored to a field nobody could see. Anchoring below the
        // field's bottom edge keeps the surface clear of the header for as long
        // as that edge is, so the moment it passes underneath, close.
        // Not for a trigger that lives *in* the header — the account menu and the
        // guild switcher are anchored there, their bottom edge is above the
        // header's own, and this rule would close them the instant they opened.
        const headerBottom = (inHeader || !header)
            ? 0 : header.getBoundingClientRect().bottom;
        if (box.bottom <= headerBottom || box.top > window.innerHeight) {
            close();
            return;
        }
        surface.style.setProperty(
            '--popover-top', `${Math.round(box.bottom + offsetY + 8)}px`);
        if (options.align === 'left') {
            surface.style.setProperty(
                '--popover-left', `${Math.round(box.left + offsetX)}px`);
            surface.style.setProperty('--popover-right', 'auto');
        } else {
            // The page never scrolls horizontally, so the document's right edge
            // and the viewport's coincide and this holds for both modes.
            surface.style.setProperty(
                '--popover-right', `${Math.round(window.innerWidth - box.right)}px`);
        }
        if (options.matchWidth) {
            surface.style.setProperty('--popover-width', `${Math.round(box.width)}px`);
        }
    };
    place();

    // Coalesced to one reposition per frame. Repositioning synchronously on
    // every scroll event made the surface visibly trail its trigger, because
    // scroll fires far more often than the page paints.
    let frame = 0;
    const schedulePlace = () => {
        if (frame) return;
        frame = requestAnimationFrame(() => {
            frame = 0;
            place();
        });
    };
    // Capture, because the scroll happens on whichever ancestor is scrollable
    // and those events do not bubble. Passive, because this never preventDefaults.
    window.addEventListener('scroll', schedulePlace, {capture: true, passive: true});
    closers.push(() => {
        window.removeEventListener('scroll', schedulePlace, {capture: true});
        if (frame) cancelAnimationFrame(frame);
    });

    surface.appendChild(menu);
    root.appendChild(surface);
    trigger.setAttribute('aria-expanded', 'true');
    closeOpenPopover = close;

    // Registered a tick later so the click that opened this menu cannot also be
    // seen as the outside click that dismisses it.
    setTimeout(() => document.addEventListener('click', onOutsideClick), 0);
    document.addEventListener('keydown', onKeyDown, true);

    // Only a *width* change invalidates the anchoring. An on-screen keyboard
    // changes the height alone, and closing on that made every picker unusable
    // on a phone: the search field took focus, the keyboard opened, and the
    // resize it caused dismissed the menu immediately.
    const openedAt = window.innerWidth;
    const onResize = () => {
        // A width change moves the layout under the menu; reposition rather than
        // close, and only give up if the trigger has gone.
        if (window.innerWidth !== openedAt) schedulePlace();
    };
    window.addEventListener('resize', onResize);
    closers.push(() => window.removeEventListener('resize', onResize));

    // Autofocusing a text field summons the keyboard, so on a touch device the
    // search box is left for the user to tap. The list is what they came for.
    const wantsKeyboard = !window.matchMedia?.('(pointer: coarse)').matches;
    const target = (wantsKeyboard && menu.querySelector('[data-autofocus]'))
        || menu.querySelector(`[role="${itemRole}"]`);
    target?.focus();
    return close;
}

function menuItem(label, {symbol, checked = false, danger = false, onSelect}) {
    const item = element('button', `menu-item${danger ? ' danger' : ''}`);
    item.type = 'button';
    item.setAttribute('role', 'menuitem');
    if (symbol) item.appendChild(icon(symbol, 'ic ic-sm'));
    item.appendChild(element('span', 'menu-item-label', label));
    if (checked) item.appendChild(icon('ic-check', 'ic ic-sm menu-check'));
    item.addEventListener('click', onSelect);
    return item;
}

function menuSectionLabel(text) {
    return element('div', 'menu-section', text);
}

/** Avatar image with a monogram fallback, used for both users and guilds. */
function avatarNode(url, name, className = 'avatar') {
    if (url) {
        const image = document.createElement('img');
        image.className = className;
        image.src = url;
        image.alt = '';
        return image;
    }
    const initial = (name || '?').trim().charAt(0).toUpperCase() || '?';
    const node = element('span', `${className} avatar-monogram`, initial);
    node.setAttribute('aria-hidden', 'true');
    return node;
}

/* ------------------------------------------------------------- app startup */

document.addEventListener('DOMContentLoaded', () => { start(); });

let started = false;

async function start() {
    // Binding twice would attach every listener twice, so a second entry is a
    // no-op rather than a subtly broken interface.
    if (started) return;
    started = true;
    try {
        await loadLocale(window.potatoLanguage.current());
        bindShell();
        await authenticate();
    } catch (error) {
        showFatal(error.message);
    }
}

async function loadLocale(language) {
    const query = language ? `?lang=${encodeURIComponent(language)}` : '';
    const result = await api(`/locale${query}`);
    locale = result.data;
    languages = result.available || [];
    activeLanguage = result.language;
    document.documentElement.lang = result.language;
    localize();
}

/* When the locale endpoint itself is what failed there is no catalogue to
 * translate the error with, so this operational fallback is deliberately plain
 * English rather than an unresolved key shown to the operator. */
const BOOTSTRAP_FALLBACK = 'The dashboard could not start. Check the server logs, then reload.';

function showFatal(message) {
    document.getElementById('login-screen').classList.add('hidden');
    document.getElementById('main-dashboard').classList.add('hidden');

    const resolved = message && !message.startsWith('[') ? message : tr('dashboard.startup_failed');
    document.getElementById('fatal-message').textContent =
        resolved.startsWith('[') ? BOOTSTRAP_FALLBACK : resolved;

    const retry = document.getElementById('fatal-retry');
    if (!retry.textContent || retry.textContent.startsWith('[')) retry.textContent = 'Reload';

    document.getElementById('fatal-screen').classList.remove('hidden');
}

function showLogin() {
    stopSessionTimer();
    document.getElementById('main-dashboard').classList.add('hidden');
    document.getElementById('fatal-screen').classList.add('hidden');
    document.getElementById('login-screen').classList.remove('hidden');
}

function bindShell() {
    document.getElementById('fatal-retry').addEventListener('click', () => location.reload());

    document.querySelectorAll('.nav-item').forEach((button) => {
        button.addEventListener('click', () => { showPage(button.dataset.page); });
    });

    document.querySelectorAll('[data-toggle-group]').forEach((button) => {
        button.addEventListener('click', () => {
            const group = button.closest('.nav-group');
            const collapsed = group.dataset.collapsed === 'true';
            group.dataset.collapsed = String(!collapsed);
            button.setAttribute('aria-expanded', String(collapsed));
        });
    });

    const sidebar = document.getElementById('sidebar');
    document.getElementById('mobile-menu-btn').addEventListener('click', () => sidebar.classList.toggle('open'));
    document.getElementById('sidebar-scrim').addEventListener('click', () => sidebar.classList.remove('open'));

    popover(document.getElementById('account-trigger'), buildAccountMenu);
    popover(document.getElementById('guild-trigger'), buildGuildMenu);

    const settingsForm = document.getElementById('settings-form');
    settingsForm.addEventListener('submit', saveSettings);
    settingsForm.addEventListener('reset', (event) => {
        event.preventDefault();
        renderSettings(activePage);
    });
    // Delegated, because the inputs are rebuilt on every render. `input` covers
    // typing and `change` covers the selectors and checkboxes.
    settingsForm.addEventListener('input', refreshSettingsDirtyState);
    settingsForm.addEventListener('change', refreshSettingsDirtyState);
    document.getElementById('gacha-form').addEventListener('submit', saveGacha);
    document.getElementById('gacha-banner-form').addEventListener('submit', createGachaBanner);
    document.getElementById('work-response-form').addEventListener('submit', createWorkResponse);
    const shopItemForm = document.getElementById('shop-item-form');
    shopItemForm.addEventListener('submit', createShopItem);
    shopItemForm.template_type.addEventListener('change', renderShopItemTemplate);
    document.getElementById('builder-form').addEventListener('submit', saveBuilder);
    document.getElementById('erasure-form').addEventListener('submit', eraseMember);

    document.querySelectorAll('[data-builder]').forEach((button) => {
        button.addEventListener('click', () => {
            builderType = button.dataset.builder;
            document.querySelectorAll('[data-builder]').forEach((other) => {
                other.classList.toggle('active', other === button);
            });
        });
    });
}

/* ------------------------------------------------- account and guild menus */

function accountAvatarUrl() {
    if (!account?.avatar) return null;
    return `https://cdn.discordapp.com/avatars/${account.id}/${account.avatar}.png`;
}

function selectedGuild() {
    return guilds.find((guild) => guild.id === guildId) || null;
}

function renderHeaderControls() {
    const accountTrigger = document.getElementById('account-trigger');
    accountTrigger.replaceChildren(
        avatarNode(accountAvatarUrl(), account?.username, 'avatar avatar-sm'),
    );

    const guildTrigger = document.getElementById('guild-trigger');
    const guild = selectedGuild();
    guildTrigger.classList.toggle('hidden', guilds.length === 0);
    guildTrigger.disabled = guilds.length < 2;
    if (guild) {
        guildTrigger.replaceChildren(
            avatarNode(guild.icon_url, guild.name, 'avatar avatar-sm'),
            element('span', 'guild-name', guild.name),
        );
        if (guilds.length > 1) guildTrigger.appendChild(icon('ic-chevron-down', 'ic ic-sm'));
    }
}

function buildAccountMenu(menu, close) {
    const header = element('div', 'menu-header');
    header.append(
        avatarNode(accountAvatarUrl(), account?.username, 'avatar'),
        element('span', 'menu-header-name', account?.username || ''),
    );
    menu.appendChild(header);

    menu.appendChild(menuSectionLabel(tr('dashboard.theme')));
    window.potatoTheme.order.forEach((mode) => {
        menu.appendChild(menuItem(tr(`dashboard.themes.${mode}`), {
            checked: window.potatoTheme.current() === mode,
            onSelect: () => {
                window.potatoTheme.set(mode);
                close();
            },
        }));
    });

    if (languages.length > 1) {
        menu.appendChild(menuSectionLabel(tr('dashboard.language')));
        languages.forEach((language) => {
            menu.appendChild(menuItem(tr(`dashboard.languages.${language}`), {
                checked: activeLanguage === language,
                onSelect: async () => {
                    close();
                    await switchLanguage(language);
                },
            }));
        });
    }

    menu.appendChild(element('div', 'menu-divider'));
    menu.appendChild(menuItem(tr('dashboard.logout'), {
        symbol: 'ic-logout',
        danger: true,
        onSelect: () => { close(); logout(); },
    }));
}

function buildGuildMenu(menu, close) {
    menu.appendChild(menuSectionLabel(tr('dashboard.guild_label')));
    guilds.forEach((guild) => {
        const item = menuItem(guild.name, {
            checked: guild.id === guildId,
            onSelect: async () => {
                close();
                if (guild.id === guildId) return;
                guildId = guild.id;
                renderHeaderControls();
                await loadGuild();
            },
        });
        item.prepend(avatarNode(guild.icon_url, guild.name, 'avatar avatar-xs'));
        menu.appendChild(item);
    });
}

/** Re-fetch the catalogue and repaint everything that carries translated text. */
async function switchLanguage(language) {
    window.potatoLanguage.set(language);
    try {
        await loadLocale(language);
    } catch (error) {
        handleApiError(error);
        return;
    }
    renderHeaderControls();
    // Feature cards are built during loadGuild, so re-rendering the active page
    // alone would leave the features view holding stale labels.
    if (Object.keys(featureState).length) renderFeatures();
    await showPage(activePage);
}

async function authenticate() {
    const result = await api('/auth/status');
    if (!result.logged_in) {
        showLogin();
        return;
    }

    csrf = result.csrf_token;
    isHost = result.is_host === true;
    startSessionTimer(result.idle_timeout_seconds);
    account = result.user;
    guilds = result.guilds || [];
    guildId = guilds[0]?.id || null;

    document.getElementById('main-dashboard').classList.remove('hidden');
    document.getElementById('brand-version').textContent = result.version || '';
    renderHeaderControls();

    if (!guildId) {
        renderNoGuilds();
        return;
    }

    const [registryData, catalogData] = await Promise.all([
        api('/settings/registry'),
        api('/item-catalog'),
    ]);
    registry = registryData.data;
    featureGroupOrder = registryData.feature_group_order || [];
    itemCatalog = catalogData.data;
    await loadGuild();
}

function renderNoGuilds() {
    document.getElementById('page-title').textContent = tr('dashboard.title_overview');
    document.getElementById('page-subtitle').textContent = '';
    document.querySelectorAll('.page').forEach((node) => node.classList.add('hidden'));
    const overview = document.getElementById('overview');
    overview.classList.remove('hidden');
    overview.replaceChildren();
    const card = element('div', 'card');
    card.appendChild(emptyState('dashboard.no_guilds'));
    overview.appendChild(card);
}

async function logout() {
    try {
        await api('/auth/logout', {method: 'POST', headers: headers(), body: '{}'});
    } catch (error) {
        // A failed logout still clears the client; the cookie call is idempotent.
    }
    location.assign('/');
}

const headers = () => ({'Content-Type': 'application/json', 'X-CSRF-Token': csrf});

async function loadGuild() {
    if (!guildId) return;
    try {
        const [features, settingData, gachaData, resourceData] = await Promise.all([
            api(`/guilds/${guildId}/features`),
            api(`/guilds/${guildId}/settings`),
            api(`/guilds/${guildId}/gacha`),
            api(`/guilds/${guildId}/discord-resources`),
        ]);
        featureState = features.data;
        settings = settingData.data;
        permissionFindings = null;
        gachaBanners = gachaData.data;
        resources = resourceData.data;
        // A banner deleted in another tab must not leave the page editing a
        // banner the server no longer has.
        gacha = gachaBanners.find((banner) => banner.banner_key === activeBannerKey)
            || gachaBanners.find((banner) => banner.is_default)
            || gachaBanners[0]
            || null;
        activeBannerKey = gacha?.banner_key ?? null;
    } catch (error) {
        handleApiError(error);
        return;
    }
    renderFeatures();
    updateNavigation();
    await showPage(activePage);
}

/* ------------------------------------------------------------- navigation */

/** A category page stays visible while any of its settings is still owned by an
 *  enabled feature; only pages with a dedicated owner follow that flag directly. */
function categoryHasVisibleSettings(category) {
    return visibleDefinitions(category).length > 0;
}

function updateNavigation() {
    document.querySelectorAll('.nav-item[data-feature]').forEach((button) => {
        button.classList.toggle('hidden', featureState[button.dataset.feature]?.enabled === false);
    });

    document.querySelectorAll('.nav-item[data-category]').forEach((button) => {
        button.classList.toggle('hidden', !categoryHasVisibleSettings(button.dataset.category));
    });

    document.querySelectorAll('.nav-group').forEach((group) => {
        const items = [...group.querySelectorAll('.nav-item')];
        const anyVisible = items.some((item) => !item.classList.contains('hidden'));
        const header = group.querySelector('.nav-category');
        if (header) header.classList.toggle('hidden', !anyVisible);
    });

    // Never leave the operator looking at a page they just turned off.
    const current = document.querySelector(`.nav-item[data-page="${activePage}"]`);
    if (current && current.classList.contains('hidden')) activePage = 'overview';
}

async function showPage(page) {
    activePage = page;
    // Not awaited: navigation must not wait on the network, and the pages that
    // do fetch will slide the deadline themselves anyway.
    keepSessionAlive();
    document.querySelectorAll('.page').forEach((node) => node.classList.add('hidden'));
    document.querySelectorAll('.nav-item').forEach((node) => {
        const active = node.dataset.page === page;
        node.classList.toggle('active', active);
        if (active) node.setAttribute('aria-current', 'page');
        else node.removeAttribute('aria-current');
    });

    // Reset the shared header slots before rendering, so a page is free to fill
    // its own subtitle and actions without them being wiped afterwards.
    document.getElementById('page-title').textContent = tr(`dashboard.title_${page.replaceAll('-', '_')}`);
    document.getElementById('page-subtitle').textContent = '';
    document.getElementById('page-actions').replaceChildren();
    document.getElementById('sidebar').classList.remove('open');

    const direct = document.getElementById(page);
    if (direct) direct.classList.remove('hidden');
    else {
        document.getElementById('settings-page').classList.remove('hidden');
        renderSettings(page);
    }

    if (page === 'overview') await renderOverview();
    if (page === 'features') updateFeatureSubtitle();
    if (page === 'gacha') renderGacha();
    if (page === 'work-responses') await loadWorkResponses();
    if (page === 'shop-builder') await loadShopItems();
    if (page === 'builders') await loadBuilders();
    if (page === 'audit') await loadAudit();
    if (page === 'permissions') await loadPermissionReport();
    if (page === 'changelog') await loadChangelog();
}

function setSubtitle(path, values) {
    document.getElementById('page-subtitle').textContent = format(path, values);
}

function addPageAction(labelKey, symbolId, handler) {
    const button = element('button', 'btn btn-chrome', tr(labelKey));
    button.type = 'button';
    button.prepend(icon(symbolId));
    button.addEventListener('click', handler);
    document.getElementById('page-actions').appendChild(button);
}

/* --------------------------------------------------------------- overview */

async function renderOverview() {
    const actions = document.getElementById('overview-actions');
    actions.replaceChildren();

    [
        {page: 'features', labelKey: 'dashboard.nav_features', symbol: 'ic-features'},
        {page: 'community', labelKey: 'dashboard.nav_community', symbol: 'ic-community'},
        {page: 'audit', labelKey: 'dashboard.nav_audit', symbol: 'ic-audit'},
        {page: 'permissions', labelKey: 'dashboard.nav_permissions', symbol: 'ic-shield-check'},
    ].forEach((entry) => {
        const button = element('button', 'quick-action');
        button.type = 'button';
        const iconBox = element('span', 'qa-icon');
        iconBox.appendChild(icon(entry.symbol, 'ic ic-lg'));
        button.append(iconBox, element('span', 'qa-label', tr(entry.labelKey)), icon('ic-chevron-right', 'ic qa-chevron'));
        button.addEventListener('click', () => { showPage(entry.page); });
        actions.appendChild(button);
    });

    const stats = document.getElementById('overview-stats');
    renderSkeleton(stats, 3);

    // Both extra lists come from endpoints the shop and builder pages already use.
    const [fulfillment, documents, setup] = await Promise.all([
        api(`/guilds/${guildId}/fulfillment`).then((result) => result.data).catch(() => null),
        api(`/guilds/${guildId}/builders`).then((result) => result.data).catch(() => null),
        ensurePermissionFindings(),
    ]);

    const featureEntries = Object.values(featureState);
    const enabled = featureEntries.filter((entry) => entry.enabled).length;
    const openRequests = fulfillment ? fulfillment.filter((item) => item.status === 'open').length : null;

    stats.replaceChildren();
    [
        {
            symbol: 'ic-features',
            value: `${enabled} / ${featureEntries.length}`,
            labelKey: 'dashboard.overview_features',
        },
        {
            symbol: 'ic-inbox',
            value: openRequests === null ? '—' : String(openRequests),
            labelKey: 'dashboard.overview_open_fulfillment',
        },
        {
            symbol: 'ic-builders',
            value: documents === null ? '—' : String(documents.length),
            labelKey: 'dashboard.overview_builder_drafts',
        },
        {
            symbol: 'ic-shield-check',
            value: !setup.available ? '—'
                : String(setup.blocking + setup.degraded),
            labelKey: 'dashboard.overview_setup_findings',
        },
    ].forEach((entry) => {
        const tile = element('div', 'stat-tile');
        const iconBox = element('span', 'st-icon');
        iconBox.appendChild(icon(entry.symbol, 'ic ic-lg'));
        const meta = element('span', 'st-meta');
        meta.append(element('span', 'st-value', entry.value), element('span', 'st-label', tr(entry.labelKey)));
        tile.append(iconBox, meta);
        stats.appendChild(tile);
    });

    setSubtitle('dashboard.subtitle_overview', {enabled, total: featureEntries.length});
}

/* --------------------------------------------------------------- features */

function updateFeatureSubtitle() {
    const entries = Object.values(featureState);
    setSubtitle('dashboard.subtitle_features', {
        enabled: entries.filter((entry) => entry.enabled).length,
        total: entries.length,
    });
}

const OTHER_GROUP = 'other';

/** Group features by the group the registry declares for each one.
 *  Grouping by the first dependency instead put every casino game, every
 *  Everydle game, the shop and the gacha into one `economy` block, because
 *  they all depend on the economy. The registry also supplies the render
 *  order, so a new group appears where it was declared to. */
function featureGroups() {
    const byGroup = new Map();
    Object.entries(featureState).forEach(([key, state]) => {
        const groupKey = state.group || OTHER_GROUP;
        if (!byGroup.has(groupKey)) byGroup.set(groupKey, []);
        byGroup.get(groupKey).push([key, state]);
    });

    const ordered = new Map();
    featureGroupOrder.forEach((groupKey) => {
        if (byGroup.has(groupKey)) {
            ordered.set(groupKey, byGroup.get(groupKey));
            byGroup.delete(groupKey);
        }
    });
    // An unlisted group is shown rather than dropped, so adding one to the
    // registry without extending the order is visible instead of silent.
    byGroup.forEach((entries, groupKey) => ordered.set(groupKey, entries));
    return ordered;
}

function featureGroupLabel(groupKey) {
    return tr(`dashboard.feature_groups.${groupKey}`);
}

function renderFeatures() {
    const host = document.getElementById('feature-grid');
    host.replaceChildren();

    featureGroups().forEach((entries, groupKey) => {
        const section = element('fieldset', 'form-section');
        section.appendChild(element('legend', null, featureGroupLabel(groupKey)));
        const grid = element('div', 'feature-grid');

        entries.forEach(([key, state]) => {
            const row = element('label', 'feature-row');
            const text = element('span', 'feature-text');
            text.appendChild(element('span', 'feature-name', tr(state.locale_key)));

            const blockers = (state.dependencies || []).filter(
                (dependency) => featureState[dependency]?.enabled === false,
            );
            if (blockers.length) {
                const names = blockers.map((dependency) => tr(featureState[dependency].locale_key)).join(', ');
                text.appendChild(element('span', 'feature-dep', format('dashboard.feature_requires', {features: names})));
            }

            const switchWrap = element('span', 'switch');
            const input = document.createElement('input');
            input.type = 'checkbox';
            input.checked = state.enabled;
            input.disabled = blockers.length > 0 && !state.enabled;
            input.addEventListener('change', () => saveFeature(key, input, state.revision));
            switchWrap.append(input, element('span', 'track'));

            row.append(text, switchWrap);
            grid.appendChild(row);
        });

        section.appendChild(grid);
        host.appendChild(section);
    });
}

/** Features that would cascade off with this one, for the confirmation prompt.
 *  The database disables dependants transitively, so listing only the direct
 *  ones understated what the operator was about to turn off: disabling
 *  `economy` also takes `shop_gacha` with it through `shop`. */
function dependentFeatures(key) {
    const affected = new Set();
    const pending = [key];
    while (pending.length) {
        const parent = pending.pop();
        Object.entries(featureState).forEach(([other, state]) => {
            if (other === key || affected.has(other) || !state.enabled) return;
            if ((state.dependencies || []).includes(parent)) {
                affected.add(other);
                pending.push(other);
            }
        });
    }
    return [...affected].map((other) => tr(featureState[other].locale_key));
}

async function saveFeature(key, input, revision) {
    const enabling = input.checked;
    if (!enabling) {
        const dependents = dependentFeatures(key);
        if (dependents.length) {
            const accepted = await confirmAction(
                format('dashboard.feature_cascade_confirm', {features: dependents.join(', ')}),
            );
            if (!accepted) {
                input.checked = true;
                return;
            }
        }
    }

    input.disabled = true;
    try {
        const result = await api(`/guilds/${guildId}/features`, {
            method: 'POST',
            headers: headers(),
            body: JSON.stringify({feature_key: key, enabled: enabling, revision}),
        });
        toast(result.message);
    } catch (error) {
        handleApiError(error);
    }
    await loadGuild();
}

/* --------------------------------------------------------------- settings */

function visibleDefinitions(category) {
    return Object.values(registry).filter((definition) => definition.category === category
        && (!definition.owner_feature || featureState[definition.owner_feature]?.enabled !== false));
}

function renderSettings(category) {
    document.getElementById('settings-heading').textContent = tr(`dashboard.category_${category}`);
    const host = document.getElementById('settings-grid');
    host.replaceChildren();

    const definitions = visibleDefinitions(category);
    if (!definitions.length) {
        host.appendChild(emptyState('dashboard.no_settings', CATEGORY_ICONS[category] || 'ic-administration'));
        setSubtitle('dashboard.subtitle_settings', {count: 0});
        return;
    }

    // The registry already assigns every definition to a page; use it as the
    // section grouping instead of rendering one flat wall of inputs.
    const pages = new Map();
    definitions.forEach((definition) => {
        const page = definition.page || 'general';
        if (!pages.has(page)) pages.set(page, []);
        pages.get(page).push(definition);
    });

    pages.forEach((entries, page) => {
        const section = element('fieldset', 'form-section');
        section.dataset.page = page;
        const legend = element('legend', null, tr(`dashboard.pages.${page}`));
        // Filled in by refreshSettingsDirtyState so a section with unsaved
        // edits says so next to its own title, rather than the operator having
        // to guess whether the one Save button at the bottom applies to it.
        legend.appendChild(pill('dashboard.unsaved', 'pending'));
        section.appendChild(legend);
        const grid = element('div', 'form-grid');

        entries.forEach((definition) => {
            const wrapsButton = ['channel', 'role', 'channel_list', 'role_list']
                .includes(definition.value_type);
            const group = element(wrapsButton ? 'div' : 'label', 'input-group');
            if (['string_list', 'json', 'channel_list', 'role_list'].includes(definition.value_type)) {
                group.classList.add('wide');
            }
            group.appendChild(element('span', 'field-label', tr(definition.locale_key)));

            group.dataset.setting = definition.key;
            const input = settingInput(definition, settings[definition.key]?.value);
            input.dataset.key = definition.key;
            group.appendChild(input);

            const badge = element('small', `apply-${definition.apply_behavior}`,
                tr(`dashboard.apply_${definition.apply_behavior}`));
            group.appendChild(badge);
            // An installation-wide setting is edited from a guild page but is
            // not that guild's. Saying so is the only way the interface can
            // express it — the API cannot reject a legitimate save, and an
            // operator changing "the language" for one server and finding it
            // changed everywhere would be right to call that a bug.
            if (definition.scope === 'instance') {
                group.appendChild(element('small', 'field-scope',
                    tr('dashboard.scope_instance')));
            }
            grid.appendChild(group);
        });

        section.appendChild(grid);
        host.appendChild(section);
    });

    refreshSettingsDirtyState();
    setSubtitle('dashboard.subtitle_settings', {count: definitions.length});
    // Deliberately not awaited: the diagnostic is an enhancement, and the form
    // must be usable before it answers — or if it never does.
    applyPermissionNotes();
}

/** The permission diagnostic, indexed by the setting each finding is about.
 *
 *  Same report the permissions page renders, so the two can never disagree. A
 *  standalone dashboard has no in-process bot and answers 503; the notes are an
 *  enhancement, so that has to read as "nothing to add" rather than an error.
 */
async function ensurePermissionFindings() {
    if (permissionFindings) return permissionFindings;
    const subjects = {};
    let blocking = 0;
    let degraded = 0;
    let available = false;
    try {
        const report = (await api(`/guilds/${guildId}/permissions`)).data;
        available = true;
        blocking = report.blocking_count;
        degraded = report.degraded_count;
        report.findings.forEach((finding) => {
            if (!finding.subject) return;
            (subjects[finding.subject] = subjects[finding.subject] || []).push(finding);
        });
    } catch (error) {
        // A standalone dashboard has no in-process bot and answers 503. The
        // report is an enhancement, so its absence reads as "nothing to add".
    }
    permissionFindings = {subjects, blocking, degraded, available};
    return permissionFindings;
}

/** Put each finding under the field it concerns.
 *  The permissions page explains what is wrong installation-wide; this is the
 *  same sentence where an operator would actually act on it. */
async function applyPermissionNotes() {
    const found = await ensurePermissionFindings();
    document.querySelectorAll('#settings-grid .input-group[data-setting]').forEach((group) => {
        group.querySelectorAll('.field-finding').forEach((node) => node.remove());
        (found.subjects[group.dataset.setting] || []).forEach((finding) => {
            const note = element('div', `field-finding ${finding.severity}`);
            note.appendChild(icon('ic-alert', 'ic ic-sm'));
            note.appendChild(element('span', null, permissionFindingText(finding)));
            group.appendChild(note);
        });
    });
}

/** The edits the form would submit, or null when one field cannot be parsed. */
function collectSettingChanges() {
    try {
        // Only dirty fields are submitted, so one edit does not bump every
        // revision in the category or write an audit row per untouched setting.
        return [...document.querySelectorAll('#settings-grid [data-key]')]
            .map((input) => {
                const key = input.dataset.key;
                return {
                    key,
                    value: readSettingInput(registry[key], input),
                    revision: settings[key]?.revision || 0,
                };
            })
            .filter((change) => JSON.stringify(change.value)
                !== JSON.stringify(settings[change.key]?.value ?? null));
    } catch (error) {
        return null;
    }
}

/** Show what is unsaved: a count on the apply button and a pill per section.
 *  A JSON field mid-edit cannot be parsed, so the count is unknown rather than
 *  zero — reporting zero there would disable the only way to save. */
function refreshSettingsDirtyState() {
    const form = document.getElementById('settings-form');
    const submit = form.querySelector('button[type="submit"]');
    const reset = form.querySelector('button[type="reset"]');
    const changes = collectSettingChanges();
    const dirtyKeys = new Set((changes || []).map((change) => change.key));

    submit.textContent = changes && changes.length
        ? format('dashboard.save_settings_count', {count: changes.length})
        : tr('dashboard.save_settings');
    submit.disabled = changes !== null && changes.length === 0;
    reset.disabled = submit.disabled;

    document.querySelectorAll('#settings-grid .form-section').forEach((section) => {
        const dirty = [...section.querySelectorAll('[data-key]')]
            .some((input) => dirtyKeys.has(input.dataset.key));
        section.querySelector('legend .pill')?.classList.toggle('hidden', !dirty);
    });
}

/* Channel kinds draw an icon from the inline sprite. A native <option> could not
 * hold an SVG, which is why this used to be a text glyph; the picker's rows are
 * buttons, so the marker can be the same iconography as the rest of the page. */
const CHANNEL_TYPE_ICONS = {
    text: 'ic-ch-text', news: 'ic-ch-news', voice: 'ic-ch-voice',
    stage_voice: 'ic-ch-stage', forum: 'ic-ch-forum', category: 'ic-ch-category',
};

function channelIconId(type) {
    return CHANNEL_TYPE_ICONS[type] || 'ic-ch-text';
}

/** Channels a definition may name, in Discord's own order.
 *  `channel_types` narrows the list to the kinds that can actually work: a
 *  ticket category has to be a category and a voice lobby has to be a voice
 *  channel, and offering the other 40 only invites a setting that silently
 *  never fires. */
function selectableChannels(definition) {
    const allowed = definition.channel_types || [];
    const categories = new Map(
        resources.channels
            .filter((channel) => channel.type === 'category')
            .map((channel) => [String(channel.id), channel.name]),
    );
    return resources.channels
        .filter((channel) => !allowed.length || allowed.includes(channel.type))
        .map((channel) => ({
            ...channel,
            groupLabel: channel.parent_id
                ? categories.get(String(channel.parent_id)) || ''
                : '',
        }))
        .sort((left, right) => left.groupLabel.localeCompare(right.groupLabel)
            || (left.position - right.position)
            || left.name.localeCompare(right.name));
}

/** Build one channel or role dropdown.
 *  A stored id that is not in the live resource list — a deleted channel, or a
 *  guild the bot cannot currently see fully — is kept as its own selected
 *  option. Dropping it would have shown "no channel" and quietly cleared a
 *  working setting the first time the form was saved. */
/** The rows a picker may offer, already ordered the way Discord orders them. */
function pickerCandidates(definition) {
    if (definition.value_type.startsWith('channel')) {
        return selectableChannels(definition).map((channel) => ({
            id: String(channel.id),
            name: channel.name,
            group: channel.groupLabel || '',
            symbol: channelIconId(channel.type),
        }));
    }
    // Only a setting whose role the bot has to *grant* may be filtered by
    // grantability. `premium_roles` and `admin_roles` merely recognise
    // membership, and those roles sit above the bot deliberately — filtering
    // them out made them unselectable and dropped them on the next save.
    const mustGrant = Boolean(definition.role_must_be_assignable);
    return [...resources.roles]
        .filter((role) => !mustGrant || role.manageable)
        .sort((left, right) => (right.position || 0) - (left.position || 0))
        .map((role) => ({
            id: String(role.id),
            name: `@${role.name}`,
            group: '',
            symbol: null,
            // 0 is Discord's "no colour"; those roles inherit the text tone.
            color: role.color ? `#${role.color.toString(16).padStart(6, '0')}` : null,
            // Shown as a hint, never as a reason to hide the row: a
            // recognition-only setting is allowed to name a role above the bot.
            unmanageable: !role.manageable,
        }));
}

/** Build one channel or role picker.
 *
 *  A hidden <select> stays the value carrier, so `readSettingInput`,
 *  `collectSettingChanges` and a native form reset all keep working against a
 *  real form control; the visible part is a listbox of buttons, which is what
 *  lets a row carry an SVG and a category heading read as a heading.
 *
 *  A stored id that is not in the live resource list — a deleted channel, or a
 *  guild the bot cannot currently see fully — is kept as its own selected chip.
 *  Dropping it would have shown "no channel" and quietly cleared a working
 *  setting the first time the form was saved, so it is surfaced instead, with a
 *  control that clears the dead ones deliberately.
 */
/** The leading marker for one row or chip: a channel icon, or Discord's own
 *  role colour as a dot. A role with no colour set gets a hollow dot rather
 *  than a filled one, which is what Discord shows too. */
function pickerMarker(entry) {
    if (entry.symbol) return icon(entry.symbol, 'ic ic-sm picker-option-icon');
    const dot = element('span', `role-dot${entry.color ? '' : ' role-dot-empty'}`);
    if (entry.color) dot.style.setProperty('--role-color', entry.color);
    return dot;
}

/** A row editor for a role menu: {label: {id, emoji}}.
 *
 *  The same four decisions the resource picker documents apply here, and the
 *  first is the load-bearing one: **a hidden textarea stays the value carrier**.
 *  `readSettingInput`, `collectSettingChanges`, the dirty-state check and a
 *  native form reset all act on a real form control, so this editor adds a way
 *  to *edit* the value without becoming a second way to *save* it.
 *
 *  Role ids stay strings throughout. A Discord id is 64-bit and a JavaScript
 *  number holds 53 bits, so calling Number() on one here would round it and the
 *  save would write a role that does not exist.
 *
 *  Entry order is preserved, because `collectSettingChanges` compares
 *  JSON.stringify against the loaded value and a reordered object would read as
 *  a change nobody made.
 */
function roleMenuEditor(definition, value) {
    const entries = (value && typeof value === 'object' && !Array.isArray(value))
        ? value : {};

    const wrapper = element('div', 'role-menu-editor');
    const carrier = document.createElement('textarea');
    carrier.className = 'menu-carrier';
    carrier.hidden = true;
    wrapper.appendChild(carrier);

    const rows = element('div', 'menu-rows');
    wrapper.appendChild(rows);

    const serialise = () => {
        const collected = {};
        rows.querySelectorAll('.menu-row').forEach((row) => {
            const label = row.querySelector('.menu-label').value.trim();
            if (!label) return;
            const picker = row.querySelector('.picker-carrier');
            const chosen = [...picker.selectedOptions].map((option) => option.value);
            collected[label] = {
                id: chosen[0] || '0',
                emoji: row.querySelector('.menu-emoji').value.trim(),
            };
        });
        carrier.value = JSON.stringify(collected);
        // Dispatched from the carrier so the dirty-state listener, which is
        // bound to the real control, sees it.
        carrier.dispatchEvent(new Event('change', { bubbles: true }));
    };

    const addRow = (label, entry) => {
        const row = element('div', 'menu-row');

        const labelInput = document.createElement('input');
        labelInput.type = 'text';
        labelInput.className = 'menu-label';
        labelInput.value = label;
        labelInput.placeholder = tr('dashboard.role_menu_label');
        labelInput.addEventListener('input', serialise);
        row.appendChild(labelInput);

        // A synthetic single-role definition, so the picker is the same one every
        // other role field uses rather than a second implementation. The key is
        // suffixed because `applyPermissionNotes` matches findings on it and a
        // per-row note would have nowhere sensible to go.
        const picker = resourcePicker(
            { ...definition, key: `${definition.key}.entry`, value_type: 'role' },
            entry && entry.id && String(entry.id) !== '0' ? String(entry.id) : null,
        );
        picker.querySelector('.picker-carrier').addEventListener('change', serialise);
        row.appendChild(picker);

        const emojiInput = document.createElement('input');
        emojiInput.type = 'text';
        emojiInput.className = 'menu-emoji';
        emojiInput.value = (entry && entry.emoji) || '';
        emojiInput.placeholder = tr('dashboard.role_menu_emoji');
        emojiInput.addEventListener('input', serialise);
        row.appendChild(emojiInput);

        const remove = element('button', 'menu-remove icon-button');
        remove.type = 'button';
        remove.title = tr('dashboard.role_menu_remove');
        remove.appendChild(icon('ic-trash', 'ic ic-sm'));
        remove.addEventListener('click', () => { row.remove(); serialise(); });
        row.appendChild(remove);

        rows.appendChild(row);
        return row;
    };

    Object.entries(entries).forEach(([label, entry]) => addRow(label, entry));

    const add = element('button', 'menu-add btn-ghost');
    add.type = 'button';
    add.appendChild(icon('ic-plus', 'ic ic-sm'));
    add.appendChild(document.createTextNode(tr('dashboard.role_menu_add')));
    add.addEventListener('click', () => {
        const row = addRow('', null);
        row.querySelector('.menu-label').focus();
    });
    wrapper.appendChild(add);

    serialise();
    return wrapper;
}


function resourcePicker(definition, value) {
    const isList = definition.value_type.endsWith('_list');
    const chosen = isList ? (Array.isArray(value) ? value : []) : [value];
    const selected = new Set(
        chosen.filter((entry) => entry !== null && entry !== undefined && entry !== '')
            .map(String),
    );

    const candidates = pickerCandidates(definition);
    const known = new Map(candidates.map((entry) => [entry.id, entry]));

    const wrapper = element('div', `resource-picker${isList ? ' multi' : ''}`);
    const carrier = document.createElement('select');
    carrier.className = 'picker-carrier';
    carrier.multiple = isList;
    carrier.tabIndex = -1;
    carrier.setAttribute('aria-hidden', 'true');
    if (!isList) carrier.add(new Option('', ''));
    candidates.forEach((entry) => {
        const option = new Option(entry.name, entry.id);
        option.selected = selected.has(entry.id);
        carrier.add(option);
    });
    [...selected].filter((id) => !known.has(id)).forEach((id) => {
        const option = new Option(format('dashboard.resource_unavailable', {id}), id);
        option.selected = true;
        carrier.add(option);
    });

    const trigger = element('button', 'picker-trigger');
    trigger.type = 'button';
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    const chips = element('span', 'picker-chips');
    trigger.append(chips, icon('ic-chevron-down', 'ic ic-sm picker-caret'));
    wrapper.append(carrier, trigger);

    const optionFor = (id) => [...carrier.options].find((entry) => entry.value === String(id));
    const selectedIds = () => [...carrier.selectedOptions]
        .map((entry) => entry.value).filter(Boolean);

    function apply(id, on) {
        if (!isList) {
            [...carrier.options].forEach((entry) => { entry.selected = false; });
            const target = optionFor(on ? id : '');
            if (target) target.selected = true;
        } else {
            const target = optionFor(id);
            if (target) target.selected = on;
        }
        renderChips();
        // The carrier is what the dirty-state listener watches, so the change
        // has to be announced from it rather than from the button.
        carrier.dispatchEvent(new Event('change', {bubbles: true}));
    }

    function renderChips() {
        chips.replaceChildren();
        const ids = selectedIds();
        if (!ids.length) {
            chips.appendChild(element('span', 'picker-placeholder',
                tr(isList ? 'dashboard.picker_none_selected' : 'dashboard.resource_none')));
            return;
        }
        ids.forEach((id) => {
            const entry = known.get(id);
            const chip = element('span', `picker-chip${entry ? '' : ' unavailable'}`);
            chip.appendChild(entry ? pickerMarker(entry) : icon('ic-alert', 'ic ic-sm'));
            chip.appendChild(element('span', 'picker-chip-name',
                entry ? entry.name : format('dashboard.resource_unavailable', {id})));
            const remove = element('button', 'picker-chip-remove');
            remove.type = 'button';
            remove.title = tr('dashboard.picker_remove');
            remove.setAttribute('aria-label', tr('dashboard.picker_remove'));
            remove.appendChild(icon('ic-close', 'ic ic-sm'));
            remove.addEventListener('click', (event) => {
                event.stopPropagation();
                apply(id, false);
            });
            chip.appendChild(remove);
            chips.appendChild(chip);
        });
    }

    function buildList(menu, close) {
        const search = document.createElement('input');
        search.type = 'text';
        search.className = 'picker-search';
        search.placeholder = tr('dashboard.picker_search');
        search.setAttribute('aria-label', tr('dashboard.picker_search'));
        search.dataset.autofocus = 'true';
        const searchBox = element('div', 'picker-search-box');
        searchBox.append(icon('ic-search', 'ic ic-sm'), search);
        menu.appendChild(searchBox);

        const list = element('div', 'picker-list');
        list.setAttribute('role', 'listbox');
        if (isList) list.setAttribute('aria-multiselectable', 'true');
        menu.appendChild(list);

        const stale = selectedIds().filter((id) => !known.has(id));
        if (stale.length) {
            const clear = element('button', 'picker-clear-stale');
            clear.type = 'button';
            clear.appendChild(icon('ic-trash', 'ic ic-sm'));
            clear.appendChild(element('span', null,
                format('dashboard.picker_clear_unavailable', {count: stale.length})));
            clear.addEventListener('click', () => {
                stale.forEach((id) => {
                    const target = optionFor(id);
                    if (target) target.remove();
                });
                renderChips();
                carrier.dispatchEvent(new Event('change', {bubbles: true}));
                close();
                trigger.focus();
            });
            menu.appendChild(clear);
        }

        const draw = (term, focusId = null) => {
            list.replaceChildren();
            const needle = term.trim().toLowerCase();
            const shown = candidates.filter((entry) =>
                !needle || entry.name.toLowerCase().includes(needle));
            if (!shown.length) {
                list.appendChild(element('div', 'picker-empty', tr('dashboard.picker_no_match')));
                return;
            }
            let lastGroup = null;
            shown.forEach((entry) => {
                if (entry.group !== lastGroup) {
                    lastGroup = entry.group;
                    if (entry.group) list.appendChild(element('div', 'menu-section', entry.group));
                }
                const row = element('button', 'picker-option');
                row.type = 'button';
                row.dataset.id = entry.id;
                row.setAttribute('role', 'option');
                const on = Boolean(optionFor(entry.id)?.selected);
                row.setAttribute('aria-selected', String(on));
                row.appendChild(pickerMarker(entry));
                row.appendChild(element('span', 'picker-option-name', entry.name));
                if (entry.unmanageable) {
                    const hint = element('span', 'picker-option-hint',
                        tr('dashboard.picker_above_bot'));
                    hint.title = tr('dashboard.picker_above_bot_hint');
                    row.appendChild(hint);
                }
                if (on) row.appendChild(icon('ic-check', 'ic ic-sm picker-check'));
                row.addEventListener('click', () => {
                    const next = isList ? !on : true;
                    apply(entry.id, next);
                    if (!isList) {
                        close();
                        trigger.focus();
                        return;
                    }
                    draw(search.value, entry.id);
                });
                list.appendChild(row);
            });
            if (focusId) list.querySelector(`[data-id="${focusId}"]`)?.focus();
        };

        search.addEventListener('input', () => draw(search.value));
        draw('');
    }

    popover(trigger, buildList, {
        role: 'dialog', itemRole: 'option', ariaLabel: tr(definition.locale_key),
        align: 'left', matchWidth: true, className: 'picker-menu',
    });

    renderChips();
    return wrapper;
}

/** Label one allowed value of a constrained setting.
 *  The language list is the only one so far and it already has display names
 *  under `dashboard.languages.*`; anything else falls back to the raw value
 *  rather than rendering a missing key. */
/** The label for one constrained value, from the prefix its setting declares.
 *
 *  Declared in the registry rather than matched on the setting's key here:
 *  several settings share one set of choices — every warn action reads from the
 *  same four — and `language` used to be special-cased in this function, which
 *  is exactly how the interface starts carrying its own copy of a list.
 *
 *  With no prefix, or no catalog entry, the raw value is the label. That is
 *  safe because a choice is always a stable English identifier, so an
 *  unlabelled one reads as itself instead of as a bracketed key.
 */
function choiceLabel(definition, choice) {
    const prefix = definition.choice_locale_prefix;
    if (!prefix) return choice;
    const label = tr(`${prefix}.${choice}`);
    return label.startsWith('[') ? choice : label;
}

function settingInput(definition, storedValue) {
    // A guild with no row yet returns no value at all, so fall back to the
    // registry default before any type-specific handling touches it.
    const value = storedValue === undefined ? definition.default : storedValue;

    if (definition.value_type === 'boolean') {
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.checked = Boolean(value);
        return input;
    }

    if (['channel', 'role', 'channel_list', 'role_list'].includes(definition.value_type)) {
        return resourcePicker(definition, value);
    }

    // A JSON setting whose shape the registry declares gets a typed editor
    // instead of a text box full of braces. The shape is declared server-side so
    // the API validates the same structure this renders.
    if (definition.json_shape === 'role_menu') {
        return roleMenuEditor(definition, value);
    }

    if (['string_list', 'json'].includes(definition.value_type)) {
        const input = document.createElement('textarea');
        input.value = JSON.stringify(value ?? null, null, 2);
        return input;
    }

    // A constrained string is a dropdown, not a text box: the API rejects
    // anything outside the list, so a free-text field would only ever produce
    // an unexplained rejection.
    if ((definition.choices || []).length) {
        const input = document.createElement('select');
        definition.choices.forEach((choice) => {
            const option = new Option(choiceLabel(definition, choice), choice);
            option.selected = String(value) === String(choice);
            input.add(option);
        });
        return input;
    }

    const input = document.createElement('input');
    input.type = definition.value_type === 'integer' ? 'number' : 'text';
    if (definition.minimum !== null && definition.minimum !== undefined) input.min = definition.minimum;
    if (definition.maximum !== null && definition.maximum !== undefined) input.max = definition.maximum;
    input.value = value ?? '';
    return input;
}

function readSettingInput(definition, field) {
    // Both typed editors wrap a real form control, and this is where that pays
    // off: everything downstream — the save, the dirty check, a form reset —
    // keeps acting on a control rather than on a widget.
    let input = field;
    if (field.classList?.contains('resource-picker')) {
        input = field.querySelector('.picker-carrier');
    } else if (field.classList?.contains('role-menu-editor')) {
        input = field.querySelector('.menu-carrier');
    }
    if (definition.value_type === 'boolean') return input.checked;
    if (definition.value_type === 'integer') return Number(input.value);
    // Snowflakes stay strings. `Number("1420070400000000001")` is ...200,
    // because a Discord id is 64-bit and a JavaScript number holds 53 bits, so
    // converting here corrupted every channel and role the dashboard saved.
    // The API sends and accepts them as strings, and normalises to int on save.
    if (['channel', 'role'].includes(definition.value_type)) return input.value || null;
    if (['channel_list', 'role_list'].includes(definition.value_type)) {
        return [...input.selectedOptions].map((option) => option.value);
    }
    if (['string_list', 'json'].includes(definition.value_type)) return JSON.parse(input.value);
    return input.value;
}

async function saveSettings(event) {
    event.preventDefault();
    const submit = event.target.querySelector('button[type="submit"]');
    const changes = collectSettingChanges();

    if (changes === null) {
        toast(tr('dashboard.invalid_json'), true);
        return;
    }

    if (!changes.length) {
        toast(tr('dashboard.nothing_changed'));
        return;
    }

    submit.disabled = true;
    try {
        const result = await api(`/guilds/${guildId}/settings`, {
            method: 'PATCH',
            headers: headers(),
            body: JSON.stringify({changes}),
        });
        toast(result.message);
        await loadGuild();
    } catch (error) {
        handleApiError(error);
    } finally {
        submit.disabled = false;
    }
}

/* ------------------------------------------------------------------ gacha */

const GACHA_INTEGER_FIELDS = [
    'cost', 'hard_pity', 'soft_pity_start', 'soft_pity_multiplier',
    'four_star_guarantee_interval', 'duplicate_percent',
];
const GACHA_TIER_FIELDS = ['tier_3', 'tier_4', 'tier_5'];
const TIER_SCALE = 1000;

/** Banner picker, name field and delete control for the selected banner. */
function renderGachaBannerBar() {
    const bar = document.getElementById('gacha-banner-bar');
    bar.replaceChildren();
    if (!gacha) return;

    bar.appendChild(element('span', 'eyebrow', tr('dashboard.gacha_banner')));

    const picker = document.createElement('select');
    picker.id = 'gacha-banner-picker';
    gachaBanners.forEach((banner) => {
        const option = new Option(banner.display_name, banner.banner_key);
        option.selected = banner.banner_key === activeBannerKey;
        picker.add(option);
    });
    picker.addEventListener('change', () => {
        activeBannerKey = picker.value;
        gacha = gachaBanners.find((banner) => banner.banner_key === activeBannerKey);
        renderGacha();
    });
    bar.appendChild(picker);

    const name = document.createElement('input');
    name.type = 'text';
    name.id = 'gacha-banner-display-name';
    name.maxLength = 64;
    name.value = gacha.display_name;
    name.setAttribute('aria-label', tr('dashboard.gacha_banner_name'));
    bar.appendChild(name);

    if (gacha.is_default) {
        // `/gacha` with no argument resolves to it, so it is not deletable.
        bar.appendChild(pill('dashboard.gacha_banner_default', 'neutral'));
        return;
    }
    const remove = element('button', 'btn btn-outline danger btn-sm', tr('dashboard.delete'));
    remove.type = 'button';
    remove.addEventListener('click', () => deleteGachaBanner(gacha));
    bar.appendChild(remove);
}

async function deleteGachaBanner(banner) {
    if (!await confirmAction(tr('dashboard.gacha_banner_delete_confirm'))) return;
    try {
        const result = await api(
            `/guilds/${guildId}/gacha/banners/${encodeURIComponent(banner.banner_key)}`,
            {method: 'DELETE', headers: headers(),
             body: JSON.stringify({revision: banner.revision})},
        );
        toast(result.message);
        activeBannerKey = null;
    } catch (error) {
        await handleWriteConflict(error, loadGuild);
        return;
    }
    await loadGuild();
}

async function createGachaBanner(event) {
    event.preventDefault();
    const submit = event.target.querySelector('button[type="submit"]');
    const form = new FormData(event.target);
    submit.disabled = true;
    try {
        const result = await api(`/guilds/${guildId}/gacha/banners`, {
            method: 'POST',
            headers: headers(),
            body: JSON.stringify({
                banner_key: String(form.get('banner_key') || '').trim(),
                display_name: String(form.get('display_name') || '').trim(),
            }),
        });
        toast(result.message);
        event.target.reset();
        // Open the banner that was just created, so its rewards can be filled in.
        activeBannerKey = result.data.banner_key;
        await loadGuild();
    } catch (error) {
        handleApiError(error);
    } finally {
        submit.disabled = false;
    }
}

function renderGacha() {
    renderGachaBannerBar();
    const grid = document.getElementById('gacha-grid');
    grid.replaceChildren();

    const config = gacha?.config;
    if (!config || !config.tiers) {
        document.getElementById('gacha-total').replaceChildren();
        grid.appendChild(emptyState('dashboard.gacha_unavailable', 'ic-gacha'));
        return;
    }

    const values = {enabled: gacha.enabled};
    GACHA_INTEGER_FIELDS.forEach((key) => { values[key] = config[key]; });
    GACHA_TIER_FIELDS.forEach((key) => {
        values[key] = (config.tiers[key.slice(-1)] ?? 0) / TIER_SCALE;
    });

    Object.entries(values).forEach(([key, value]) => {
        const group = element('label', 'input-group');
        group.appendChild(element('span', 'field-label', tr(`dashboard.gacha_${key}`)));
        const input = document.createElement('input');
        input.name = key;
        if (key === 'enabled') {
            input.type = 'checkbox';
            input.checked = Boolean(value);
        } else {
            input.type = 'number';
            input.value = value;
            input.step = GACHA_TIER_FIELDS.includes(key) ? '0.001' : '1';
            if (GACHA_TIER_FIELDS.includes(key)) input.addEventListener('input', updateGachaTotal);
        }
        group.appendChild(input);
        grid.appendChild(group);
    });

    const rewardGroup = element('div', 'input-group wide');
    rewardGroup.append(
        element('span', 'field-label', tr('dashboard.gacha_rewards')),
        renderRewardTable(config.rewards),
    );
    grid.appendChild(rewardGroup);

    const multiplier = document.querySelector('#gacha-grid [name="soft_pity_multiplier"]');
    if (multiplier) multiplier.addEventListener('input', updateGachaTotal);
    updateGachaTotal();
}

const REWARD_KINDS = ['coins', 'item', 'vault', 'voucher'];

const REWARD_COLUMNS = 7;

/** Editable reward rows, replacing the raw JSON textarea.
 *
 * Rows carry their tier, key and kind in data attributes so the form can be
 * read back without holding a parallel copy of the model in a variable, and so
 * a row added here survives a save.
 */
function renderRewardTable(rewards) {
    const wrap = element('div', 'table-wrap');
    const node = table([
        'dashboard.column_reward_key', 'dashboard.column_reward_kind',
        'dashboard.column_reward_amount', 'dashboard.column_reward_weight',
        'dashboard.column_reward_chance', 'dashboard.column_status',
        'dashboard.column_actions',
    ]);
    const body = node.querySelector('tbody');

    GACHA_TIER_FIELDS.map((field) => field.slice(-1)).forEach((tier) => {
        const entries = rewards[tier] || [];

        const heading = element('tr', 'reward-tier-row');
        heading.dataset.tierHeading = tier;
        const cell = element('td');
        cell.colSpan = REWARD_COLUMNS;
        const add = element('button', 'btn btn-ghost btn-sm', tr('dashboard.gacha_add_reward'));
        add.type = 'button';
        add.addEventListener('click', () => addRewardRow(body, tier));
        cell.append(
            element('span', null, format('dashboard.gacha_tier_heading', {tier})),
            add,
        );
        heading.appendChild(cell);
        body.appendChild(heading);

        entries.forEach((entry) => {
            body.appendChild(rewardRow(tier, entry, false));
        });
    });

    wrap.appendChild(node);
    // Chances depend on the enabled rows' weights, so compute after insertion.
    setTimeout(updateRewardChances, 0);
    return wrap;
}

/** One reward row.
 *
 * A stored row shows its key and kind as text: they are the stable identifiers
 * a recorded pull references, and renaming one would orphan that history. A row
 * being added has not been drawn yet, so it picks both.
 */
function rewardRow(tier, entry, isNew) {
    const row = element('tr');
    row.dataset.tier = tier;
    row.dataset.key = entry.key || '';
    row.dataset.kind = entry.kind;

    if (isNew) {
        row.classList.add('reward-new');
        row.appendChild(rewardKeyCell(row));
        row.appendChild(rewardKindCell(row));
    } else {
        row.appendChild(element('td', 'cell-key', entry.key));
        row.appendChild(element('td', null, tr(`dashboard.reward_kind_${entry.kind}`)));
    }

    row.appendChild(numberCell('amount', entry.amount, 1));
    row.appendChild(numberCell('weight', entry.weight, 1));
    row.appendChild(element('td', 'cell-mono reward-chance', ''));

    const statusCell = element('td');
    const switchWrap = element('span', 'switch');
    const toggle = document.createElement('input');
    toggle.type = 'checkbox';
    toggle.dataset.field = 'enabled';
    toggle.checked = entry.enabled !== false;
    toggle.addEventListener('change', updateRewardChances);
    switchWrap.append(toggle, element('span', 'track'));
    statusCell.appendChild(switchWrap);
    row.appendChild(statusCell);

    const actionCell = element('td');
    const remove = element('button', 'btn btn-ghost btn-icon');
    remove.type = 'button';
    remove.setAttribute('aria-label', tr('dashboard.delete'));
    remove.appendChild(icon('ic-trash', 'ic ic-sm'));
    remove.addEventListener('click', async () => {
        // Removing a reward only stops future draws. Recorded pulls keep their
        // own reward payload and banner revision, so history stays readable.
        if (!isNew) {
            const accepted = await confirmAction(
                format('dashboard.gacha_reward_delete_confirm', {reward: entry.key}),
            );
            if (!accepted) return;
        }
        row.remove();
        updateRewardChances();
    });
    actionCell.appendChild(remove);
    row.appendChild(actionCell);

    return row;
}

/** Key control for a new row: catalog items are picked, coins and vouchers are
 *  free text because they name an amount or a duration rather than an object. */
function rewardKeyCell(row) {
    const cell = element('td', 'cell-key');
    const select = document.createElement('select');
    select.dataset.field = 'key';
    const input = document.createElement('input');
    input.type = 'text';
    input.dataset.field = 'key';
    input.hidden = true;

    const refresh = () => {
        const kind = row.dataset.kind;
        const options = (itemCatalog || []).filter((item) => item.gacha_kind === kind);
        select.replaceChildren();
        options.forEach((item) => {
            const option = document.createElement('option');
            option.value = item.key;
            option.textContent = item.key;
            select.appendChild(option);
        });
        const picked = options.length > 0;
        select.hidden = !picked;
        input.hidden = picked;
        // A catalog vault always awards its catalog reserve, so the amount is
        // shown but not editable; the server rejects any other value.
        syncRewardAmount(row);
    };

    select.addEventListener('change', () => syncRewardAmount(row));
    cell.append(select, input);
    row.addEventListener('reward-kind-change', refresh);
    setTimeout(refresh, 0);
    return cell;
}

function rewardKindCell(row) {
    const cell = element('td');
    const select = document.createElement('select');
    select.dataset.field = 'kind';
    REWARD_KINDS.forEach((kind) => {
        const option = document.createElement('option');
        option.value = kind;
        option.textContent = tr(`dashboard.reward_kind_${kind}`);
        select.appendChild(option);
    });
    select.value = row.dataset.kind;
    select.addEventListener('change', () => {
        row.dataset.kind = select.value;
        row.dispatchEvent(new Event('reward-kind-change'));
    });
    cell.appendChild(select);
    return cell;
}

/** Pin a catalog vault's amount to its shared reserve. */
function syncRewardAmount(row) {
    const amount = row.querySelector('[data-field="amount"]');
    const key = readRewardKey(row);
    const item = (itemCatalog || []).find((entry) => entry.key === key);
    if (item && item.effect === 'vault') {
        amount.value = String(item.value);
        amount.readOnly = true;
    } else {
        amount.readOnly = false;
    }
    updateRewardChances();
}

function readRewardKey(row) {
    const field = [...row.querySelectorAll('[data-field="key"]')].find((node) => !node.hidden);
    return field ? field.value.trim() : row.dataset.key;
}

function addRewardRow(body, tier) {
    const row = rewardRow(tier, {key: '', kind: 'item', amount: 1, weight: 1}, true);
    // Land under this tier's heading even when every row of the tier was
    // removed, which is why the anchor falls back to the heading itself.
    const siblings = [...body.querySelectorAll(`tr[data-tier="${tier}"]`)];
    const last = siblings[siblings.length - 1]
        || body.querySelector(`tr[data-tier-heading="${tier}"]`);
    body.insertBefore(row, last ? last.nextSibling : null);
    updateRewardChances();
}

function numberCell(field, value, minimum) {
    const cell = element('td');
    const input = document.createElement('input');
    input.type = 'number';
    input.min = String(minimum);
    input.step = '1';
    input.value = String(value);
    input.dataset.field = field;
    input.addEventListener('input', updateRewardChances);
    cell.appendChild(input);
    return cell;
}

/** Show each row's real draw chance within its tier, enabled rows only. */
function updateRewardChances() {
    const rows = [...document.querySelectorAll('#gacha-grid tbody tr[data-tier]')];
    const totals = {};
    rows.forEach((row) => {
        if (!row.querySelector('[data-field="enabled"]').checked) return;
        const weight = Number(row.querySelector('[data-field="weight"]').value) || 0;
        totals[row.dataset.tier] = (totals[row.dataset.tier] || 0) + weight;
    });
    rows.forEach((row) => {
        const target = row.querySelector('.reward-chance');
        const enabled = row.querySelector('[data-field="enabled"]').checked;
        if (!enabled) {
            target.textContent = '—';
            row.classList.add('reward-disabled');
            return;
        }
        row.classList.remove('reward-disabled');
        const weight = Number(row.querySelector('[data-field="weight"]').value) || 0;
        const total = totals[row.dataset.tier] || 0;
        target.textContent = total ? `${((weight / total) * 100).toFixed(2)} %` : '—';
    });
}

/** Rebuild the rewards object from the table.
 *
 * Key and kind come from the row itself rather than from an index into the
 * stored config, so a row added in the interface is saved and a removed one
 * disappears instead of being resurrected by its old position.
 */
function readRewardTable() {
    const rewards = {};
    GACHA_TIER_FIELDS.forEach((field) => { rewards[field.slice(-1)] = []; });

    document.querySelectorAll('#gacha-grid tbody tr[data-tier]').forEach((row) => {
        rewards[row.dataset.tier].push({
            key: readRewardKey(row),
            kind: row.dataset.kind,
            amount: Number(row.querySelector('[data-field="amount"]').value),
            weight: Number(row.querySelector('[data-field="weight"]').value),
            enabled: row.querySelector('[data-field="enabled"]').checked,
        });
    });
    return rewards;
}

function readGachaTiers() {
    const tiers = {};
    GACHA_TIER_FIELDS.forEach((key) => {
        const input = document.querySelector(`#gacha-grid [name="${key}"]`);
        tiers[key.slice(-1)] = Math.round(Number(input?.value || 0) * TIER_SCALE);
    });
    return tiers;
}

/** Mirror the two server-side banner invariants live instead of failing on save. */
function updateGachaTotal() {
    const readout = document.getElementById('gacha-total');
    const tiers = readGachaTiers();
    const total = Object.values(tiers).reduce((sum, weight) => sum + weight, 0);
    const multiplier = Number(document.querySelector('#gacha-grid [name="soft_pity_multiplier"]')?.value || 1);
    const expanded = (tiers['4'] + tiers['5']) * multiplier;

    const problems = [];
    if (total !== 100000) problems.push(format('dashboard.gacha_total_invalid', {total: (total / TIER_SCALE).toFixed(3)}));
    if (expanded > 100000) problems.push(format('dashboard.gacha_soft_pity_invalid', {total: (expanded / TIER_SCALE).toFixed(3)}));

    readout.replaceChildren();
    readout.className = `total-readout ${problems.length ? 'invalid' : 'valid'}`;
    readout.append(
        element('span', null, problems.length ? problems.join(' · ') : tr('dashboard.gacha_total_valid')),
        element('span', 'total-value', `${(total / TIER_SCALE).toFixed(3)} %`),
    );
    return problems.length === 0;
}

async function saveGacha(event) {
    event.preventDefault();
    // The form is still in the DOM when no banner could be loaded, so the
    // submit path has to say so rather than reading a null banner.
    if (!gacha) {
        toast(tr('dashboard.gacha_unavailable'), true);
        return;
    }
    if (!updateGachaTotal()) {
        toast(tr('dashboard.gacha_fix_totals'), true);
        return;
    }

    const submit = event.target.querySelector('button[type="submit"]');
    const form = new FormData(event.target);
    const config = structuredClone(gacha.config);

    GACHA_INTEGER_FIELDS.forEach((key) => { config[key] = Number(form.get(key)); });
    config.tiers = readGachaTiers();
    config.rewards = readRewardTable();

    // Every tier is still drawn, so it needs something left to award.
    const emptyTier = Object.entries(config.rewards).find(
        ([, entries]) => !entries.some((entry) => entry.enabled),
    );
    if (emptyTier) {
        toast(format('dashboard.gacha_tier_needs_one', {tier: emptyTier[0]}), true);
        return;
    }

    submit.disabled = true;
    try {
        const result = await api(`/guilds/${guildId}/gacha`, {
            method: 'PATCH',
            headers: headers(),
            body: JSON.stringify({
                enabled: event.target.enabled.checked,
                config,
                revision: gacha.revision,
                banner_key: gacha.banner_key,
                display_name: document.getElementById('gacha-banner-display-name').value.trim(),
            }),
        });
        toast(result.message);
        await loadGuild();
    } catch (error) {
        await handleWriteConflict(error, loadGuild);
    } finally {
        submit.disabled = false;
    }
}

/* -------------------------------------------------------- work responses */

let workResponses = {responses: [], tiers: [], earnings_placeholder: '{earnings}'};

async function loadWorkResponses() {
    const host = document.getElementById('work-response-list');
    renderSkeleton(host, 4);
    try {
        workResponses = (await api(`/guilds/${guildId}/work-responses`)).data;
    } catch (error) {
        host.replaceChildren(emptyState('dashboard.load_failed', 'ic-work'));
        return;
    }
    renderWorkResponseTable(host);
    const own = workResponses.responses.filter((entry) => entry.scope !== 'default');
    if (own.length === 0) {
        addPageAction('dashboard.work_copy_defaults', 'ic-work', copyWorkDefaults);
    }
    setSubtitle('dashboard.subtitle_work_responses', {count: own.length});
}

/** Copy the installation defaults into this guild so they can be edited.
 *  Without this an operator who wants to adjust one shipped line has to retype
 *  it; there is no dedicated endpoint because creating them one at a time is
 *  exactly what the existing route does. */
async function copyWorkDefaults() {
    const defaults = workResponses.responses.filter((entry) => entry.scope === 'default');
    if (!defaults.length) return;
    if (!await confirmAction(format('dashboard.work_copy_defaults_confirm',
        {count: defaults.length}))) return;
    try {
        for (const entry of defaults) {
            await api(`/guilds/${guildId}/work-responses`, {
                method: 'POST',
                headers: headers(),
                body: JSON.stringify({
                    tier: entry.tier, message: entry.message, weight: entry.weight,
                }),
            });
        }
        toast(format('dashboard.work_copied', {count: defaults.length}));
    } catch (error) {
        handleApiError(error);
    }
    await loadWorkResponses();
}

/** Each row is editable in place: the text, its weight and whether it is drawn.
 *  A tier an operator has written nothing for still works, because the command
 *  falls back to the shipped responses for that tier alone. */
function renderWorkResponseTable(host) {
    const node = table([
        'dashboard.work_tier', 'dashboard.work_message',
        'dashboard.column_reward_weight', 'dashboard.column_reward_chance',
        'dashboard.column_status', 'dashboard.column_actions',
    ]);
    const body = node.querySelector('tbody');
    const rows = workResponses.responses;
    if (!rows.length) emptyRow(body, 6, 'dashboard.work_empty');

    // A guild's own rows replace the installation defaults for that tier only,
    // so the effective pool is decided per tier — and the displayed chance has
    // to be a share of the pool that is actually drawn from.
    const tierScope = new Map();
    rows.forEach((entry) => {
        if (entry.scope !== 'default') tierScope.set(entry.tier, 'guild');
    });
    const inEffect = (entry) => (tierScope.get(entry.tier) || 'default')
        === (entry.scope === 'default' ? 'default' : 'guild');

    const tierTotals = new Map();
    rows.filter((entry) => entry.enabled && inEffect(entry)).forEach((entry) => {
        tierTotals.set(entry.tier, (tierTotals.get(entry.tier) || 0) + entry.weight);
    });

    rows.forEach((entry) => {
        const isDefault = entry.scope === 'default';
        const row = element('tr');
        if (isDefault) row.classList.add('reward-disabled');

        const tierCell = element('td', null, tr(`dashboard.work_tier_${entry.tier}`));
        if (isDefault) tierCell.appendChild(pill('dashboard.work_scope_default', 'neutral'));
        row.appendChild(tierCell);

        const messageCell = element('td', 'cell-grow');
        let message = null;
        if (isDefault) {
            // Shipped with the bot, shared by every guild that has not written
            // its own, so it is shown for reference and not edited from here.
            messageCell.appendChild(element('span', null, entry.message));
        } else {
            message = document.createElement('textarea');
            message.value = entry.message;
            message.maxLength = workResponses.message_max_length || 500;
            message.setAttribute('aria-label', tr('dashboard.work_message'));
            messageCell.appendChild(message);
        }
        row.appendChild(messageCell);

        const weightCell = element('td');
        let weight = null;
        if (isDefault) {
            weightCell.appendChild(element('span', 'cell-mono', String(entry.weight)));
        } else {
            weight = document.createElement('input');
            weight.type = 'number';
            weight.min = '1';
            weight.value = entry.weight;
            weight.setAttribute('aria-label', tr('dashboard.column_reward_weight'));
            weightCell.appendChild(weight);
        }
        row.appendChild(weightCell);

        const total = tierTotals.get(entry.tier) || 0;
        row.appendChild(element('td', 'cell-mono',
            entry.enabled && total && inEffect(entry)
                ? `${((entry.weight / total) * 100).toFixed(1)}%`
                : '—'));

        const status = element('td');
        status.appendChild(inEffect(entry)
            ? pill(entry.enabled ? 'dashboard.status_enabled' : 'dashboard.status_disabled',
                entry.enabled ? 'on' : 'off')
            : pill('dashboard.work_scope_overridden', 'neutral'));
        row.appendChild(status);

        const actions = element('td', 'cell-actions');
        if (!isDefault) {
            const save = element('button', 'btn btn-outline', tr('dashboard.save_settings'));
            save.type = 'button';
            save.addEventListener('click', () => saveWorkResponse(entry, {
                message: message.value, weight: Number(weight.value),
                enabled: entry.enabled,
            }, save));

            const toggle = element('button', 'btn btn-outline',
                tr(entry.enabled ? 'dashboard.disable' : 'dashboard.enable'));
            toggle.type = 'button';
            toggle.addEventListener('click', () => saveWorkResponse(entry, {
                message: message.value, weight: Number(weight.value),
                enabled: !entry.enabled,
            }, toggle));

            const remove = element('button', 'btn-icon danger', '');
            remove.type = 'button';
            remove.title = tr('dashboard.delete');
            remove.setAttribute('aria-label', tr('dashboard.delete'));
            remove.appendChild(icon('ic-trash', 'ic ic-sm'));
            remove.addEventListener('click', () => deleteWorkResponse(entry, remove));

            actions.append(save, toggle, remove);
        }
        row.appendChild(actions);
        body.appendChild(row);
    });

    host.replaceChildren(node);
}

async function saveWorkResponse(entry, changes, button) {
    button.disabled = true;
    try {
        const result = await api(
            `/guilds/${guildId}/work-responses/${entry.response_id}`,
            {method: 'PATCH', headers: headers(), body: JSON.stringify({
                tier: entry.tier, revision: entry.revision, ...changes,
            })},
        );
        toast(result.message);
        await loadWorkResponses();
    } catch (error) {
        await handleWriteConflict(error, loadWorkResponses);
        button.disabled = false;
    }
}

async function deleteWorkResponse(entry, button) {
    if (!await confirmAction(tr('dashboard.work_delete_confirm'))) return;
    button.disabled = true;
    try {
        const result = await api(
            `/guilds/${guildId}/work-responses/${entry.response_id}`,
            {method: 'DELETE', headers: headers(),
             body: JSON.stringify({revision: entry.revision})},
        );
        toast(result.message);
        await loadWorkResponses();
    } catch (error) {
        await handleWriteConflict(error, loadWorkResponses);
        button.disabled = false;
    }
}

async function createWorkResponse(event) {
    event.preventDefault();
    const submit = event.target.querySelector('button[type="submit"]');
    const form = new FormData(event.target);
    submit.disabled = true;
    try {
        const result = await api(`/guilds/${guildId}/work-responses`, {
            method: 'POST',
            headers: headers(),
            body: JSON.stringify({
                tier: form.get('tier'),
                message: String(form.get('message') || '').trim(),
                weight: Number(form.get('weight')),
            }),
        });
        toast(result.message);
        event.target.reset();
        await loadWorkResponses();
    } catch (error) {
        handleApiError(error);
    } finally {
        submit.disabled = false;
    }
}

/* ------------------------------------------------------------- shop items */

async function loadShopItems() {
    const itemsHost = document.getElementById('shop-items');
    const requestsHost = document.getElementById('fulfillment-list');
    renderSkeleton(itemsHost, 3);
    renderSkeleton(requestsHost, 2);
    renderShopItemTemplate();

    let items;
    try {
        const [itemResult, fulfillmentResult] = await Promise.all([
            api(`/guilds/${guildId}/shop-items`),
            api(`/guilds/${guildId}/fulfillment`),
        ]);
        items = itemResult.data;
        fulfillmentRequests = fulfillmentResult.data;
    } catch (error) {
        handleApiError(error);
        itemsHost.replaceChildren(emptyState('dashboard.load_failed', 'ic-alert'));
        requestsHost.replaceChildren();
        return;
    }

    setSubtitle('dashboard.subtitle_shop_items', {count: items.length});
    renderShopItemTable(itemsHost, items);
    renderFulfillmentTable(requestsHost);
}

/** A 409 means someone else changed the row; show them the current state rather
 *  than only reporting the conflict. */
async function handleWriteConflict(error, reload) {
    if (!(error instanceof ApiError) || error.status !== 409) {
        handleApiError(error);
        return;
    }
    toast(tr('dashboard.revision_conflict'), true);
    await reload();
}

function renderShopItemTable(host, items) {
    const node = table([
        'dashboard.column_item_key', 'dashboard.column_template',
        'dashboard.column_price', 'dashboard.column_status', 'dashboard.column_actions',
    ]);
    const body = node.querySelector('tbody');

    if (!items.length) emptyRow(body, 5, 'dashboard.shop_items_empty');
    items.forEach((item) => {
        const row = element('tr');
        row.appendChild(element('td', 'cell-key', item.item_key));
        row.appendChild(element('td', null, tr(`dashboard.template_${item.template_type}`)));
        row.appendChild(element('td', 'cell-mono',
            `${item.price} ${tr('dashboard.currency_short')}`));
        const status = element('td');
        status.appendChild(pill(item.enabled ? 'dashboard.status_enabled' : 'dashboard.status_disabled',
            item.enabled ? 'on' : 'off'));
        row.appendChild(status);

        const actions = element('td', 'cell-actions');

        const toggle = element('button', 'btn btn-outline',
            tr(item.enabled ? 'dashboard.disable' : 'dashboard.enable'));
        toggle.type = 'button';
        toggle.addEventListener('click', async () => {
            toggle.disabled = true;
            try {
                const saved = await api(`/guilds/${guildId}/shop-items/${encodeURIComponent(item.item_key)}`, {
                    method: 'PATCH',
                    headers: headers(),
                    body: JSON.stringify({
                        template_type: item.template_type,
                        enabled: !item.enabled,
                        price: item.price,
                        config: item.config,
                        hu: {name: item.name || item.item_key, description: item.description || ''},
                        revision: item.revision,
                    }),
                });
                toast(saved.message);
                await loadShopItems();
            } catch (error) {
                await handleWriteConflict(error, loadShopItems);
                toggle.disabled = false;
            }
        });

        const remove = element('button', 'btn-icon danger', '');
        remove.type = 'button';
        remove.title = tr('dashboard.delete');
        remove.setAttribute('aria-label', tr('dashboard.delete'));
        remove.appendChild(icon('ic-trash', 'ic ic-sm'));
        remove.addEventListener('click', async () => {
            const accepted = await confirmAction(
                format('dashboard.shop_item_delete_confirm', {item: item.item_key}),
            );
            if (!accepted) return;
            remove.disabled = true;
            try {
                const removed = await api(`/guilds/${guildId}/shop-items/${encodeURIComponent(item.item_key)}`, {
                    method: 'DELETE', headers: headers(),
                    body: JSON.stringify({revision: item.revision}),
                });
                toast(removed.message);
                await loadShopItems();
            } catch (error) {
                await handleWriteConflict(error, loadShopItems);
                remove.disabled = false;
            }
        });

        actions.append(toggle, remove);
        row.appendChild(actions);
        body.appendChild(row);
    });

    host.replaceChildren(node);
}

function renderFulfillmentTable(host) {
    const open = fulfillmentRequests.filter((item) => item.status === 'open');
    const node = table([
        'dashboard.column_request', 'dashboard.column_asset',
        'dashboard.column_user', 'dashboard.column_discord_item', 'dashboard.column_actions',
    ]);
    const body = node.querySelector('tbody');

    if (!open.length) emptyRow(body, 5, 'dashboard.fulfillment_empty');
    open.forEach((item) => {
        const row = element('tr');
        row.appendChild(element('td', 'cell-key', `#${item.request_id}`));
        row.appendChild(element('td', null, tr(`dashboard.asset_${item.asset_type}`)));
        row.appendChild(element('td', 'cell-mono', String(item.user_id)));

        const inputCell = element('td');
        const input = document.createElement('input');
        input.type = 'text';
        input.required = true;
        input.inputMode = 'numeric';
        input.maxLength = 24;
        input.placeholder = tr('dashboard.discord_item_id');
        inputCell.appendChild(input);
        row.appendChild(inputCell);

        const actionCell = element('td', 'cell-actions');
        const button = element('button', 'btn btn-primary', tr('dashboard.complete_fulfillment'));
        button.type = 'button';
        button.addEventListener('click', async () => {
            if (!input.value.trim()) {
                toast(tr('dashboard.discord_item_id_required'), true);
                return;
            }
            button.disabled = true;
            try {
                const saved = await api(`/guilds/${guildId}/fulfillment/${item.request_id}`, {
                    method: 'POST',
                    headers: headers(),
                    body: JSON.stringify({discord_item_id: input.value.trim()}),
                });
                toast(saved.message);
                await loadShopItems();
            } catch (error) {
                handleApiError(error);
                button.disabled = false;
            }
        });
        actionCell.appendChild(button);
        row.appendChild(actionCell);
        body.appendChild(row);
    });

    host.replaceChildren(node);
}

/** Templates whose whole configuration is one built-in item, so the operator
 *  picks from the shared catalog instead of guessing a key in raw JSON. */
const CATALOG_TEMPLATE_EFFECTS = {consumable: 'inventory', vault: 'vault'};

function renderShopItemTemplate() {
    const form = document.getElementById('shop-item-form');
    const effect = CATALOG_TEMPLATE_EFFECTS[form.template_type.value];
    const picker = document.getElementById('shop-item-picker');
    const rawConfig = document.getElementById('shop-item-config');

    picker.hidden = !effect;
    rawConfig.hidden = Boolean(effect);
    // A hidden required textarea blocks submission, so requiredness follows use.
    form.config.required = !effect;
    if (!effect) return;

    const select = form.catalog_item;
    select.replaceChildren();
    itemCatalog
        .filter((item) => item.effect === effect)
        .forEach((item) => {
            const option = document.createElement('option');
            option.value = item.key;
            option.textContent = item.key;
            select.appendChild(option);
        });
}

/** Build the config object a catalog-backed template expects. */
function shopItemConfigFromCatalog(template, itemKey) {
    if (template === 'consumable') return {item_key: itemKey};
    const item = itemCatalog.find((entry) => entry.key === itemKey);
    return {amount: item ? item.value : 0};
}

async function createShopItem(event) {
    event.preventDefault();
    const submit = event.target.querySelector('button[type="submit"]');
    const data = new FormData(event.target);
    const template = data.get('template_type');

    let payload;
    try {
        payload = {
            item_key: data.get('item_key'),
            template_type: template,
            enabled: true,
            price: Number(data.get('price')),
            config: CATALOG_TEMPLATE_EFFECTS[template]
                ? shopItemConfigFromCatalog(template, data.get('catalog_item'))
                : JSON.parse(data.get('config')),
            hu: {name: data.get('name'), description: data.get('description')},
        };
    } catch (error) {
        toast(tr('dashboard.invalid_json'), true);
        return;
    }

    submit.disabled = true;
    try {
        const result = await api(`/guilds/${guildId}/shop-items`, {
            method: 'POST', headers: headers(), body: JSON.stringify(payload),
        });
        toast(result.message);
        event.target.reset();
        // reset() restores the default template, so the config controls have to
        // follow it back.
        renderShopItemTemplate();
        await loadShopItems();
    } catch (error) {
        handleApiError(error);
    } finally {
        submit.disabled = false;
    }
}

/* ---------------------------------------------------------------- builders */

async function loadBuilders() {
    const host = document.getElementById('builder-list');
    renderSkeleton(host, 3);

    try {
        builderDocuments = (await api(`/guilds/${guildId}/builders`)).data;
    } catch (error) {
        handleApiError(error);
        host.replaceChildren(emptyState('dashboard.load_failed', 'ic-alert'));
        return;
    }

    setSubtitle('dashboard.subtitle_builders', {count: builderDocuments.length});

    const node = table([
        'dashboard.column_type', 'dashboard.column_name', 'dashboard.column_revision',
        'dashboard.column_channel', 'dashboard.column_actions',
    ]);
    const body = node.querySelector('tbody');

    if (!builderDocuments.length) emptyRow(body, 5, 'dashboard.builders_empty');

    // The parameter is deliberately not named `document`: shadowing the global
    // used to make this whole table throw before it rendered a single row.
    builderDocuments.forEach((draft) => {
        const row = element('tr');
        row.appendChild(element('td', null, tr(`dashboard.builder_${draft.document_type}`)));
        row.appendChild(element('td', 'cell-key', draft.name));
        row.appendChild(element('td', 'cell-mono', `v${draft.revision}`));

        const channelCell = element('td');
        const channel = document.createElement('select');
        channel.add(new Option(tr('dashboard.publish_channel'), ''));
        resources.channels
            .filter((item) => ['text', 'news'].includes(item.type))
            .forEach((item) => channel.add(new Option(item.name, item.id)));
        channelCell.appendChild(channel);
        row.appendChild(channelCell);

        const actionCell = element('td', 'cell-actions');
        const publish = element('button', 'btn btn-outline', tr('dashboard.publish'));
        publish.type = 'button';
        publish.prepend(icon('ic-send', 'ic ic-sm'));
        publish.addEventListener('click', async () => {
            if (!channel.value) {
                toast(tr('dashboard.publish_channel_required'), true);
                return;
            }
            const accepted = await confirmAction(format('dashboard.publish_confirm', {
                name: draft.name,
                channel: channel.selectedOptions[0].textContent,
            }));
            if (!accepted) return;

            publish.disabled = true;
            try {
                const queued = await api(`/guilds/${guildId}/builders/${draft.document_id}/publish`, {
                    method: 'POST', headers: headers(), body: JSON.stringify({channel_id: channel.value}),
                });
                toast(queued.message);
                await followAction(queued.data?.action_id);
            } catch (error) {
                handleApiError(error);
            } finally {
                publish.disabled = false;
            }
        });

        const load = element('button', 'btn-icon', '');
        load.type = 'button';
        load.title = tr('dashboard.edit_draft');
        load.setAttribute('aria-label', tr('dashboard.edit_draft'));
        load.appendChild(icon('ic-builders', 'ic ic-sm'));
        load.addEventListener('click', () => loadDraftIntoForm(draft));

        const remove = element('button', 'btn-icon danger', '');
        remove.type = 'button';
        remove.title = tr('dashboard.delete');
        remove.setAttribute('aria-label', tr('dashboard.delete'));
        remove.appendChild(icon('ic-trash', 'ic ic-sm'));
        remove.addEventListener('click', async () => {
            const accepted = await confirmAction(
                format('dashboard.draft_delete_confirm', {name: draft.name}),
            );
            if (!accepted) return;
            remove.disabled = true;
            try {
                const removed = await api(`/guilds/${guildId}/builders/${draft.document_id}`, {
                    method: 'DELETE', headers: headers(),
                    body: JSON.stringify({revision: draft.revision}),
                });
                toast(removed.message);
                await loadBuilders();
            } catch (error) {
                await handleWriteConflict(error, loadBuilders);
                remove.disabled = false;
            }
        });

        actionCell.append(load, remove, publish);
        row.appendChild(actionCell);
        body.appendChild(row);
    });

    host.replaceChildren(node);
}

const ACTION_POLL_INTERVAL = 1500;
const ACTION_POLL_ATTEMPTS = 20;

/** Report a queued publish's outcome instead of leaving it at "queued".
 *
 * The worker runs in the bot process, so the result only exists in the outbox
 * until something asks for it.
 */
async function followAction(actionId) {
    if (!actionId) return;
    for (let attempt = 0; attempt < ACTION_POLL_ATTEMPTS; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, ACTION_POLL_INTERVAL));
        let action;
        try {
            action = (await api(`/guilds/${guildId}/actions/${actionId}`)).data;
        } catch (error) {
            // A polling failure must not look like a publish failure.
            return;
        }
        if (action.status === 'completed') {
            toast(tr('dashboard.action_completed'));
            return;
        }
        if (action.status === 'failed' || action.status === 'cancelled') {
            const reason = action.error_code
                ? tr(`dashboard.action_errors.${action.error_code}`)
                : tr('dashboard.action_failed');
            toast(reason.startsWith('[') ? tr('dashboard.action_failed') : reason, true);
            return;
        }
    }
    toast(tr('dashboard.action_still_running'));
}

function loadDraftIntoForm(draft) {
    builderType = draft.document_type;
    document.querySelectorAll('[data-builder]').forEach((button) => {
        button.classList.toggle('active', button.dataset.builder === draft.document_type);
    });
    const form = document.getElementById('builder-form');
    form.name.value = draft.name;
    form.content.value = JSON.stringify(draft.content ?? {}, null, 2);
    form.name.focus();
    toast(format('dashboard.draft_loaded', {name: draft.name}));
}

async function saveBuilder(event) {
    event.preventDefault();
    const submit = event.target.querySelector('button[type="submit"]');
    const data = new FormData(event.target);
    const name = data.get('name');

    let content;
    try {
        content = JSON.parse(data.get('content'));
    } catch (error) {
        toast(tr('dashboard.invalid_json'), true);
        return;
    }

    // Saving under an existing name is an update, so it must carry that draft's
    // revision or the optimistic check rejects every save after the first.
    const existing = builderDocuments.find(
        (draft) => draft.document_type === builderType && draft.name === name,
    );

    submit.disabled = true;
    try {
        const result = await api(`/guilds/${guildId}/builders`, {
            method: 'POST',
            headers: headers(),
            body: JSON.stringify({
                document_type: builderType,
                name,
                content,
                revision: existing ? existing.revision : 0,
            }),
        });
        toast(result.message);
        event.target.reset();
        await loadBuilders();
    } catch (error) {
        handleApiError(error);
    } finally {
        submit.disabled = false;
    }
}

/* ------------------------------------------------------------------- audit */

const RELATIVE_UNITS = [
    ['day', 86400],
    ['hour', 3600],
    ['minute', 60],
];

function relativeTime(isoTimestamp) {
    const parsed = Date.parse(isoTimestamp);
    if (Number.isNaN(parsed)) return isoTimestamp;
    const seconds = Math.max(0, (Date.now() - parsed) / 1000);
    for (const [unit, size] of RELATIVE_UNITS) {
        if (seconds >= size) {
            return format(`dashboard.relative_${unit}`, {count: Math.floor(seconds / size)});
        }
    }
    return tr('dashboard.relative_now');
}

// Erasure and retention records are audit rows like any other, so they need no
// route of their own; they are simply pulled out of the feed into their own card.
const PRIVACY_ACTIONS = ['user.erase', 'user.retention_sweep'];

function renderPrivacyRecords(entries) {
    const host = document.getElementById('privacy-list');
    const records = entries.filter((entry) => PRIVACY_ACTIONS.includes(entry.action));

    const node = table([
        'dashboard.column_when', 'dashboard.column_action',
        'dashboard.column_target', 'dashboard.privacy_column_retained',
    ]);
    const body = node.querySelector('tbody');
    if (!records.length) emptyRow(body, 4, 'dashboard.privacy_empty');

    records.forEach((entry) => {
        const receipt = entry.new_value || {};
        const retained = Object.entries(receipt.retained_rows || {})
            .map(([name, count]) => `${name}: ${count}`).join(', ');
        const when = element('td', 'cell-muted', relativeTime(entry.created_at));
        when.title = entry.created_at;
        const row = element('tr');
        row.append(
            when,
            element('td', 'cell-key', entry.action),
            element('td', 'cell-mono', entry.target_key ?? ''),
            element('td', 'cell-muted', retained || tr('dashboard.privacy_nothing_retained')),
        );
        body.appendChild(row);
    });

    host.replaceChildren(node);
}

async function eraseMember(event) {
    event.preventDefault();
    const submit = event.target.querySelector('button[type="submit"]');
    const subject = new FormData(event.target).get('user_id');
    if (!await confirmAction(format('dashboard.erasure_confirm', {user: subject}))) return;

    submit.disabled = true;
    try {
        const result = await api(`/guilds/${guildId}/privacy/erasures`, {
            method: 'POST', headers: headers(),
            body: JSON.stringify({user_id: String(subject), confirm: true}),
        });
        toast(result.message);
        event.target.reset();
    } catch (error) {
        handleApiError(error);
    } finally {
        submit.disabled = false;
    }
}

/* ----------------------------------------------------------- permissions */

/** A finding's own text. Every part is a locale key or a Discord-supplied name,
 *  and both reach the DOM as text nodes. */
function permissionFindingText(finding) {
    const names = (finding.permissions || [])
        .map((name) => tr(`dashboard.permission_names.${name}`))
        .join(', ');
    return format(`dashboard.permission_finding_${finding.code}`, {
        identifier: finding.identifier,
        permissions: names,
    });
}

/** What a finding is about: the feature that owns it, else the setting. */
function permissionFindingSubject(finding) {
    if (finding.feature) return tr(featureState[finding.feature]?.locale_key || finding.feature);
    if (finding.subject) return tr(registry[finding.subject]?.locale_key || finding.subject);
    return tr('dashboard.permissions_heading');
}

async function loadPermissionReport() {
    const summary = document.getElementById('permission-summary');
    const findingsHost = document.getElementById('permission-findings');
    const featuresHost = document.getElementById('permission-features');
    renderSkeleton(summary, 2);
    renderSkeleton(findingsHost, 3);
    renderSkeleton(featuresHost, 3);

    let report;
    try {
        report = (await api(`/guilds/${guildId}/permissions`)).data;
    } catch (error) {
        summary.replaceChildren();
        featuresHost.replaceChildren();
        findingsHost.replaceChildren(
            emptyState('dashboard.permissions_unavailable', 'ic-shield-check'),
        );
        return;
    }

    summary.replaceChildren();
    [
        {symbol: 'ic-alert', value: String(report.blocking_count), labelKey: 'dashboard.permissions_blocking'},
        {symbol: 'ic-features', value: String(report.degraded_count), labelKey: 'dashboard.permissions_degraded'},
        {
            symbol: 'ic-shield-check',
            value: tr(report.administrator ? 'dashboard.status_enabled' : 'dashboard.status_disabled'),
            labelKey: 'dashboard.permissions_administrator',
        },
    ].forEach((entry) => {
        const tile = element('div', 'stat-tile');
        const iconBox = element('span', 'st-icon');
        iconBox.appendChild(icon(entry.symbol, 'ic ic-lg'));
        const meta = element('span', 'st-meta');
        meta.append(element('span', 'st-value', entry.value), element('span', 'st-label', tr(entry.labelKey)));
        tile.append(iconBox, meta);
        summary.appendChild(tile);
    });

    const findings = table([
        'dashboard.column_status', 'dashboard.column_target', 'dashboard.column_action',
    ]);
    const findingBody = findings.querySelector('tbody');
    if (!report.findings.length) {
        emptyRow(findingBody, 3, 'dashboard.permissions_all_clear');
    } else {
        report.findings.forEach((finding) => {
            const row = element('tr');
            const status = element('td');
            status.appendChild(pill(
                `dashboard.permissions_severity_${finding.severity}`,
                finding.severity === 'blocking' ? 'off' : 'pending',
            ));
            row.append(
                status,
                element('td', null, permissionFindingSubject(finding)),
                element('td', null, permissionFindingText(finding)),
            );
            findingBody.appendChild(row);
        });
    }
    findingsHost.replaceChildren(findings);

    const features = table([
        'dashboard.column_name', 'dashboard.column_status', 'dashboard.permissions_required',
    ]);
    const featureBody = features.querySelector('tbody');
    report.features.forEach((entry) => {
        const row = element('tr');
        const status = element('td');
        status.appendChild(pill(
            entry.enabled ? 'dashboard.status_enabled' : 'dashboard.status_disabled',
            entry.enabled ? 'on' : 'neutral',
        ));
        // Missing permissions come first, so a broken feature reads as broken.
        const required = (entry.missing.length ? entry.missing : entry.required)
            .map((name) => tr(`dashboard.permission_names.${name}`))
            .join(', ');
        const cell = element('td', entry.missing.length ? 'cell-danger' : null,
            required || tr('dashboard.permissions_none_required'));
        row.append(
            element('td', null, tr(featureState[entry.key]?.locale_key || entry.key)),
            status,
            cell,
        );
        featureBody.appendChild(row);
    });
    featuresHost.replaceChildren(features);

    setSubtitle('dashboard.subtitle_permissions', {
        blocking: report.blocking_count, degraded: report.degraded_count,
    });
}

/* ------------------------------------------------------------- changelog */

/** Render the deployed release notes.
 *  The server sends parsed sections rather than markdown, because the front end
 *  may not use a markup sink; every line here becomes a text node. */
async function loadChangelog() {
    const host = document.getElementById('changelog-list');
    renderSkeleton(host, 3);

    let releases;
    try {
        releases = (await api('/changelog')).data;
    } catch (error) {
        host.replaceChildren(emptyState('dashboard.changelog_unavailable', 'ic-changelog'));
        return;
    }

    host.replaceChildren();
    if (!releases.length) {
        host.appendChild(emptyState('dashboard.changelog_empty', 'ic-changelog'));
        setSubtitle('dashboard.subtitle_changelog', {count: 0});
        return;
    }

    releases.forEach((release) => {
        const section = element('section', 'changelog-release');
        const head = element('div', 'changelog-head');
        head.appendChild(element('h3', 'changelog-version', release.version));
        if (release.label) head.appendChild(element('span', 'pill neutral', release.label));
        section.appendChild(head);

        const list = element('ul', 'changelog-entries');
        release.entries.forEach((entry) => list.appendChild(element('li', null, entry)));
        section.appendChild(list);
        host.appendChild(section);
    });

    setSubtitle('dashboard.subtitle_changelog', {count: releases.length});
}

async function loadAudit() {
    const host = document.getElementById('audit-list');
    renderSkeleton(host, 4);
    // Hidden rather than merely rejected: erasure is installation-wide, so a
    // guild administrator has no route to it however many guilds they manage.
    document.getElementById('erasure-form').classList.toggle('hidden', !isHost);

    let entries;
    try {
        entries = (await api(`/guilds/${guildId}/audit`)).data;
    } catch (error) {
        handleApiError(error);
        host.replaceChildren(emptyState('dashboard.load_failed', 'ic-alert'));
        document.getElementById('privacy-list').replaceChildren(
            emptyState('dashboard.load_failed', 'ic-alert')
        );
        return;
    }
    renderPrivacyRecords(entries);

    setSubtitle('dashboard.subtitle_audit', {count: entries.length});

    const node = table([
        'dashboard.column_when', 'dashboard.column_action',
        'dashboard.column_target', 'dashboard.column_actor',
    ]);
    const body = node.querySelector('tbody');

    if (!entries.length) emptyRow(body, 4, 'dashboard.audit_empty');
    entries.forEach((entry) => {
        const row = element('tr');
        const when = element('td', 'cell-muted', relativeTime(entry.created_at));
        when.title = entry.created_at;
        row.append(
            when,
            element('td', 'cell-key', entry.action),
            element('td', 'cell-mono', entry.target_key ?? ''),
            element('td', 'cell-mono', String(entry.actor_id ?? '')),
        );
        body.appendChild(row);
    });

    host.replaceChildren(node);
}
