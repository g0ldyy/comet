"""Bind parameters for chunked multi-row upserts."""

import re
from collections.abc import Mapping, Sequence

_BIND_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")


def chunk_parameters(
    chunk: Sequence[Mapping[str, object]],
    shared_columns: frozenset[str],
) -> dict[str, object]:
    """Bind batch-invariant columns once and the rest per row.

    SQLAlchemy charges per bind parameter, so a multi-row statement that repeats an
    invariant column for every row pays for it every time. Columns named in
    ``shared_columns`` are bound once under their bare name; the caller's SQL references
    them without an index suffix.

    Raises ``ValueError`` if a supposedly invariant column actually varies, so a future
    caller cannot silently write row zero's value into every row.
    """
    if not chunk:
        raise ValueError("SQL batch chunk must not be empty")
    expected_keys = set(chunk[0])
    if (
        not shared_columns <= expected_keys
        or any(_BIND_KEY.fullmatch(key) is None for key in expected_keys)
        or any(set(row) != expected_keys for row in chunk)
    ):
        raise ValueError("SQL batch rows must have one canonical shape")

    values: dict[str, object] = {key: chunk[0][key] for key in shared_columns}
    for index, row in enumerate(chunk):
        for key in expected_keys:
            value = row[key]
            if key in shared_columns:
                if value != values[key]:
                    raise ValueError(
                        f"batch-invariant column {key} varies within a chunk"
                    )
                continue
            values[f"{key}_{index}"] = value
    return values
