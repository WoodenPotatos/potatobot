/* A 401 is an ended session; a 403 is a refusal. They are not the same thing.
 *
 * `handleApiError` took both to the login card and announced both as "your
 * session expired". So a CSRF mismatch, a guild you may not touch, and a
 * permission refresh Discord refused all told the operator to log in again —
 * advice that could not help, and which threw away the page they were on. It is
 * what sent a real lockout the wrong way for two days: the cause was a 429 being
 * mistaken for a revoked grant, and the interface reported it as an expiry.
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

const dom = new JSDOM(html, {url: 'https://d.test/', runScripts: 'outside-only'});
const { window } = dom;
window.potatoLanguage = {current: () => 'en', set: () => {}};
const ok = (body) => ({ok: true, status: 200, text: async () => JSON.stringify(body)});
/* What the next `/api/guilds/…` request answers. Driven through the real `api()`
 * rather than by constructing an ApiError, which is a class declaration inside
 * the script's own eval scope and unreachable from here — and driving the real
 * path is the more faithful test anyway. */
let refusal = null;
window.fetch = async (url) => {
    const u = String(url);
    if (refusal && u.includes('/guilds/')) {
        const {status, message} = refusal;
        return {ok: false, status,
                text: async () => JSON.stringify({status: 'error', message})};
    }
    if (u.includes('/locale')) {
        return ok({language: 'en', available: ['en'], data: {dashboard: locale.dashboard}});
    }
    if (u.includes('/auth/status')) {
        return ok({logged_in: true,
                   user: {id: '42', username: 'tester', avatar: null},
                   csrf_token: 'csrf-token', is_host: false,
                   idle_timeout_seconds: 600, version: '0.0.0-test',
                   asset_version: 'test-token',
                   guilds: [{id: '1', name: 'Test Guild'}]});
    }
    if (u.includes('/settings/registry')) {
        return ok({status: 'success', data: {settings: {}, features: [], groups: []}});
    }
    return ok({status: 'success', data: []});
};

const loginVisible = () =>
    !window.document.getElementById('login-screen').classList.contains('hidden');
const toasts = () => [...window.document.querySelectorAll('.toast')]
    .map((node) => node.textContent);

(async () => {
    window.eval(script);
    const event = window.document.createEvent('Event');
    event.initEvent('DOMContentLoaded', true, true);
    window.document.dispatchEvent(event);
    await new Promise((r) => setTimeout(r, 400));

    // A refusal: the operator stays where they are and is told the actual
    // reason, which is the only thing that can lead them to the fix.
    refusal = {status: 403, message: locale.dashboard.csrf_invalid};
    await window.eval("api('/guilds/1/settings').catch(handleApiError)");
    await new Promise((r) => setTimeout(r, 20));
    assert.ok(!loginVisible(),
        'a 403 must not send the operator to the login card');
    assert.ok(toasts().some((text) => text.includes(locale.dashboard.csrf_invalid)),
        `a 403 must report the server's reason; got ${JSON.stringify(toasts())}`);
    assert.ok(!toasts().some((text) => text === locale.dashboard.session_expired),
        'a 403 must not claim the session expired');

    // An ended session: the login card is the right place, and the server's own
    // message is preferred where it gave one.
    refusal = {status: 401, message: locale.dashboard.unauthorized};
    await window.eval("api('/guilds/1/settings').catch(handleApiError)");
    await new Promise((r) => setTimeout(r, 20));
    assert.ok(loginVisible(), 'a 401 must show the login card');
    assert.ok(toasts().some((text) => text.includes(locale.dashboard.unauthorized)),
        "a 401 must prefer the server's own reason where it gave one");

    console.log('ok');
    process.exit(0);
})();
