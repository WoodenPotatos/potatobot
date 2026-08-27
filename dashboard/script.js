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
let shippedRewards = null;
let activeBannerKey = null;
// The shared built-in item catalog. Identical for every guild, so it is fetched
// once and reused by the gacha reward picker and the shop item builder.
let itemCatalog = [];
let itemList = [];
let activePage = 'overview';
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
let fulfillmentRequests = [];
let languages = [];
let activeLanguage = '';
let guilds = [];
let account = null;

const CATEGORY_ICONS = {
    community: 'ic-community',
    economy: 'ic-economy',
    games: 'ic-games',
    everydle: 'ic-games',
    casino: 'ic-casino',
    moderation: 'ic-moderation',
    factions: 'ic-factions',
    builders: 'ic-builders',
    administration: 'ic-administration',
};

/* ------------------------------------------------------------ localization */

const tr = (path) => {
    // A missing key renders as `[key]` rather than throwing, and an *absent* key
    // name has to do the same: `undefined.split` took a whole page down when one
    // synthetic definition omitted its `locale_key`, and a bracketed placeholder
    // is a visible defect where a TypeError is an invisible one.
    if (typeof path !== 'string') return `[${path}]`;
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

// A request that never settles renders as a skeleton that never goes away, with
// nothing on screen or in the console to say why — "it loads endlessly". A
// dashboard request should never take this long: the slowest thing behind one is
// a live Discord permission refresh, which the server itself gives ten seconds.
// Past that the request has not been slow, it has been lost, and an error the
// operator can see beats a spinner that cannot end.
const REQUEST_TIMEOUT_MS = 20000;

async function api(path, options = {}) {
    let response;
    const controller = new AbortController();
    const expiry = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
        response = await fetch(`${API}${path}`, {...options, signal: controller.signal});
    } catch (error) {
        throw new ApiError(
            tr(error.name === 'AbortError'
                ? 'dashboard.request_timeout' : 'dashboard.network_error'),
            0,
        );
    } finally {
        clearTimeout(expiry);
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
    document.getElementById('erasure-form').addEventListener('submit', eraseMember);

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
        // The payload grew a wrapper when the shipped reward table joined it, so
        // an older shape (a bare array) is still accepted rather than crashing
        // the whole page load on a stale cached script.
        gachaBanners = Array.isArray(gachaData.data)
            ? gachaData.data : gachaData.data.banners;
        shippedRewards = Array.isArray(gachaData.data)
            ? null : gachaData.data.shipped_rewards;
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
    // Sub-toggles are content too: the Casino page owns no settings at all and
    // exists to hold the five game switches, so measuring settings alone would
    // hide it exactly the way it hid the Builders page.
    return visibleDefinitions(category).length > 0
        || childFeaturesOf(category).length > 0;
}

function updateNavigation() {
    // An editor page is NOT hidden by the feature it edits. It used to be, and
    // the consequence was that turning off `shop` — which exists to gate the
    // `/shop` command — removed staff's only way to edit items and, worse, to
    // see and complete fulfillment requests members had already paid for,
    // including gacha-sourced ones that have nothing to do with the shop flag.
    // The routes behind these pages enforce no feature check at all, so the
    // interface was hiding working functionality. The flag is shown as a muted
    // nav item plus a notice on the page instead.
    document.querySelectorAll('.nav-item[data-feature]').forEach((button) => {
        button.classList.toggle(
            'nav-item-off', featureState[button.dataset.feature]?.enabled === false);
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

/** Say so when the page you are on configures a switched-off feature. */
function updateFeatureNotice(page) {
    const host = document.getElementById('feature-notice');
    if (!host) return;
    const item = document.querySelector(`.nav-item[data-page="${page}"][data-feature]`);
    const feature = item?.dataset.feature;
    const off = Boolean(feature) && featureState[feature]?.enabled === false;
    host.classList.toggle('hidden', !off);
    host.replaceChildren();
    if (!off) return;
    host.appendChild(element('span', null, format('dashboard.feature_off_notice', {
        feature: tr(`dashboard.features.${feature}`),
    })));
}

async function showPage(page) {
    activePage = page;
    updateFeatureNotice(page);
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
    if (page === 'redeems') await loadRedeems();
    if (MANAGED_PAGES[page]) await loadManaged(page);
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

    // Both extra lists come from endpoints the shop and content pages already use.
    // Drafts used to be counted here; there are none any more, so this reports
    // what is actually posted — the number an operator can act on.
    const [fulfillment, managed, setup] = await Promise.all([
        api(`/guilds/${guildId}/fulfillment`).then((result) => result.data).catch(() => null),
        Promise.all(Object.keys(MANAGED_KINDS).map((kind) =>
            api(`/guilds/${guildId}/managed/${kind}`)
                .then((result) => result.data).catch(() => [])))
            .then((lists) => lists.flat()),
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
            value: `${managed.filter((item) => item.posted).length} / ${managed.length}`,
            labelKey: 'dashboard.overview_managed_messages',
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
        // A sub-toggle renders on its parent's settings page instead. Eight
        // near-identical games in the flat list pushed everything else off it.
        if (state.parent) return;
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

/** One feature switch. Shared by the Features page and the sub-toggle block on a
 *  parent's settings page, so a child behaves exactly like its parent does —
 *  including refusing to turn on while a dependency is off. */
function featureSwitchRow(key, state) {
    const row = element('label', 'feature-row');
    const text = element('span', 'feature-text');
    text.appendChild(element('span', 'feature-name', tr(state.locale_key)));

    const blockers = (state.dependencies || []).filter(
        (dependency) => featureState[dependency]?.enabled === false,
    );
    if (blockers.length) {
        const names = blockers
            .map((dependency) => tr(featureState[dependency].locale_key)).join(', ');
        text.appendChild(element('span', 'feature-dep',
            format('dashboard.feature_requires', {features: names})));
    }

    const switchWrap = element('span', 'switch');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = state.enabled;
    input.disabled = blockers.length > 0 && !state.enabled;
    input.addEventListener('change', () => saveFeature(key, input, state.revision));
    switchWrap.append(input, element('span', 'track'));

    row.append(text, switchWrap);
    return row;
}

/** The sub-toggles a settings category owns, by the convention that a child's
 *  `parent` is the category it renders on. */
function childFeaturesOf(category) {
    return Object.entries(featureState)
        .filter(([, state]) => state.parent === category);
}

function renderFeatures() {
    const host = document.getElementById('feature-grid');
    host.replaceChildren();

    featureGroups().forEach((entries, groupKey) => {
        const section = element('fieldset', 'form-section');
        section.appendChild(element('legend', null, featureGroupLabel(groupKey)));
        const grid = element('div', 'feature-grid');

        entries.forEach(([key, state]) => {
            grid.appendChild(featureSwitchRow(key, state));
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
        // A setting the registry marks as edited elsewhere is real — stored,
        // validated, audited — but its editing surface is a better page. The
        // shop prices are the case: a price belongs beside its item, and having
        // them here too meant built-in prices were set on one page and custom
        // ones on another, with neither page showing the other half.
        && !definition.edited_elsewhere
        && (!definition.owner_feature || featureState[definition.owner_feature]?.enabled !== false));
}

function renderSettings(category) {
    document.getElementById('settings-heading').textContent = tr(`dashboard.category_${category}`);
    const host = document.getElementById('settings-grid');
    host.replaceChildren();

    // The master's own sub-toggles come first: they decide whether the settings
    // below them do anything at all.
    const children = childFeaturesOf(category);
    if (children.length) {
        const section = element('fieldset', 'form-section');
        section.appendChild(element('legend', null, tr('dashboard.sub_features')));
        const grid = element('div', 'feature-grid');
        children.forEach(([key, state]) => grid.appendChild(featureSwitchRow(key, state)));
        section.appendChild(grid);
        section.appendChild(element('p', 'section-hint', tr('dashboard.sub_features_hint')));
        host.appendChild(section);
    }

    const definitions = visibleDefinitions(category);
    if (!definitions.length) {
        if (!children.length) {
            host.appendChild(emptyState('dashboard.no_settings',
                CATEGORY_ICONS[category] || 'ic-administration'));
        }
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
        // Created hidden. It used to be created visible and hidden only by the
        // dirty check at the end of this function, so anything throwing in
        // between showed an unsaved marker on every section of every page — the
        // same symptom as a real phantom change, with no cause to find. Hidden
        // by default fails quiet instead.
        const unsavedPill = pill('dashboard.unsaved', 'pending');
        unsavedPill.classList.add('hidden');
        legend.appendChild(unsavedPill);
        section.appendChild(legend);
        const grid = element('div', 'form-grid');

        entries.forEach((definition) => {
            // A <label> forwards a click to the labelable control it wraps, and a
            // button is labelable — so a field whose editor *contains* buttons
            // must be a div. That is why the picker types are listed here, and
            // a shaped-JSON editor is full of them too: row pickers, an add
            // button and a remove button per row.
            const wrapsButton = ['channel', 'role', 'channel_list', 'role_list']
                .includes(definition.value_type)
                || Boolean(JSON_ROW_SHAPES[definition.json_shape]);
            const group = element(wrapsButton ? 'div' : 'label', 'input-group');
            if (['string_list', 'json', 'channel_list', 'role_list'].includes(definition.value_type)) {
                group.classList.add('wide');
            }
            group.appendChild(element('span', 'field-label', tr(definition.locale_key)));

            group.dataset.setting = definition.key;
            const input = settingInput(definition, settings[definition.key]?.value);
            input.dataset.key = definition.key;
            group.appendChild(input);

            if (WEIGHT_GROUPS[definition.key]) {
                const share = element('small', 'field-share');
                share.dataset.forKey = definition.key;
                group.appendChild(share);
            }

            const hint = fieldHint(definition.key);
            if (hint) group.appendChild(hint);

            const badge = element('small', `apply-${definition.apply_behavior}`,
                tr(`dashboard.apply_${definition.apply_behavior}`));
            group.appendChild(badge);
            // An installation-wide setting is edited from a guild page but is
            // not that guild's, so the badge says so — an operator changing "the
            // language" for one server and finding it changed everywhere would
            // be right to call that a bug. The API refuses the save for anyone
            // but the host now, so a guild admin is shown the control disabled
            // rather than being allowed to try and then told no.
            if (definition.scope === 'instance') {
                group.appendChild(element('small', 'field-scope',
                    tr('dashboard.scope_instance')));
                if (!isHost) {
                    const control = group.querySelector(
                        'input, textarea, select, .picker-trigger');
                    if (control) control.disabled = true;
                }
            }
            grid.appendChild(group);
        });

        section.appendChild(grid);
        host.appendChild(section);
    });

    refreshWeightShares();
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

/** One stable text form for a setting value, whatever order its keys arrive in.
 *
 *  `JSON.stringify` preserves insertion order, and the two sides of the
 *  dirty-state comparison do not agree on it: Flask sets `app.json.sort_keys`,
 *  so an entry reaches the browser with its fields alphabetical, while a row
 *  editor's `pack()` builds them in the order the columns are declared. For a
 *  role menu that is `{emoji, id}` against `{id, emoji}` — identical values,
 *  different text — so all three role menus reported themselves changed on every
 *  load and the save button offered to save three things nobody had touched.
 *
 *  Comparing canonically instead of textually removes the whole class: key order
 *  and number-versus-string can no longer manufacture a difference. This is the
 *  fourth instance of that class to be fixed by hand, which is why it is now one
 *  function used by both sides rather than a fix per shape.
 */
function canonicalValue(value) {
    const walk = (node) => {
        if (Array.isArray(node)) return node.map(walk);
        if (node && typeof node === 'object') {
            return Object.keys(node).sort().reduce((out, key) => {
                out[key] = walk(node[key]);
                return out;
            }, {});
        }
        // A Discord id crosses the wire as a string but may still be a number in
        // a locally built value; those are the same setting.
        return typeof node === 'number' ? String(node) : node;
    };
    return JSON.stringify(walk(value ?? null));
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
            .filter((change) => {
                const sent = settings[change.key]?.value ?? null;
                if (canonicalValue(change.value) === canonicalValue(sent)) return false;
                // Naming the setting is what was missing when three
                // identical-looking objects disagreed and nothing said which.
                console.debug('[settings] differs:', change.key,
                    {form: change.value, server: sent});
                return true;
            });
    } catch (error) {
        return null;
    }
}

/** Show what is unsaved: a count on the apply button and a pill per section.
 *  A JSON field mid-edit cannot be parsed, so the count is unknown rather than
 *  zero — reporting zero there would disable the only way to save. */
function refreshSettingsDirtyState() {
    refreshWeightShares();
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

/** How each shaped JSON setting becomes rows, declared rather than coded four
 *  times. `key` is the map key; `columns` are the fields of one entry; `unpack`
 *  turns a stored entry into column values and `pack` turns them back.
 *
 *  The shapes and their validators live in `settings_registry`, so a column
 *  here that the API would reject is a bug this table can be checked against.
 */
const JSON_ROW_SHAPES = {
    role_menu: {
        key: {kind: 'text', label: 'dashboard.role_menu_label'},
        columns: [
            {name: 'id', kind: 'role', label: 'dashboard.role_menu_role',
             required: true},
            {name: 'emoji', kind: 'text', label: 'dashboard.role_menu_emoji',
             narrow: true},
        ],
        unpack: (entry) => (entry && typeof entry === 'object')
            ? {id: entry.id, emoji: entry.emoji || ''}
            : {id: entry, emoji: ''},
        pack: (columns) => ({id: columns.id, emoji: columns.emoji || ''}),
    },
    level_roles: {
        // The key is the milestone. `min` is 2 because level 2 is the lowest a
        // member can reach, and the API refuses anything below it.
        key: {kind: 'number', label: 'dashboard.level_roles_level', min: 2,
              max: 1000},
        columns: [{name: 'role', kind: 'role',
                   label: 'dashboard.level_roles_role', required: true}],
        unpack: (entry) => ({role: entry}),
        pack: (columns) => columns.role,
    },
    lfg_channels: {
        // A team-finding post goes in a text channel, so the picker is narrowed
        // the way every other channel selector is. Presentational only, as
        // `channel_types` always is: deciding a channel's kind needs live
        // Discord state and a save must not start failing while Discord is
        // unreachable.
        key: {kind: 'channel', label: 'dashboard.lfg_channel',
              channelTypes: ['text', 'news']},
        columns: [{name: 'role', kind: 'role', label: 'dashboard.lfg_role',
                   required: true}],
        unpack: (entry) => ({role: entry}),
        pack: (columns) => columns.role,
    },
    factions: {
        key: {kind: 'text', label: 'dashboard.faction_key'},
        columns: [
            {name: 'leader_role_id', kind: 'role',
             label: 'dashboard.faction_leader', required: true},
            {name: 'manageable_ids', kind: 'role_list',
             label: 'dashboard.faction_managed'},
        ],
        unpack: (entry) => (entry && typeof entry === 'object') ? entry : {},
        pack: (columns) => ({
            leader_role_id: columns.leader_role_id,
            manageable_ids: columns.manageable_ids || [],
        }),
    },
};

/** A row editor for a shaped JSON setting.
 *
 *  The load-bearing decision, the same one the resource picker documents: **a
 *  hidden textarea stays the value carrier**. `readSettingInput`,
 *  `collectSettingChanges`, the dirty-state check and a native form reset all
 *  act on a real form control, so this adds a way to *edit* the value without
 *  becoming a second way to *save* it.
 *
 *  Ids stay strings throughout. A Discord id is 64-bit and a JavaScript number
 *  holds 53 bits, so calling Number() on one here would round it and the save
 *  would write a role that does not exist.
 *
 *  Entry order is preserved, because `collectSettingChanges` compares
 *  JSON.stringify against the loaded value and a reordered object would read as
 *  a change nobody made.
 */
function jsonRowEditor(definition, value) {
    const shape = JSON_ROW_SHAPES[definition.json_shape];
    const entries = (value && typeof value === 'object' && !Array.isArray(value))
        ? value : {};

    const wrapper = element('div', 'json-row-editor');
    const carrier = document.createElement('textarea');
    carrier.className = 'menu-carrier';
    carrier.hidden = true;
    wrapper.appendChild(carrier);

    // Column headers, from the same spec the rows are built from. Without them a
    // populated menu is three anonymous boxes: a text cell's placeholder vanishes
    // the moment it has a value, and a picker never had one at all.
    const head = element('div', 'menu-row menu-head');
    head.style.setProperty('--row-columns', String(shape.columns.length + 1));
    head.appendChild(element('span', null, tr(shape.key.label)));
    shape.columns.forEach((spec) => head.appendChild(element('span', null, tr(spec.label))));
    head.appendChild(element('span', 'menu-head-spacer'));
    wrapper.appendChild(head);

    const rows = element('div', 'menu-rows');
    wrapper.appendChild(rows);

    /** One field, by kind. A picker field returns the ids it holds. */
    const field = (spec, current) => {
        if (spec.kind === 'role' || spec.kind === 'channel'
                || spec.kind === 'role_list') {
            const valueType = spec.kind === 'role_list' ? 'role_list' : spec.kind;
            // A synthetic definition, so every picker in the page is the same
            // one rather than a second implementation. The key is suffixed
            // because `applyPermissionNotes` matches findings on it and a
            // per-row note would have nowhere sensible to go.
            const picker = resourcePicker(
                {...definition, key: `${definition.key}.${spec.name}`,
                 value_type: valueType,
                 channel_types: spec.channelTypes || definition.channel_types},
                current,
            );
            picker.dataset.column = spec.name;
            return picker;
        }
        const input = document.createElement('input');
        input.type = spec.kind === 'number' ? 'number' : 'text';
        if (spec.min !== undefined) input.min = spec.min;
        if (spec.max !== undefined) input.max = spec.max;
        input.className = spec.narrow ? 'row-field narrow' : 'row-field';
        input.dataset.column = spec.name;
        input.placeholder = tr(spec.label);
        input.value = current ?? '';
        return input;
    };

    const readField = (node, spec) => {
        if (spec.kind === 'role' || spec.kind === 'channel') {
            const carrierNode = node.querySelector('.picker-carrier');
            return [...carrierNode.selectedOptions].map((o) => o.value)[0] || '';
        }
        if (spec.kind === 'role_list') {
            const carrierNode = node.querySelector('.picker-carrier');
            // Sorted to match how the server stores it. The options are ordered
            // by role position, so reading them in option order produced a
            // different array from the same set of roles and the form looked
            // dirty the moment it opened.
            return [...carrierNode.selectedOptions].map((o) => o.value)
                .sort((a, b) => (BigInt(a) < BigInt(b) ? -1 : 1));
        }
        return node.value.trim();
    };

    const serialise = () => {
        const collected = {};
        rows.querySelectorAll('.menu-row').forEach((row) => {
            const keyNode = row.querySelector('[data-column="__key"]');
            const key = readField(keyNode, shape.key);
            const columns = {};
            shape.columns.forEach((spec) => {
                columns[spec.name] = readField(
                    row.querySelector(`[data-column="${spec.name}"]`), spec);
            });
            // An incomplete row is left out entirely rather than serialised with
            // a stand-in. `pack` used to turn an unset picker into the id "0",
            // which the API rejects as not a snowflake — and it rejects the
            // *whole* patch, so one half-filled row made every change in the
            // category fail to save while the section stayed marked unsaved.
            // The row is flagged instead, so it is visibly the thing to finish.
            const incomplete = !key
                || shape.columns.some((spec) => spec.required && !columns[spec.name]);
            row.classList.toggle('menu-row-incomplete', incomplete && !isBlankRow(key, columns));
            if (incomplete) return;
            collected[key] = shape.pack(columns);
        });
        carrier.value = JSON.stringify(collected);
        // Dispatched from the carrier, because the dirty-state listener is
        // bound to the real control.
        carrier.dispatchEvent(new Event('change', {bubbles: true}));
    };

    /** A row nobody has typed in yet. Freshly added rows are empty by
     *  definition, so they are not "incomplete" until something is filled in. */
    const isBlankRow = (key, columns) => !key
        && shape.columns.every((spec) => {
            const held = columns[spec.name];
            return Array.isArray(held) ? held.length === 0 : !held;
        });

    const addRow = (key, entry) => {
        const row = element('div', 'menu-row');
        row.style.setProperty('--row-columns', String(shape.columns.length + 1));

        const keyNode = field({...shape.key, name: '__key'}, key);
        keyNode.dataset.column = '__key';
        row.appendChild(keyNode);

        const columns = shape.unpack(entry);
        shape.columns.forEach((spec) => {
            const current = columns[spec.name];
            row.appendChild(field(
                spec,
                spec.kind === 'role_list'
                    ? (current || []).map(String)
                    : (current !== undefined && current !== null
                        && String(current) !== '0' ? String(current) : null),
            ));
        });

        const remove = element('button', 'menu-remove');
        remove.type = 'button';
        remove.title = tr('dashboard.role_menu_remove');
        remove.appendChild(icon('ic-trash', 'ic ic-sm'));
        remove.addEventListener('click', () => { row.remove(); serialise(); });
        row.appendChild(remove);

        row.querySelectorAll('input').forEach(
            (input) => input.addEventListener('input', serialise));
        row.querySelectorAll('.picker-carrier').forEach(
            (node) => node.addEventListener('change', serialise));

        rows.appendChild(row);
        return row;
    };

    Object.entries(entries).forEach(([key, entry]) => addRow(key, entry));

    const add = element('button', 'menu-add btn btn-ghost btn-sm');
    add.type = 'button';
    add.appendChild(icon('ic-plus', 'ic ic-sm'));
    add.appendChild(document.createTextNode(tr('dashboard.role_menu_add')));
    add.addEventListener('click', () => {
        const row = addRow('', null);
        const first = row.querySelector('input, button');
        if (first) first.focus();
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
        // A definition built by hand for a builder field has no registry locale
        // key; naming the picker after its own key beats naming it `[undefined]`.
        role: 'dialog', itemRole: 'option',
        ariaLabel: tr(definition.locale_key || `dashboard.${definition.key}`),
        align: 'left', matchWidth: true, className: 'picker-menu',
    });

    renderChips();
    return wrapper;
}

/** Label one allowed value of a constrained setting.
 *  The language list is the only one so far and it already has display names
 *  under `dashboard.languages.*`; anything else falls back to the raw value
 *  rather than rendering a missing key. */
/** Settings that are weights, and the group each competes against.
 *
 *  A weight only means something relative to its siblings, so the label alone
 *  cannot be understood — the gacha reward table already solved this with a
 *  computed "chance within the tier" column, and this brings the same reading to
 *  the settings form. Declared rather than inferred from the key prefix, because
 *  a future weight that is *not* drawn against these three must not silently
 *  join the group.
 */
const WEIGHT_GROUPS = {
    work_tier_normal_weight: ['work_tier_normal_weight', 'work_tier_free_weight',
                              'work_tier_high_weight'],
    work_tier_free_weight: ['work_tier_normal_weight', 'work_tier_free_weight',
                            'work_tier_high_weight'],
    work_tier_high_weight: ['work_tier_normal_weight', 'work_tier_free_weight',
                            'work_tier_high_weight'],
};

/** Keep every weight field's computed share in step with what is typed. */
function refreshWeightShares() {
    const read = (key) => {
        const field = document.querySelector(`#settings-grid [data-key="${key}"]`);
        const value = Number(field?.value);
        return Number.isFinite(value) && value > 0 ? value : 0;
    };
    document.querySelectorAll('#settings-grid .field-share').forEach((node) => {
        const group = WEIGHT_GROUPS[node.dataset.forKey] || [];
        const total = group.reduce((sum, key) => sum + read(key), 0);
        const own = read(node.dataset.forKey);
        node.textContent = total
            ? format('dashboard.weight_share', {percent: ((own / total) * 100).toFixed(1)})
            : tr('dashboard.weight_share_none');
    });
}


/** An explanatory line under a field, when the catalogs have one for it.
 *
 *  Locale-driven rather than registry-driven: a hint is prose about what a
 *  number means, it changes without the setting changing, and every hint needs
 *  translating anyway. A missing key renders nothing, so a field without a hint
 *  is simply a field without a hint — `tr` returns a bracketed key for a miss,
 *  which is the test for absence.
 *
 *  This exists because several numbers on this page cannot be understood from
 *  their label alone: a weight is relative to the others in its group, a tier
 *  total is a share of every pull, and the duplicate refund applies to exactly
 *  one reward kind.
 */
function fieldHint(key) {
    const text = tr(`dashboard.hints.${key}`);
    return text.startsWith('[') ? null : element('span', 'field-hint', text);
}


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
    if (JSON_ROW_SHAPES[definition.json_shape]) {
        return jsonRowEditor(definition, value);
    }

    // A plain list of words is edited as one entry per line. It used to render
    // as a JSON textarea, so an empty list showed the two characters `[]` and it
    // was anybody's guess whether the brackets and the quotes were part of the
    // value. Pasting a list from somewhere else now works, and empty means empty.
    if (definition.value_type === 'string_list') {
        const input = document.createElement('textarea');
        input.value = (Array.isArray(value) ? value : []).join('\n');
        input.placeholder = tr('dashboard.string_list_placeholder');
        input.spellcheck = false;
        return input;
    }

    if (definition.value_type === 'json') {
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
    } else if (field.classList?.contains('json-row-editor')) {
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
    if (definition.value_type === 'string_list') {
        // Blank lines and stray whitespace are the operator's formatting, not
        // entries. A term that normalises to nothing is dropped server-side too.
        return input.value.split('\n').map((line) => line.trim()).filter(Boolean);
    }
    if (definition.value_type === 'json') return JSON.parse(input.value);
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
    'four_star_guarantee_interval', 'duplicate_percent', 'featured_split',
];
/** The standard banner is the pool a lost featured chance draws *from*, so it
 *  cannot itself feature a reward. The server refuses it; this hides the column
 *  rather than offering a control that can only be rejected. */
const DEFAULT_BANNER_KEY = 'standard';
function bannerCanFeature() {
    return Boolean(gacha) && gacha.banner_key !== DEFAULT_BANNER_KEY;
}
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
    picker.className = 'banner-picker';
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
    name.className = 'banner-name';
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

/** Which page actions this banner needs.
 *
 *  These used to be added once per navigation, so switching banners in the
 *  picker left them describing the banner you arrived on: a banner missing a
 *  shipped reward showed no "add missing" button, and the only way to obtain
 *  the reward was "reset rewards", which discards the operator's own table.
 *  They belong to the banner, so they are rebuilt with it.
 */
function renderGachaPageActions() {
    if (activePage !== 'gacha') return;
    const host = document.getElementById('page-actions');
    host.replaceChildren();
    const missing = Object.values(gacha?.missing_rewards || {})
        .reduce((sum, entries) => sum + entries.length, 0);
    if (missing) {
        addPageAction('dashboard.gacha_add_missing', 'ic-plus', addMissingRewards);
    }
    addPageAction('dashboard.gacha_reset_rewards', 'ic-gacha', resetBannerRewards);
}

function renderGacha() {
    renderGachaPageActions();
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
        const hint = fieldHint(`gacha_${key}`);
        if (hint) group.appendChild(hint);
        grid.appendChild(group);
    });

    const rewardGroup = element('div', 'input-group wide');
    const featuredNotice = element('div', 'field-hint');
    featuredNotice.id = 'gacha-featured-notice';
    rewardGroup.append(
        element('span', 'field-label', tr('dashboard.gacha_rewards')),
        element('span', 'field-hint', tr('dashboard.gacha_amount_hint')),
        featuredNotice,
        renderRewardTable(config.rewards),
    );
    grid.appendChild(rewardGroup);

    const multiplier = document.querySelector('#gacha-grid [name="soft_pity_multiplier"]');
    if (multiplier) multiplier.addEventListener('input', updateGachaTotal);
    const splitInput = document.querySelector('#gacha-grid [name="featured_split"]');
    if (splitInput) splitInput.addEventListener('input', updateRewardChances);
    updateGachaTotal();
}

const REWARD_KINDS = ['coins', 'item', 'vault', 'voucher'];

const REWARD_COLUMNS = 8;

/** Editable reward rows, replacing the raw JSON textarea.
 *
 * Rows carry their tier, key and kind in data attributes so the form can be
 * read back without holding a parallel copy of the model in a variable, and so
 * a row added here survives a save.
 */
/** Put the shipped reward table back, wholesale.
 *
 *  Paired with `addMissingRewards` on purpose: a reset is what you want when a
 *  banner has drifted somewhere you no longer like, and it destroys your edits;
 *  adding what is missing is what you want when the bot shipped a new reward and
 *  you only need that. Neither can stand in for the other.
 */
async function resetBannerRewards() {
    if (!shippedRewards || !gacha) return;
    if (!await confirmAction(tr('dashboard.gacha_reset_rewards_confirm'))) return;
    gacha.config.rewards = JSON.parse(JSON.stringify(shippedRewards));
    renderGacha();
    toast(tr('dashboard.gacha_rewards_replaced'));
}

/** Append the shipped rewards this banner's table does not have.
 *
 *  A stored banner is frozen at the shipped set of the day it was first saved,
 *  and nothing reconciled the two — which is how `streak_freeze` reached the
 *  shop and the shipped 4-star tier while being unobtainable from the banner
 *  actually in use. Existing rows are untouched, so an operator's weights and
 *  their deliberate omissions both survive until they ask for this.
 */
function addMissingRewards() {
    if (!gacha) return;
    const missing = gacha.missing_rewards || {};
    let added = 0;
    Object.entries(missing).forEach(([tier, entries]) => {
        const table = gacha.config.rewards[tier] || (gacha.config.rewards[tier] = []);
        entries.forEach((entry) => { table.push({...entry}); added += 1; });
    });
    if (!added) {
        toast(tr('dashboard.gacha_nothing_missing'));
        return;
    }
    gacha.missing_rewards = {};
    renderGacha();
    toast(format('dashboard.gacha_rewards_added', {count: added}));
}

function renderRewardTable(rewards) {
    const wrap = element('div', 'table-wrap');
    const node = table([
        'dashboard.column_reward_key', 'dashboard.column_reward_kind',
        'dashboard.column_reward_amount', 'dashboard.column_reward_weight',
        'dashboard.column_reward_chance', 'dashboard.column_featured',
        'dashboard.column_status', 'dashboard.column_actions',
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
            // A banner the guild has never saved is still the synthesised shipped
            // one, so its rows are committed to nothing and their key and kind
            // stay editable. Rendering them locked is why a standard banner's
            // rows behaved differently from ones you added yourself.
            body.appendChild(rewardRow(tier, entry, !gacha || !gacha.revision));
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

    // A radio rather than a checkbox: a tier features at most one reward, so
    // "guaranteed featured" names exactly one thing. Tier 3 never splits, and
    // the standard banner never features, so both render an empty cell — the
    // column still exists so every row has the same shape.
    const featuredCell = element('td', 'cell-featured');
    if (tier !== '3' && bannerCanFeature()) {
        const mark = document.createElement('input');
        mark.type = 'radio';
        mark.name = `featured_${tier}`;
        mark.dataset.field = 'featured';
        mark.checked = entry.featured === true;
        mark.setAttribute('aria-label', tr('dashboard.column_featured'));
        // Radios cannot be unset by clicking, and a tier is allowed to feature
        // nothing, so a second click on the checked one clears it.
        mark.addEventListener('click', () => {
            if (mark.dataset.wasChecked === 'true') mark.checked = false;
            [...document.querySelectorAll(`input[name="featured_${tier}"]`)]
                .forEach((other) => { other.dataset.wasChecked = String(other.checked); });
            updateRewardChances();
        });
        mark.dataset.wasChecked = String(mark.checked);
        featuredCell.appendChild(mark);
    }
    row.appendChild(featuredCell);

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

/** The standard banner's enabled rewards for one tier, as this guild has saved
 *  them — the pool a lost featured chance actually draws from.
 *
 *  Read from `gachaBanners`, not from `shippedRewards`: the operator curates the
 *  standard banner, so the shipped table is the wrong answer the moment they do.
 *  Returns null when there is no split — the standard banner is disabled, or is
 *  the banner being edited. An *absent* standard banner still splits, against
 *  the shipped table, which is what the server does.
 */
function standardPoolFor(tier) {
    if (!bannerCanFeature()) return null;
    const standard = gachaBanners.find((banner) => banner.banner_key === DEFAULT_BANNER_KEY);
    if (standard && !standard.enabled) return null;
    const rewards = standard?.config?.rewards || shippedRewards;
    const pool = (rewards?.[tier] || []).filter((entry) => entry.enabled !== false);
    return pool.length ? pool : null;
}

/** Show each row's real draw chance within its tier, enabled rows only.
 *
 *  With a featured reward in the tier, `weight / Σweight` is simply not the
 *  probability any more: the featured row takes the split, and every other row
 *  shares what is left *in proportion to the standard banner's* weights, not to
 *  this banner's. A column labelled "chance within the tier" showing the old
 *  number would be a lie on exactly the banners an operator most wants to check.
 */
function updateRewardChances() {
    const rows = [...document.querySelectorAll('#gacha-grid tbody tr[data-tier]')];
    const split = Number(document.querySelector('#gacha-grid [name="featured_split"]')?.value ?? 50);
    const totals = {};
    const featuredKeys = {};
    rows.forEach((row) => {
        if (!row.querySelector('[data-field="enabled"]').checked) return;
        const weight = Number(row.querySelector('[data-field="weight"]').value) || 0;
        totals[row.dataset.tier] = (totals[row.dataset.tier] || 0) + weight;
        if (row.querySelector('[data-field="featured"]')?.checked) {
            featuredKeys[row.dataset.tier] = row.dataset.key || readRewardKey(row);
        }
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
        const tier = row.dataset.tier;
        const weight = Number(row.querySelector('[data-field="weight"]').value) || 0;

        const pool = featuredKeys[tier] ? standardPoolFor(tier) : null;
        if (pool) {
            // This tier splits. The featured row is the split itself; every other
            // row is only reachable through a loss, against the standard pool.
            if (row.querySelector('[data-field="featured"]')?.checked) {
                target.textContent = `${split.toFixed(2)} %`;
                target.classList.add('reward-chance-featured');
                return;
            }
            target.classList.remove('reward-chance-featured');
            const key = row.dataset.key || readRewardKey(row);
            const poolTotal = pool.reduce((sum, entry) => sum + (entry.weight || 0), 0);
            const match = pool.find((entry) => entry.key === key);
            if (!match || !poolTotal) {
                // Not in the standard pool at all, so a loss can never award it
                // and a win never does either: this row is unreachable here.
                target.textContent = tr('dashboard.gacha_chance_unreachable');
                return;
            }
            const share = (100 - split) * (match.weight / poolTotal);
            target.textContent = `${share.toFixed(2)} %`;
            return;
        }

        target.classList.remove('reward-chance-featured');
        const total = totals[tier] || 0;
        target.textContent = total ? `${((weight / total) * 100).toFixed(2)} %` : '—';
    });

    updateFeaturedNotice(featuredKeys);
}

/** Warn when a featured key is still enabled in the standard pool.
 *
 *  Not a refusal. The design is that an operator curates the standard banner
 *  down and runs the remainder as featured items, so overlapping is a state
 *  they may pass through deliberately. But while it holds, a loss can award the
 *  very item the split is for — pushing the real rate above the configured one
 *  and arming a guarantee for something already won — and that is invisible
 *  from the table alone.
 */
function updateFeaturedNotice(featuredKeys) {
    // Its own container rather than the totals readout, which `updateGachaTotal`
    // clears with replaceChildren on every tier-weight keystroke.
    const host = document.getElementById('gacha-featured-notice');
    if (!host) return;
    const clashes = Object.entries(featuredKeys).filter(([tier, key]) => {
        const pool = standardPoolFor(tier);
        return pool && pool.some((entry) => entry.key === key);
    });
    host.replaceChildren();
    if (!clashes.length) return;
    host.appendChild(element(
        'span', 'featured-notice',
        format('dashboard.gacha_featured_in_standard', {
            rewards: clashes.map(([, key]) => key).join(', '),
        }),
    ));
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
            // Emitted on every row, exactly as `enabled` is, so a row's shape
            // does not depend on which tier it happens to be in. The validator
            // accepts `featured: false` anywhere and refuses `true` outside the
            // rare tiers.
            featured: Boolean(row.querySelector('[data-field="featured"]')?.checked),
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

    // Fall back to what was loaded rather than to Number('') === 0. A scalar a
    // stored banner predates renders as an empty input, and reading that as zero
    // is how `featured_split` would silently become "always lose the split".
    GACHA_INTEGER_FIELDS.forEach((key) => {
        const raw = form.get(key);
        config[key] = raw === null || raw === ''
            ? gacha.config[key] ?? config[key]
            : Number(raw);
    });
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

let workResponses = {responses: [], tiers: [],
                     earnings_placeholder: '{earnings}',
                     coin_placeholder: '{coin}'};

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
    renderWorkTokenHelp();
    setSubtitle('dashboard.subtitle_work_responses',
                {count: workResponses.responses.length});
}

/** What an operator may type into a work response.
 *
 *  The two token strings come from the API, which reads them off
 *  `database.WORK_EARNINGS_PLACEHOLDER` and `WORK_COIN_PLACEHOLDER`, so this can
 *  never advertise a token the bot does not substitute. The locale strings use
 *  their own `{token}` slot rather than naming the token literally — `format`
 *  splits on `{name}`, so a string containing `{earnings}` and given an
 *  `earnings` value would substitute itself.
 */
function renderWorkTokenHelp() {
    const host = document.getElementById('work-token-help');
    if (!host) return;

    const block = element('div', 'token-help');
    block.appendChild(element('h3', 'token-help-title', tr('dashboard.work_tokens_heading')));

    const list = element('ul', 'token-help-list');
    [
        [workResponses.earnings_placeholder, 'dashboard.work_token_earnings'],
        [workResponses.coin_placeholder, 'dashboard.work_token_coin'],
    ].forEach(([token, describes]) => {
        if (!token) return;
        const row = element('li');
        row.appendChild(element('code', 'token', token));
        row.appendChild(document.createTextNode(` ${tr(describes)}`));
        list.appendChild(row);
    });
    block.appendChild(list);

    block.appendChild(element('p', 'token-help-note',
        format('dashboard.work_tokens_note',
               {limit: workResponses.message_max_length || 500})));
    host.replaceChildren(block);
}

/** Each row is editable in place: the text, its weight and whether it is drawn.
 *  A shipped line is an ordinary row — editing or deleting one copies its whole
 *  tier into this guild first, server-side and in one transaction, so what the
 *  operator sees is a plain list they can change. The "copy the defaults over"
 *  button this replaces existed because a shipped row was unreachable, and it
 *  made half the page look read-only for no reason the operator could act on. */
function renderWorkResponseTable(host) {
    const node = table([
        'dashboard.work_tier', 'dashboard.work_message',
        'dashboard.column_reward_weight', 'dashboard.column_reward_chance',
        'dashboard.column_status', 'dashboard.column_actions',
    ]);
    const body = node.querySelector('tbody');
    const rows = workResponses.responses;
    if (!rows.length) emptyRow(body, 6, 'dashboard.work_empty');

    // Every row here is in effect: the API resolves per tier and returns the
    // guild's own lines for a tier it owns, the shipped ones otherwise. So the
    // chance is a share of the pool that is genuinely drawn from, and there is
    // no such thing as an overridden row to render differently. A shipped row
    // still looks and behaves like any other — editing it copies its tier into
    // this guild first, which is the server's job, not something to explain in
    // three badges and a read-only cell.
    const tierTotals = new Map();
    rows.filter((entry) => entry.enabled).forEach((entry) => {
        tierTotals.set(entry.tier, (tierTotals.get(entry.tier) || 0) + entry.weight);
    });

    rows.forEach((entry) => {
        const row = element('tr');

        row.appendChild(element('td', null, tr(`dashboard.work_tier_${entry.tier}`)));

        const messageCell = element('td', 'cell-grow');
        const message = document.createElement('textarea');
        message.value = entry.message;
        message.maxLength = workResponses.message_max_length || 500;
        message.setAttribute('aria-label', tr('dashboard.work_message'));
        messageCell.appendChild(message);
        row.appendChild(messageCell);

        const weightCell = element('td');
        const weight = document.createElement('input');
        weight.type = 'number';
        weight.min = '1';
        weight.value = entry.weight;
        weight.setAttribute('aria-label', tr('dashboard.column_reward_weight'));
        weightCell.appendChild(weight);
        row.appendChild(weightCell);

        const total = tierTotals.get(entry.tier) || 0;
        row.appendChild(element('td', 'cell-mono',
            entry.enabled && total
                ? `${((entry.weight / total) * 100).toFixed(1)}%`
                : '—'));

        const status = element('td');
        status.appendChild(pill(
            entry.enabled ? 'dashboard.status_enabled' : 'dashboard.status_disabled',
            entry.enabled ? 'on' : 'off'));
        row.appendChild(status);

        const actions = element('td', 'cell-actions');
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

/** Which item the creator is open on: undefined for the list, null for a new
 *  one, an item for an edit. Mirrors how the managed-message pages track it. */
let itemEditorTarget;

async function loadShopItems() {
    const itemsHost = document.getElementById('shop-items');
    const listCard = document.getElementById('shop-item-list-card');
    const editor = document.getElementById('shop-item-editor');
    renderSkeleton(itemsHost, 3);
    listCard.classList.remove('hidden');
    editor.classList.add('hidden');
    editor.replaceChildren();

    let payload;
    try {
        payload = await api(
            `/guilds/${guildId}/items?lang=${encodeURIComponent(activeLanguage)}`);
    } catch (error) {
        handleApiError(error);
        itemsHost.replaceChildren(emptyState('dashboard.load_failed', 'ic-alert'));
        return;
    }
    itemList = payload.data;

    const custom = payload.custom_count;
    setSubtitle('dashboard.subtitle_shop_items',
                {count: itemList.length, custom, limit: payload.limit});
    renderItemTable(itemsHost, itemList);

    // One owner of the page-action slot: every render clears it and adds exactly
    // one action, or they accumulate one per save.
    const host = document.getElementById('page-actions');
    host.replaceChildren();
    const create = element('button', 'btn btn-primary', tr('dashboard.item_new'));
    create.type = 'button';
    create.disabled = custom >= payload.limit;
    if (create.disabled) create.title = format('dashboard.item_limit_reached',
                                               {limit: payload.limit});
    create.addEventListener('click', () => renderItemEditor(null));
    host.appendChild(create);
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

/** Every item this guild can sell, built-in and custom, the way `/shop` reads.
 *
 * Built-ins were visible only as a price field on the settings page, named
 * "Loaded die price" and nothing else — an operator could not see what any item
 * actually does without reading the bot's own catalog. The endpoint merges the
 * mechanics, the locale text and the guild's live prices, because none of the
 * three is reachable from the browser on its own.
 */
function renderItemTable(host, items) {
    const node = table([
        'dashboard.column_item', 'dashboard.column_item_effect',
        'dashboard.column_price', 'dashboard.column_available',
        'dashboard.column_status', 'dashboard.column_actions',
    ]);
    const body = node.querySelector('tbody');
    if (!items.length) emptyRow(body, 6, 'dashboard.shop_items_empty');

    items.forEach((item) => {
        const row = element('tr');
        row.classList.toggle('reward-disabled', !item.enabled);

        const identity = element('td');
        identity.appendChild(element('div', 'item-name', item.name));
        if (item.description) {
            identity.appendChild(element('div', 'item-desc', item.description));
        }
        identity.appendChild(element('div', 'cell-key', item.item_key));
        row.appendChild(identity);

        row.appendChild(element('td', null, item.source === 'builtin'
            ? tr(`dashboard.item_effects.${item.effect}`)
            : tr(`dashboard.item_templates.${item.effect}`)));
        // A built-in's price is typed here rather than on a settings page, so
        // every item's price is in one list. It still writes the registered
        // setting, so storage, validation and the audit row are unchanged.
        const priceCell = element('td', 'cell-mono');
        if (item.price === null) {
            priceCell.textContent = '—';
        } else if (item.source === 'builtin' && item.price_setting) {
            const input = document.createElement('input');
            input.type = 'number';
            input.min = '0';
            input.step = '1';
            input.value = String(item.price);
            input.className = 'price-input';
            input.addEventListener('change', () => {
                const price = Number(input.value);
                if (!Number.isInteger(price) || price < 0) {
                    toast(tr('dashboard.item_price_invalid'), true);
                    input.value = String(item.price);
                    return;
                }
                if (price !== item.price) saveBuiltinPrice(item, price, input);
            });
            priceCell.appendChild(input);
        } else {
            priceCell.textContent = `${item.price} ${tr('dashboard.currency_short')}`;
        }
        row.appendChild(priceCell);

        // Where it comes from, which is the question the old table could not
        // answer at all: several items are drawable but not for sale.
        const sources = element('td');
        if (item.in_shop) sources.appendChild(pill('dashboard.source_shop', 'on'));
        if (item.in_gacha) sources.appendChild(pill('dashboard.source_gacha', 'on'));
        if (!item.in_shop && !item.in_gacha) {
            sources.appendChild(pill('dashboard.source_none', 'off'));
        }
        row.appendChild(sources);

        const status = element('td');
        status.appendChild(pill(
            item.source === 'builtin' ? 'dashboard.source_builtin'
                : item.enabled ? 'dashboard.status_enabled' : 'dashboard.status_disabled',
            item.source === 'builtin' ? 'neutral' : item.enabled ? 'on' : 'off'));
        row.appendChild(status);

        const actions = element('td', 'cell-actions');
        if (item.editable) {
            const edit = element('button', 'btn btn-outline', tr('dashboard.edit'));
            edit.type = 'button';
            edit.addEventListener('click', () => renderItemEditor(item));
            actions.appendChild(edit);

            const toggle = element('button', 'btn btn-outline',
                tr(item.enabled ? 'dashboard.disable' : 'dashboard.enable'));
            toggle.type = 'button';
            toggle.addEventListener('click', () => toggleItem(item, toggle));
            actions.appendChild(toggle);

            const remove = element('button', 'btn-icon danger', '');
            remove.type = 'button';
            remove.title = tr('dashboard.delete');
            remove.setAttribute('aria-label', tr('dashboard.delete'));
            remove.appendChild(icon('ic-trash', 'ic ic-sm'));
            remove.addEventListener('click', () => deleteItem(item, remove));
            actions.appendChild(remove);
        } else if (item.price_setting) {
            // A built-in's one editable field, typed here rather than on a
            // settings page: it still writes the registered setting, so the
            // storage, validation and audit trail are unchanged.
            // Nothing else to do to a built-in: its price is editable in place
            // above, and its behaviour is the bot's rather than a guild's.
            actions.appendChild(element('span', 'cell-muted',
                                        tr('dashboard.item_builtin_note')));
        }
        row.appendChild(actions);
        body.appendChild(row);
    });
    host.replaceChildren(node);
}

/** Write a built-in item's price through the setting that already holds it.
 *
 *  The same optimistic revision the settings form sends, so two people editing
 *  the same price still conflict rather than one silently winning.
 */
async function saveBuiltinPrice(item, price, input) {
    input.disabled = true;
    try {
        const saved = await api(`/guilds/${guildId}/settings`, {
            method: 'PATCH', headers: headers(),
            body: JSON.stringify({changes: [{
                key: item.price_setting, value: price,
                revision: settings[item.price_setting]?.revision || 0,
            }]}),
        });
        toast(saved.message);
        await loadGuild();
        await loadShopItems();
    } catch (error) {
        await handleWriteConflict(error, loadShopItems);
        input.disabled = false;
    }
}

async function toggleItem(item, button) {
    button.disabled = true;
    try {
        const saved = await api(
            `/guilds/${guildId}/shop-items/${encodeURIComponent(item.item_key)}`, {
                method: 'PATCH', headers: headers(),
                body: JSON.stringify(itemPatchBody(item, {enabled: !item.enabled})),
            });
        toast(saved.message);
        await loadShopItems();
    } catch (error) {
        await handleWriteConflict(error, loadShopItems);
        button.disabled = false;
    }
}

/** The full body a PATCH needs, unchanged except for what the caller overrides.
 *  The route reuses the creation validator, so a partial body is refused. */
function itemPatchBody(item, changes) {
    return {
        template_type: item.effect,
        enabled: item.enabled,
        price: item.price,
        config: item.config,
        text: {name: item.name || item.item_key,
               description: item.description || ''},
        revision: item.revision,
        ...changes,
    };
}

async function deleteItem(item, button) {
    const accepted = await confirmAction(
        format('dashboard.shop_item_delete_confirm', {item: item.name || item.item_key}),
    );
    if (!accepted) return;
    button.disabled = true;
    try {
        const removed = await api(
            `/guilds/${guildId}/shop-items/${encodeURIComponent(item.item_key)}`, {
                method: 'DELETE', headers: headers(),
                body: JSON.stringify({revision: item.revision}),
            });
        toast(removed.message);
        await loadShopItems();
    } catch (error) {
        await handleWriteConflict(error, loadShopItems);
        button.disabled = false;
    }
}

/* ------------------------------------------------------------- redeems */

/* Its own page, and deliberately not gated on the shop.
 *
 * The fulfillment queue used to sit inside the shop item editor, so turning the
 * shop feature off hid requests that members had already paid for — including
 * gacha-sourced ones, which have nothing to do with the shop at all.
 */
async function loadRedeems() {
    const requestsHost = document.getElementById('fulfillment-list');
    const activeHost = document.getElementById('active-entitlements');
    renderSkeleton(requestsHost, 2);
    renderSkeleton(activeHost, 3);

    let active;
    try {
        const [fulfillment, entitlements] = await Promise.all([
            api(`/guilds/${guildId}/fulfillment`),
            api(`/guilds/${guildId}/entitlements`),
        ]);
        fulfillmentRequests = fulfillment.data;
        active = entitlements.data;
    } catch (error) {
        handleApiError(error);
        requestsHost.replaceChildren(emptyState('dashboard.load_failed', 'ic-alert'));
        activeHost.replaceChildren();
        return;
    }

    const open = fulfillmentRequests.filter((item) => item.status === 'open').length;
    setSubtitle('dashboard.subtitle_redeems', {open, active: active.length});
    renderFulfillmentTable(requestsHost);
    renderActiveEntitlements(activeHost, active);
}

/** Whole units only, largest first: "3d 11h" rather than a raw second count.
 *  Under a minute reads as "under a minute" — a countdown to zero on a page
 *  nobody is watching is precision with no purpose. */
function formatRemaining(seconds) {
    if (seconds === null || seconds === undefined) return '—';
    if (seconds < 60) return tr('dashboard.remaining_soon');
    const days = Math.floor(seconds / 86400);
    const hours = Math.floor((seconds % 86400) / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (days) return format('dashboard.remaining_days', {days, hours});
    if (hours) return format('dashboard.remaining_hours', {hours, minutes});
    return format('dashboard.remaining_minutes', {minutes});
}

function renderActiveEntitlements(host, rows) {
    const node = table([
        'dashboard.column_user', 'dashboard.column_grant',
        'dashboard.column_source', 'dashboard.column_remaining',
        'dashboard.column_expires',
    ]);
    const body = node.querySelector('tbody');
    if (!rows.length) emptyRow(body, 5, 'dashboard.active_empty');

    rows.forEach((row) => {
        const line = element('tr');
        line.appendChild(element('td', 'cell-mono', row.user_id));

        // `role:<id>` is the only composite kind; everything else is a plain
        // name the locale family covers.
        const kind = row.kind.startsWith('role:') ? 'role' : row.kind;
        const grant = element('td');
        grant.appendChild(element('div', null, tr(`dashboard.grant_kinds.${kind}`)));
        if (row.discord_item_id) {
            grant.appendChild(element('div', 'cell-key', row.discord_item_id));
        }
        line.appendChild(grant);

        line.appendChild(element('td', null, row.source_type
            ? tr(`dashboard.source_${row.source_type}`)
            : tr('dashboard.source_none')));
        line.appendChild(element('td', 'cell-mono',
                                 formatRemaining(row.remaining_seconds)));
        line.appendChild(element('td', 'cell-mono',
                                 (row.expires_at || '').slice(0, 16).replace('T', ' ')));
        body.appendChild(line);
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
        row.appendChild(element('td', null, tr(`dashboard.item_asset_types.${item.asset_type}`)));
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
                await loadRedeems();
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

/* ------------------------------------------------------- the item creator */

/* Six templates, declared rather than branched.
 *
 * This was one form with a raw JSON textarea: two of the six templates got a
 * picker and the other four expected the operator to type
 * `{"role_id": 1469…, "duration_days": 30}` correctly first time, with no role
 * picker and nothing in the interface saying what shape was wanted. The same
 * visible slot silently changed meaning, because `form.config.required` was
 * toggled on a hidden field. It is the shape `MANAGED_KINDS` already uses, for
 * the same reason.
 *
 * `fields` are the config's own fields; the identity, price and text fields are
 * shared by every template and rendered once. `pack` builds the `config` object
 * the server validates, and `unpack` turns a stored row back into form values,
 * so opening an item and saving it unchanged writes back what was there.
 */
const SHOP_TEMPLATE_FIELDS = {
    role_id: {kind: 'role', label: 'dashboard.item_role',
              localeKey: 'dashboard.item_role', required: true,
              hint: 'item_role'},
    duration_days: {kind: 'number', label: 'dashboard.item_duration', min: 1,
                    max: 3650, default: 30, required: true,
                    hint: 'item_duration'},
    vault_item: {kind: 'catalog', label: 'dashboard.item_vault', effect: 'vault',
                 required: true, hint: 'item_vault'},
    consumable_item: {kind: 'catalog', label: 'dashboard.item_consumable',
                      effect: 'inventory', required: true,
                      hint: 'item_consumable'},
    coin_amount: {kind: 'number', label: 'dashboard.item_coin_amount', min: 0,
                  default: 1000, required: true, hint: 'item_coin_amount'},
    repeatable: {kind: 'checkbox', label: 'dashboard.item_repeatable',
                 default: false, hint: 'item_repeatable'},
    asset_type: {kind: 'choice', label: 'dashboard.item_asset_type',
                 options: ['emoji', 'sticker', 'sound'], default: 'emoji',
                 optionLabels: 'dashboard.item_asset_types',
                 hint: 'item_asset_type'},
};

const SHOP_TEMPLATES = {
    fixed_role: {
        fields: ['role_id'],
        unpack: (config) => ({role_id: config.role_id ? String(config.role_id) : null}),
        pack: (values) => ({role_id: values.role_id}),
    },
    timed_role: {
        fields: ['role_id', 'duration_days'],
        unpack: (config) => ({role_id: config.role_id ? String(config.role_id) : null,
                              duration_days: config.duration_days}),
        pack: (values) => ({role_id: values.role_id,
                            duration_days: values.duration_days}),
    },
    vault: {
        fields: ['vault_item'],
        // Stored as an amount, chosen as an item: the reserve a vault protects
        // is the catalog's to decide, and the server refuses a mismatch.
        unpack: (config) => ({vault_item: vaultKeyForAmount(config.amount)}),
        pack: (values) => ({amount: vaultAmountForKey(values.vault_item)}),
    },
    consumable: {
        fields: ['consumable_item'],
        unpack: (config) => ({consumable_item: config.item_key}),
        pack: (values) => ({item_key: values.consumable_item}),
    },
    coin_bundle: {
        fields: ['coin_amount', 'repeatable'],
        unpack: (config) => ({coin_amount: config.amount,
                              repeatable: Boolean(config.repeatable)}),
        pack: (values) => ({amount: values.coin_amount,
                            repeatable: Boolean(values.repeatable)}),
    },
    fulfillment_voucher: {
        fields: ['asset_type', 'duration_days'],
        unpack: (config) => ({asset_type: config.asset_type,
                              duration_days: config.duration_days}),
        pack: (values) => ({asset_type: values.asset_type,
                            duration_days: values.duration_days}),
    },
};

function vaultKeyForAmount(amount) {
    const match = (itemList || []).find(
        (item) => item.effect === 'vault' && item.value === amount);
    return match ? match.item_key : null;
}

function vaultAmountForKey(key) {
    const match = (itemList || []).find((item) => item.item_key === key);
    return match ? match.value : null;
}

/** The item creator, in place of the list. Same shape as the content builders:
 *  numbered sections, a Back action, and no raw JSON anywhere. */
function renderItemEditor(existing) {
    const host = document.getElementById('shop-item-editor');
    const listCard = document.getElementById('shop-item-list-card');
    if (!host || !listCard) return;
    listCard.classList.add('hidden');
    host.classList.remove('hidden');
    host.replaceChildren();

    const isNew = !existing;
    // `effect`, not `template_type`: `/items` merges built-ins and customs into
    // one shape and serves a custom item's template under `effect` — which is
    // why `itemPatchBody` sends `template_type: item.effect`. Reading the wrong
    // name gave `undefined`, the choice field fell back to its first option, and
    // *every* item opened as "Permanent role" asking which role to grant,
    // whatever it actually was. Editing a vault and saving would have rewritten
    // it into a role grant.
    const template = existing ? existing.effect : 'fixed_role';
    const readers = {};
    const form = element('form', 'stack');

    const identity = itemEditorSection(1, 'dashboard.item_section_identity');
    const grid = element('div', 'form-grid');
    addEditorField(grid, readers, 'item_key', {
        kind: 'text', label: 'dashboard.item_key', max: 64, required: true,
        immutableAfterCreate: true, hint: 'item_key',
    }, existing ? existing.item_key : '', isNew);
    const templateControl = addEditorField(grid, readers, 'template_type', {
        kind: 'choice', label: 'dashboard.item_template',
        options: Object.keys(SHOP_TEMPLATES), default: template,
        optionLabels: 'dashboard.item_templates', hint: 'item_template',
    }, template, isNew);
    addEditorField(grid, readers, 'price', {
        kind: 'number', label: 'dashboard.item_price', min: 0, default: 1000,
        required: true, hint: 'item_price',
    }, existing ? existing.price : undefined, isNew);
    identity.appendChild(grid);
    form.appendChild(identity);

    // One text, in whatever language this guild speaks: a custom item lives in
    // their database and only their members read it.
    const text = itemEditorSection(2, 'dashboard.item_section_text');
    const textGrid = element('div', 'form-grid');
    addEditorField(textGrid, readers, 'name', {
        kind: 'text', label: 'dashboard.item_name', max: 100, required: true,
        hint: 'item_name',
    }, existing ? existing.name || '' : '', isNew);
    addEditorField(textGrid, readers, 'description', {
        kind: 'multiline', label: 'dashboard.item_description', max: 400,
        required: true, wide: true, hint: 'item_description',
    }, existing ? existing.description || '' : '', isNew);
    text.appendChild(textGrid);
    form.appendChild(text);

    // Section three is the only part that changes with the template, so it is
    // rebuilt in place when the choice changes rather than the whole form.
    const configSection = itemEditorSection(3, 'dashboard.item_section_config');
    const configGrid = element('div', 'form-grid');
    configSection.appendChild(configGrid);
    form.appendChild(configSection);

    const configReaders = {};
    const drawConfig = () => {
        Object.keys(configReaders).forEach((key) => delete configReaders[key]);
        configGrid.replaceChildren();
        const chosen = readers.template_type();
        const spec = SHOP_TEMPLATES[chosen];
        // `effect`, not `template_type`: that is the field `/items` serves a
        // custom item's kind under. Comparing the wrong name was always false,
        // so editing an item drew section three *empty* — a vault whose reserve
        // read "Nothing selected", which saving would then have written back.
        const values = existing && existing.effect === chosen
            ? spec.unpack(existing.config || {}) : {};
        spec.fields.forEach((name) => {
            addEditorField(configGrid, configReaders, name,
                           SHOP_TEMPLATE_FIELDS[name], values[name], isNew);
        });
    };
    drawConfig();
    // Bound to the kind control itself. This was a delegated listener on the
    // form that matched the event's ancestor — first on `.field`, which
    // `managedFieldWrapper` never sets, and then on `[data-field]`. Both times
    // the symptom was identical and silent: section three kept asking for a
    // role whichever kind you picked, so a vault could not be created at all.
    // A redraw that depends on an event reaching an ancestor and matching a
    // selector has two ways to fail; binding to the control has none. `input`
    // as well as `change` because a keyboard-driven select fires `input` first
    // and some browsers coalesce the `change`.
    ['change', 'input'].forEach((name) => {
        templateControl.addEventListener(name, drawConfig);
    });

    const actions = element('div', 'form-actions');
    const submit = element('button', 'btn btn-primary',
                           tr(isNew ? 'dashboard.create' : 'dashboard.save'));
    submit.type = 'submit';
    actions.appendChild(submit);
    form.appendChild(actions);

    form.addEventListener('submit', (event) => {
        event.preventDefault();
        saveItem(existing, readers, configReaders, submit);
    });
    host.appendChild(form);

    const back = element('button', 'btn btn-ghost', tr('dashboard.back'));
    back.type = 'button';
    back.addEventListener('click', () => { itemEditorTarget = undefined; loadShopItems(); });
    document.getElementById('page-actions').replaceChildren(back);
}

function itemEditorSection(index, labelKey) {
    const node = element('fieldset', 'managed-section');
    node.appendChild(element('legend', null, `${index}. ${tr(labelKey)}`));
    return node;
}

/** One labelled field, registering its reader under `name`. */
function addEditorField(host, readers, name, spec, value, isNew) {
    const control = managedFieldControl(name, spec, value, isNew);
    const wrapper = managedFieldWrapper(spec, control.node);
    wrapper.dataset.field = name;
    if (spec.wide) wrapper.classList.add('wide');
    host.appendChild(wrapper);
    readers[name] = control.read;
    // Returned so a caller that must react to *this* field can bind to the
    // control itself rather than delegating from an ancestor and matching on
    // `data-field` — an indirection that has already silently broken twice.
    return control.node;
}

async function saveItem(existing, readers, configReaders, submit) {
    const values = {};
    Object.entries(readers).forEach(([name, read]) => { values[name] = read(); });
    const config = {};
    Object.entries(configReaders).forEach(([name, read]) => { config[name] = read(); });

    const template = values.template_type;
    const packed = SHOP_TEMPLATES[template].pack(config);
    // An incomplete row must not be packed into a value the server will reject
    // for a reason the operator cannot see. Say which field is missing instead.
    const missing = SHOP_TEMPLATES[template].fields.find(
        (name) => SHOP_TEMPLATE_FIELDS[name].required
            && (config[name] === null || config[name] === undefined
                || config[name] === ''));
    if (missing) {
        toast(format('dashboard.item_field_required',
                     {field: tr(SHOP_TEMPLATE_FIELDS[missing].label)}), true);
        return;
    }

    const payload = {
        template_type: template,
        enabled: existing ? existing.enabled : true,
        price: values.price,
        config: packed,
        text: {name: values.name, description: values.description},
    };

    submit.disabled = true;
    try {
        if (existing) {
            payload.revision = existing.revision;
            const result = await api(
                `/guilds/${guildId}/shop-items/${encodeURIComponent(existing.item_key)}`,
                {method: 'PATCH', headers: headers(), body: JSON.stringify(payload)});
            toast(result.message);
        } else {
            payload.item_key = values.item_key;
            const result = await api(`/guilds/${guildId}/shop-items`, {
                method: 'POST', headers: headers(), body: JSON.stringify(payload),
            });
            toast(result.message);
        }
        itemEditorTarget = undefined;
        await loadShopItems();
    } catch (error) {
        await handleWriteConflict(error, loadShopItems);
    } finally {
        submit.disabled = false;
    }
}

/* ------------------------------------------------------- managed messages */

/* The four pages that edit a posted Discord message.
 *
 * They are one renderer driven by a declaration rather than four functions,
 * for the same reason `JSON_ROW_SHAPES` is a declaration: the alternative was
 * one function full of `if (kind === 'rules')`, and a fifth kind would have made
 * it unreadable. What a kind cannot declare — how a stored row unpacks and packs
 * again, and the cross-field rules Discord imposes — stays as functions here,
 * beside the fields they belong to.
 */

/* One field descriptor, reused across kinds. `localeKey` matters: a picker names
 * itself from it, and a definition without one used to throw. */
const MANAGED_FIELDS = {
    display_name: {kind: 'text', label: 'dashboard.managed_name', max: 80,
                   required: true, hint: 'managed_name'},
    menu_key: {kind: 'text', label: 'dashboard.managed_key', max: 32,
               required: true, immutableAfterCreate: true, hint: 'managed_key'},
    colour: {kind: 'colour', label: 'dashboard.managed_colour'},
    channel: {kind: 'channel', label: 'dashboard.managed_channel',
              localeKey: 'dashboard.managed_channel',
              channelTypes: ['text', 'news'], hint: 'managed_channel'},
    title: {kind: 'text', label: 'dashboard.managed_title', max: 256},
    body: {kind: 'multiline', label: 'dashboard.managed_body', max: 4096,
           wide: true, hint: 'managed_body'},
    accept_button: {kind: 'checkbox', label: 'dashboard.managed_accept_button',
                    default: true, hint: 'managed_accept_button'},
    thumbnail: {kind: 'checkbox', label: 'dashboard.managed_thumbnail',
                default: true},
    button_label: {kind: 'text', label: 'dashboard.managed_button_label',
                   max: 80, hint: 'managed_button_label'},
    image_url: {kind: 'text', label: 'dashboard.managed_image', max: 1024,
                wide: true, hint: 'managed_image'},
};

const RULES_SECTION_LIMIT = 10;
const MANAGED_ENTRY_LIMIT = 25;
const MESSAGE_TOTAL_LIMIT = 6000;

/* Per kind: which page it owns, which numbered sections its creator has, how a
 * stored row becomes form values and back, and which preview it draws. */
const MANAGED_KINDS = {
    rules: {
        page: 'rules-panel',
        buttonStyle: 'success',
        buttonEmoji: '✅',
        sections: [
            {legend: 'dashboard.managed_step_message',
             fields: ['display_name', 'menu_key', 'colour', 'channel']},
            {legend: 'dashboard.managed_step_embeds', repeat: 'embeds'},
            {legend: 'dashboard.managed_step_button',
             fields: ['accept_button', 'button_label', 'thumbnail',
                      'image_url']},
        ],
        repeat: {
            name: 'embeds', limit: RULES_SECTION_LIMIT, layout: 'block',
            addLabel: 'dashboard.managed_add_embed',
            caption: 'dashboard.managed_embed_index',
            fields: [
                {name: 'title', kind: 'text', max: 256,
                 label: 'dashboard.managed_section_title'},
                {name: 'body', kind: 'multiline', max: 4096, required: true,
                 label: 'dashboard.managed_section_body'},
            ],
        },
        unpack: (item) => ({
            display_name: item?.display_name ?? '',
            menu_key: item?.menu_key ?? 'rules',
            colour: item?.colour ?? 0xF5B041,
            channel: item?.channel_id ?? null,
            accept_button: item?.options?.accept_button !== false,
            thumbnail: item?.options?.thumbnail !== false,
            button_label: item?.options?.button_label ?? '',
            image_url: item?.options?.image_url ?? '',
            embeds: (item?.options?.sections ?? []).map((section) => ({
                title: section.title ?? '', body: section.body ?? '',
            })),
        }),
        pack: (values) => ({
            title: null, body: null,
            options: {
                sections: values.embeds.map((row) => ({
                    title: row.title || null, body: row.body,
                })),
                accept_button: values.accept_button,
                thumbnail: values.thumbnail,
                button_label: values.button_label || null,
                image_url: values.image_url || null,
            },
            entries: [],
        }),
    },
    role_menu: {
        page: 'role-menus',
        buttonStyle: 'secondary',
        sections: [
            {legend: 'dashboard.managed_step_message',
             fields: ['display_name', 'menu_key', 'colour', 'channel',
                      'title', 'body']},
            {legend: 'dashboard.managed_step_roles', repeat: 'entries'},
        ],
        repeat: {
            name: 'entries', limit: MANAGED_ENTRY_LIMIT, layout: 'row',
            addLabel: 'dashboard.role_menu_add',
            fields: [
                {name: 'label', kind: 'text', max: 80, required: true,
                 label: 'dashboard.role_menu_label'},
                {name: 'role_id', kind: 'role', required: true,
                 localeKey: 'dashboard.role_menu_role',
                 label: 'dashboard.role_menu_role'},
                {name: 'emoji', kind: 'text', max: 64, narrow: true,
                 label: 'dashboard.role_menu_emoji'},
            ],
        },
        unpack: (item) => ({
            display_name: item?.display_name ?? '',
            menu_key: item?.menu_key ?? '',
            colour: item?.colour ?? 0xF5B041,
            channel: item?.channel_id ?? null,
            title: item?.title ?? '',
            body: item?.body ?? '',
            entries: (item?.entries ?? []).map((entry) => ({
                label: entry.label ?? '', role_id: entry.role_id ?? '',
                emoji: entry.emoji ?? '',
            })),
        }),
        pack: (values) => ({
            title: values.title || null, body: values.body || null,
            options: {}, entries: values.entries,
        }),
    },
    embed: {
        // The plain sender: a message the bot says, and nothing around it. No
        // button, no server icon, no drafts — it is posted, and afterwards it is
        // edited in place like everything else on these pages.
        page: 'embeds',
        sections: [
            {legend: 'dashboard.managed_step_message',
             fields: ['display_name', 'menu_key', 'colour', 'channel']},
            {legend: 'dashboard.managed_step_embeds', repeat: 'embeds'},
            {legend: 'dashboard.managed_step_banner', fields: ['image_url']},
        ],
        repeat: {
            name: 'embeds', limit: RULES_SECTION_LIMIT, layout: 'block',
            addLabel: 'dashboard.managed_add_embed',
            caption: 'dashboard.managed_embed_index',
            fields: [
                {name: 'title', kind: 'text', max: 256,
                 label: 'dashboard.managed_section_title'},
                {name: 'body', kind: 'multiline', max: 4096, required: true,
                 label: 'dashboard.managed_section_body'},
            ],
        },
        unpack: (item) => ({
            display_name: item?.display_name ?? '',
            menu_key: item?.menu_key ?? '',
            colour: item?.colour ?? 0xF5B041,
            channel: item?.channel_id ?? null,
            image_url: item?.options?.image_url ?? '',
            embeds: (item?.options?.sections ?? []).map((section) => ({
                title: section.title ?? '', body: section.body ?? '',
            })),
        }),
        pack: (values) => ({
            title: null, body: null,
            options: {
                sections: values.embeds.map((row) => ({
                    title: row.title || null, body: row.body,
                })),
                image_url: values.image_url || null,
            },
            entries: [],
        }),
    },
    ticket: {
        page: 'ticket-launcher',
        buttonStyle: 'primary',
        buttonEmoji: '📩',
        defaultKey: 'ticket',
        sections: [
            {legend: 'dashboard.managed_step_message',
             fields: ['display_name', 'menu_key', 'colour', 'channel',
                      'title', 'body']},
            {legend: 'dashboard.managed_step_button', fields: ['button_label']},
        ],
        unpack: (item) => simplePanelValues(item, 'ticket'),
        pack: simplePanelPayload,
    },
    airlock: {
        page: 'entry-gate',
        buttonStyle: 'success',
        buttonEmoji: '🚀',
        defaultKey: 'airlock',
        sections: [
            {legend: 'dashboard.managed_step_message',
             fields: ['display_name', 'menu_key', 'colour', 'channel',
                      'title', 'body']},
            {legend: 'dashboard.managed_step_button', fields: ['button_label']},
        ],
        unpack: (item) => simplePanelValues(item, 'airlock'),
        pack: simplePanelPayload,
    },
};

/* The two one-button panels differ only in their defaults, so they share these
 * rather than declaring the same pair twice. */
function simplePanelValues(item, defaultKey) {
    return {
        display_name: item?.display_name ?? '',
        menu_key: item?.menu_key ?? defaultKey,
        colour: item?.colour ?? 0xF5B041,
        channel: item?.channel_id ?? null,
        title: item?.title ?? '',
        body: item?.body ?? '',
        button_label: item?.options?.button_label ?? '',
    };
}

function simplePanelPayload(values) {
    return {
        title: values.title || null, body: values.body || null,
        options: values.button_label ? {button_label: values.button_label} : {},
        entries: [],
    };
}

/* Derived, never a second hand-kept list — the same reason `FEATURE_GROUP_ORDER`
 * reaches the client from the registry instead of being retyped here. */
const MANAGED_PAGES = Object.fromEntries(
    Object.entries(MANAGED_KINDS).map(([kind, spec]) => [spec.page, {kind, ...spec}]),
);

let managedItems = [];
let managedDirty = false;

async function loadManaged(page) {
    const spec = MANAGED_PAGES[page];
    const host = document.getElementById(`${page}-list`);
    renderSkeleton(host, 3);
    try {
        managedItems = (await api(`/guilds/${guildId}/managed/${spec.kind}`)).data;
    } catch (error) {
        handleApiError(error);
        host.replaceChildren(emptyState('dashboard.load_failed', 'ic-alert'));
        return;
    }
    showManagedList(page);
}

/* One owner of the page-action slot.
 *
 * `loadManaged` used to add a "New" action every time it ran, and both saving
 * and deleting call it again — so the buttons accumulated, one per save, until
 * you navigated away. Every render clears the slot first and puts exactly one
 * action in it. */
function setManagedAction(labelKey, symbol, handler) {
    document.getElementById('page-actions').replaceChildren();
    addPageAction(labelKey, symbol, handler);
}

function showManagedList(page) {
    managedDirty = false;
    document.getElementById(`${page}-list-card`).classList.remove('hidden');
    document.getElementById(`${page}-editor`).classList.add('hidden');
    setSubtitle(`dashboard.subtitle_${page.replaceAll('-', '_')}`,
                {count: managedItems.length});
    renderManagedList(page);
    setManagedAction('dashboard.managed_new', 'ic-plus',
                     () => showManagedEditor(page, null));
}

function showManagedEditor(page, item) {
    document.getElementById(`${page}-list-card`).classList.add('hidden');
    const host = document.getElementById(`${page}-editor`);
    host.classList.remove('hidden');
    renderManagedEditor(page, item);
    setManagedAction('dashboard.managed_back', 'ic-chevron-right', async () => {
        // Leaving with unsaved text is the one thing this view can lose, so it
        // is the one thing it asks about.
        if (managedDirty && !await confirmAction(tr('dashboard.managed_discard_confirm'))) {
            return;
        }
        showManagedList(page);
    });
}

function renderManagedList(page) {
    const host = document.getElementById(`${page}-list`);
    const node = table(['dashboard.column_name', 'dashboard.column_key',
                        'dashboard.column_status', 'dashboard.column_channel',
                        'dashboard.column_actions']);
    const body = node.querySelector('tbody');
    if (!managedItems.length) emptyRow(body, 5, 'dashboard.managed_empty');

    managedItems.forEach((item) => {
        const row = element('tr');
        row.appendChild(element('td', null, item.display_name));
        row.appendChild(element('td', 'cell-key', item.menu_key));

        const status = element('td');
        status.appendChild(pill(item.posted ? 'dashboard.managed_posted'
                                            : 'dashboard.managed_draft',
                                item.posted ? 'on' : 'off'));
        row.appendChild(status);

        const channel = resources.channels.find(
            (entry) => String(entry.id) === String(item.channel_id));
        row.appendChild(element('td', 'cell-mono', channel ? `#${channel.name}` : '—'));

        const actions = element('td', 'cell-actions');
        const edit = element('button', 'btn btn-outline', tr('dashboard.edit'));
        edit.type = 'button';
        edit.addEventListener('click', () => showManagedEditor(page, item));
        const remove = element('button', 'btn-icon danger', '');
        remove.type = 'button';
        remove.title = tr('dashboard.delete');
        remove.setAttribute('aria-label', tr('dashboard.delete'));
        remove.appendChild(icon('ic-trash', 'ic ic-sm'));
        remove.addEventListener('click', () => deleteManaged(page, item, remove));
        actions.append(edit, remove);
        row.appendChild(actions);
        body.appendChild(row);
    });
    host.replaceChildren(node);
}

/* ------------------------------------------------------------- the creator */

/** One field, by declared kind. Returns the node and how to read it back. */
function managedFieldControl(name, spec, value, isNew) {
    if (spec.kind === 'channel' || spec.kind === 'role') {
        const picker = resourcePicker({
            key: `managed.${name}`, value_type: spec.kind,
            locale_key: spec.localeKey || spec.label,
            channel_types: spec.channelTypes || [],
        }, value || null);
        return {node: picker, read: () => readManagedChannel(picker)};
    }
    if (spec.kind === 'checkbox') {
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.checked = value === undefined ? Boolean(spec.default) : Boolean(value);
        return {node: input, read: () => input.checked};
    }
    if (spec.kind === 'colour') {
        const input = document.createElement('input');
        input.type = 'color';
        const numeric = Number.isInteger(value) ? value : 0xF5B041;
        input.value = `#${numeric.toString(16).padStart(6, '0')}`;
        return {node: input, read: () => parseInt(input.value.slice(1), 16)};
    }
    if (spec.kind === 'multiline') {
        const input = document.createElement('textarea');
        input.value = value ?? '';
        if (spec.max) input.maxLength = spec.max;
        return {node: input, read: () => input.value};
    }
    if (spec.kind === 'number') {
        const input = document.createElement('input');
        input.type = 'number';
        input.value = value ?? (spec.default ?? '');
        if (spec.min !== undefined) input.min = spec.min;
        if (spec.max !== undefined) input.max = spec.max;
        if (spec.required) input.required = true;
        // An empty numeric field reads as null, not 0. Zero is a legitimate
        // price and "not filled in" must stay distinguishable from it, or a
        // half-finished row saves as free.
        return {node: input, read: () => (
            input.value === '' ? null : Number(input.value))};
    }
    if (spec.kind === 'choice') {
        const select = document.createElement('select');
        (spec.options || []).forEach((option) => {
            const node = document.createElement('option');
            node.value = option;
            // Options are stable English identifiers, so an unlabelled one reads
            // as itself rather than as a bracketed placeholder.
            node.textContent = spec.optionLabels
                ? tr(`${spec.optionLabels}.${option}`) : option;
            select.appendChild(node);
        });
        if (value !== undefined && value !== null) select.value = value;
        else if (spec.default !== undefined) select.value = spec.default;
        return {node: select, read: () => select.value};
    }
    if (spec.kind === 'catalog') {
        // The consumable picker. It listed raw keys like `loaded_die`, because
        // /api/item-catalog carries no names; the item list endpoint does, so
        // this reads them from there.
        const select = document.createElement('select');
        (itemList || []).filter((item) => item.source === 'builtin'
                                && item.effect === spec.effect)
            .forEach((item) => {
                const node = document.createElement('option');
                node.value = item.item_key;
                node.textContent = item.name;
                select.appendChild(node);
            });
        if (value) select.value = value;
        return {node: select, read: () => select.value || null};
    }
    const input = document.createElement('input');
    input.type = 'text';
    input.value = value ?? '';
    if (spec.max) input.maxLength = spec.max;
    if (spec.placeholder) input.placeholder = spec.placeholder;
    if (spec.required) input.required = true;
    // The key is the row's primary key and addresses the posted message, so it
    // is chosen once: renaming it would orphan the message rather than rename it.
    if (spec.immutableAfterCreate && !isNew) input.disabled = true;
    return {node: input, read: () => input.value.trim()};
}

/** A labelled field. Not a `<label>` around a picker: a label forwards its click
 *  to the labelable control it wraps, and a button is labelable, so the field
 *  name would close and instantly reopen the menu. */
function managedFieldWrapper(spec, control) {
    const isPicker = spec.kind === 'channel' || spec.kind === 'role';
    const wrapper = element(isPicker ? 'div' : 'label',
                            `input-group${spec.wide ? ' wide' : ''}`);
    wrapper.appendChild(element('span', 'field-label', tr(spec.label)));
    wrapper.appendChild(control);
    const hint = spec.hint ? fieldHint(spec.hint) : null;
    if (hint) wrapper.appendChild(hint);
    return wrapper;
}

/** The hidden `<select>` a resource picker carries its value in. */
function readManagedChannel(picker) {
    const carrier = picker.querySelector('.picker-carrier');
    return [...carrier.selectedOptions].map((option) => option.value)[0] || '';
}

function renderManagedEditor(page, item) {
    const spec = MANAGED_PAGES[page];
    const host = document.getElementById(`${page}-editor`);
    const values = spec.unpack(item);
    const isNew = !item;
    host.replaceChildren();

    const head = element('div', 'card-head');
    const headings = element('div');
    headings.appendChild(element('h2', 'display', item
        ? format('dashboard.managed_edit_heading', {name: item.display_name})
        : tr('dashboard.managed_new_heading')));
    headings.appendChild(element('p', 'card-hint', tr(`dashboard.${page.replaceAll('-', '_')}_hint`)));
    head.appendChild(headings);
    host.appendChild(head);

    const layout = element('div', 'managed-creator');
    const form = document.createElement('form');
    const readers = {};
    let repeat = null;

    spec.sections.forEach((section, index) => {
        const fieldset = document.createElement('fieldset');
        fieldset.className = 'form-section';
        const legend = document.createElement('legend');
        legend.textContent = `${index + 1}. ${tr(section.legend)}`;
        fieldset.appendChild(legend);

        if (section.repeat) {
            repeat = managedRepeat(spec.repeat, values[spec.repeat.name] || []);
            fieldset.appendChild(repeat.node);
            readers[spec.repeat.name] = repeat.read;
        } else {
            const grid = element('div', 'form-grid');
            section.fields.forEach((name) => {
                const fieldSpec = MANAGED_FIELDS[name];
                const control = managedFieldControl(name, fieldSpec,
                                                    values[name], isNew);
                grid.appendChild(managedFieldWrapper(fieldSpec, control.node));
                readers[name] = control.read;
            });
            fieldset.appendChild(grid);
        }
        form.appendChild(fieldset);
    });

    const collect = () => Object.fromEntries(
        Object.entries(readers).map(([name, read]) => [name, read()]));

    const preview = element('div', 'message-preview');
    const redraw = () => {
        preview.replaceChildren(messagePreview(spec, collect()));
    };

    // Offered only while this row owns no message: once one is linked, Update is
    // the way to change it and a second link would just move the target.
    if (!item?.posted) {
        form.appendChild(managedAdoptSection(page, spec, () => ({
            menu_key: readers.menu_key(),
            display_name: readers.display_name(),
        })));
    }

    const footer = element('div', 'form-footer');
    const save = element('button', 'btn btn-outline', tr('dashboard.save_settings'));
    save.type = 'button';
    const publish = element('button', 'btn btn-primary',
        tr(item?.posted ? 'dashboard.managed_update' : 'dashboard.managed_post'));
    publish.type = 'submit';
    publish.prepend(icon('ic-send', 'ic ic-sm'));
    footer.append(save, publish);
    form.appendChild(footer);

    const submit = async (alsoPublish) => {
        const values = collect();
        const problem = managedProblem(spec, values);
        if (problem) {
            toast(tr(problem), true);
            return;
        }
        if (alsoPublish && !values.channel) {
            toast(tr('dashboard.publish_channel_required'), true);
            return;
        }
        save.disabled = true;
        publish.disabled = true;
        try {
            const payload = {
                menu_key: values.menu_key,
                display_name: values.display_name,
                revision: item?.revision ?? 0,
                colour: values.colour,
                ...spec.pack(values),
            };
            const saved = await api(`/guilds/${guildId}/managed/${spec.kind}`,
                                    {method: 'POST', headers: headers(),
                                     body: JSON.stringify(payload)});
            toast(saved.message);
            managedDirty = false;
            if (alsoPublish) {
                const queued = await api(
                    `/guilds/${guildId}/managed/${spec.kind}/`
                    + `${encodeURIComponent(values.menu_key)}/publish`,
                    {method: 'POST', headers: headers(),
                     body: JSON.stringify({channel_id: values.channel})});
                toast(queued.message);
                await followAction(queued.data?.action_id);
            }
            await loadManaged(page);
        } catch (error) {
            await handleWriteConflict(error, () => loadManaged(page));
        } finally {
            save.disabled = false;
            publish.disabled = false;
        }
    };

    save.addEventListener('click', () => submit(false));
    form.addEventListener('submit', (event) => {
        event.preventDefault();
        submit(true);
    });

    // Delegated, because blocks and rows appear and disappear at runtime and a
    // per-field listener would go stale. The resource picker dispatches `change`
    // from its carrier with `bubbles`, so a channel choice arrives here too.
    let pending = null;
    const onEdit = () => {
        managedDirty = true;
        // A colour input fires `input` continuously while dragging and a
        // ten-embed panel is ~40 nodes, so the redraw is coalesced.
        clearTimeout(pending);
        pending = setTimeout(redraw, 120);
    };
    form.addEventListener('input', onEdit);
    form.addEventListener('change', onEdit);
    if (repeat) repeat.onChange(() => { managedDirty = true; redraw(); });

    layout.append(form, preview);
    host.appendChild(layout);
    redraw();
    const first = form.querySelector('input:not([disabled]), textarea');
    if (first) first.focus();
}

/** Take over a message the bot already posted.
 *
 *  The schema-12 migration leaves `message_id` NULL on purpose, so a menu that
 *  was already up keeps working while the dashboard cannot yet edit *that*
 *  message. Without this the only route was posting a second copy and deleting
 *  the first by hand, which moves the message to the bottom of its channel and
 *  loses its pins.
 */
function managedAdoptSection(page, spec, identity) {
    const fieldset = document.createElement('fieldset');
    fieldset.className = 'form-section';
    const legend = document.createElement('legend');
    legend.textContent = tr('dashboard.managed_step_adopt');
    fieldset.appendChild(legend);

    const grid = element('div', 'form-grid');
    const reference = document.createElement('input');
    reference.type = 'text';
    reference.placeholder = 'https://discord.com/channels/…';
    const field = element('label', 'input-group wide');
    field.appendChild(element('span', 'field-label', tr('dashboard.managed_adopt_field')));
    field.appendChild(reference);
    const hint = fieldHint('managed_adopt');
    if (hint) field.appendChild(hint);
    grid.appendChild(field);
    fieldset.appendChild(grid);

    const action = element('button', 'btn btn-outline', tr('dashboard.managed_adopt'));
    action.type = 'button';
    action.addEventListener('click', async () => {
        const {menu_key: menuKey, display_name: displayName} = identity();
        if (!menuKey || !displayName) {
            toast(tr('dashboard.errors.managed_name_invalid'), true);
            return;
        }
        if (!reference.value.trim()) {
            toast(tr('dashboard.errors.managed_adopt_reference'), true);
            return;
        }
        action.disabled = true;
        try {
            const result = await api(`/guilds/${guildId}/managed/${spec.kind}/adopt`,
                                     {method: 'POST', headers: headers(),
                                      body: JSON.stringify({
                                          message: reference.value.trim(),
                                          menu_key: menuKey,
                                          display_name: displayName})});
            toast(result.message);
            managedDirty = false;
            await loadManaged(page);
            // Straight back into the creator, now filled from the message, so
            // what was read can be checked before anything else is changed.
            const adopted = managedItems.find((row) => row.menu_key === menuKey);
            if (adopted) showManagedEditor(page, adopted);
        } catch (error) {
            handleApiError(error);
            action.disabled = false;
        }
    });
    fieldset.appendChild(action);
    return fieldset;
}

/** The cross-field rules a declaration cannot express, per kind. */
function managedProblem(spec, values) {
    if (!values.display_name) return 'dashboard.errors.managed_name_invalid';
    if (!values.menu_key) return 'dashboard.errors.managed_key_invalid';
    if (spec.kind === 'rules' || spec.kind === 'embed') {
        const sections = values.embeds || [];
        if (!sections.length) return 'dashboard.errors.managed_rules_sections';
        const total = sections.reduce(
            (sum, row) => sum + row.title.length + row.body.length, 0);
        // One message carries 6000 characters across all its embeds, and going
        // over fails the whole send rather than truncating.
        if (total > MESSAGE_TOTAL_LIMIT) return 'dashboard.errors.managed_rules_total';
    }
    if (spec.kind === 'role_menu') {
        const labels = (values.entries || []).map((entry) => entry.label);
        if (!labels.length) return 'dashboard.errors.managed_menu_empty';
        // The label is the button's `custom_id`, so two the same cannot be told
        // apart — and the API refuses the whole save rather than one row.
        if (new Set(labels).size !== labels.length) {
            return 'dashboard.errors.managed_entry_duplicate';
        }
    }
    return null;
}

/** Repeatable rows: numbered embed blocks, or role-menu buttons. */
function managedRepeat(spec, existing) {
    const block = spec.layout === 'block';
    const node = element('div', 'json-row-editor');
    let notify = () => {};

    if (!block) {
        const head = element('div', 'menu-row menu-head');
        head.style.setProperty('--row-columns', String(spec.fields.length));
        spec.fields.forEach((field) => head.appendChild(
            element('span', null, tr(field.label))));
        head.appendChild(element('span', 'menu-head-spacer'));
        node.appendChild(head);
    }

    const rows = element('div', block ? 'embed-blocks' : 'menu-rows');
    node.appendChild(rows);

    const renumber = () => {
        [...rows.children].forEach((row, index) => {
            const caption = row.querySelector('.embed-block-index');
            if (caption) {
                caption.textContent = format(spec.caption, {n: index + 1});
            }
        });
    };

    const addRow = (values = {}) => {
        if (rows.children.length >= spec.limit) {
            toast(format('dashboard.managed_row_limit', {limit: spec.limit}), true);
            return;
        }
        const row = element('div', block ? 'embed-block' : 'menu-row');
        const controls = block ? element('div', 'embed-block-fields') : row;
        if (block) {
            const bar = element('div', 'embed-block-bar');
            bar.appendChild(element('span', 'embed-block-index', ''));
            row.appendChild(bar);
        } else {
            row.style.setProperty('--row-columns', String(spec.fields.length));
        }

        spec.fields.forEach((field) => {
            const control = managedFieldControl(field.name, field,
                                                values[field.name], true);
            control.node.dataset.column = field.name;
            if (control.node.classList) {
                control.node.classList.add(
                    field.narrow ? 'row-field' : 'row-field');
                if (field.narrow) control.node.classList.add('narrow');
            }
            if (field.kind === 'text' || field.kind === 'multiline') {
                control.node.placeholder = tr(field.label);
            }
            if (block) {
                controls.appendChild(managedFieldWrapper(
                    {...field, wide: field.kind === 'multiline'}, control.node));
            } else {
                controls.appendChild(control.node);
            }
        });
        if (block) row.appendChild(controls);

        const remove = element('button', 'btn-icon danger', '');
        remove.type = 'button';
        remove.title = tr('dashboard.role_menu_remove');
        remove.setAttribute('aria-label', tr('dashboard.role_menu_remove'));
        remove.appendChild(icon('ic-trash', 'ic ic-sm'));
        remove.addEventListener('click', () => {
            row.remove();
            renumber();
            notify();
        });
        (block ? row.querySelector('.embed-block-bar') : row).appendChild(remove);

        rows.appendChild(row);
        renumber();
    };

    existing.forEach(addRow);
    // A panel with no section cannot be posted and a menu with no button is a
    // message nobody can use, so a new one opens with the row that is the point
    // rather than with an empty editor.
    if (!existing.length) addRow();

    const add = element('button', 'btn btn-outline menu-add', '');
    add.type = 'button';
    add.appendChild(icon('ic-plus', 'ic ic-sm'));
    add.appendChild(document.createTextNode(tr(spec.addLabel)));
    add.addEventListener('click', () => { addRow(); notify(); });
    node.appendChild(add);

    const read = () => [...rows.children].map((row) => {
        const values = {};
        spec.fields.forEach((field) => {
            const target = row.querySelector(`[data-column="${field.name}"]`);
            values[field.name] = (field.kind === 'role')
                ? readManagedChannel(target.closest('.resource-picker') || target)
                : target.value.trim();
        });
        return values;
        // An incomplete row is skipped rather than serialised with a stand-in:
        // the API refuses the whole patch over one bad value.
    }).filter((values) => spec.fields.every(
        (field) => !field.required || values[field.name]));

    return {node, read, onChange: (handler) => { notify = handler; }};
}

async function deleteManaged(page, item, button) {
    const accepted = await confirmAction(format(
        item.posted ? 'dashboard.managed_delete_posted_confirm'
                    : 'dashboard.managed_delete_confirm',
        {name: item.display_name}));
    if (!accepted) return;
    button.disabled = true;
    try {
        const removed = await api(
            `/guilds/${guildId}/managed/${item.kind}/`
            + `${encodeURIComponent(item.menu_key)}`,
            {method: 'DELETE', headers: headers(),
             body: JSON.stringify({revision: item.revision})});
        toast(removed.message);
        await followAction(removed.data?.action_id);
        await loadManaged(page);
    } catch (error) {
        await handleWriteConflict(error, () => loadManaged(page));
        button.disabled = false;
    }
}

/* ------------------------------------------------------------- the preview */

/** Roughly what Discord will show, built from the form's collected values.
 *
 *  From the values rather than from the DOM, so the preview and the thing that
 *  is posted cannot disagree — the argument `collectSettingChanges` makes on the
 *  settings form. What it deliberately does not do is render markdown: Discord's
 *  flavour is large enough that a partial parser shows a *different* wrong
 *  answer than plain text, and a preview that is confidently wrong is worse than
 *  one that is plainly literal. The note under it says so.
 */
function messagePreview(spec, values) {
    const wrapper = element('div');
    wrapper.appendChild(element('h3', 'token-help-title',
                                tr('dashboard.managed_preview_heading')));

    const message = element('div', 'preview-message');
    const guild = selectedGuild();
    const author = element('div', 'preview-author');
    author.appendChild(avatarNode('/brand-avatar.png', 'Bot', 'preview-avatar'));
    author.appendChild(element('span', 'preview-author-name', guild?.name || ''));
    message.appendChild(author);

    const embeds = (spec.kind === 'rules' || spec.kind === 'embed')
        ? (values.embeds || []).map((row) => ({title: row.title, body: row.body}))
        : [{title: values.title, body: values.body}];

    embeds.forEach((embed, index) => {
        const card = element('div', 'preview-embed');
        // A custom property, not a `style=` attribute: CSSOM is what the CSP
        // permits, and the value is derived from a bounded integer rather than
        // from anything an operator typed.
        card.style.setProperty('--preview-accent',
            `#${(values.colour & 0xFFFFFF).toString(16).padStart(6, '0')}`);
        const text = element('div', 'preview-embed-text');
        if (embed.title) {
            text.appendChild(element('div', 'preview-embed-title', embed.title));
        }
        // The one transformation the renderer performs, mirrored here so the
        // preview does not lie about it.
        text.appendChild(element('div', 'preview-embed-body',
                                 String(embed.body || '').replaceAll('\\n', '\n')));
        card.appendChild(text);
        // The thumbnail is first-embed-only, exactly as the renderer does it.
        if (index === 0 && spec.kind === 'rules' && values.thumbnail
                && guild?.icon_url) {
            card.appendChild(avatarNode(guild.icon_url, guild.name, 'preview-thumb'));
        }
        message.appendChild(card);
        if (index === 0 && String(values.image_url || '').startsWith('https://')) {
            const banner = document.createElement('img');
            banner.className = 'preview-banner';
            banner.src = values.image_url;
            banner.alt = '';
            message.appendChild(banner);
        }
    });

    const buttons = previewButtons(spec, values);
    if (buttons.length) {
        const bar = element('div', 'preview-buttons');
        buttons.forEach(({label, style, emoji}) => {
            const chip = element('div', `preview-button ${style}`);
            if (emoji) chip.appendChild(element('span', null, emoji));
            chip.appendChild(element('span', null, label));
            bar.appendChild(chip);
        });
        message.appendChild(bar);
    }
    wrapper.appendChild(message);

    if (spec.kind === 'rules' || spec.kind === 'embed') {
        const total = (values.embeds || []).reduce(
            (sum, row) => sum + row.title.length + row.body.length, 0);
        const readout = element('div', 'total-readout');
        readout.appendChild(element('span', null, tr('dashboard.managed_total_characters')));
        readout.appendChild(element('span',
            `total-value ${total > MESSAGE_TOTAL_LIMIT ? 'invalid' : 'valid'}`,
            `${total} / ${MESSAGE_TOTAL_LIMIT}`));
        wrapper.appendChild(readout);
    }

    wrapper.appendChild(element('p', 'section-hint',
                                tr('dashboard.managed_preview_note')));
    return wrapper;
}

/** The buttons a kind's message carries. Knowledge about `cogs/`, not a form. */
function previewButtons(spec, values) {
    // An embed carries no button at all: it is the message and nothing else.
    if (spec.kind === 'embed') return [];
    if (spec.kind === 'role_menu') {
        return (values.entries || [])
            .filter((entry) => entry.label)
            .map((entry) => ({label: entry.label, style: 'secondary',
                              emoji: entry.emoji}));
    }
    if (spec.kind === 'rules' && !values.accept_button) return [];
    return [{
        label: values.button_label || tr(`dashboard.managed_default_button_${spec.kind}`),
        style: spec.buttonStyle,
        emoji: spec.buttonEmoji,
    }];
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

// Largest first: the loop returns the first unit the gap has filled, and its
// locale family is `dashboard.relative_<unit>` plus `relative_now` below a
// minute. This was referenced by `relativeTime` and declared nowhere, so the
// audit page rendered its subtitle and then threw before a single row —
// `node --check` only parses, and the boot harness stubbed the feed empty, so
// the row loop this sits in had never once run.
const RELATIVE_UNITS = [['day', 86400], ['hour', 3600], ['minute', 60]];

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
