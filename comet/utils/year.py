import re

_YEAR_PATTERN = re.compile(r"(?:18|19|20)\d{2}")


def parse_year(value) -> int | None:
    if isinstance(value, int):
        return value

    if not isinstance(value, str):
        return None

    match = _YEAR_PATTERN.search(value)
    if not match:
        return None

    return int(match.group(0))


def parse_year_range(value) -> tuple[int | None, int | None]:
    if isinstance(value, int):
        return value, None

    if not isinstance(value, str):
        return None, None

    years = [int(year) for year in _YEAR_PATTERN.findall(value)]
    if not years:
        return None, None

    start_year = years[0]
    end_year = years[1] if len(years) > 1 else None
    if end_year is not None and end_year < start_year:
        end_year = None

    return start_year, end_year
