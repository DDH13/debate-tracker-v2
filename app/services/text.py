"""Text and name normalization helpers, ported from the Java app's `StringUtil` and
`InstitutionService`. Pure functions only: nothing here touches the database."""

import html
import re
import unicodedata

_TAG_RE = re.compile(r"<[^>]*>")
_WHITESPACE_RE = re.compile(r"\s+")
_SPACE_CHARS = {
    " ": " ",  # NBSP
    " ": " ",  # figure space
    " ": " ",  # narrow NBSP
}
_PUNCTUATION_MAP = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
    }
)
_NAME_PREFIXES = {"de", "la", "van", "von"}
_LOWERCASE_PARTICLES = {"la", "van", "von"}
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")


def normalize_text(raw: str | None) -> str:
    """Strip HTML tags, unescape entities, NFKC-normalize, map exotic spaces/quotes/dashes to
    ASCII, collapse whitespace and trim."""
    if not raw:
        return ""
    text = _TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    for char, replacement in _SPACE_CHARS.items():
        text = text.replace(char, replacement)
    text = text.translate(_PUNCTUATION_MAP)
    return _WHITESPACE_RE.sub(" ", text).strip()


def name_key(name: str | None) -> str:
    """Case-insensitive identity key for a person's name."""
    return normalize_text(name).casefold()


def split_name(name: str | None) -> tuple[str | None, str | None]:
    """Heuristic first/last split, used only when the source supplies no `last_name`.

    First token is the first name; the last name is the last token, or a prefix
    (`de|la|van|von`) plus the token after it. A single token has no last name."""
    tokens = [t for t in re.split(r"[\s.]+", normalize_text(name)) if t]
    if not tokens:
        return None, None
    first = tokens[0]
    if len(tokens) == 1:
        return first, None
    last = tokens[-1]
    if len(tokens) > 2 and tokens[-2].lower() in _NAME_PREFIXES:
        last = f"{tokens[-2]} {tokens[-1]}"
    return first, last


def tidy_case(name: str) -> str:
    """Title-case a name only when it is entirely lower- or upper-case, keeping `la/van/von`
    lowercase. Mixed-case input (e.g. "McDonald") is left untouched."""
    if not name or not (name.islower() or name.isupper()):
        return name
    words = []
    for index, word in enumerate(name.split(" ")):
        lowered = word.lower()
        if index > 0 and lowered in _LOWERCASE_PARTICLES:
            words.append(lowered)
        else:
            words.append("-".join(part.capitalize() for part in lowered.split("-")))
    return " ".join(words)


def institution_key(name: str | None) -> str:
    """Lowercase and drop everything except `[a-z0-9]`, so "St. Peter's College" and
    "St Peters College" share a key."""
    return _NON_ALNUM_RE.sub("", normalize_text(name).lower())


def jaro_winkler(a: str, b: str) -> float:
    """Jaro-Winkler similarity in [0, 1] with the standard 0.1 prefix scale and 4-char
    prefix cap, matching Apache Commons Text."""
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0

    match_window = max(max(len(a), len(b)) // 2 - 1, 0)
    a_matched = [False] * len(a)
    b_matched = [False] * len(b)
    matches = 0
    for i, char in enumerate(a):
        low = max(0, i - match_window)
        high = min(len(b), i + match_window + 1)
        for j in range(low, high):
            if not b_matched[j] and b[j] == char:
                a_matched[i] = b_matched[j] = True
                matches += 1
                break
    if matches == 0:
        return 0.0

    transpositions = 0
    j = 0
    for i, char in enumerate(a):
        if a_matched[i]:
            while not b_matched[j]:
                j += 1
            if char != b[j]:
                transpositions += 1
            j += 1
    transpositions //= 2

    jaro = (matches / len(a) + matches / len(b) + (matches - transpositions) / matches) / 3
    prefix = 0
    for x, y in zip(a[:4], b[:4]):
        if x != y:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1 - jaro)
