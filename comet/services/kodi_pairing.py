import secrets
import time

from comet.core.database import database

KODI_SETUP_CODE_TTL_SECONDS = 300
KODI_SETUP_CODE_BYTES = 4
KODI_SETUP_MAX_GENERATION_ATTEMPTS = 8

_INSERT_SETUP_CODE_QUERY = """
INSERT INTO kodi_setup_codes (
    code,
    nonce,
    config_b64,
    expires_at,
    consumed_at
) VALUES (
    :code,
    :nonce,
    NULL,
    :expires_at,
    NULL
)
ON CONFLICT DO NOTHING
RETURNING code
"""

_ASSOCIATE_SETUP_CODE_QUERY = """
UPDATE kodi_setup_codes
SET config_b64 = :b64config
WHERE code = :code
  AND consumed_at IS NULL
  AND expires_at >= :now
RETURNING code
"""

_SELECT_CONSUMABLE_SETUP_CODE_QUERY = """
SELECT config_b64, nonce
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
  AND config_b64 = :b64config
RETURNING code
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

        inserted_row = await database.fetch_one(
            _INSERT_SETUP_CODE_QUERY,
            {
                "code": code,
                "nonce": nonce,
                "expires_at": expires_at,
            },
            force_primary=True,
        )
        if inserted_row:
            return code, ttl_seconds

    raise RuntimeError("Unable to generate unique Kodi setup code")


async def associate_setup_code_with_b64config(code: str, b64config: str):
    now = time.time()

    associated_row = await database.fetch_one(
        _ASSOCIATE_SETUP_CODE_QUERY,
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

        b64config = row["config_b64"]
        if b64config is None:
            return None

        current_nonce = row["nonce"]
        consumed_row = await database.fetch_one(
            _CONSUME_SETUP_CODE_QUERY,
            {
                "code": code,
                "consumed_at": now,
                "consumed_nonce": consumed_nonce,
                "current_nonce": current_nonce,
                "b64config": b64config,
            },
            force_primary=True,
        )
        if consumed_row is None:
            return None

        await database.execute(_DELETE_SETUP_CODE_QUERY, {"code": code})

    return b64config
