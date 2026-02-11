import secrets
import time

from comet.core.database import ON_CONFLICT_DO_NOTHING, OR_IGNORE, database

KODI_SETUP_CODE_TTL_SECONDS = 300
KODI_SETUP_CODE_BYTES = 4
KODI_SETUP_MAX_GENERATION_ATTEMPTS = 8

_INSERT_SETUP_CODE_QUERY = f"""
INSERT {OR_IGNORE} INTO kodi_setup_codes (
    code,
    nonce,
    b64config,
    created_at,
    expires_at,
    consumed_at
) VALUES (
    :code,
    :nonce,
    NULL,
    :created_at,
    :expires_at,
    NULL
)
{ON_CONFLICT_DO_NOTHING}
"""

_SELECT_INSERTED_SETUP_CODE_QUERY = """
SELECT code
FROM kodi_setup_codes
WHERE code = :code
  AND nonce = :nonce
"""

_ASSOCIATE_SETUP_CODE_QUERY = """
UPDATE kodi_setup_codes
SET b64config = :b64config
WHERE code = :code
  AND consumed_at IS NULL
  AND expires_at >= :now
"""

_SELECT_ASSOCIATED_SETUP_CODE_QUERY = """
SELECT code
FROM kodi_setup_codes
WHERE code = :code
  AND b64config = :b64config
  AND consumed_at IS NULL
  AND expires_at >= :now
"""

_SELECT_CONSUMABLE_SETUP_CODE_QUERY = """
SELECT b64config, nonce
FROM kodi_setup_codes
WHERE code = :code
  AND consumed_at IS NULL
  AND expires_at >= :now
"""

_CONSUME_SETUP_CODE_QUERY = """
UPDATE kodi_setup_codes
SET consumed_at = :consumed_at,
    nonce = :consumed_nonce
WHERE code = :code
  AND consumed_at IS NULL
  AND nonce = :current_nonce
  AND b64config = :b64config
"""

_SELECT_CONSUMED_SETUP_CODE_QUERY = """
SELECT code
FROM kodi_setup_codes
WHERE code = :code
  AND nonce = :consumed_nonce
  AND consumed_at IS NOT NULL
"""

_DELETE_SETUP_CODE_QUERY = """
DELETE FROM kodi_setup_codes
WHERE code = :code
"""


async def create_setup_code(ttl_seconds: int = KODI_SETUP_CODE_TTL_SECONDS):
    now = time.time()
    expires_at = now + ttl_seconds

    for _ in range(KODI_SETUP_MAX_GENERATION_ATTEMPTS):
        code = secrets.token_hex(KODI_SETUP_CODE_BYTES)
        nonce = secrets.token_hex(8)

        await database.execute(
            _INSERT_SETUP_CODE_QUERY,
            {
                "code": code,
                "nonce": nonce,
                "created_at": now,
                "expires_at": expires_at,
            },
        )

        inserted_row = await database.fetch_one(
            _SELECT_INSERTED_SETUP_CODE_QUERY,
            {"code": code, "nonce": nonce},
            force_primary=True,
        )
        if inserted_row:
            return code, ttl_seconds

    raise RuntimeError("Unable to generate unique Kodi setup code")


async def associate_setup_code_with_b64config(code: str, b64config: str):
    now = time.time()

    await database.execute(
        _ASSOCIATE_SETUP_CODE_QUERY,
        {"code": code, "b64config": b64config, "now": now},
    )

    associated_row = await database.fetch_one(
        _SELECT_ASSOCIATED_SETUP_CODE_QUERY,
        {"code": code, "b64config": b64config, "now": now},
        force_primary=True,
    )
    return associated_row is not None


async def consume_b64config_for_setup_code(code: str):
    now = time.time()
    consumed_nonce = secrets.token_hex(16)

    async with database.transaction():
        row = await database.fetch_one(
            _SELECT_CONSUMABLE_SETUP_CODE_QUERY,
            {"code": code, "now": now},
            force_primary=True,
        )
        if row is None:
            return None

        b64config = row["b64config"]
        if b64config is None:
            return None

        current_nonce = row["nonce"]
        await database.execute(
            _CONSUME_SETUP_CODE_QUERY,
            {
                "code": code,
                "consumed_at": now,
                "consumed_nonce": consumed_nonce,
                "current_nonce": current_nonce,
                "b64config": b64config,
            },
        )
        consumed_row = await database.fetch_one(
            _SELECT_CONSUMED_SETUP_CODE_QUERY,
            {"code": code, "consumed_nonce": consumed_nonce},
            force_primary=True,
        )
        if consumed_row is None:
            return None

        await database.execute(_DELETE_SETUP_CODE_QUERY, {"code": code})

    return b64config
