import unittest
from unittest.mock import patch

from starlette.requests import Request

import main


class IncidentsPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_incidents_page_renders_fast_shell_context(self):
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
            "monitors": [],
            "incident_feed_url": "/api/incidents/feed",
        }

        with (
            patch("main.build_incidents_context") as context_mock,
            patch("main.build_incidents_shell_context", return_value=context) as shell_mock,
            patch("main.render_template", return_value="ok") as render_mock,
        ):
            response = await main.incidents_page(request)

        self.assertEqual(response, "ok")
        context_mock.assert_not_called()
        shell_mock.assert_called_once_with(request)
        render_mock.assert_called_once_with(request, "incidents.html", context)

    def test_incidents_template_loads_recent_items_before_full_history(self):
        from pathlib import Path

        template = (Path(__file__).parents[1] / "templates" / "incidents.html").read_text(encoding="utf-8")
        self.assertIn("render_incident_feed_loading()", template)
        self.assertIn("quick=1", template)
        self.assertLess(template.index("quickResponse"), template.index("fullResponse"))


if __name__ == "__main__":
    unittest.main()
