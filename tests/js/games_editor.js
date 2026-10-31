/* The Game channels page: one row per game, LFG and patch notes together.
 *
 * The fixture is the real routes' payloads, written by
 * tests/test_games_page.py, so the stub cannot drift from what the server
 * sends. Argument 1 is the repository root, argument 2 the fixture file.
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const repo = process.argv[2];
const fixture = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const html = fs.readFileSync(path.join(repo, 'dashboard/index.html'), 'utf8');
const script = fs.readFileSync(path.join(repo, 'dashboard/script.js'), 'utf8');
const locale = JSON.parse(fs.readFileSync(path.join(repo, 'locales/en.json'), 'utf8'));

const dom = new JSDOM(html, {url: 'https://d.test/', runScripts: 'outside-only'});
const { window } = dom;
window.potatoLanguage = {current: () => 'en', set: () => {}};
const ok = (body) => ({ok: true, status: 200, text: async () => JSON.stringify(body)});

const uncaught = [];
const patches = [];
window.addEventListener('error', (event) => uncaught.push(event.error || event.message));

const featurePosts = [];
const requests = [];
window.fetch = async (url, init) => {
    const u = String(url);
    if (init && init.method && init.method !== 'GET') requests.push({method: init.method, url: u});
    if (u.includes('/locale')) {
        return ok({language: 'en', available: ['en'], data: {dashboard: locale.dashboard}});
    }
    if (u.includes('/auth/status')) {
        return ok({logged_in: true, user: {id: '42', username: 'tester', avatar: null},
                   csrf_token: 'csrf-token', is_host: true, idle_timeout_seconds: 600,
                   version: '0.0.0-test', asset_version: 'test-token',
                   guilds: [{id: '1', name: 'Test Guild'}]});
    }
    if (u.includes('/settings/registry')) {
        return ok({status: 'success', data: fixture.registry,
                   feature_group_order: [], supported_games: fixture.supportedGames,
                   announcement_placeholders: {}});
    }
    if (u.endsWith('/guilds/1/features') && init && init.method === 'POST') {
        featurePosts.push(JSON.parse(init.body));
        return ok({status: 'success', message: 'Saved.'});
    }
    if (u.endsWith('/guilds/1/features')) return ok({status: 'success', data: fixture.features});
    if (u.endsWith('/guilds/1/settings') && init && init.method === 'PATCH'
            && JSON.parse(init.body).changes.some((change) => change.value === 999)) {
        // The server's refusal shape: a 400 with the reason as its message.
        return {ok: false, status: 400,
                text: async () => JSON.stringify({status: 'error', message: 'Too many hours.'})};
    }
    if (u.endsWith('/guilds/1/settings') && init && init.method === 'PATCH') {
        // What the server answers: each saved row, at its next revision.
        const saved = {};
        JSON.parse(init.body).changes.forEach((change) => {
            saved[change.key] = {value: change.value, revision: change.revision + 1};
        });
        patches.push(JSON.parse(init.body).changes);
        Object.assign(fixture.settings, saved);
        return ok({status: 'success', message: 'Saved.', data: saved});
    }
    if (u.endsWith('/guilds/1/settings')) {
        return ok({status: 'success', data: fixture.settings, is_full_admin: true});
    }
    if (u.includes('/discord-resources')) {
        return ok({status: 'success', data: fixture.resources});
    }
    return ok({status: 'success', data: []});
};

const tick = (ms = 40) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
    window.eval(script);
    const event = window.document.createEvent('Event');
    event.initEvent('DOMContentLoaded', true, true);
    window.document.dispatchEvent(event);
    await tick(500);
    await window.eval("showPage('games')");
    await tick();
    const doc = window.document;
    const editor = doc.querySelector('[data-setting="games"]');
    assert.ok(editor, 'the games setting renders on its page');
    // Looked up live every time: a refused save redraws the page, and a row
    // held from before the redraw is detached and tells the old story.
    const rows = () => [...doc.querySelectorAll(
        '[data-setting="games"] .menu-rows > .menu-row')];
    const rowFor = (key) => rows().find((row) =>
        (row.querySelector('[data-column="__key"]').dataset.value
         ?? row.querySelector('[data-column="__key"]').value) === key);
    const changes = () => window.eval(
        "JSON.stringify(collectSettingChanges(document.getElementById('settings-grid')))");

    // Opening the page changes nothing.
    assert.strictEqual(changes(), '[]', `the form opened dirty: ${changes()}`);
    console.log('ok    the page opens clean');

    // A supported game is named, and has a patch switch and a patch channel.
    const lol = rowFor('lol');
    assert.ok(lol, 'the stored supported game has a row');
    assert.strictEqual(lol.querySelector('.row-game-name')?.textContent,
        locale.dashboard.game_names.lol);
    const lolSwitch = lol.querySelector('input[data-feature="patchbot_lol"]');
    assert.ok(lolSwitch, 'a saved supported game has its patch notes switch');
    assert.strictEqual(lolSwitch.checked, fixture.features.patchbot_lol.enabled);
    assert.ok(lol.querySelector('[data-column="patch_channel"] .picker-carrier'));
    assert.ok(lol.querySelector('[data-column="lfg"] input').checked);
    // A custom game is typed, and has neither.
    const custom = rowFor('Among Us');
    assert.ok(custom, 'the stored custom game has a row');
    assert.strictEqual(custom.querySelector('[data-column="__key"]').tagName, 'INPUT');
    assert.ok(!custom.querySelector('input[data-feature]'), 'a custom game has no patch switch');
    assert.ok(!custom.querySelector('[data-column="patch_channel"] .picker-carrier'));
    console.log('ok    supported and custom rows draw the right cells');

    // Each row is its own grid, so an `auto` column sizes per row and a row
    // with "—" where another has a switch drifts sideways.
    rows().forEach((row) => assert.ok(
        !/\bauto\b/.test(row.style.getPropertyValue('--row-template')),
        'a templated row may not size a column by its own content'));
    // The section says "Games"; its one field must not say it again under it.
    assert.ok(editor.querySelector(':scope > .field-label').classList.contains('hidden'),
        'the field label repeats the section heading');
    console.log('ok    columns align across rows and no heading repeats');

    // The patch switches live in the rows, not again above the form.
    assert.strictEqual(doc.querySelectorAll('#settings-grid .feature-grid input').length, 0,
        'a per-game switch was drawn above the form as well');
    console.log('ok    no switch is drawn twice');

    // Adding a supported game: named, matched by name, saved after a pause.
    const pick = editor.querySelector('.game-quick-pick');
    pick.value = 'valorant';
    pick.dispatchEvent(new window.Event('change'));
    await tick();
    const valorant = rowFor('valorant');
    assert.ok(valorant, 'the picked game was added');
    assert.strictEqual(valorant.querySelector('[data-column="channel"] .picker-carrier').value,
        fixture.valorantChannel, 'its channel was matched by name');
    assert.ok(valorant.querySelector('input[data-feature="patchbot_valorant"]'),
        'a new supported game has its switch at once');
    // The list is rebuilt as it opens, so a game added since is filtered.
    pick.dispatchEvent(new window.Event('focus'));
    assert.ok(![...pick.options].some((option) => option.value === 'valorant'),
        'a game already listed is not offered again');
    assert.strictEqual(patches.length, 0, 'saved before the pause was over');
    await tick(1000);
    assert.strictEqual(patches.length, 1, 'the new game was never saved');
    assert.deepStrictEqual(patches[0].map((change) => change.key), ['games'],
        'a save carries only what changed');
    assert.ok('valorant' in patches[0][0].value);
    assert.strictEqual(changes(), '[]', 'saved, yet the form still differs');
    assert.strictEqual(doc.querySelector('#settings-form .autosave-status').textContent,
        locale.dashboard.autosave_saved);
    console.log('ok    a change saves itself after a pause, alone');

    // An unfinished row holds the save rather than deleting the saved game.
    const lfg = valorant.querySelector('[data-column="lfg"] input');
    lfg.checked = true;
    lfg.dispatchEvent(new window.Event('change', {bubbles: true}));
    await tick(1000);
    assert.ok(valorant.classList.contains('menu-row-incomplete'));
    assert.strictEqual(patches.length, 1,
        'an LFG row missing its role was saved, deleting the game it edits');
    assert.ok('valorant' in fixture.settings.games.value, 'the stored game survived');
    console.log('ok    an unfinished row waits instead of deleting its game');

    // A plain box saves after a pause in typing, without being left: its
    // `change` only fires on blur, so this is the `input` path alone.
    const hours = doc.querySelector('[data-key="lfg_auto_close_hours"]');
    assert.ok(hours, 'the LFG timeout renders on this page');
    const before = patches.length;
    hours.value = '12';
    hours.dispatchEvent(new window.Event('input', {bubbles: true}));
    await tick(1000);
    assert.strictEqual(patches.length, before + 1, 'typing a value never saved it');
    assert.deepStrictEqual(patches[before].map((change) => change.key), ['lfg_auto_close_hours']);
    console.log('ok    typing saves after a pause, without leaving the box');

    // A refused value says why, and the box goes back to what is saved.
    let box = doc.querySelector('[data-key="lfg_auto_close_hours"]');
    box.value = '999';
    box.dispatchEvent(new window.Event('change', {bubbles: true}));
    await tick(1200);
    box = doc.querySelector('[data-key="lfg_auto_close_hours"]');
    assert.strictEqual(box.value, '12', 'a refused value stayed in the box');
    assert.ok([...doc.querySelectorAll('.toast')].some((t) => t.textContent.includes('Too many hours.')),
        'the refusal did not say why');
    console.log('ok    a refused value says why and reverts');

    // A switch saves the pending edit first, then itself. The refusal above
    // redrew the page, so every control is looked up again.
    const name = rowFor('Among Us').querySelector('[data-column="__key"]');
    name.value = 'Among Us 2';
    name.dispatchEvent(new window.Event('input', {bubbles: true}));
    const lolToggle = rowFor('lol').querySelector('input[data-feature="patchbot_lol"]');
    lolToggle.checked = !lolToggle.checked;
    lolToggle.dispatchEvent(new window.Event('change', {bubbles: true}));
    await tick(150);
    const order = requests.map((request) => `${request.method} ${request.url.split('/').pop()}`);
    const lastPatch = order.lastIndexOf('PATCH settings');
    const post = order.indexOf('POST features');
    assert.ok(lastPatch >= 0 && post > lastPatch, `switch before its pending save: ${order}`);
    assert.strictEqual(featurePosts[0].feature_key, 'patchbot_lol');
    assert.ok('Among Us 2' in fixture.settings.games.value, 'the typed rename was lost');
    console.log('ok    a switch saves pending edits first, then itself');

    assert.deepStrictEqual(uncaught, []);
    process.exit(0);
})().catch((error) => {
    console.log(`FAIL  ${error && error.stack || error}`);
    process.exit(1);
});
