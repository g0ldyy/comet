import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from comet.api.endpoints.kodi import (
    AssociateManifestRequest,
    GenerateSetupCodeRequest,
    associate_manifest,
    generate_setup_code,
)


class KodiEndpointContractTests(unittest.IsolatedAsyncioTestCase):
    def test_request_models_ignore_extensions_and_reject_type_coercion(self):
        self.assertEqual(
            AssociateManifestRequest.model_validate(
                {
                    "code": "1234abcd",
                    "manifest_url": "https://comet.test/manifest.json",
                    "legacy": True,
                }
            ).code,
            "1234abcd",
        )
        for model, payload in (
            (GenerateSetupCodeRequest, {"secret_string": [], "legacy": True}),
            (
                AssociateManifestRequest,
                {"code": 12345678, "manifest_url": "https://comet.test/manifest.json"},
            ),
        ):
            with self.subTest(model=model.__name__), self.assertRaises(ValidationError):
                model.model_validate(payload)

    async def test_association_requires_current_request_origin(self):
        request = SimpleNamespace(
            url=SimpleNamespace(scheme="https", netloc="comet.test")
        )
        payload = AssociateManifestRequest(
            code="1234abcd",
            manifest_url="https://attacker.test/manifest.json",
        )

        with self.assertRaises(HTTPException) as caught:
            await associate_manifest(request, payload)

        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("does not match", caught.exception.detail)

    async def test_setup_rejects_invalid_configuration_before_code_creation(self):
        request = SimpleNamespace(
            url=SimpleNamespace(scheme="https", netloc="comet.test")
        )
        payload = GenerateSetupCodeRequest(secret_string="not+a+configuration")

        with (
            patch(
                "comet.api.endpoints.kodi.create_setup_code",
                new=AsyncMock(),
            ) as create,
            self.assertRaises(HTTPException) as caught,
        ):
            await generate_setup_code(request, payload)

        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.detail, "Invalid Comet configuration payload")
        create.assert_not_awaited()

    async def test_current_same_origin_manifest_associates(self):
        request = SimpleNamespace(
            url=SimpleNamespace(scheme="https", netloc="comet.test")
        )
        payload = AssociateManifestRequest(
            code="1234abcd",
            manifest_url="https://comet.test/manifest.json",
        )

        with patch(
            "comet.api.endpoints.kodi.associate_setup_code_with_b64config",
            new=AsyncMock(return_value=True),
        ) as associate:
            response = await associate_manifest(request, payload)

        self.assertEqual(response.status_code, 200)
        associate.assert_awaited_once_with("1234abcd", "")

    async def test_default_https_port_is_the_same_origin(self):
        request = SimpleNamespace(
            url=SimpleNamespace(scheme="https", netloc="comet.test")
        )
        payload = AssociateManifestRequest(
            code="1234abcd",
            manifest_url="https://comet.test:443/manifest.json",
        )

        with patch(
            "comet.api.endpoints.kodi.associate_setup_code_with_b64config",
            new=AsyncMock(return_value=True),
        ):
            response = await associate_manifest(request, payload)

        self.assertEqual(response.status_code, 200)
