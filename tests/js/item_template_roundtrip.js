/* The item creator's `unpack`/`pack` pair, exercised in the language it is
 * written in.
 *
 * Driven from Python through Node for the reason `managed_roundtrip.js` gives:
 * a Python re-implementation could stay green while the real pair broke. What
 * matters is that opening a stored item and saving it unchanged produces the
 * same config the server already holds — the class of bug that rounded every
 * snowflake the dashboard had ever saved.
 */
const assert = require('assert');
const fs = require('fs');

const source = fs.readFileSync(process.argv[2], 'utf8');
const start = source.indexOf('const SHOP_TEMPLATES = {');
const end = source.indexOf('\n};', start) + 3;
assert.ok(start > 0, 'SHOP_TEMPLATES not found');

// `vault` resolves its amount through the item list, so the stubs stand in for
// what the endpoint serves.
const itemList = [
    {item_key: 'small_vault', effect: 'vault', value: 25000},
    {item_key: 'med_vault', effect: 'vault', value: 100000},
    {item_key: 'big_vault', effect: 'vault', value: 500000},
];
function vaultKeyForAmount(amount) {
    const match = itemList.find((i) => i.effect === 'vault' && i.value === amount);
    return match ? match.item_key : null;
}
function vaultAmountForKey(key) {
    const match = itemList.find((i) => i.item_key === key);
    return match ? match.value : null;
}

const SHOP_TEMPLATES = eval(`(${source.slice(start + 'const SHOP_TEMPLATES = '.length, end - 1)})`);

// One stored config per template, shaped the way get_shop_item_definitions
// returns it — ids as the integers the database holds.
const stored = {
    fixed_role: {role_id: 1420070400000000002},
    timed_role: {role_id: 1420070400000000002, duration_days: 30},
    vault: {amount: 100000},
    consumable: {item_key: 'loaded_die'},
    coin_bundle: {amount: 5000, repeatable: true},
    fulfillment_voucher: {asset_type: 'emoji', duration_days: 180},
};

Object.entries(stored).forEach(([template, config]) => {
    const spec = SHOP_TEMPLATES[template];
    assert.ok(spec, `no declaration for ${template}`);
    const values = spec.unpack(config);
    // Every declared field must come back from unpack, or the form opens blank
    // and the operator retypes something that was already correct.
    spec.fields.forEach((field) => {
        assert.ok(field in values,
            `${template}: unpack lost ${field}`);
    });
    const packed = spec.pack(values);
    assert.deepStrictEqual(
        Object.keys(packed).sort(), Object.keys(config).sort(),
        `${template}: pack emitted a different field set`);
    Object.keys(config).forEach((key) => {
        assert.strictEqual(String(packed[key]), String(config[key]),
            `${template}: ${key} changed across the round trip`);
    });
});

// A snowflake must survive as a string: 1420070400000000002 is above 2^53, so
// anything that passed it through Number() would come back rounded.
const roleId = '1420070400000000002';
const unpacked = SHOP_TEMPLATES.timed_role.unpack({role_id: Number(roleId) ? 1420070400000000002 : 0, duration_days: 7});
assert.strictEqual(typeof unpacked.role_id, 'string',
    'a role id must leave unpack as a string');

console.log('ok');
