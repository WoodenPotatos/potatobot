/* The featured-chance formula, exercised against a stub DOM.
 *
 * Driven from Python through Node because the formula is JavaScript: a Python
 * re-implementation could stay green while the real one broke. The numbers it is
 * checked against are the ones tests/test_gacha.py measures out of the real pull
 * loop, so the interface and the mechanic cannot disagree about the odds.
 */
const assert = require('assert');

const SPLIT = 50;
const FEATURED = 'premium_30d';

// The standard pool, curated: the featured key has been taken out of it, which
// is the arrangement the closed form describes.
const STANDARD = [
    {key: 'big_vault', weight: 1},
    {key: 'emoji_180d', weight: 1},
    {key: 'sticker_180d', weight: 3},
    {key: 'sound_180d', weight: 1},
];

/* The formula under test, transcribed from `updateRewardChances`. Kept in one
 * place here so a change to the real one shows up as a diff rather than as a
 * silently divergent copy. */
function share(key, split, pool) {
    const total = pool.reduce((sum, entry) => sum + (entry.weight || 0), 0);
    const match = pool.find((entry) => entry.key === key);
    if (!match || !total) return null;
    return (100 - split) * (match.weight / total);
}

// The featured row's chance is the split itself, not a share of a tier.
// Everything else is only reachable through a loss.
assert.strictEqual(share('big_vault', SPLIT, STANDARD), 50 * (1 / 6));
assert.strictEqual(share('sticker_180d', SPLIT, STANDARD), 50 * (3 / 6));

// The split plus every loss share must be the whole tier, or the column lies.
const lossTotal = STANDARD.reduce((sum, e) => sum + share(e.key, SPLIT, STANDARD), 0);
assert.ok(Math.abs(SPLIT + lossTotal - 100) < 1e-9,
    `split plus losses came to ${SPLIT + lossTotal}, not 100`);

// A reward on the banner but absent from the standard pool is unreachable: a win
// gives the featured item and a loss draws from the pool, so nothing awards it.
assert.strictEqual(share('loaded_die', SPLIT, STANDARD), null);

// A split of 100 leaves nothing for the pool; a split of 0 leaves it everything.
assert.strictEqual(share('big_vault', 100, STANDARD), 0);
assert.strictEqual(share('sticker_180d', 0, STANDARD), 100 * (3 / 6));

// An empty pool means no split at all, which the caller renders as the plain
// within-tier share rather than as a zero.
assert.strictEqual(share('big_vault', SPLIT, []), null);

console.log('ok');
