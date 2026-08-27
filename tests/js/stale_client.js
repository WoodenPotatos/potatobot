/* A tab left open across a deploy has to say so.
 *
 * The version in the sidebar comes from the *server*, so a page still executing
 * yesterday's script displays today's version number — which is
 * indistinguishable from a fix that did not work, and cost three rounds of
 * "it is still broken" before the cause was found. The bundle's own token is in
 * the `src` it was loaded from, so the comparison is exact.
 *
 * Argument 1 is the repository root, 2 the server's token, 3 the loaded one.
 * Exits non-zero if the notice does not match what the tokens imply.
 */
const assert = require('assert');
const fs = require('fs'), path = require('path');
const { JSDOM } = require('jsdom');
const repo = process.argv[2];
const serverToken = process.argv[3];
const loadedToken = process.argv[4];
let html = fs.readFileSync(path.join(repo, 'dashboard/index.html'), 'utf8');
html = html.replace('src="script.js"', `src="script.js?v=${loadedToken}"`);
const script = fs.readFileSync(path.join(repo, 'dashboard/script.js'), 'utf8');
const locale = JSON.parse(fs.readFileSync(path.join(repo, 'locales/en.json'), 'utf8'));
const dom = new JSDOM(html, {url:'https://d.test/', runScripts:'outside-only'});
const { window } = dom;
window.potatoLanguage = {current: () => 'en', set: () => {}};
const ok = (b) => ({ok:true, status:200, text: async () => JSON.stringify(b)});
window.fetch = async (url) => {
  const u = String(url);
  if (u.includes('/locale')) return ok({language:'en', available:['en'], data:{dashboard: locale.dashboard}});
  if (u.includes('/auth/status')) return ok({logged_in:true,
      user:{id:'42',username:'t',avatar:null}, csrf_token:'tok', is_host:true,
      idle_timeout_seconds:600, version:'2.8.0-alpha.1',
      asset_version: serverToken, guilds:[{id:'1',name:'G'}]});
  if (u.includes('/settings/registry')) return ok({status:'success', data:{settings:{}, features:[], groups:[]}});
  return ok({status:'success', data: []});
};
(async () => {
  window.eval(script);
  const ev = window.document.createEvent('Event'); ev.initEvent('DOMContentLoaded', true, true);
  window.document.dispatchEvent(ev);
  await new Promise(r => setTimeout(r, 500));
  const notice = window.document.getElementById('stale-notice');
  const shown = !notice.classList.contains('hidden');
  // A missing server token means nothing to compare; a missing loaded token
  // means the shell was not stamped, which is local development.
  const expected = Boolean(serverToken) && Boolean(loadedToken)
      && serverToken !== loadedToken;
  assert.strictEqual(shown, expected,
      `loaded=${loadedToken} server=${serverToken}: notice was `
      + `${shown ? 'shown' : 'hidden'}`);
  console.log('ok');
  process.exit(0);
})();
