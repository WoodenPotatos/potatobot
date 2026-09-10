/* Filling one reward tier from the shipped table.
 *
 * A new banner starts with one placeholder per curated tier because a tier
 * cannot be empty, and tier 3 can never carry a featured reward — so an event
 * banner's 3-star tier is pure filler that had to be typed in seven rows at a
 * time. The Import button is the tier-scoped half of "Add missing rewards", and
 * it shares one implementation with it: what counts as missing is computed once,
 * by the server, and pruned by whichever path appended it.
 *
 * Pruning is the part worth testing. A stale `missing_rewards` entry lets the
 * page action append a key the tier already has, and `_validated_gacha_config`
 * refuses a tier listing one reward twice — two rows for one reward double its
 * odds and make the displayed chance a lie.
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

const SCALARS = {cost: 5000, duplicate_percent: 10, featured_split: 50,
                 four_star_guarantee_interval: 10, hard_pity: 100,
                 soft_pity_multiplier: 3, soft_pity_start: 75};
const TIERS = {3: 97800, 4: 1600, 5: 600};
const row = (key, kind, amount, weight) =>
    ({key, kind, amount, weight, enabled: true, featured: false});

/* The shipped 3-star tier as it stands: seven items, no coins. Kept as data here
 * rather than read from database.py, because this harness is the *browser* half
 * — the Python side asserts the pool itself. */
const SHIPPED_THREE = [
    row('loaded_die', 'item', 1, 400), row('lockpick', 'item', 1, 100),
    row('lucky_charm', 'item', 1, 100), row('stacked_deck', 'item', 1, 100),
    row('marked_card', 'item', 1, 100), row('metal_detector', 'item', 1, 100),
    row('parachute', 'item', 1, 100),
];

/* An event banner shaped like the live one: a stored table with a placeholder
 * 3-star tier, so every shipped 3-star is missing from it. */
const EVENT = {
    banner_key: 'summer_end_2026_1', display_name: 'Event', enabled: false,
    is_default: false, revision: 2,
    config: {...SCALARS, tiers: TIERS, rewards: {
        3: [row('coins_250', 'coins', 250, 1)],
        4: [row('coins_250', 'coins', 250, 1)],
        5: [row('vault_3m', 'vault', 3000000, 1)],
    }},
    /* What the server computed, under the name it actually sends. `missing_rewards`
     * on the wire, `missing_shipped_rewards` in `database.py` — a stub answering
     * in the wrong shape tests the client's absent-value path and reports it as
     * success, which is how three harnesses came to boot the login screen.
     * Verified against a real payload rather than read off the function name.
     * Tiers 4 and 5 are curated, so nothing shipped is claimed missing there. */
    missing_rewards: {3: SHIPPED_THREE},
};

const dom = new JSDOM(html, {url: 'https://d.test/', runScripts: 'outside-only'});
const { window } = dom;
window.potatoLanguage = {current: () => 'en', set: () => {}};
window.confirm = () => true;
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
        return ok({status: 'success', data: []});
    }
    if (u.includes('/discord-resources')) {
        return ok({status: 'success', data: {channels: [], roles: []}});
    }
    if (u.includes('/gacha')) {
        return ok({status: 'success', data: {
            banners: [structuredClone(EVENT)],
            shipped_rewards: {3: SHIPPED_THREE, 4: [], 5: []},
            custom_rewards: [],
        }});
    }
    if (u.includes('/items')) {
        return ok({status: 'success', data: [], categories: [], custom_count: 0});
    }
    return ok({status: 'success', data: []});
};

/** The tier heading row for one tier, and the buttons in it. */
function heading(tier) {
    const node = window.document.querySelector(`tr[data-tier-heading="${tier}"]`);
    assert.ok(node, `no heading row for tier ${tier}`);
    return [...node.querySelectorAll('button')];
}
const importButton = (tier) => heading(tier).find(
    (button) => button.textContent.startsWith('Import'));
const keysIn = (tier) => [...window.document.querySelectorAll(`tr[data-tier="${tier}"]`)]
    .map((node) => node.dataset.key);

(async () => {
    window.eval(script);
    const event = window.document.createEvent('Event');
    event.initEvent('DOMContentLoaded', true, true);
    window.document.dispatchEvent(event);
    await new Promise((r) => setTimeout(r, 500));
    await window.eval("showPage('gacha')");
    await new Promise((r) => setTimeout(r, 150));

    // The tier that is missing shipped rewards offers the import, named with the
    // count so the operator knows what the click will do.
    const bring = importButton(3);
    assert.ok(bring, 'tier 3 is missing seven shipped rewards and offers no import');
    assert.strictEqual(bring.textContent, 'Import 7 shipped');

    // A curated tier is not missing anything shipped, so it offers nothing —
    // the button is not a permanent fixture that sometimes does nothing.
    assert.ok(!importButton(4), 'tier 4 has nothing missing and must offer no import');
    assert.ok(!importButton(5), 'tier 5 has nothing missing and must offer no import');
    assert.ok(heading(3).some((button) => button.textContent === 'Add a reward'),
        'the import must sit beside Add a reward, not replace it');

    assert.deepStrictEqual(keysIn(3), ['coins_250'],
        'the premise is wrong: tier 3 should start as one placeholder');

    bring.click();
    await new Promise((r) => setTimeout(r, 120));

    // Appended, in order, leaving the placeholder for the operator to remove.
    assert.deepStrictEqual(
        keysIn(3),
        ['coins_250', ...SHIPPED_THREE.map((entry) => entry.key)],
        'the shipped 3-star rewards were not appended');
    // And gone once the tier is complete.
    assert.ok(!importButton(3), 'the import must leave once the tier is complete');

    /* The page action shares `importTier`, so the tier it already imported must
     * be pruned: appending those keys a second time is a duplicate, which the
     * server refuses for the whole banner. */
    await window.eval('addMissingRewards()');
    await new Promise((r) => setTimeout(r, 120));
    const keys = keysIn(3);
    assert.strictEqual(new Set(keys).size, keys.length,
        `"Add missing rewards" re-appended an imported reward: ${keys}`);

    console.log('ok');
    // Explicit, because an authenticated boot starts the session countdown and
    // the feature poller, whose pending timers keep Node alive indefinitely.
    process.exit(0);
})();
