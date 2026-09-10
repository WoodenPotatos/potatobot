/* What the *browser* would POST for each item template.
 *
 * `pack` runs over the values the form holds, which is what `unpack` produced —
 * and `unpack` deliberately turns a stored role id into a **string**, because a
 * snowflake is 64-bit and a JSON number holds 53 bits exactly. Every Python
 * fixture wrote `role_id` as an int literal instead, so the server was only ever
 * tested with a type the browser never sends, and `_validate_shop_item` demanding
 * an `int` refused *every* role item created from the dashboard without a single
 * test noticing.
 *
 * Prints one JSON object per template so the Python side can POST it through the
 * real route rather than restating it.
 */
const assert = require('assert');
const fs = require('fs');

const source = fs.readFileSync(process.argv[2], 'utf8');
const start = source.indexOf('const SHOP_TEMPLATES = {');
assert.ok(start > 0, 'SHOP_TEMPLATES not found');
const end = source.indexOf('\n};', start) + 3;

// `consumable`'s catalog field resolves through the item list; the others do not.
const itemList = [{item_key: 'loaded_die', effect: 'inventory', value: null}];
const SHOP_TEMPLATES = eval(
    `(${source.slice(start + 'const SHOP_TEMPLATES = '.length, end - 1)})`);

/* Configs shaped the way the *endpoint* sends them, which is the only shape the
 * browser ever sees: `_wire_item_config` stringifies a snowflake before it
 * leaves, precisely because writing 1420070400000000001 as a JSON number gives
 * JavaScript 1420070400000000000. Written as a string here for the same reason —
 * a number literal in this file would round before `unpack` ever ran, and the
 * harness would then be testing the rounding rather than the transform. */
const ROLE = '1420070400000000001';
const stored = {
    fixed_role: {role_id: ROLE},
    timed_role: {role_id: ROLE, duration_days: 7},
    vault: {amount: 300000},
    consumable: {item_key: 'loaded_die'},
    coin_bundle: {amount: 500, repeatable: false},
    fulfillment_voucher: {asset_type: 'emoji', duration_days: 30},
};

const out = {};
for (const [template, config] of Object.entries(stored)) {
    const spec = SHOP_TEMPLATES[template];
    assert.ok(spec, `${template} is not declared`);
    const packed = spec.pack(spec.unpack(config));
    if ('role_id' in config) {
        // The id must come out byte-identical. It is above 2^53, so anything
        // that let it touch `Number()` would round it here.
        assert.strictEqual(String(packed.role_id), ROLE,
            `${template} rounded the role id: ${packed.role_id}`);
    }
    out[template] = packed;
}
process.stdout.write(JSON.stringify(out));
