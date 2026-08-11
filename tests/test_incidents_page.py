import unittest
from unittest.mock import patch

from starlette.requests import Request

import main


class IncidentsPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_incidents_page_renders_full_feed_context(self):
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/incidents",
            "query_string": b"",
            "headers": [],
            "server": ("testserver", 80),
            "scheme": "http",
        }
        request = Request(scope)
        context = {
            "settings": {"app_name": "KeepUp"},
            "feed_items": [],
            "selected_item": None,
            "pagination": {"total_pages": 1},
        }

        with (
            patch("main.build_incidents_context", return_value=context) as context_mock,
            patch("main.build_incidents_shell_context") as shell_mock,
            patch("main.render_template", return_value="ok") as render_mock,
        ):
            response = await main.incidents_page(request)

        self.assertEqual(response, "ok")
        context_mock.assert_called_once_with(request)
        shell_mock.assert_not_called()
        render_mock.assert_called_once_with(request, "incidents.html", context)


if __name__ == "__main__":
    unittest.main()
