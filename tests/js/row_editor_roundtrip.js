/* Does a shaped-JSON editor report itself clean the moment it opens?

   Run by tests/test_settings_cache.py, which extracts JSON_ROW_SHAPES from
   dashboard/script.js and pipes it in ahead of this file along with the values
   the API would send. Both halves have to agree or the settings form marks
   itself unsaved with nothing touched — and, worse, a row serialised with a
   stand-in id makes the whole patch fail to save.

   The DOM is not needed: what is under test is the shape spec's unpack/pack
   pair and the required-column rule, which are plain data. */

let failures = 0;

function editorOutput(shape, sent) {
    const collected = {};
    for (const [entryKey, entry] of Object.entries(sent)) {
        const unpacked = shape.unpack(entry);
        const columns = {};
        for (const spec of shape.columns) {
            const current = unpacked[spec.name];
            columns[spec.name] = spec.kind === 'role_list'
                ? (current || []).map(String)
                      .sort((a, b) => (BigInt(a) < BigInt(b) ? -1 : 1))
                : (current === undefined || current === null ? '' : String(current));
        }
        const incomplete = !entryKey
            || shape.columns.some((spec) => spec.required && !columns[spec.name]);
        if (incomplete) continue;
        collected[entryKey] = shape.pack(columns);
    }
    return collected;
}

function check(label, shapeName, sent, expected) {
    const shape = JSON_ROW_SHAPES[shapeName];
    if (!shape) { console.log(`  MISSING SHAPE ${shapeName}`); failures++; return; }
    const got = editorOutput(shape, sent);
    const want = expected === undefined ? sent : expected;
    const ok = JSON.stringify(got) === JSON.stringify(want);
    if (!ok) {
        failures++;
        console.log(`  FAIL ${label}`);
        console.log(`     want: ${JSON.stringify(want)}`);
        console.log(`     got : ${JSON.stringify(got)}`);
    }
}

// Every value the API would send has to come back byte-identical.
for (const [key, sent] of Object.entries(wired)) {
    check(`${key} round trip`, SHAPE_OF[key], sent);
}

// An entry the API sends with every field present must survive untouched.
check('role menu with an emoji', 'role_menu',
      {LoL: {id: '1420070400000000001', emoji: '<:lol:1420070400000000002>'}});
check('role menu with no emoji', 'role_menu',
      {LoL: {id: '1420070400000000001', emoji: ''}});

// A faction's managed roles are a set; order must not read as a change.
check('faction managed roles already sorted', 'factions',
      {alpha: {leader_role_id: '1420070400000000001',
               manageable_ids: ['1420070400000000002', '1420070400000000003']}});

// A row missing a required value is left out entirely rather than serialised
// with a stand-in id, which the API rejects for the whole patch.
check('role menu row with no role picked', 'role_menu',
      {Named: {id: '', emoji: ''}}, {});
check('level with no role picked', 'level_roles',
      {5: ''}, {});
check('faction with no leader picked', 'factions',
      {alpha: {leader_role_id: '', manageable_ids: []}}, {});

// And nothing emits the string "0" as an id any more.
const packed = JSON.stringify(Object.values(JSON_ROW_SHAPES).map(
    (shape) => shape.pack({})));
if (packed.includes('"0"')) {
    failures++;
    console.log(`  FAIL a shape still packs the id "0": ${packed}`);
}

console.log(failures ? `  ${failures} failure(s)` : '  all row-editor shapes report clean');
process.exit(failures ? 1 : 0);
