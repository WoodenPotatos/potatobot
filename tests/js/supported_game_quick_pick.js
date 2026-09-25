// Driven by tests/test_settings_cache.py, which prepends:
//   - the real `matchResourceByName`/`supportedGameOptions` source
//   - `const supportedGames = [...]`
//   - a stub `tr(key)` resolving `dashboard.game_names.<key>` from a fixture
//
// No DOM: both functions are plain data transforms, so this checks that
// directly rather than through a rendered picker.

const failures = [];
const check = (label, condition) => {
    if (!condition) failures.push(label);
};

// supportedGameOptions() translates every key in the server's declared order,
// never inventing or dropping one.
const options = supportedGameOptions();
check('options length matches supportedGames',
      options.length === supportedGames.length);
check('options preserve server order',
      options.every((option, index) => option.key === supportedGames[index]));
check('every option is translated, not the raw key',
      options.every((option) => option.label !== option.key));

// matchResourceByName is case-insensitive, substring-based, and a
// convenience that can come back empty -- never a required match.
const roles = [{id: '1', name: 'Valorant'}, {id: '2', name: 'Minecraft Fan'}];
check('exact match', matchResourceByName(roles, 'Valorant') === '1');
check('case-insensitive match',
      matchResourceByName(roles, 'valorant') === '1');
check('substring match against a longer resource name',
      matchResourceByName(roles, 'Minecraft') === '2');
check('no match returns null, not a guess',
      matchResourceByName(roles, 'Genshin Impact') === null);
check('an empty label matches nothing',
      matchResourceByName(roles, '') === null);
check('an empty resource list matches nothing',
      matchResourceByName([], 'Valorant') === null);

if (failures.length) {
    console.error('FAILED:\n' + failures.join('\n'));
    process.exit(1);
}
process.exit(0);
