import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import orjson

from comet.api.endpoints.base import ready
from comet.usenet.engine_transport import EngineUnavailable


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def request(*, initialized: bool = True):
        return SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    worker_initialized=initialized,
                )
            )
        )

    async def test_disabled_usenet_does_not_affect_readiness(self):
        database = type(
            "Database",
            (),
            {"fetch_one": AsyncMock(return_value={"version": "current"})},
        )()
        with (
            patch("comet.api.endpoints.base.database", database),
            patch("comet.api.endpoints.base.settings.USENET_ENABLED", False),
        ):
            response = await ready(self.request())

        body = orjson.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["components"]["artifact_storage"], "disabled")
        self.assertEqual(body["components"]["usenet_engine"], "disabled")
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_optional_engine_outage_is_degraded_but_ready(self):
        database = type(
            "Database",
            (),
            {"fetch_one": AsyncMock(return_value={"version": "current"})},
        )()
        engine = type(
            "Engine",
            (),
            {"health": AsyncMock(side_effect=EngineUnavailable("private path"))},
        )()
        with (
            patch("comet.api.endpoints.base.database", database),
            patch("comet.api.endpoints.base.settings.USENET_ENABLED", True),
            patch(
                "comet.api.endpoints.base.settings.USENET_ENGINE_REQUIRED",
                False,
            ),
            patch(
                "comet.api.endpoints.base.settings.USENET_ARTIFACT_DIR",
                "/secret/artifacts",
            ),
            patch(
                "comet.api.endpoints.base.settings.USENET_RUNTIME_DIR",
                "/secret/runtime",
            ),
            patch(
                "comet.api.endpoints.base._artifact_root_ready",
                return_value=True,
            ),
            patch(
                "comet.api.endpoints.base.EngineClient",
                return_value=engine,
            ),
        ):
            response = await ready(self.request())

        body = orjson.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["status"], "degraded")
        self.assertEqual(body["components"]["artifact_storage"], "ready")
        self.assertEqual(body["components"]["usenet_engine"], "degraded")
        self.assertNotIn("secret", response.body.decode())

    async def test_required_engine_outage_fails_readiness(self):
        database = type(
            "Database",
            (),
            {"fetch_one": AsyncMock(return_value={"version": "current"})},
        )()
        engine = type(
            "Engine",
            (),
            {"health": AsyncMock(side_effect=EngineUnavailable("unavailable"))},
        )()
        with (
            patch("comet.api.endpoints.base.database", database),
            patch("comet.api.endpoints.base.settings.USENET_ENABLED", True),
            patch(
                "comet.api.endpoints.base.settings.USENET_ENGINE_REQUIRED",
                True,
            ),
            patch(
                "comet.api.endpoints.base._artifact_root_ready",
                return_value=True,
            ),
            patch(
                "comet.api.endpoints.base.EngineClient",
                return_value=engine,
            ),
        ):
            response = await ready(self.request())

        body = orjson.loads(response.body)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(body["status"], "unready")
        self.assertEqual(
            body["components"]["usenet_engine"],
            "required_unavailable",
        )

    async def test_artifact_storage_is_a_hard_gate(self):
        database = type(
            "Database",
            (),
            {"fetch_one": AsyncMock(return_value={"version": "current"})},
        )()
        engine = type(
            "Engine",
            (),
            {"health": AsyncMock(return_value={"version": 1})},
        )()
        with (
            patch("comet.api.endpoints.base.database", database),
            patch("comet.api.endpoints.base.settings.USENET_ENABLED", True),
            patch(
                "comet.api.endpoints.base.settings.USENET_ENGINE_REQUIRED",
                False,
            ),
            patch(
                "comet.api.endpoints.base._artifact_root_ready",
                return_value=False,
            ),
            patch(
                "comet.api.endpoints.base.EngineClient",
                return_value=engine,
            ),
        ):
            response = await ready(self.request())

        body = orjson.loads(response.body)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(body["components"]["artifact_storage"], "unavailable")
        self.assertEqual(body["components"]["usenet_engine"], "ready")

    async def test_worker_database_and_schema_are_hard_gates(self):
        cases = (
            (False, {"version": "current"}),
            (True, None),
            (True, OSError("database unavailable")),
        )
        for initialized, result in cases:
            with self.subTest(initialized=initialized, result=result):
                fetch = (
                    AsyncMock(side_effect=result)
                    if isinstance(result, Exception)
                    else AsyncMock(return_value=result)
                )
                database = type(
                    "Database",
                    (),
                    {"fetch_one": fetch},
                )()
                with (
                    patch("comet.api.endpoints.base.database", database),
                    patch(
                        "comet.api.endpoints.base.settings.USENET_ENABLED",
                        False,
                    ),
                ):
                    response = await ready(self.request(initialized=initialized))

                self.assertEqual(response.status_code, 503)
                self.assertEqual(
                    orjson.loads(response.body)["status"],
                    "unready",
                )

    async def test_unexpected_database_probe_failure_is_exposed(self):
        database = type(
            "Database",
            (),
            {"fetch_one": AsyncMock(side_effect=RuntimeError("implementation bug"))},
        )()
        with (
            patch("comet.api.endpoints.base.database", database),
            patch("comet.api.endpoints.base.settings.USENET_ENABLED", False),
            self.assertRaisesRegex(RuntimeError, "implementation bug"),
        ):
            await ready(self.request())

    async def test_unexpected_engine_probe_failure_is_exposed(self):
        database = type(
            "Database",
            (),
            {"fetch_one": AsyncMock(return_value={"version": "current"})},
        )()
        engine = type(
            "Engine",
            (),
            {"health": AsyncMock(side_effect=RuntimeError("implementation bug"))},
        )()
        with (
            patch("comet.api.endpoints.base.database", database),
            patch("comet.api.endpoints.base.settings.USENET_ENABLED", True),
            patch(
                "comet.api.endpoints.base._artifact_root_ready",
                return_value=True,
            ),
            patch(
                "comet.api.endpoints.base.EngineClient",
                return_value=engine,
            ),
            self.assertRaisesRegex(RuntimeError, "implementation bug"),
        ):
            await ready(self.request())

    async def test_unexpected_storage_probe_failure_is_exposed(self):
        database = type(
            "Database",
            (),
            {"fetch_one": AsyncMock(return_value={"version": "current"})},
        )()
        engine = type(
            "Engine",
            (),
            {"health": AsyncMock(return_value={"version": 1})},
        )()
        with (
            patch("comet.api.endpoints.base.database", database),
            patch("comet.api.endpoints.base.settings.USENET_ENABLED", True),
            patch(
                "comet.api.endpoints.base._artifact_root_ready",
                side_effect=RuntimeError("implementation bug"),
            ),
            patch(
                "comet.api.endpoints.base.EngineClient",
                return_value=engine,
            ),
            self.assertRaisesRegex(RuntimeError, "implementation bug"),
        ):
            await ready(self.request())
