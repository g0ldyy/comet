import unittest
from unittest.mock import patch

from starlette.requests import Request

from comet.api.endpoints import admin, config


def _request(path: str):
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
        }
    )


class TemplateResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_template_responses_use_the_current_starlette_contract(self):
        admin_request = _request("/admin")
        login_response = await admin.admin_root(admin_request, admin_session=None)

        with patch.object(admin.secrets, "compare_digest", return_value=False):
            invalid_login_response = await admin.admin_login(
                admin_request, password="invalid"
            )

        with (
            patch.object(admin, "require_admin_auth"),
            patch.object(
                admin.UpdateManager,
                "get_version_info",
                return_value={},
            ),
        ):
            dashboard_response = await admin.admin_dashboard(
                admin_request, admin_session=None
            )

        configure_request = _request("/configure")
        configure_login_response = config._render_configure_login(
            configure_request, next_url="/configure"
        )
        with patch.object(config, "CONFIGURE_PAGE_PASSWORD_ENABLED", False):
            configure_response = await config.configure(configure_request)

        expected_responses = (
            (login_response, "admin_login.html", admin_request),
            (invalid_login_response, "admin_login.html", admin_request),
            (dashboard_response, "admin_dashboard.html", admin_request),
            (configure_login_response, "admin_login.html", configure_request),
            (configure_response, "index.html", configure_request),
        )
        for response, template_name, request in expected_responses:
            with self.subTest(template=template_name):
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.template.name, template_name)
                self.assertIs(response.context["request"], request)


if __name__ == "__main__":
    unittest.main()
