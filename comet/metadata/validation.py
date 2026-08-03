"""Small normalization helpers shared by metadata providers."""


def metadata_text(value: str | None) -> str | None:
    if not value or not (value := value.strip()):
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    return value


def metadata_year(value: int | None) -> int | None:
    return value


def episode_coordinate(value: int | str | None) -> int | None:
    return None if value is None else int(value)


def normalize_aliases(value: dict[str, list[str]]) -> dict[str, list[str]]:
    normalized: dict[str, list[str]] = {}
    for raw_scope, raw_titles in value.items():
        scope = raw_scope.lower()
        titles = [title for raw in raw_titles if (title := metadata_text(raw))]
        normalized[scope] = list(dict.fromkeys(titles))
    return normalized
