import secrets
import time

from comet.core.database import database, fetch_flag

KODI_SETUP_CODE_TTL_SECONDS = 300
KODI_SETUP_CODE_BYTES = 4
KODI_SETUP_MAX_GENERATION_ATTEMPTS = 8

_INSERT_SETUP_CODE_QUERY = """
INSERT INTO kodi_setup_codes (
    code,
    config_b64,
    expires_at
) VALUES (
    :code,
    NULL,
    :expires_at
)
ON CONFLICT DO NOTHING
RETURNING code
"""

_ASSOCIATE_SETUP_CODE_QUERY = """
UPDATE kodi_setup_codes
SET config_b64 = :b64config
WHERE code = :code
  AND expires_at >= :now
RETURNING code
"""

_CONSUME_SETUP_CODE_QUERY = """
DELETE FROM kodi_setup_codes
WHERE code = :code
  AND config_b64 IS NOT NULL
  AND expires_at >= :now
RETURNING config_b64
"""


async def create_setup_code(ttl_seconds: int = KODI_SETUP_CODE_TTL_SECONDS):
    now = time.time()
    expires_at = now + ttl_seconds

    for _ in range(KODI_SETUP_MAX_GENERATION_ATTEMPTS):
        code = secrets.token_hex(KODI_SETUP_CODE_BYTES)

        inserted = await fetch_flag(
            _INSERT_SETUP_CODE_QUERY,
            {
                "code": code,
                "expires_at": expires_at,
            },
            force_primary=True,
        )
        if inserted:
            return code, ttl_seconds

    raise RuntimeError("Unable to generate unique Kodi setup code")


async def associate_setup_code_with_b64config(code: str, b64config: str):
    now = time.time()

    return await fetch_flag(
        _ASSOCIATE_SETUP_CODE_QUERY,
        {"code": code, "b64config": b64config, "now": now},
        force_primary=True,
    )


async def consume_b64config_for_setup_code(code: str):
    now = time.time()
    row = await database.fetch_one(
        _CONSUME_SETUP_CODE_QUERY,
        {"code": code, "now": now},
        force_primary=True,
    )
    return None if row is None else row["config_b64"]
