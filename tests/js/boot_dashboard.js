/* Boot the real dashboard and open every page.
 *
 * Two outages came from a throw during startup: a picker built from a
 * definition missing its locale key, and a listener bound to markup that had
 * been replaced. Both left "Cannot read properties of null" on screen and no
 * dashboard at all, and neither was catchable by anything the suite had —
 * `node --check` parses without executing, and nothing else drove the page.
 *
 * Argument 1 is the repository root. Prints one line per page and exits
 * non-zero if the shell fails to start or any page throws.
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const repo = process.argv[2];
const root = path.join(repo, 'dashboard');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'script.js'), 'utf8');
const locale = JSON.parse(fs.readFileSync(path.join(repo, 'locales/en.json'), 'utf8'));

const dom = new JSDOM(html, {url: 'https://dash.test/', runScripts: 'outside-only'});
const { window } = dom;
window.potatoLanguage = {current: () => 'en', set: () => {}};

const GUILD = '1420070400000000001';
const ok = (body) => ({ok: true, status: 200, text: async () => JSON.stringify(body)});

/* Shaped the way each route really answers. A stub that returns the wrong shape
 * produces a throw the real server never would, which is worse than no test. */
window.fetch = async (url) => {
    const u = String(url);
    if (u.includes('/locale')) {
        return ok({language: 'en', available: ['hu', 'en'],
                   data: {dashboard: locale.dashboard}});
    }
    if (u.includes('/auth/status')) {
        return ok({status: 'success', data: {
            user: {id: '42', username: 'tester', avatar: null},
            is_host: true, idle_timeout_seconds: 600,
            guilds: [{guild_id: GUILD, name: 'Test Guild', icon: null}]}});
    }
    if (u.includes('/settings/registry')) {
        return ok({status: 'success', data: {settings: {}, features: [], groups: []}});
    }
    if (u.includes('/work-responses')) {
        return ok({status: 'success', data: {
            responses: [], tiers: ['normal', 'free', 'high'],
            earnings_placeholder: '{earnings}', coin_placeholder: '{coin}'}});
    }
    if (u.includes('/permissions')) {
        return ok({status: 'success', data: {
            findings: [], features: [], blocking_count: 0, degraded_count: 0,
            administrator: false}});
    }
    if (u.includes('/gacha')) {
        return ok({status: 'success', data: [], shipped_rewards: {}});
    }
    if (u.includes('/items')) {
        return ok({status: 'success', data: [], limit: 10, custom_count: 0});
    }
    return ok({status: 'success', data: []});
};

const uncaught = [];
window.addEventListener('error', (event) => uncaught.push(event.error || event.message));

window.eval(script);

(async () => {
    const event = window.document.createEvent('Event');
    event.initEvent('DOMContentLoaded', true, true);
    window.document.dispatchEvent(event);
    await new Promise((resolve) => setTimeout(resolve, 600));

    let failed = false;
    const fatal = window.document.getElementById('fatal-screen');
    if (!fatal.classList.contains('hidden')) {
        const message = window.document.getElementById('fatal-message').textContent;
        console.log(`FAIL  the shell did not start: ${message}`);
        failed = true;
    } else {
        console.log('ok    shell started');
    }

    const pages = [...window.document.querySelectorAll('.nav-item[data-page]')]
        .map((button) => button.dataset.page);
    if (pages.length < 10) {
        console.log(`FAIL  only ${pages.length} pages found; the premise is wrong`);
        failed = true;
    }
    for (const page of pages) {
        const before = uncaught.length;
        let thrown = null;
        try {
            await window.eval(`showPage(${JSON.stringify(page)})`);
            await new Promise((resolve) => setTimeout(resolve, 40));
        } catch (error) {
            thrown = error;
        }
        const problem = thrown || uncaught.slice(before)[0];
        if (problem) {
            console.log(`FAIL  ${page}: ${problem.message || problem}`);
            failed = true;
        } else {
            console.log(`ok    ${page}`);
        }
    }
    process.exit(failed ? 1 : 0);
})();
