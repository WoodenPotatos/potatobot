/* The message preview must resolve <#id>/<@&id> mentions, leave anything it
 * cannot resolve exactly as written -- never guessed, never markup -- and
 * (2026-09-25) render Discord's actual markdown token set through
 * `renderMarkdownBody`, built from `element()`/text nodes only.
 *
 * Run by tests/test_managed_messages.py, which extracts `element` and the
 * whole markdown-rendering block from dashboard/script.js and prepends them
 * here along with a `resources` fixture, in the same "no full app boot
 * needed" style the other roundtrip fixtures use. A hand-rolled document
 * stands in for a real one -- everything under test only calls
 * createElement/createTextNode, appendChild and textContent, so pulling in
 * jsdom for that alone would only add a module-resolution dependency this
 * temp-file-driven harness does not otherwise need. `element()`'s own
 * `node.textContent = text` shortcut is why the two spots that need a
 * *literal*, not-further-parsed string (inline code, a code block's
 * contents) build their text node explicitly instead of through it: this
 * FakeElement only defines a `textContent` getter, matching real DOM's
 * childNodes-driven read but not its setter, so relying on the shortcut
 * here would silently no-op rather than throw.
 */
const assert = require('assert');

class TextNode {
    constructor(text) {
        this.nodeType = 3; // Node.TEXT_NODE
        this.textContent = text;
    }
}

class FakeElement {
    constructor() {
        this.className = '';
        this.childNodes = [];
    }

    appendChild(node) {
        this.childNodes.push(node);
        return node;
    }

    get textContent() {
        return this.childNodes.map((child) => child.textContent).join('');
    }
}

const document = {
    createElement: () => new FakeElement(),
    createTextNode: (text) => new TextNode(text),
};

function classesOf(node, found = new Set()) {
    if (node.className) found.add(node.className);
    for (const child of node.childNodes || []) classesOf(child, found);
    return found;
}

// --- mentions: resolved and unresolved ------------------------------------

const mentions = renderMarkdownBody(
    'See <#1420070400000000010>, ping <@&1420070400000000011>, '
    + 'and an unknown <#9999999999999999999> mention.'
);
assert.strictEqual(mentions.className, 'preview-embed-body');
assert.strictEqual(
    mentions.textContent,
    'See #general, ping @Mods, and an unknown <#9999999999999999999> mention.'
);
console.log('ok resolved and unresolved mentions render as literal text');

// --- emphasis, nesting, code, spoiler, strike, underline ------------------

const emphasis = renderMarkdownBody(
    'plain **bold** *italic* ***both*** __under__ ~~gone~~ `code *not italic*` ||hidden||'
);
assert.strictEqual(emphasis.textContent,
    'plain bold italic both under gone code *not italic* hidden');
const emphasisClasses = classesOf(emphasis);
for (const expected of ['preview-strong', 'preview-em', 'preview-underline',
                         'preview-strike', 'preview-inline-code', 'preview-spoiler']) {
    assert.ok(emphasisClasses.has(expected), `missing ${expected} in ${[...emphasisClasses]}`);
}
console.log('ok bold/italic/bold-italic/underline/strike/code/spoiler all render');

// A code span must suppress markdown inside it, not just leave the visible
// text looking right by coincidence -- so this one has nothing else in the
// input that could independently produce a `preview-strong` element.
const onlyCode = renderMarkdownBody('`only **not bold** here`');
assert.strictEqual(onlyCode.textContent, 'only **not bold** here');
assert.ok(!classesOf(onlyCode).has('preview-strong'),
    'a "**" run inside a code span must never become bold');
console.log('ok a code span is not further parsed for markdown');

// --- masked links -----------------------------------------------------

const link = renderMarkdownBody('read the [docs](https://example.test/path) now');
assert.strictEqual(link.textContent, 'read the docs now');
assert.ok(classesOf(link).has('preview-link'), 'the link did not render');
const nonHttp = renderMarkdownBody('[click](javascript:alert(1))');
assert.strictEqual(nonHttp.textContent, '[click](javascript:alert(1))',
    'a non-http(s) scheme must stay literal, never a clickable link');
console.log('ok masked links resolve for http(s) only, and stay literal otherwise');

// --- headings, quotes, lists -----------------------------------------------

const blocks = renderMarkdownBody(
    '# Title\n## Sub\nplain line\n> quoted one\n> quoted two\n- item one\n- item two\n1. first'
);
const blockClasses = classesOf(blocks);
for (const expected of ['preview-heading-1', 'preview-heading-2', 'preview-line',
                         'preview-quote', 'preview-quote-line', 'preview-list',
                         'preview-list-item']) {
    assert.ok(blockClasses.has(expected), `missing ${expected}`);
}
// Two "- " lines must merge into one list, not two separate ones.
const lists = blocks.childNodes.filter((node) => node.className === 'preview-list');
assert.strictEqual(lists.length, 2, // the bullet list and the numbered list
    `expected exactly two list elements (bullet + numbered), got ${lists.length}`);
const bulletList = lists[0];
assert.strictEqual(bulletList.childNodes.length, 2,
    'two consecutive "- " lines must merge into one list, not two');
console.log('ok headings, quotes and lists render and consecutive list items merge');

// --- fenced code blocks: never interpreted as markdown or as block lines --

const fenced = renderMarkdownBody('before\n```\n# not a heading\n**not bold**\n```\nafter');
assert.ok(classesOf(fenced).has('preview-code-block'), 'the fenced block did not render');
assert.ok(fenced.textContent.includes('# not a heading'),
    'a fenced block\'s content must survive literally, including its leading #');
assert.ok(!classesOf(fenced).has('preview-heading-1'),
    'a "#" line inside a fenced code block must never become a heading');
assert.ok(!classesOf(fenced).has('preview-strong'),
    'a "**" run inside a fenced code block must never become bold');
console.log('ok a fenced code block is never interpreted as markdown');

// --- timestamps -------------------------------------------------------

const timestamp = renderMarkdownBody('meet at <t:1735689600:d>');
assert.ok(!timestamp.textContent.includes('<t:'),
    'a well-formed timestamp token must be replaced, not left literal');
console.log('ok a timestamp token renders as a formatted date rather than the raw token');

// --- a throw inside the renderer must not escape ---------------------------

// `undefined` has no `.length`, so `renderMarkdownBodyUnsafe` throws on it --
// exactly the case `renderMarkdownBody`'s try/catch exists for, matching
// `tr()`'s "must not throw" rule for a missing locale key.
const brokenInput = renderMarkdownBody(undefined);
assert.ok(brokenInput, 'renderMarkdownBody must return a node rather than throw');
console.log('ok an input that would throw inside the renderer is caught, not escaped');
