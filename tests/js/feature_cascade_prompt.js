/* The cascade prompt must say which feature pulls each one.
 *
 * It listed bare names, so disabling `economy` read "Shop, Rentals, Potato
 * Gacha" with no hint that the last two come *through* the shop. An operator
 * asking whether a dependency is real could not tell from the prompt which
 * declaration to look at — which is exactly the question that turned up
 * `shop_gacha -> shop`, a dependency nothing enforced.
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

/* A transitive chain, the case bare names lost: rentals hangs off the shop,
 * which hangs off economy, while the gacha now hangs off economy directly.
 * Delivered through the real `/features` route, because `featureState` is a
 * `let` in the script's own scope — assigning it from outside creates a second
 * binding the client never reads. */
const FEATURES = {
    economy:    {enabled: true, dependencies: [],                            locale_key: 'f.economy'},
    shop:       {enabled: true, dependencies: ['economy'],                   locale_key: 'f.shop'},
    rentals:    {enabled: true, dependencies: ['economy', 'shop', 'tickets'], locale_key: 'f.rentals'},
    shop_gacha: {enabled: true, dependencies: ['economy'],                   locale_key: 'f.gacha'},
    tickets:    {enabled: true, dependencies: [],                            locale_key: 'f.tickets'},
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
    if (u.includes('/features')) {
        return ok({status: 'success', data: FEATURES});
    }
    if (u.includes('/settings/registry')) {
        return ok({status: 'success', data: {settings: {}, features: [], groups: []}});
    }
    if (u.includes('/discord-resources')) {
        return ok({status: 'success', data: {channels: [], roles: []}});
    }
    return ok({status: 'success', data: []});
};

(async () => {
    window.eval(script);
    const event = window.document.createEvent('Event');
    event.initEvent('DOMContentLoaded', true, true);
    window.document.dispatchEvent(event);
    await new Promise((r) => setTimeout(r, 500));

    const lines = window.eval("dependentFeatures('economy')");
    assert.ok(lines.length >= 3, `expected a transitive cascade, got ${JSON.stringify(lines)}`);

    // Every line names a parent, not just a feature.
    lines.forEach((line) => {
        assert.ok(/\(/.test(line),
            `"${line}" names no parent; a bare list is what this replaced`);
    });

    /* The parent named must be one this feature actually declares — naming a
     * plausible-looking ancestor that is not a dependency would be worse than
     * the bare list, because it would send the reader to the wrong line. */
    const byLocale = Object.fromEntries(
        Object.entries(FEATURES).map(([key, state]) => [`[${state.locale_key}]`, key]));
    lines.forEach((line) => {
        const [, shown, parent] = line.match(/^(\[[^\]]+\]) \(needs (\[[^\]]+\])\)$/) || [];
        assert.ok(shown && parent, `unparsable line: ${line}`);
        const feature = byLocale[shown];
        const named = byLocale[parent];
        assert.ok(feature && named, `unknown feature in: ${line}`);
        assert.ok(FEATURES[feature].dependencies.includes(named),
            `${line} names a parent ${feature} does not declare`);
    });

    // The gacha no longer depends on the shop, so disabling the shop alone
    // must not mention it at all.
    const fromShop = window.eval("dependentFeatures('shop')");
    assert.ok(!fromShop.some((l) => l.includes('f.gacha')),
        `the gacha must survive the shop being off: ${JSON.stringify(fromShop)}`);
    assert.ok(fromShop.some((l) => l.includes('f.rentals')),
        'rentals genuinely needs the shop and must still be listed');

    console.log('ok');
    process.exit(0);
})();
