"""Wordlist loading and bypass-resistant matching for the AutoMod language filter.

Both the banned words and the message text are reduced to a canonical form before
comparing, so disguised versions such as "he11o", "h.e.l.l.o", "h e l l o",
"hhheeelllooo" or "hеllo" (Cyrillic е) all match a banned "hello".
"""

import os
import re
import unicodedata

# Characters commonly swapped in to dodge filters, mapped onto the letter they imitate.
# "l" is folded into "i" because 1, ! and | are used for both.
LEET_MAP = {
    '0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '7': 't', '8': 'b', '9': 'g',
    '@': 'a', '$': 's', '!': 'i', '|': 'i', '+': 't', '€': 'e', 'l': 'i',
}
LEET_SYMBOLS = {c for c in LEET_MAP if not c.isalnum()}

# Lookalike letters from other alphabets that NFKD normalisation does not fold.
HOMOGLYPHS = str.maketrans({
    'а': 'a', 'в': 'b', 'е': 'e', 'ё': 'e', 'к': 'k', 'м': 'm', 'н': 'h', 'о': 'o',
    'р': 'p', 'с': 'c', 'т': 't', 'у': 'y', 'х': 'x', 'і': 'i', 'ї': 'i', 'ј': 'j',
    'ѕ': 's', 'ԁ': 'd', 'ɡ': 'g', 'α': 'a', 'β': 'b', 'ε': 'e', 'η': 'n', 'ι': 'i',
    'κ': 'k', 'ν': 'v', 'ο': 'o', 'ρ': 'p', 'τ': 't', 'υ': 'u', 'χ': 'x', 'ω': 'w',
})

# Regex character classes used when building Discord AutoMod patterns. Mirrors LEET_MAP.
NATIVE_CLASSES = {
    'a': '[a4@]', 'b': '[b8]', 'e': '[e3€]', 'g': '[g9]', 'i': '[il1!|]',
    'o': '[o0]', 's': '[s5$]', 't': '[t7+]',
}
NATIVE_START = r'(?:^|\W)'
NATIVE_END = r'(?:\W|$)'

# Discord AutoMod keyword rule limits.
NATIVE_MAX_KEYWORDS = 1000
NATIVE_MAX_KEYWORD_LENGTH = 60
NATIVE_MAX_REGEXES = 10
NATIVE_MAX_REGEX_LENGTH = 260
NATIVE_MAX_ALLOW = 100

EXACT, PREFIX, SUFFIX, CONTAINS = 'exact', 'prefix', 'suffix', 'contains'


def normalise_text(text: str) -> str:
    """Lowercase, strip accents, invisible characters and lookalike letters."""
    text = unicodedata.normalize('NFKD', text)
    text = ''.join(c for c in text if unicodedata.category(c) not in ('Mn', 'Cf'))
    return text.casefold().translate(HOMOGLYPHS)


def canonicalise(word: str) -> str:
    """Map leetspeak characters onto letters and drop anything left that isn't alphanumeric."""
    return ''.join(c for c in (LEET_MAP.get(c, c) for c in word) if c.isalnum())


def to_runs(word: str) -> list[tuple[str, int]]:
    """'hello' -> [('h', 1), ('e', 1), ('l', 2), ('o', 1)]"""
    runs = []
    for c in word:
        if runs and runs[-1][0] == c:
            runs[-1] = (c, runs[-1][1] + 1)
        else:
            runs.append((c, 1))
    return runs


def _runs_match_at(pattern, token, offset) -> bool:
    return all(
        token[offset + i][0] == c and token[offset + i][1] >= n
        for i, (c, n) in enumerate(pattern)
    )


def runs_match(pattern, token, mode) -> bool:
    """Compare run-length encoded words. Extra repeated letters in the token are allowed."""
    if not pattern or len(pattern) > len(token):
        return False
    if mode == EXACT:
        return len(pattern) == len(token) and _runs_match_at(pattern, token, 0)
    if mode == PREFIX:
        return _runs_match_at(pattern, token, 0)
    if mode == SUFFIX:
        return _runs_match_at(pattern, token, len(token) - len(pattern))
    return any(_runs_match_at(pattern, token, i) for i in range(len(token) - len(pattern) + 1))


def _strip_edge_symbols(word: str) -> str:
    start, end = 0, len(word)
    while start < end and word[start] in LEET_SYMBOLS:
        start += 1
    while end > start and word[end - 1] in LEET_SYMBOLS:
        end -= 1
    return word[start:end]


_SEPARATOR_RE = re.compile(r'[^\w\s' + re.escape(''.join(LEET_SYMBOLS)) + r']+|_+')


def _token_forms(raw: str) -> set[str]:
    """All canonical readings of one whitespace-separated token."""
    parts = [p for p in _SEPARATOR_RE.split(raw) if p]
    candidates = set(parts)
    if len(parts) > 1:
        candidates.add(''.join(parts))  # "h.e.l.l.o" -> "hello"
    forms = set()
    for candidate in candidates:
        # Leet symbols at the edges are usually real punctuation ("hello!"), but not always ("$hit").
        for variant in (candidate, _strip_edge_symbols(candidate)):
            form = canonicalise(variant)
            if form:
                forms.add(form)
    return forms


class _Pattern:
    __slots__ = ('entry', 'mode', 'words', 'runs')

    def __init__(self, entry: str):
        self.entry = entry
        text = entry
        starts, ends = text.startswith('*'), text.endswith('*')
        text = text.strip('*').strip()
        if starts and ends:
            self.mode = CONTAINS
        elif ends:
            self.mode = PREFIX
        elif starts:
            self.mode = SUFFIX
        else:
            self.mode = EXACT
        self.words = [canonicalise(w) for w in normalise_text(text).split()]
        self.words = [w for w in self.words if w]
        self.runs = [to_runs(w) for w in self.words]

    def word_mode(self, index: int) -> str:
        """Wildcards only loosen the outer edges of a phrase."""
        if len(self.words) == 1:
            return self.mode
        first, last = index == 0, index == len(self.words) - 1
        if first and self.mode in (SUFFIX, CONTAINS):
            return SUFFIX
        if last and self.mode in (PREFIX, CONTAINS):
            return PREFIX
        return EXACT

    def native_regex(self) -> str:
        words = []
        for word in self.words:
            parts = []
            for c, n in to_runs(word):
                cls = NATIVE_CLASSES.get(c, re.escape(c))
                parts.append(cls + ('+' if n == 1 else f'{{{n},}}'))
            words.append(''.join(parts))
        return r'\s+'.join(words)


class WordFilter:
    """A wordlist file that reloads itself when edited.

    File syntax (one entry per line, # for comments):
        word      whole word only, including disguised versions
        word*     words starting with "word"
        *word     words ending with "word"
        *word*    "word" anywhere, even inside other words
        !word     never flag this word (for false positives caused by wildcards)
    """

    def __init__(self, path: str):
        self.path = path
        self._mtime = None
        self.patterns: list[_Pattern] = []
        self.allowed: list[list[tuple[str, int]]] = []
        self.raw_entries: list[str] = []
        self.raw_allowed: list[str] = []
        self.reload_if_changed()

    def __len__(self):
        return len(self.patterns)

    def reload_if_changed(self) -> bool:
        """Reload the file if it changed since the last load. Returns True if it did."""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = None
        if mtime == self._mtime:
            return False
        self._mtime = mtime
        self._load()
        return True

    def _load(self):
        entries, allowed = [], []
        if self._mtime is not None:
            try:
                with open(self.path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'):
                            continue
                        if line.startswith('!'):
                            if line[1:].strip():
                                allowed.append(line[1:].strip())
                        elif line.strip('*').strip():
                            entries.append(line)
            except (IOError, UnicodeDecodeError) as e:
                print(f"[AutoMod] Could not read wordlist {self.path}: {e}")
        self.raw_entries = list(dict.fromkeys(entries))
        self.raw_allowed = list(dict.fromkeys(allowed))
        self.patterns = [p for p in map(_Pattern, self.raw_entries) if p.words]
        self.allowed = [to_runs(canonicalise(normalise_text(w))) for w in self.raw_allowed]
        print(f"[AutoMod] Loaded {len(self.patterns)} banned word(s) and {len(self.allowed)} allowed word(s)")

    def _is_allowed(self, forms) -> bool:
        return any(runs_match(a, to_runs(f), EXACT) for a in self.allowed for f in forms)

    def find(self, text: str) -> tuple[str, str] | None:
        """Return (banned entry, offending text) for the first match in text, or None."""
        if not self.patterns or not text:
            return None

        raw_tokens = normalise_text(text).split()
        positions = [(raw, _token_forms(raw)) for raw in raw_tokens]

        # Letters spaced out to dodge the filter: "h e l l o" becomes one extra token.
        single = [len(forms) == 1 and len(next(iter(forms))) == 1 for _, forms in positions]
        i = 0
        while i < len(single):
            if single[i]:
                j = i
                while j < len(single) and single[j]:
                    j += 1
                if j - i >= 3:
                    raw = ' '.join(r for r, _ in positions[i:j])
                    joined = ''.join(next(iter(f)) for _, f in positions[i:j])
                    positions.append((raw, {joined}))
                i = j
            else:
                i += 1

        token_runs = [
            (raw, [(form, to_runs(form)) for form in forms])
            for raw, forms in positions
            if not (self.allowed and self._is_allowed(forms))
        ]

        for pattern in self.patterns:
            n = len(pattern.runs)
            for start in range(len(token_runs) - n + 1):
                if all(
                    any(runs_match(pattern.runs[k], runs, pattern.word_mode(k)) for _, runs in token_runs[start + k][1])
                    for k in range(n)
                ):
                    offending = ' '.join(raw for raw, _ in token_runs[start:start + n])
                    return pattern.entry, offending
        return None

    def native_rule_data(self) -> tuple[list[str], list[str], list[str]]:
        """Build (keywords, regex patterns, allow list) for a Discord AutoMod keyword rule.

        Every entry goes in as a plain keyword. Leetspeak-aware regexes are packed into
        Discord's 10 x 260 character budget in file order; entries that don't fit are still
        covered by the plain keyword and by the bot-side check.
        """
        keywords = [e for e in self.raw_entries if len(e) <= NATIVE_MAX_KEYWORD_LENGTH][:NATIVE_MAX_KEYWORDS]
        allow = [w for w in self.raw_allowed if len(w) <= NATIVE_MAX_KEYWORD_LENGTH][:NATIVE_MAX_ALLOW]

        groups = {EXACT: [], PREFIX: [], SUFFIX: [], CONTAINS: []}
        for pattern in self.patterns:
            groups[pattern.mode].append(pattern.native_regex())

        wrappers = {
            EXACT: (NATIVE_START, NATIVE_END),
            PREFIX: (NATIVE_START, ''),
            SUFFIX: ('', NATIVE_END),
            CONTAINS: ('', ''),
        }
        regexes = []
        for mode, bodies in groups.items():
            before, after = wrappers[mode]
            current = []
            for body in bodies:
                candidate = '(?i)' + before + '(?:' + '|'.join(current + [body]) + ')' + after
                if len(candidate) <= NATIVE_MAX_REGEX_LENGTH:
                    current.append(body)
                    continue
                if current:
                    regexes.append('(?i)' + before + '(?:' + '|'.join(current) + ')' + after)
                single = '(?i)' + before + '(?:' + body + ')' + after
                current = [body] if len(single) <= NATIVE_MAX_REGEX_LENGTH else []
            if current:
                regexes.append('(?i)' + before + '(?:' + '|'.join(current) + ')' + after)

        return keywords, regexes[:NATIVE_MAX_REGEXES], allow
