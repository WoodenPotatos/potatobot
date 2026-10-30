/* The builders' and announcements' markdown preview, case by case against
 * what Discord renders. Emphasis inside bold used to break apart and print its
 * asterisks, and an asterisk next to a space read as emphasis.
 *
 * Argument 1 is the repository root.
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const repo = process.argv[2];
const html = fs.readFileSync(path.join(repo, 'dashboard/index.html'), 'utf8');
const script = fs.readFileSync(path.join(repo, 'dashboard/script.js'), 'utf8');

const dom = new JSDOM(html, {url: 'https://d.test/', runScripts: 'outside-only'});
const { window } = dom;
window.potatoLanguage = {current: () => 'en', set: () => {}};
window.fetch = async () => ({ok: false, status: 500, text: async () => ''});
window.eval(script);

// The rendered tags only, without classes, so a case reads like the markdown.
const render = (text) => window.eval(`renderMarkdownBody(${JSON.stringify(text)}).innerHTML`)
    .replace(/ class="[^"]*"/g, '')
    .replace(/^<div>|<\/div>$/g, '');

const CASES = [
    ['**bold**', '<strong>bold</strong>'],
    ['*italic*', '<em>italic</em>'],
    ['***both***', '<strong><em>both</em></strong>'],
    ['**bold *and italic* inside**', '<strong>bold <em>and italic</em> inside</strong>'],
    ['*it **bold** it*', '<em>it <strong>bold</strong> it</em>'],
    ['**a** and **b**', '<strong>a</strong> and <strong>b</strong>'],
    ['__***x***__', '<u><strong><em>x</em></strong></u>'],
    ['~~*x*~~', '<s><em>x</em></s>'],
    ['2 * 3 * 4', '2 * 3 * 4'],
    ['a ** b', 'a ** b'],
];

for (const [input, expected] of CASES) {
    assert.strictEqual(render(input), expected, `rendering ${JSON.stringify(input)}`);
}
console.log('ok');
process.exit(0);
