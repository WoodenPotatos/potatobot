/* Does a creator hand back what the API demands?

   Run by tests/test_managed_messages.py, which extracts MANAGED_KINDS from
   dashboard/script.js and pipes it in ahead of this file with one real GET row
   per kind.

   Two things are under test and both have bitten this codebase before. The POST
   route uses `require_exact_keys`, so a payload missing *or* gaining a key is
   refused wholesale — `pack` must emit all eight every time, including an empty
   `entries` for a kind that has none. And `unpack` then `pack` must reproduce
   what was sent, or opening a creator and pressing Save writes back something
   different from what was there.

   No DOM: the unpack/pack pair is plain data, exactly as the row-editor harness
   is. */

const REQUIRED_KEYS = ['menu_key', 'display_name', 'revision', 'title', 'body',
                       'colour', 'options', 'entries'];

let failures = 0;

function fail(message) {
    console.error(message);
    failures += 1;
}

function payloadFor(kind, row) {
    const spec = MANAGED_KINDS[kind];
    const values = spec.unpack(row);
    return {
        menu_key: values.menu_key,
        display_name: values.display_name,
        revision: row?.revision ?? 0,
        colour: values.colour,
        ...spec.pack(values),
    };
}

for (const [kind, row] of Object.entries(ROWS)) {
    const payload = payloadFor(kind, row);

    const keys = Object.keys(payload).sort();
    const wanted = [...REQUIRED_KEYS].sort();
    if (JSON.stringify(keys) !== JSON.stringify(wanted)) {
        fail(`${kind}: payload keys ${JSON.stringify(keys)} !== ${JSON.stringify(wanted)}`);
    }

    // What was stored has to survive being opened and packed again.
    if (payload.menu_key !== row.menu_key) {
        fail(`${kind}: menu_key ${payload.menu_key} !== ${row.menu_key}`);
    }
    if (payload.display_name !== row.display_name) {
        fail(`${kind}: display_name changed`);
    }
    if (payload.colour !== row.colour) {
        fail(`${kind}: colour ${payload.colour} !== ${row.colour}`);
    }
    if (JSON.stringify(payload.entries) !== JSON.stringify(row.entries || [])) {
        fail(`${kind}: entries ${JSON.stringify(payload.entries)} `
             + `!== ${JSON.stringify(row.entries || [])}`);
    }

    // A role id is a snowflake and must still be a string: a number would have
    // been rounded past 2**53 on the way through.
    for (const entry of payload.entries) {
        if (typeof entry.role_id !== 'string') {
            fail(`${kind}: role_id is a ${typeof entry.role_id}, not a string`);
        }
    }

    if (kind === 'rules' || kind === 'embed') {
        const sent = row.options.sections;
        const back = payload.options.sections;
        if (sent.length !== back.length) {
            fail(`rules: ${back.length} sections packed from ${sent.length}`);
        }
        sent.forEach((section, index) => {
            if ((section.title ?? null) !== back[index].title
                    || section.body !== back[index].body) {
                fail(`rules: section ${index} changed`);
            }
        });
        if (payload.options.image_url !== (row.options.image_url ?? null)) {
            fail(`${kind}: image_url changed`);
        }
        if (kind === 'embed') continue;
        for (const flag of ['accept_button', 'thumbnail']) {
            if (payload.options[flag] !== row.options[flag]) {
                fail(`rules: ${flag} ${payload.options[flag]} !== ${row.options[flag]}`);
            }
        }
        if (payload.options.button_label !== row.options.button_label) {
            fail('rules: button_label changed');
        }
    }
    if ((kind === 'ticket' || kind === 'airlock')
            && payload.options.button_label !== row.options.button_label) {
        fail(`${kind}: button_label ${payload.options.button_label} `
             + `!== ${row.options.button_label}`);
    }
}

if (failures) process.exit(1);
