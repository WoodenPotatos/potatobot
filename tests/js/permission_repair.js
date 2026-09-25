/* Only a `channel_missing_permission` finding may offer a Repair button,
 * and clicking it must queue exactly the channel/subject the finding named.
 *
 * `channel_member_missing_permission` names someone *else's* grant, and
 * granting that back could override an intentional restriction rather than
 * fix a bug -- so it must never grow this button, even though both finding
 * codes share one row renderer.
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

const REPAIRABLE = {
    code: 'channel_missing_permission', severity: 'blocking',
    subject: 'bot_log_channel', feature: 'general', identifier: 'bot-log',
    permissions: ['send_messages'], channel_id: '1420070400000000010',
};
const MEMBER_FACING = {
    code: 'channel_member_missing_permission', severity: 'degraded',
    subject: 'economy_channels', feature: 'economy', identifier: 'casino',
    permissions: ['use_application_commands'], channel_id: '1420070400000000011',
};

const dom = new JSDOM(html, {url: 'https://d.test/', runScripts: 'outside-only'});
const { window } = dom;
window.potatoLanguage = {current: () => 'en', set: () => {}};
const ok = (body) => ({ok: true, status: 200, text: async () => JSON.stringify(body)});

let repairCalls = [];
window.fetch = async (url, init) => {
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
    if (u.includes('/permissions/repair')) {
        repairCalls.push(JSON.parse(init.body));
        return ok({status: 'success', message: 'Repair queued.', data: {action_id: 1}});
    }
    if (u.includes('/permissions')) {
        return ok({status: 'success', data: {
            findings: [REPAIRABLE, MEMBER_FACING], features: [],
            blocking_count: 1, degraded_count: 1, administrator: false}});
    }
    return ok({status: 'success', data: []});
};

(async () => {
    window.eval(script);
    const event = window.document.createEvent('Event');
    event.initEvent('DOMContentLoaded', true, true);
    window.document.dispatchEvent(event);
    await new Promise((r) => setTimeout(r, 500));
    await window.eval("showPage('permissions')");
    await new Promise((r) => setTimeout(r, 200));

    const rows = [...window.document.querySelectorAll('#permission-findings tbody tr')];
    assert.strictEqual(rows.length, 2, 'both findings must render as rows');

    const buttonsFor = (identifier) => {
        const row = rows.find((node) => node.textContent.includes(identifier));
        assert.ok(row, `no row for ${identifier}`);
        return [...row.querySelectorAll('button')]
            .filter((button) => button.textContent === 'Repair');
    };
    assert.strictEqual(buttonsFor('bot-log').length, 1,
        'channel_missing_permission must offer Repair');
    assert.strictEqual(buttonsFor('casino').length, 0,
        'channel_member_missing_permission must never offer Repair');

    buttonsFor('bot-log')[0].click();
    await new Promise((r) => setTimeout(r, 20));
    const accept = [...window.document.querySelectorAll('#modal-root .btn-primary')]
        .find((button) => button.textContent === 'Yes, continue');
    assert.ok(accept, 'the confirmation dialog did not open');
    accept.click();
    await new Promise((r) => setTimeout(r, 40));

    assert.strictEqual(repairCalls.length, 1, 'the repair request was not sent');
    assert.deepStrictEqual(repairCalls[0], {
        channel_id: REPAIRABLE.channel_id, subject: REPAIRABLE.subject, confirm: true,
    });

    console.log('ok');
    process.exit(0);
})();
