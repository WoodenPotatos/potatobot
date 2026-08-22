/* Appearance bootstrap.
 *
 * Loaded synchronously in <head> so the stored theme is stamped onto the
 * document before the first paint and the page never flashes the wrong one.
 * The display language lives here too, so the very first /api/locale request
 * already carries it and the interface does not re-render after load.
 *
 * Absence of `data-theme` means "follow the operating system", which the
 * stylesheet handles with a prefers-color-scheme block.
 */
(function () {
    var THEME_KEY = 'potatobot-theme';
    var LANGUAGE_KEY = 'potatobot-language';
    var THEMES = ['system', 'light', 'dark'];

    function read(key) {
        try {
            return window.localStorage.getItem(key);
        } catch (error) {
            // Private browsing modes can deny storage entirely.
            return null;
        }
    }

    function write(key, value) {
        try {
            if (value === null) window.localStorage.removeItem(key);
            else window.localStorage.setItem(key, value);
        } catch (error) {
            // A failed write only costs persistence, not the applied preference.
        }
    }

    function storedTheme() {
        var value = read(THEME_KEY);
        return THEMES.indexOf(value) === -1 ? 'system' : value;
    }

    function applyTheme(mode) {
        if (mode === 'system') delete document.documentElement.dataset.theme;
        else document.documentElement.dataset.theme = mode;
    }

    applyTheme(storedTheme());

    window.potatoTheme = {
        order: THEMES,
        current: storedTheme,
        set: function (mode) {
            if (THEMES.indexOf(mode) === -1) return;
            applyTheme(mode);
            write(THEME_KEY, mode === 'system' ? null : mode);
        },
        next: function () {
            var mode = THEMES[(THEMES.indexOf(storedTheme()) + 1) % THEMES.length];
            this.set(mode);
            return mode;
        }
    };

    window.potatoLanguage = {
        // Null means "use whatever the server considers the instance language".
        current: function () { return read(LANGUAGE_KEY); },
        set: function (language) { write(LANGUAGE_KEY, language || null); }
    };
})();
