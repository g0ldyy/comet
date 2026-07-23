import unicodedata


COUNTRY_TO_LANGUAGE = {
    "ad": "fr",
    "ae": "ar",
    "af": "fa",
    "ag": "en",
    "ai": "en",
    "an": "nl",
    "ao": "pt",
    "ar": "es",
    "as": "en",
    "at": "de",
    "au": "en",
    "aw": "nl",
    "ax": "sv",
    "ba": "hr",
    "bb": "en",
    "bd": "bn",
    "be": "nl",
    "bf": "fr",
    "bg": "bg",
    "bh": "ar",
    "bi": "fr",
    "bj": "fr",
    "bm": "en",
    "bn": "ms",
    "bo": "es",
    "br": "pt",
    "bs": "en",
    "bw": "en",
    "bz": "en",
    "ca": "en",
    "cc": "ms",
    "cd": "fr",
    "cf": "fr",
    "cg": "fr",
    "ch": "de",
    "ci": "fr",
    "ck": "en",
    "cl": "es",
    "cm": "fr",
    "cn": "zh",
    "co": "es",
    "cr": "es",
    "cs": "sr",
    "cu": "es",
    "cv": "pt",
    "cx": "ms",
    "cy": "el",
    "cz": "cs",
    "de": "de",
    "dj": "fr",
    "dk": "da",
    "dm": "en",
    "do": "es",
    "dz": "ar",
    "ec": "es",
    "ee": "et",
    "eg": "ar",
    "er": "en",
    "es": "es",
    "et": "en",
    "fi": "fi",
    "fj": "en",
    "fk": "en",
    "fm": "en",
    "fo": "da",
    "fr": "fr",
    "ga": "fr",
    "gb": "en",
    "gd": "en",
    "gf": "fr",
    "gh": "en",
    "gi": "en",
    "gl": "da",
    "gm": "en",
    "gn": "fr",
    "gp": "fr",
    "gq": "es",
    "gr": "el",
    "gt": "es",
    "gu": "en",
    "gw": "pt",
    "gy": "en",
    "hk": "zh",
    "hn": "es",
    "hr": "hr",
    "ht": "fr",
    "hu": "hu",
    "id": "id",
    "ie": "en",
    "il": "he",
    "in": "hi",
    "io": "en",
    "iq": "ar",
    "ir": "fa",
    "it": "it",
    "jm": "en",
    "jo": "ar",
    "jp": "ja",
    "ke": "en",
    "ki": "en",
    "km": "fr",
    "kn": "en",
    "kp": "ko",
    "kr": "ko",
    "kw": "ar",
    "ky": "en",
    "lb": "ar",
    "lc": "en",
    "li": "de",
    "lr": "en",
    "ls": "en",
    "lt": "lt",
    "lu": "fr",
    "lv": "lv",
    "ly": "ar",
    "ma": "ar",
    "mc": "fr",
    "md": "ro",
    "mg": "fr",
    "mh": "en",
    "ml": "fr",
    "mo": "zh",
    "mp": "en",
    "mq": "fr",
    "mr": "ar",
    "ms": "en",
    "mt": "en",
    "mu": "en",
    "mw": "en",
    "mx": "es",
    "my": "ms",
    "mz": "pt",
    "na": "en",
    "nc": "fr",
    "ne": "fr",
    "nf": "en",
    "ng": "en",
    "ni": "es",
    "nl": "nl",
    "no": "no",
    "nr": "en",
    "nu": "en",
    "nz": "en",
    "om": "ar",
    "pa": "es",
    "pe": "es",
    "pf": "fr",
    "pg": "en",
    "ph": "en",
    "pk": "en",
    "pl": "pl",
    "pm": "fr",
    "pn": "en",
    "pr": "es",
    "ps": "ar",
    "pt": "pt",
    "pw": "en",
    "py": "es",
    "qa": "ar",
    "re": "fr",
    "ro": "ro",
    "ru": "ru",
    "rw": "fr",
    "sa": "ar",
    "sb": "en",
    "sc": "en",
    "sd": "ar",
    "se": "sv",
    "sg": "en",
    "sh": "en",
    "si": "hu",
    "sk": "sk",
    "sl": "en",
    "sm": "it",
    "so": "ar",
    "sr": "nl",
    "st": "pt",
    "sv": "es",
    "sy": "ar",
    "sz": "en",
    "tc": "en",
    "td": "fr",
    "tg": "fr",
    "th": "th",
    "tk": "en",
    "tl": "pt",
    "tn": "ar",
    "to": "en",
    "tr": "tr",
    "tt": "en",
    "tw": "zh",
    "ua": "uk",
    "ug": "en",
    "um": "en",
    "us": "en",
    "uy": "es",
    "va": "it",
    "vc": "en",
    "ve": "es",
    "vg": "en",
    "vi": "en",
    "vn": "vi",
    "vu": "en",
    "wf": "fr",
    "ws": "en",
    "ye": "ar",
    "yt": "fr",
    "yu": "sr",
    "za": "en",
    "zm": "en",
    "zw": "en",
}


def alias_language(scope: str) -> str | None:
    for prefix in ("lang:", "original:"):
        if scope.startswith(prefix):
            language = scope[len(prefix) :]
            if len(language) == 2 and language.isascii() and language.isalpha():
                return language
            return None
    return COUNTRY_TO_LANGUAGE.get(scope)


def merge_aliases(*collections: object) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    for aliases in collections:
        if not isinstance(aliases, dict):
            continue
        for scope, titles in aliases.items():
            if not isinstance(scope, str) or not isinstance(titles, list):
                continue
            scope_seen = seen.setdefault(scope, set())
            for title in titles:
                if isinstance(title, str) and title and title not in scope_seen:
                    scope_seen.add(title)
                    merged.setdefault(scope, []).append(title)
    return merged


def _strip_latin_diacritics(value: str) -> str:
    characters = []
    follows_latin_character = False
    for character in unicodedata.normalize("NFD", value):
        if unicodedata.combining(character):
            if not follows_latin_character:
                characters.append(character)
            continue
        characters.append(character)
        follows_latin_character = "LATIN" in unicodedata.name(character, "")
    return unicodedata.normalize("NFC", "".join(characters))


def select_indexer_titles(
    title: str,
    aliases: object,
    languages: list[str],
    *,
    include_canonical: bool = True,
    include_original: bool = True,
) -> tuple[str, ...]:
    """Return the bounded, ordered set of titles requested by the operator."""

    selected = []
    seen = set()

    def append(candidate: object):
        if not isinstance(candidate, str) or not (
            candidate := " ".join(candidate.split())
        ):
            return
        candidate = _strip_latin_diacritics(candidate)
        identity = unicodedata.normalize("NFKC", candidate).casefold()
        if identity in seen:
            return
        seen.add(identity)
        selected.append(candidate)

    if include_canonical:
        append(title)

    if not isinstance(aliases, dict):
        if not selected:
            append(title)
        return tuple(selected)

    if include_original:
        original_count = len(selected)
        for scope, scope_titles in aliases.items():
            if not isinstance(scope, str) or not isinstance(scope_titles, list):
                continue
            normalized_scope = scope.lower()
            if normalized_scope != "original" and not normalized_scope.startswith(
                "original:"
            ):
                continue
            for alias in scope_titles:
                append(alias)
                if len(selected) > original_count:
                    break
            if len(selected) > original_count:
                break

    aliases_by_language: dict[str, list[str]] = {}
    for scope, scope_titles in aliases.items():
        if not isinstance(scope, str) or not isinstance(scope_titles, list):
            continue
        language = alias_language(scope.lower())
        if language is not None:
            aliases_by_language.setdefault(language, []).extend(scope_titles)

    for language in languages:
        language_count = len(selected)
        for alias in aliases_by_language.get(language, ()):
            append(alias)
            if len(selected) > language_count:
                break

    if not selected:
        append(title)

    return tuple(selected)
