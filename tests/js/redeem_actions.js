/* The Redeems page gained two delete-style actions: cancelling an open
 * fulfillment request, and revoking an active entitlement. Both were missing
 * entirely before -- an operator could list and complete, but never remove,
 * which is why a glitched one-year grant had no way off the page short of
 * waiting out its own expiry.
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

const REQUEST = {
    request_id: 7, voucher_id: 'v7', user_id: '1420070400000000001',
    asset_type: 'sticker', status: 'open', created_at: '2026-01-01T00:00:00+00:00',
};
const ENTITLEMENT = {
    entitlement_id: 3, user_id: '1420070400000000002', kind: 'sticker',
    reward_key: null, source_type: 'gacha', discord_item_id: '555',
    granted_by: null, expires_at: '2027-01-01T00:00:00+00:00',
    remaining_seconds: 31536000, missing_on_discord: true,
};

const dom = new JSDOM(html, {url: 'https://d.test/', runScripts: 'outside-only'});
const { window } = dom;
window.potatoLanguage = {current: () => 'en', set: () => {}};
const ok = (body) => ({ok: true, status: 200, text: async () => JSON.stringify(body)});

const deletes = [];
window.fetch = async (url, init) => {
    const u = String(url);
    const method = (init && init.method) || 'GET';
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
    if (method === 'DELETE' && u.includes('/fulfillment/')) {
        deletes.push({url: u, method});
        return ok({status: 'success', message: 'Cancelled.', data: {cancelled: true}});
    }
    if (method === 'DELETE' && u.includes('/entitlements/')) {
        deletes.push({url: u, method});
        return ok({status: 'success', message: 'Revoked.', data: {revoked: true}});
    }
    if (u.includes('/fulfillment')) {
        return ok({status: 'success', data: [REQUEST]});
    }
    if (u.includes('/entitlements')) {
        return ok({status: 'success', data: [ENTITLEMENT]});
    }
    return ok({status: 'success', data: []});
};

(async () => {
    window.eval(script);
    const event = window.document.createEvent('Event');
    event.initEvent('DOMContentLoaded', true, true);
    window.document.dispatchEvent(event);
    await new Promise((r) => setTimeout(r, 500));
    await window.eval("showPage('redeems')");
    await new Promise((r) => setTimeout(r, 200));

    const fulfillmentRows = [...window.document.querySelectorAll('#fulfillment-list tbody tr')];
    assert.strictEqual(fulfillmentRows.length, 1, 'the open request must render one row');
    const cancelButton = fulfillmentRows[0].querySelector('button.btn-icon.danger');
    assert.ok(cancelButton, 'the fulfillment row has no cancel button');

    const entitlementRows = [...window.document.querySelectorAll('#active-entitlements tbody tr')];
    assert.strictEqual(entitlementRows.length, 1, 'the active entitlement must render one row');
    const revokeButton = entitlementRows[0].querySelector('button.btn-icon.danger');
    assert.ok(revokeButton, 'the entitlement row has no revoke button');

    // Set by entitlement_cleanup's daily verification pass, not a live
    // per-row Discord call -- see cogs/gacha.py's check_asset_presence.
    const missingPill = entitlementRows[0].querySelector('.pill.pending');
    assert.ok(missingPill, 'a missing_on_discord entitlement must show the badge');
    assert.strictEqual(missingPill.textContent, 'Missing on Discord');

    const confirmAndClick = async (button) => {
        button.click();
        await new Promise((r) => setTimeout(r, 20));
        const accept = [...window.document.querySelectorAll('#modal-root .btn-primary')]
            .find((b) => b.textContent === 'Yes, continue');
        assert.ok(accept, 'the confirmation dialog did not open');
        accept.click();
        await new Promise((r) => setTimeout(r, 40));
    };

    await confirmAndClick(cancelButton);
    assert.strictEqual(deletes.length, 1, 'cancelling must send exactly one DELETE');
    assert.ok(deletes[0].url.includes('/guilds/1/fulfillment/7'),
        `expected the fulfillment DELETE to name request 7, got ${deletes[0].url}`);

    await confirmAndClick(revokeButton);
    assert.strictEqual(deletes.length, 2, 'revoking must send exactly one more DELETE');
    assert.ok(deletes[1].url.includes('/guilds/1/entitlements/3'),
        `expected the entitlement DELETE to name entitlement 3, got ${deletes[1].url}`);

    console.log('ok');
    process.exit(0);
})();
