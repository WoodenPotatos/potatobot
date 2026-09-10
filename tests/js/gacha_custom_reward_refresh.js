/* A custom item created on the item page must be offerable on the gacha page.
 *
 * `customRewards` is the pool the reward picker adds to the built-in catalog,
 * and it was loaded by `loadGuild()` alone — which runs at boot and on a guild
 * switch. So creating an item and walking to the Gacha page left the picker
 * holding the pool from before the item existed.
 *
 * It showed for a voucher and not for a vault, which is why it read as "vouchers
 * are broken": **no built-in item has a `voucher` gacha kind**, so that list is
 * the custom pool and nothing else, while the vault kinds still offer the three
 * shipped vaults and merely lacked the new one.
 *
 * Driven through `loadShopItems()`, because that is what every item mutation
 * ends with — save, delete and the enable toggle all converge there, so the
 * refresh belongs at that seam rather than in four handlers that can drift.
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

// A voucher and a vault, so the test can tell "the pool is stale" from "vouchers
// are special": only the voucher list is empty without the refresh.
const VOUCHER = {key: 'premium_7d', kind: 'voucher', amount: 7,
                 name: '7 day Premium', sold_in_shop: false};
const VAULT = {key: 'vault_3m', kind: 'vault', amount: 3000000,
               name: '3M vault', sold_in_shop: false};

// The state the operator's creation changes. Starts as it was before they
// created anything.
let itemsExist = false;

const CATALOG = [
    {key: 'small_vault', gacha_kind: 'vault', effect: 'vault', value: 25000},
    {key: 'loaded_die', gacha_kind: 'item', effect: 'inventory', value: null},
    // No built-in carries a `voucher` gacha kind, and that is the fact this
    // whole defect hid behind. Asserted below rather than trusted.
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
        return ok({logged_in: true,
                   user: {id: '42', username: 'tester', avatar: null},
                   csrf_token: 'csrf-token', is_host: true,
                   idle_timeout_seconds: 600, version: '0.0.0-test',
                   asset_version: 'test-token',
                   guilds: [{id: '1', name: 'Test Guild'}]});
    }
    if (u.includes('/settings/registry')) {
        return ok({status: 'success', data: {settings: {}, features: [], groups: []}});
    }
    if (u.includes('/item-catalog')) {
        return ok({status: 'success', data: CATALOG});
    }
    if (u.includes('/discord-resources')) {
        return ok({status: 'success', data: {channels: [], roles: []}});
    }
    if (u.includes('/gacha')) {
        return ok({status: 'success', data: {
            banners: [{banner_key: 'standard', display_name: 'standard',
                       enabled: true, is_default: true, revision: 3,
                       config: {cost: 5000, duplicate_percent: 10,
                                featured_split: 50, hard_pity: 100,
                                four_star_guarantee_interval: 10,
                                soft_pity_multiplier: 3, soft_pity_start: 75,
                                tiers: {3: 97800, 4: 1600, 5: 600},
                                rewards: {3: [{key: 'coins_250', kind: 'coins',
                                               amount: 250, weight: 1,
                                               enabled: true, featured: false}],
                                          4: [], 5: []}},
                       missing_shipped_rewards: []}],
            shipped_rewards: {3: [], 4: [], 5: []},
            custom_rewards: itemsExist ? [VOUCHER, VAULT] : [],
        }});
    }
    if (u.includes('/items')) {
        return ok({status: 'success', data: [], categories: [], custom_count: 0});
    }
    return ok({status: 'success', data: []});
};

/** The key options the picker offers for one reward kind. */
async function optionsFor(kind) {
    await window.eval("showPage('gacha')");
    await new Promise((r) => setTimeout(r, 120));
    const add = [...window.document.querySelectorAll('#gacha-form button')]
        .find((button) => button.textContent === locale.dashboard.gacha_add_reward);
    assert.ok(add, 'the tier heading has no add-reward button');
    add.click();
    await new Promise((r) => setTimeout(r, 60));
    const row = window.document.querySelector('tr.reward-new');
    assert.ok(row, 'no new reward row was added');
    const kindSelect = row.querySelector('select[data-field="kind"]');
    kindSelect.value = kind;
    kindSelect.dispatchEvent(new window.Event('change'));
    await new Promise((r) => setTimeout(r, 60));
    const keySelect = row.querySelector('select[data-field="key"]');
    return [...keySelect.options].map((option) => option.value);
}

(async () => {
    assert.ok(!CATALOG.some((item) => item.gacha_kind === 'voucher'),
        'the premise is wrong: a built-in would mask an empty custom pool');

    window.eval(script);
    const event = window.document.createEvent('Event');
    event.initEvent('DOMContentLoaded', true, true);
    window.document.dispatchEvent(event);
    await new Promise((r) => setTimeout(r, 500));

    // Before: nothing of the guild's own exists, so the voucher list is empty.
    // That state is correct and is what the free-text fallback is for.
    assert.deepStrictEqual(await optionsFor('voucher'), []);

    // The operator creates the item. Every item mutation ends in
    // `loadShopItems()`, so that is the whole of what happens next.
    itemsExist = true;
    await window.eval('loadShopItems()');
    await new Promise((r) => setTimeout(r, 120));

    assert.deepStrictEqual(await optionsFor('voucher'), ['premium_7d'],
        'a voucher item created on the item page is not offered to a banner');
    // And the vault list gains it beside the built-ins, which is why only the
    // voucher looked broken.
    assert.deepStrictEqual(await optionsFor('vault'), ['small_vault', 'vault_3m']);

    console.log('ok');
    process.exit(0);
})();
