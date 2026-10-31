/* The sidebar's branches: a page with sub-pages under it.
 *
 * Four things, each of which fails silently in a browser:
 * - the active page is never inside a closed branch;
 * - a click on the expander is remembered, and only that;
 * - a sub-page hides with its parent, as part of the one hide decision;
 * - an expander with nothing visible under it is hidden too.
 *
 * Argument 1 is the repository root.
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const repo = process.argv[2];
const html = fs.readFileSync(path.join(repo, 'dashboard/index.html'), 'utf8');
const script = fs.readFileSync(path.join(repo, 'dashboard/script.js'), 'utf8');
const locale = JSON.parse(fs.readFileSync(path.join(repo, 'locales/en.json'), 'utf8'));

// Delivered through the real routes: `featureState`, `registry` and
// `activePage` are `let`s in the script's own scope and cannot be reached from
// here, so a change goes through `/features` and a reload, as it does live.
const FEATURES = {
    onboarding: {enabled: true, dependencies: [], locale_key: 'f.onboarding'},
    economy: {enabled: true, dependencies: [], locale_key: 'f.economy'},
    // A `child_pages` parent: its children get generated sub-pages.
    member_announcements: {enabled: true, dependencies: [], locale_key: 'f.announce',
                           child_pages: true},
    announce_leave: {enabled: true, dependencies: ['member_announcements'],
                     locale_key: 'f.leave', parent: 'member_announcements'},
    announce_join: {enabled: true, dependencies: ['member_announcements'],
                    locale_key: 'f.join', parent: 'member_announcements'},
};
const definition = (key, category, owner, order = 1) => ({
    key, category, page: category, owner_feature: owner, value_type: 'integer',
    locale_key: `x.${key}`, apply_behavior: 'live', default: 0, order,
});
const REGISTRY = {
    member_role: definition('member_role', 'onboarding', 'onboarding'),
    work_payout_min: definition('work_payout_min', 'work', 'economy'),
    // Leave sorts before join in the payload but after it in `order`: the
    // sidebar must follow `order`, as Flask sorts the payload's keys.
    leave_channel: definition('leave_channel', 'member_announcements', 'announce_leave', 3),
    join_channel: definition('join_channel', 'member_announcements', 'announce_join', 2),
};

const dom = new JSDOM(html, {url: 'https://d.test/', runScripts: 'outside-only'});
const { window } = dom;
window.potatoLanguage = {current: () => 'en', set: () => {}};
const ok = (body) => ({ok: true, status: 200, text: async () => JSON.stringify(body)});
window.fetch = async (url) => {
    const u = String(url);
    if (u.includes('/locale')) {
        return ok({language: 'en', available: ['en'], data: {dashboard: locale.dashboard}});
    }
    if (u.includes('/auth/status')) {
        return ok({logged_in: true, user: {id: '42', username: 't', avatar: null},
                   csrf_token: 'c', is_host: true, idle_timeout_seconds: 600,
                   version: '0.0.0-test', asset_version: 'test',
                   guilds: [{id: '1', name: 'Test Guild'}]});
    }
    if (u.includes('/features')) return ok({status: 'success', data: FEATURES});
    if (u.includes('/settings/registry')) {
        return ok({status: 'success', data: REGISTRY, feature_group_order: []});
    }
    if (u.includes('/discord-resources')) {
        if (resourcesFail) {
            return {ok: false, status: 500,
                    text: async () => JSON.stringify({status: 'error', message: 'boom'})};
        }
        return ok({status: 'success', data: {channels: [], roles: []}});
    }
    return ok({status: 'success', data: []});
};

/* Set by the check that the Discord lists failing does not cost the page its
 * feature state, which is how the local stand-ins' drift hid every sub-page. */
let resourcesFail = false;

const settle = (ms = 60) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
    window.eval(script);
    const event = window.document.createEvent('Event');
    event.initEvent('DOMContentLoaded', true, true);
    window.document.dispatchEvent(event);
    await settle(500);

    const doc = window.document;
    const branch = doc.querySelector('.nav-branch[data-branch="onboarding"]');
    const expander = branch.querySelector('.nav-expand');
    const parent = branch.querySelector('.nav-branch-row > .nav-item');
    const children = [...branch.querySelectorAll('.nav-children > .nav-item')];
    const stored = () => JSON.parse(window.localStorage.getItem('potato.nav.open') || '[]');

    assert.ok(children.length >= 2, 'the premise: onboarding has sub-pages');
    assert.ok(!branch.classList.contains('open'), 'a branch starts closed');
    assert.strictEqual(expander.getAttribute('aria-expanded'), 'false');
    assert.ok(!parent.classList.contains('hidden'), 'the parent is reachable');
    assert.strictEqual(expander.querySelector('.nav-expand-count').textContent,
        String(children.length), 'the expander says how many sub-pages it holds');
    console.log('ok    a branch starts closed, visible, and counts its sub-pages');

    await window.eval('showPage("entry-gate")');
    await settle();
    assert.ok(branch.classList.contains('open'),
        'the active page must not sit inside a closed branch');
    assert.strictEqual(expander.getAttribute('aria-expanded'), 'true');
    assert.deepStrictEqual(stored(), [], 'opening for navigation is not a preference');
    console.log('ok    showing a sub-page opens its branch, unremembered');

    expander.click();
    assert.ok(!branch.classList.contains('open'));
    expander.click();
    assert.ok(branch.classList.contains('open'));
    assert.deepStrictEqual(stored(), ['onboarding']);
    console.log('ok    the expander toggles and is remembered');

    FEATURES.onboarding.enabled = false;
    await window.eval('loadGuild()');
    await settle();
    assert.ok(parent.classList.contains('hidden'), 'the switched-off parent hides');
    children.forEach((item) => assert.ok(item.classList.contains('hidden'),
        `${item.dataset.page} must hide with its parent`));
    assert.ok(expander.classList.contains('hidden'), 'an expander over nothing hides');
    assert.strictEqual(doc.querySelector('.nav-item.active')?.dataset.page, 'overview',
        'a sub-page whose parent is gone falls back past it');
    console.log('ok    a sub-page hides with its parent, and so does the expander');

    FEATURES.onboarding.enabled = true;
    await window.eval('loadGuild()');
    await settle();
    assert.ok(!parent.classList.contains('hidden'));
    children.forEach((item) => assert.ok(!item.classList.contains('hidden')));
    assert.ok(!expander.classList.contains('hidden'));
    console.log('ok    switching it back on restores the branch');


    // Visiting a parent page opens its branch, so its sub-pages are seen.
    const patchBranch = doc.querySelector('.nav-branch[data-branch="member_announcements"]');
    window.eval(`setBranchOpen(document.querySelector('.nav-branch[data-branch="member_announcements"]'), false)`);
    await window.eval('showPage("member_announcements")');
    await settle();
    assert.ok(patchBranch.classList.contains('open'), 'a parent page opens its branch');
    console.log('ok    visiting a parent page opens its branch');

    // Generated sub-pages: a `child_pages` parent's children that own settings.
    const generated = () => [...doc.querySelectorAll(
        '.nav-branch[data-branch="member_announcements"] .nav-children > .nav-item')];
    const pages = () => generated().map((item) => item.dataset.page);
    const active = () => doc.querySelector('.nav-item.active')?.dataset.page;
    assert.deepStrictEqual(pages(), ['member_announcements:announce_join', 'member_announcements:announce_leave'],
        'sub-pages follow the registry order');
    await window.eval('loadGuild()');
    await settle();
    assert.strictEqual(generated().length, 2, 'a reload rebuilds rather than appends');
    console.log('ok    generated sub-pages are listed in order, once');

    await window.eval('showPage("member_announcements:announce_leave")');
    await settle();
    assert.strictEqual(active(), 'member_announcements:announce_leave');
    assert.deepStrictEqual([...doc.querySelectorAll('#settings-grid [data-setting]')]
        .map((node) => node.dataset.setting), ['leave_channel'],
        'a sub-page shows only the settings its child owns');
    await window.eval('showPage("member_announcements")');
    await settle();
    assert.strictEqual(doc.querySelectorAll('#settings-grid [data-setting]').length, 0,
        'the parent page must not repeat what its sub-pages show');
    console.log('ok    a sub-page and its parent split the settings between them');

    await window.eval('showPage("member_announcements:announce_leave")');
    await settle();
    FEATURES.announce_leave.enabled = false;
    await window.eval('loadGuild()');
    await settle();
    const leave = generated().find((item) => item.dataset.page === 'member_announcements:announce_leave');
    assert.ok(leave.classList.contains('hidden'), 'a switched-off child hides its sub-page');
    assert.strictEqual(active(), 'member_announcements', 'and the page falls back to its parent');
    console.log('ok    a switched-off child falls back to its parent page');

    FEATURES.announce_leave.enabled = true;
    FEATURES.member_announcements.enabled = false;
    await window.eval('loadGuild()');
    await settle();
    generated().forEach((item) => assert.ok(item.classList.contains('hidden'),
        `${item.dataset.page} must hide with its parent, though its own switch is on`));
    assert.strictEqual(patchBranch.querySelector('.nav-expand-count').textContent, '0');
    console.log('ok    a switched-off parent hides children that are still on');

    FEATURES.member_announcements.enabled = true;
    resourcesFail = true;
    await window.eval('loadGuild()');
    await settle();
    resourcesFail = false;
    assert.strictEqual(generated().filter((item) => !item.classList.contains('hidden')).length, 2,
        'the Discord lists failing must not cost the page its feature state');
    assert.ok(doc.querySelector('.toast.error'), 'and the failure says so');
    console.log('ok    a failed resource list keeps features, and says so');

    process.exit(0);
})().catch((error) => {
    console.log(`FAIL  ${error.stack || error}`);
    process.exit(1);
});
