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
        return ok({status: 'success', data: {
            user: {id: '42', username: 't', avatar: null}, is_host: true,
            idle_timeout_seconds: 600,
            guilds: [{guild_id: '1', name: 'G', icon: null}]}});
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
        return ok({status: 'success', data: [], limit: 8, custom_count: 0});
    }
    return ok({status: 'success', data: []});
};
window.eval(script);

// What each template must ask for, by the field descriptors it declares.
const EXPECTED = {
    fixed_role: ['Role'],
    timed_role: ['Role', 'Duration (days)'],
    vault: ['Vault'],
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
    const offered = [...select.options].map((o) => o.value);
    assert.deepStrictEqual(offered.sort(), Object.keys(EXPECTED).sort(),
        'the template list is not the declared one');

    const labels = () => [...window.document.querySelectorAll(
        '#shop-item-editor fieldset:nth-of-type(3) .field-label')]
        .map((node) => node.textContent);

    for (const [template, expected] of Object.entries(EXPECTED)) {
        select.value = template;
        select.dispatchEvent(new window.Event('change', {bubbles: true}));
        await new Promise((r) => setTimeout(r, 20));
        assert.deepStrictEqual(labels(), expected,
            `${template} asked for ${JSON.stringify(labels())}`);
    }

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
        vault: {amount: 25000},
        consumable: {item_key: 'loaded_die'},
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
        assert.deepStrictEqual(labels(), EXPECTED[template],
            `editing a ${template} asked for ${JSON.stringify(labels())}`);
        // The key is the row's identity and cannot be edited after creation.
        const keyInput = window.document.querySelector(
            '#shop-item-editor input[name="item_key"], #shop-item-editor [data-field="item_key"] input');
        if (keyInput) {
            assert.ok(keyInput.disabled || keyInput.readOnly,
                `editing a ${template} left the item key editable`);
        }
    }

    console.log('ok');
})();
