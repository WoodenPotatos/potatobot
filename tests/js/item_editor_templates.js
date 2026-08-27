/* Section three of the item creator must follow the template chosen above it.
 *
 * It did not: the redraw listened for a change inside `.field`, but
 * `managedFieldWrapper` gives the wrapper the class `input-group`, so the
 * selector matched nothing and section three kept whatever the first template
 * had put there. Every kind asked for a role — pick "vault" and it still said
 * "select a role" — and no test could see it, because the declaration was
 * correct and only the wiring was wrong.
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const repo = process.argv[2];
const html = fs.readFileSync(path.join(repo, 'dashboard/index.html'), 'utf8');
const script = fs.readFileSync(path.join(repo, 'dashboard/script.js'), 'utf8');
const locale = JSON.parse(fs.readFileSync(path.join(repo, 'locales/en.json'), 'utf8'));

const ROLE = '1420070400000000002';

/* The `catalog` field kinds — the vault and consumable pickers — build their
 * options from this list, filtered on `source === 'builtin'` and on `effect`.
 * `effect` here is `ItemEffect.value`, lowercase, because that is what
 * `guild_item_list` sends; stubbing it uppercase leaves those two selects empty
 * and the test passes while proving nothing about them. */
const BUILTINS = [
    {item_key: 'small_vault', source: 'builtin', name: 'Small vault',
     description: '', effect: 'vault', value: 25000, price: 500000,
     in_shop: true, in_gacha: true, enabled: true, editable: false,
     price_setting: 'shop_price_small_vault'},
    {item_key: 'med_vault', source: 'builtin', name: 'Medium vault',
     description: '', effect: 'vault', value: 100000, price: 1500000,
     in_shop: true, in_gacha: true, enabled: true, editable: false,
     price_setting: 'shop_price_med_vault'},
    {item_key: 'stacked_deck', source: 'builtin', name: 'Stacked deck',
     description: '', effect: 'inventory', value: null, price: 12000,
     in_shop: true, in_gacha: true, enabled: true, editable: false,
     price_setting: 'shop_price_stacked_deck'},
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
    if (u.includes('/discord-resources')) {
        return ok({status: 'success', data: {channels: [], roles: [
            {id: ROLE, name: 'Member', color: 0, position: 1,
             managed: false, manageable: true}]}});
    }
    if (u.includes('/items')) {
        return ok({status: 'success', data: BUILTINS, limit: 8, custom_count: 0});
    }
    return ok({status: 'success', data: []});
};
window.eval(script);

// What each template must ask for, by the field descriptors it declares.
const EXPECTED = {
    fixed_role: ['Role'],
    timed_role: ['Role', 'Duration (days)'],
    vault: ['Protected reserve'],
    consumable: ['Consumable'],
    coin_bundle: ['Coins', 'Can be bought more than once'],
    fulfillment_voucher: ['Asset kind', 'Duration (days)'],
};

(async () => {
    const event = window.document.createEvent('Event');
    event.initEvent('DOMContentLoaded', true, true);
    window.document.dispatchEvent(event);
    await new Promise((r) => setTimeout(r, 500));
    await window.eval("showPage('shop-builder')");
    await new Promise((r) => setTimeout(r, 150));
    await window.eval('renderItemEditor(null)');

    const select = window.document.querySelector('#shop-item-editor select');
    assert.ok(select, 'the creator did not render');
    /* A *new* item cannot be a `consumable`: it grants +1 of an existing built-in
     * and cannot change any of its numbers, so offering it invites the
     * reasonable expectation that it is how a variant is built, which it is not.
     * It stays available when *editing* one, because a guild that already has
     * such a row must be able to open and delete it. */
    const offered = [...select.options].map((o) => o.value);
    assert.ok(!offered.includes('consumable'),
        'a new item must not be offered the consumable kind');
    assert.deepStrictEqual(
        offered.slice().sort(),
        Object.keys(EXPECTED).filter((k) => k !== 'consumable').sort(),
        'the template list is not the declared one minus what is withheld');

    const labels = () => [...window.document.querySelectorAll(
        '#shop-item-editor fieldset:nth-of-type(3) .field-label')]
        .map((node) => node.textContent);

    const note = () => window.document.querySelector(
        '#shop-item-editor fieldset:nth-of-type(3) .section-note').textContent;

    for (const [template, expected] of Object.entries(EXPECTED)) {
        if (template === 'consumable') continue;   // not offered for a new item
        select.value = template;
        select.dispatchEvent(new window.Event('change', {bubbles: true}));
        await new Promise((r) => setTimeout(r, 20));
        assert.deepStrictEqual(labels(), expected,
            `${template} asked for ${JSON.stringify(labels())}`);
        // Every kind explains itself, redrawn with the section so the sentence
        // always describes the kind on screen. A vault's field is a reserve and
        // a consumable's is which existing item to hand over; neither is
        // apparent from a number and a dropdown, which is what made the creator
        // read as broken.
        assert.ok(note().length > 0, `${template} explains nothing`);
        assert.ok(!note().startsWith('['), `${template}'s note is a raw key`);
    }
    // And the notes are distinct — a copied sentence explains nothing either.
    const notes = new Set();
    for (const template of Object.keys(EXPECTED)) {
        if (template === 'consumable') continue;
        select.value = template;
        select.dispatchEvent(new window.Event('change', {bubbles: true}));
        await new Promise((r) => setTimeout(r, 10));
        notes.add(note());
    }
    assert.strictEqual(notes.size, Object.keys(EXPECTED).length - 1,
        'two templates share an explanation');

    // One text, in whatever language the guild speaks.
    const text = [...window.document.querySelectorAll(
        '#shop-item-editor fieldset:nth-of-type(2) .field-label')]
        .map((node) => node.textContent);
    assert.strictEqual(text.length, 2,
        `the text section has ${text.length} fields, not one name and one description`);

    /* Opening an *existing* item must show that item's own kind.
     *
     * Everything above drives the creator, where the template is whatever the
     * dropdown was last set to — so none of it could see that `renderItemEditor`
     * read `existing.template_type` while `/items` serves a custom item's
     * template under `effect` (which is why `itemPatchBody` sends
     * `template_type: item.effect`). The read gave `undefined`, the choice fell
     * back to its first option, and every item opened as "Permanent role"
     * asking which role to grant. Saving a vault from that form would have
     * rewritten it into a role grant. */
    const STORED = {
        fixed_role: {role_id: ROLE},
        timed_role: {role_id: ROLE, duration_days: 30},
        vault: {amount: 300000},
        consumable: {item_key: 'stacked_deck'},
        coin_bundle: {amount: 100, repeatable: false},
        fulfillment_voucher: {asset_type: 'emoji', duration_days: 180},
    };
    for (const [template, config] of Object.entries(STORED)) {
        const stored = {
            item_key: `stored_${template}`, source: 'custom', editable: true,
            enabled: true, price: 5000, revision: 2, config,
            name: 'Stored', description: 'A stored item.',
            effect: template, value: null, in_shop: true, in_gacha: false,
            price_setting: null,
        };
        await window.eval(`renderItemEditor(${JSON.stringify(stored)})`);
        await new Promise((r) => setTimeout(r, 20));
        const chosen = window.document.querySelector('#shop-item-editor select');
        assert.strictEqual(chosen.value, template,
            `editing a ${template} opened as ${chosen.value}`);
        // A withheld kind is still selectable while editing an item that is one,
        // or the row could never be opened.
        assert.ok([...chosen.options].some((o) => o.value === template),
            `${template} is not offered even when editing one`);
        assert.deepStrictEqual(labels(), EXPECTED[template],
            `editing a ${template} asked for ${JSON.stringify(labels())}`);

        /* And the fields must carry the stored values. Comparing the wrong
         * field name made `values` empty for every kind, so a vault opened
         * reading "Nothing selected" and saving it wrote that back. Right
         * fields with blank values is the more dangerous half of this bug,
         * because the form looks correct. */
        const filled = [...window.document.querySelectorAll(
            '#shop-item-editor fieldset:nth-of-type(3) [data-field]')]
            .map((wrap) => {
                const input = wrap.querySelector('select, input, textarea');
                if (!input) return [wrap.dataset.field, null];
                return [wrap.dataset.field,
                        input.type === 'checkbox' ? input.checked : input.value];
            });
        for (const [field, value] of filled) {
            assert.ok(value !== '' && value !== null && value !== undefined,
                `editing a ${template} left ${field} empty`);
        }
        // The key is the row's identity and cannot be edited after creation.
        const keyInput = window.document.querySelector(
            '#shop-item-editor input[name="item_key"], #shop-item-editor [data-field="item_key"] input');
        if (keyInput) {
            assert.ok(keyInput.disabled || keyInput.readOnly,
                `editing a ${template} left the item key editable`);
        }
    }

    /* The redraw must not depend on delegation. It was a listener on the form
     * matching the event's ancestor, and that broke twice — first on a class
     * `managedFieldWrapper` never sets, then on the `data-field` attribute.
     * Both times a vault could not be created, silently. A non-bubbling event
     * dispatched straight at the control still has to redraw. */
    await window.eval('renderItemEditor(null)');
    await new Promise((r) => setTimeout(r, 20));
    const kind = window.document.querySelector('#shop-item-editor select');
    kind.value = 'vault';
    kind.dispatchEvent(new window.Event('change', {bubbles: false}));
    await new Promise((r) => setTimeout(r, 20));
    assert.deepStrictEqual(labels(), EXPECTED.vault,
        'the redraw still relies on the event bubbling to an ancestor');

    console.log('ok');
    // Explicit, because an authenticated boot starts the session
    // countdown and the feature poller, and those pending timers keep
    // Node alive indefinitely once the harness has finished.
    process.exit(0);
})();
