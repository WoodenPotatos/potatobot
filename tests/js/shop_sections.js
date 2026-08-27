/* The item page groups by section, and says how much room each has left.
 *
 * The shop menu is one Discord select and a select holds 25 options, so a flat
 * menu made the whole shop 25 items — 17 built in, leaving an operator eight,
 * and falling by one every time an item shipped. Sections make the ceiling 25
 * each, and the interface has to be able to *say* so: a rule the API enforces
 * that the form cannot express turns into an unexplained rejection.
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

const SECTIONS = [
    {id: 'perks', label: 'Perks', limit: 25, builtin: 1, custom: 1, used: 2, remaining: 23},
    // Full, so the pill and the disabled option both have something to render.
    {id: 'casino', label: 'Casino', limit: 25, builtin: 6, custom: 19, used: 25, remaining: 0},
    // Empty, which is a legal destination and must still be offered.
    {id: 'heist', label: 'Heist', limit: 25, builtin: 0, custom: 0, used: 0, remaining: 25},
];
const ITEMS = [
    {item_key: 'premium', source: 'builtin', name: 'Premium', description: 'd',
     effect: 'role', value: 'premium_role', price: 300000, in_shop: true,
     in_gacha: false, enabled: true, editable: false,
     price_setting: 'shop_price_premium', category: 'perks', hidden: false},
    {item_key: 'rent_sound', source: 'builtin', name: 'Sound rental',
     description: 'd', effect: 'rent_ticket', value: null, price: 314000,
     in_shop: true, in_gacha: false, enabled: true, editable: false,
     price_setting: 'shop_price_rent_sound', category: 'casino', hidden: true},
    {item_key: 'vip', source: 'custom', name: 'VIP', description: 'd',
     effect: 'coin_bundle', value: null, price: 500, in_shop: true,
     in_gacha: false, enabled: true, editable: true, price_setting: null,
     revision: 2, config: {amount: 10, repeatable: false},
     category: 'perks', category_stored: null, hidden: false},
];

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
        return ok({status: 'success', data: {
            user: {id: '42', username: 't', avatar: null}, is_host: true,
            idle_timeout_seconds: 600,
            guilds: [{guild_id: '1', name: 'G', icon: null}]}});
    }
    if (u.includes('/settings/registry')) {
        return ok({status: 'success', data: {settings: {}, features: [], groups: []}});
    }
    if (u.includes('/discord-resources')) {
        return ok({status: 'success', data: {channels: [], roles: []}});
    }
    if (u.includes('/items')) {
        return ok({status: 'success', data: ITEMS, categories: SECTIONS,
                   custom_count: 1});
    }
    return ok({status: 'success', data: []});
};

(async () => {
    window.eval(script);
    const event = window.document.createEvent('Event');
    event.initEvent('DOMContentLoaded', true, true);
    window.document.dispatchEvent(event);
    await new Promise((r) => setTimeout(r, 500));
    await window.eval("showPage('shop-builder')");
    await new Promise((r) => setTimeout(r, 200));

    const heads = [...window.document.querySelectorAll('#shop-items .section-head')];
    assert.deepStrictEqual(heads.map((node) => node.querySelector('h3').textContent),
        SECTIONS.map((section) => section.label),
        'the sections are not rendered in the order the endpoint reported');

    // An empty section still renders — it is where a new item can go.
    const rowsFor = (label) => {
        const head = heads.find((node) => node.querySelector('h3').textContent === label);
        return head.nextElementSibling.querySelectorAll('tbody tr');
    };
    assert.strictEqual(rowsFor('Perks').length, 2);
    assert.strictEqual(rowsFor('Heist').length, 1, 'an empty section must show its empty row');

    // Each header carries the room left, and a full one is marked.
    const pillText = (label) => heads
        .find((n) => n.querySelector('h3').textContent === label)
        .querySelector('.pill');
    assert.strictEqual(pillText('Casino').textContent, '25/25');
    assert.ok(pillText('Casino').className.includes('off'),
        'a full section must be marked, not merely counted');
    assert.ok(!pillText('Heist').className.includes('off'));

    // A hidden built-in is still listed, or nobody could un-hide it, and it
    // offers Show rather than Hide.
    const casinoRows = [...rowsFor('Casino')];
    const hiddenRow = casinoRows.find((row) => row.textContent.includes('rent_sound'));
    assert.ok(hiddenRow, 'a hidden built-in must still be listed');
    assert.ok(hiddenRow.className.includes('reward-disabled'));
    assert.ok([...hiddenRow.querySelectorAll('button')]
        .some((button) => button.textContent === 'Show'),
        'a hidden item must offer Show');
    const shownRow = [...rowsFor('Perks')].find(
        (row) => row.textContent.includes('premium'));
    assert.ok([...shownRow.querySelectorAll('button')]
        .some((button) => button.textContent === 'Hide'));

    // The editor's section picker: blank reads as null, a full section cannot
    // be chosen, and an empty one can.
    await window.eval('renderItemEditor(null)');
    await new Promise((r) => setTimeout(r, 40));
    const picker = [...window.document.querySelectorAll('#shop-item-editor select')]
        .find((node) => [...node.options].some((option) => option.value === 'heist'));
    assert.ok(picker, 'the creator has no section picker');
    assert.strictEqual(picker.value, '', 'the default must be "follow the kind"');
    const option = (id) => [...picker.options].find((node) => node.value === id);
    assert.ok(option('casino').disabled, 'a full section must not be choosable');
    assert.ok(!option('heist').disabled);
    assert.ok(option('casino').textContent.includes('25/25'),
        'a full section must say why it cannot be picked');

    console.log('ok');
})();
