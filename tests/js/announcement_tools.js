/* The Announcements page holds the six event switches; each event's editor is
 * a sub-page of its own under it in the sidebar. An event's section gets its
 * placeholder list, a live preview that follows the fields, and a Send test
 * that refuses to queue while the section has unsaved edits (the test posts
 * the *saved* template).
 *
 * The boot harness stubs an empty registry, so it never reaches this code; the
 * fixture here is the real registry's declarations, written by the Python test.
 *
 * Argument 1 is the repository root, argument 2 the fixture file.
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
// The server's shipped texts, which are Hungarian here while the dashboard is not.
const shipped = fixture.serverTexts.announcements;

const dom = new JSDOM(html, {url: 'https://d.test/', runScripts: 'outside-only'});
const { window } = dom;
window.potatoLanguage = {current: () => 'en', set: () => {}};
const ok = (body) => ({ok: true, status: 200, text: async () => JSON.stringify(body)});

const uncaught = [];
window.addEventListener('error', (event) => uncaught.push(event.error || event.message));

const testCalls = [];
const cardCalls = [];
window.fetch = async (url, init) => {
    const u = String(url);
    if (u.includes('/locale')) {
        // An English dashboard on a Hungarian server: the shape the real
        // route sends, with the previewed texts in the server's language.
        return ok({language: 'en', available: ['en', 'hu'],
                   data: {dashboard: locale.dashboard},
                   server: {language: 'hu', texts: fixture.serverTexts}});
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
        return ok({status: 'success', data: fixture.registry,
                   feature_group_order: [], supported_games: [],
                   announcement_placeholders: fixture.placeholders});
    }
    if (u.endsWith('/guilds/1/features')) return ok({status: 'success', data: fixture.features});
    if (u.endsWith('/guilds/1/settings')) {
        return ok({status: 'success', data: fixture.settings, is_full_admin: true});
    }
    if (u.includes('/discord-resources')) {
        return ok({status: 'success', data: {channels: [], roles: []}});
    }
    if (u.includes('/card-background')) {
        cardCalls.push({method: (init && init.method) || 'GET', body: init && init.body});
        return ok({status: 'success', message: 'Saved.', data: {exists: false}});
    }
    if (u.includes('/announcements/')) {
        testCalls.push({url: u, body: init && init.body,
                        language: init && init.headers && init.headers['X-Dashboard-Language']});
        return ok({status: 'success', message: 'Queued.', data: {action_id: null}});
    }
    if (u.includes('/gacha')) return ok({status: 'success', data: []});
    return ok({status: 'success', data: []});
};

const tick = (ms = 30) => new Promise((resolve) => setTimeout(resolve, ms));

(async () => {
    window.eval(script);
    const event = window.document.createEvent('Event');
    event.initEvent('DOMContentLoaded', true, true);
    window.document.dispatchEvent(event);
    await tick(500);
    const EVENTS = ['join', 'leave', 'kick', 'ban', 'boost', 'boost_reward'];
    const doc = window.document;

    // The sidebar lists one sub-page per event, in the registry's declared
    // order rather than the payload's alphabetical one.
    const subPages = [...doc.querySelectorAll(
        '.nav-branch[data-branch="member_announcements"] .nav-children > .nav-item')]
        .map((item) => item.dataset.page);
    assert.deepStrictEqual(subPages,
        EVENTS.map((event) => `member_announcements:announce_${event}`));

    // The parent page is the switches, and no event's settings.
    await window.eval("showPage('member_announcements')");
    await tick();
    assert.strictEqual(doc.querySelectorAll(
        '#settings-grid fieldset[data-page^="announce_"]').length, 0,
        'an event editor rendered on the parent page as well as its own');
    assert.strictEqual(doc.querySelectorAll('#settings-grid .feature-grid > *').length, 6,
        'the parent page should hold the six event switches');

    const openEvent = async (event) => {
        await window.eval(`showPage('member_announcements:announce_${event}')`);
        await tick();
        const found = [...doc.querySelectorAll('#settings-grid fieldset[data-page^="announce_"]')];
        // The join page holds two blocks: the new member's, then the returning one's.
        assert.deepStrictEqual(found.map((section) => section.dataset.page),
            [`announce_${event}`].concat(event === 'join' ? ['announce_rejoin'] : []),
            `the ${event} sub-page shows other events`);
        // Its own switch only, so it can be turned off from where it is edited.
        assert.strictEqual(doc.querySelectorAll('#settings-grid .feature-grid > *').length, 1);
        assert.ok(!found[0].querySelector('.section-toggle'),
            'a sub-page holds one editor, so nothing collapses');
        return found[0];
    };

    // Every section in the registry's declared order: its channel first, then
    // the message. The boost channel rendered last because the page followed
    // the payload's alphabetical keys, where `booster_` sorts after `boost_m`.
    for (const name of EVENTS) {
        const section = await openEvent(name);
        assert.ok(section.querySelector('.announcement-tools'),
            `${section.dataset.page} has no announcement tools`);
        const keys = [...section.querySelectorAll(':scope > .form-grid > [data-setting]')]
            .map((node) => node.dataset.setting);
        const event = section.dataset.page.slice('announce_'.length);
        assert.ok(/_channel$/.test(keys[0]), `${section.dataset.page} starts with ${keys[0]}`);
        // Join and leave carry an image card's two fields.
        const card = ['join', 'leave'].includes(event)
            ? ['card', 'card_text'].map((field) => `${event}_message_${field}`) : [];
        assert.deepStrictEqual(keys.slice(1), ['title', 'texts', 'content', 'color',
            'image_url', 'thumbnail'].map((field) => `${event}_message_${field}`)
            .concat(card),
            `${section.dataset.page} fields out of order: ${keys}`);
        assert.ok(section.classList.contains('announcement-section'),
            'the previews sit beside the fields');
    }

    // The returning member's block: its own four fields, its own previews,
    // and empty fields that say what they fall back to.
    await openEvent('join');
    const rejoinSection = doc.querySelector('fieldset[data-page="announce_rejoin"]');
    assert.deepStrictEqual(
        [...rejoinSection.querySelectorAll(':scope > .form-grid > [data-setting]')]
            .map((node) => node.dataset.setting),
        ['title', 'texts', 'content', 'card_text'].map((field) => `rejoin_message_${field}`));
    const rejoinTitle = rejoinSection.querySelector('[data-key="rejoin_message_title"]');
    // The fixture saved the join title "Hi {user}": that is what an empty
    // returning title means, so that is what it shows.
    assert.strictEqual(rejoinTitle.placeholder, 'Hi {user}');
    assert.strictEqual(rejoinSection.querySelector('.announcement-preview-title').textContent,
        'Hi @tester');
    rejoinTitle.value = 'Welcome back {display_name}';
    rejoinTitle.dispatchEvent(new window.Event('input', {bubbles: false}));
    assert.strictEqual(rejoinSection.querySelector('.announcement-preview-title').textContent,
        'Welcome back tester');
    rejoinTitle.value = '';
    rejoinTitle.dispatchEvent(new window.Event('input', {bubbles: false}));
    await tick(1000);
    const rejoinTest = [...rejoinSection.querySelectorAll('button')].find(
        (button) => button.textContent === locale.dashboard.announcement_send_test);
    rejoinTest.click();
    await tick();
    const lastTest = testCalls[testCalls.length - 1];
    assert.ok(lastTest.url.endsWith('/announcements/join/test'), lastTest.url);
    assert.strictEqual(lastTest.body, '{"returning":true}', 'the returning test was not marked');
    console.log('ok returning block');
    // The join checks below count their own test sends from zero.
    testCalls.length = 0;

    // An unset field shows the shipped text: greyed in the box, filled in
    // the preview. Nothing is put in the box's value, so nothing is saved.
    const leave = await openEvent('leave');
    const leaveTitle = leave.querySelector('[data-key="leave_message_title"]');
    assert.strictEqual(leaveTitle.value, '');
    assert.strictEqual(leaveTitle.placeholder, shipped.leave_title);
    assert.strictEqual(leave.querySelector(
        '[data-key="leave_message_texts"] .embed-block textarea').placeholder,
        shipped.leave_desc);
    assert.strictEqual(leave.querySelector('.announcement-preview-title').textContent,
        shipped.leave_title);
    const leaveText = leave.querySelector('.announcement-preview-description').textContent;
    assert.ok(leaveText.includes('tester') && leaveText.includes('42')
        && !leaveText.includes('{'), `shipped leave text not filled: ${leaveText}`);

    const join = await openEvent('join');
    const chips = [...join.querySelectorAll('.announcement-placeholder')]
        .map((node) => node.textContent);
    assert.ok(chips.includes('{user}') && chips.includes('{user_id}'),
        `placeholders listed: ${chips}`);
    // Each variant is its own multi-line box now, so the editor no longer
    // teaches a line-break placeholder.
    assert.ok(!chips.includes('{newline}'), 'the {newline} chip should be gone');
    assert.ok(!chips.includes('{reason}'), 'a join cannot fill {reason}');

    const title = () => join.querySelector('.announcement-preview-title').textContent;
    const description = () => join.querySelector(
        '.announcement-preview-description').textContent;
    assert.strictEqual(title(), 'Hi @tester');
    assert.strictEqual(description(), 'Line one');
    assert.ok(join.querySelector('.announcement-preview-meta'),
        'two variants should be counted');
    assert.strictEqual(join.querySelector('.announcement-preview-embed')
        .style.getPropertyValue('--embed-color'), '#123456');


    // Bound to the control itself: a non-bubbling event must still redraw.
    const titleInput = join.querySelector('[data-key="join_message_title"]');
    titleInput.value = 'Bye {server} {nope}';
    titleInput.dispatchEvent(new window.Event('input', {bubbles: false}));
    assert.strictEqual(title(), 'Bye Test Guild {nope}');

    const sendTest = [...join.querySelectorAll('button')].find(
        (button) => button.textContent === locale.dashboard.announcement_send_test);
    assert.ok(sendTest, 'no Send test button');

    // Variants: one multi-line box each, added like a rules section.
    const texts = join.querySelector('[data-key="join_message_texts"]');
    const boxes = () => [...texts.querySelectorAll('.embed-block textarea')];
    assert.deepStrictEqual(boxes().map((box) => box.value), ['Line one', 'Line two']);
    const addVariant = [...texts.querySelectorAll('button')].find(
        (button) => button.textContent === locale.dashboard.multiline_list_add);
    assert.ok(addVariant, 'no Add variant button');
    addVariant.click();
    assert.strictEqual(boxes().length, 3);
    // A box after the first is a variant, not the shipped text.
    assert.strictEqual(boxes()[2].placeholder, locale.dashboard.multiline_list_text);
    const third = boxes()[2];
    third.value = '**Hello** *there*\nsecond line';
    third.dispatchEvent(new window.Event('input', {bubbles: true}));
    third.dispatchEvent(new window.FocusEvent('focusin', {bubbles: true}));
    const shown = join.querySelector('.announcement-preview-description');
    assert.ok(shown.querySelector('strong') && shown.querySelector('em'),
        `markdown not rendered in the preview: ${shown.innerHTML}`);
    assert.strictEqual(shown.querySelectorAll('.preview-line').length, 2,
        'a line break inside a variant must survive');
    // Through JSON: an array built in the page's realm has another prototype.
    assert.deepStrictEqual(JSON.parse(window.eval(
        "JSON.stringify(readSettingInput({value_type: 'string_list', multiline: true}, "
        + "document.querySelector('[data-key=\"join_message_texts\"]')))")),
        ['Line one', 'Line two', '**Hello** *there*\nsecond line']);

    // Five at most; the sixth is refused before the API would refuse it.
    addVariant.click(); addVariant.click(); addVariant.click();
    assert.strictEqual(boxes().length, 5, 'the variant limit was not enforced');

    sendTest.click();
    await tick();
    assert.strictEqual(testCalls.length, 0, 'a test was queued over an added variant');

    // Removing the added boxes returns the section to exactly what was saved.
    [...texts.querySelectorAll('.embed-block')].slice(2).forEach((block) =>
        block.querySelector('.btn-icon.danger').click());
    assert.strictEqual(boxes().length, 2);

    sendTest.click();
    await tick();
    assert.strictEqual(testCalls.length, 0, 'a test was queued over unsaved edits');

    titleInput.value = 'Hi {user}';
    titleInput.dispatchEvent(new window.Event('input', {bubbles: false}));
    sendTest.click();
    await tick();
    assert.strictEqual(testCalls.length, 1, 'the saved template was not queued');
    assert.ok(testCalls[0].url.endsWith('/guilds/1/announcements/join/test'), testCalls[0].url);
    assert.strictEqual(testCalls[0].body, '{}');
    // Every request names the operator's language, so the reply matches the page.
    assert.strictEqual(testCalls[0].language, 'en');
    assert.notStrictEqual(shipped.leave_title, locale.announcements?.leave_title,
        'the server texts must not be the dashboard catalog');

    // An image card's controls: on join and leave only, shown with the switch.
    const kickSection = await openEvent('kick');
    assert.ok(!kickSection.querySelector('.card-tools'), 'kick has no card');
    const joinAgain = await openEvent('join');
    const cardBox = joinAgain.querySelector('.card-tools');
    assert.ok(cardBox && cardBox.classList.contains('hidden'), 'the card starts off and hidden');
    const cardSwitch = joinAgain.querySelector('[data-key="join_message_card"]');
    cardSwitch.checked = true;
    cardSwitch.dispatchEvent(new window.Event('change', {bubbles: true}));
    await tick();
    assert.ok(!cardBox.classList.contains('hidden'), 'switching the card on shows it');
    const cardPreview = cardBox.querySelector('.card-preview');
    assert.ok(/\/api\/guilds\/1\/announcements\/join\/card-preview\?v=\d+$/.test(cardPreview.src),
        `preview source: ${cardPreview.src}`);
    // Switching on also autosaves the switch, which redraws again: so each
    // check below asks whether the preview moved, not where it landed.
    const cardVersion = () => Number(cardPreview.src.split('v=')[1]);
    let last = cardVersion();
    assert.deepStrictEqual(cardCalls.map((call) => call.method), ['GET', 'GET'],
        'whether a background exists is asked once per card');
    // Two cards on the join page, one per welcome, both behind the one switch.
    const cards = [...window.document.querySelectorAll('.card-tools')];
    assert.strictEqual(cards.length, 2, 'one card box per welcome');
    const rejoinCard = cards.find((box) => box !== cardBox);
    assert.ok(!rejoinCard.classList.contains('hidden'), 'the one switch shows both cards');
    assert.ok(/\/announcements\/rejoin\/card-preview\?v=\d+$/.test(
        rejoinCard.querySelector('.card-preview').src), 'the returning card draws its own');

    // Uploading sends the browser-scaled image; jsdom has no canvas, so the
    // scaling is stood in for and everything around it is real.
    window.eval("scaleToCard = async () => 'c2NhbGVk'");
    const picker = cardBox.querySelector('.card-file');
    Object.defineProperty(picker, 'files', {value: [{name: 'bg.png'}], configurable: true});
    picker.dispatchEvent(new window.Event('change'));
    await tick(80);
    const posted = cardCalls.find((call) => call.method === 'POST');
    assert.ok(posted, 'the background was never sent');
    assert.deepStrictEqual(JSON.parse(posted.body), {image: 'c2NhbGVk'});
    assert.ok(cardVersion() > last, 'the preview did not refresh after upload');
    last = cardVersion();

    const resetButton = [...cardBox.querySelectorAll('button')].find(
        (button) => button.textContent === locale.dashboard.card_background_remove);
    resetButton.click();
    await tick();
    assert.ok(cardCalls.some((call) => call.method === 'DELETE'));
    assert.ok(cardVersion() > last, 'the preview did not refresh after the reset');
    last = cardVersion();

    // A saved card field redraws the preview; the save arrives as an event.
    window.document.dispatchEvent(new window.CustomEvent('settings-saved',
        {detail: ['join_message_card_text']}));
    assert.strictEqual(cardVersion(), last + 1, 'a saved card text left the preview stale');
    last = cardVersion();
    window.document.dispatchEvent(new window.CustomEvent('settings-saved',
        {detail: ['leave_message_card_text']}));
    assert.strictEqual(cardVersion(), last, 'another event\'s save redrew this card');

    // The page title names the event, not a missing key.
    assert.strictEqual(doc.getElementById('page-title').textContent,
        locale.dashboard.features.announce_join);

    assert.deepStrictEqual(uncaught, []);
    console.log('ok');
    process.exit(0);
})().catch((error) => {
    console.log(`FAIL ${error && error.stack || error}`);
    process.exit(1);
});
