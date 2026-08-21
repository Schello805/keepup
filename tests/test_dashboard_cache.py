import unittest
from types import SimpleNamespace
from unittest.mock import patch

import main


class DashboardCacheTests(unittest.TestCase):
    def tearDown(self):
        with main._dashboard_cards_cache_lock:
            main._dashboard_cards_cache["html"] = None
            main._dashboard_cards_cache["expires_at"] = 0.0
            main._dashboard_cards_cache["generation"] = 0
        with main._dashboard_snapshot_cache_lock:
            main._dashboard_snapshot_cache.update(version=0, expires_at=0.0, payload=None)

    def test_stale_existing_cards_do_not_need_immediate_rebuild(self):
        with main._dashboard_cards_cache_lock:
            main._dashboard_cards_cache["html"] = "<section>cached</section>"
            main._dashboard_cards_cache["expires_at"] = 9999999999.0

        main.mark_dashboard_cards_cache_stale()

        self.assertEqual(main.peek_dashboard_cards_html(), "<section>cached</section>")
        self.assertFalse(main.dashboard_cards_cache_needs_immediate_rebuild())
        self.assertTrue(main.dashboard_cards_cache_is_stale())

    def test_empty_cards_cache_needs_immediate_rebuild(self):
        main.invalidate_dashboard_cards_cache()

        self.assertIsNone(main.peek_dashboard_cards_html())
        self.assertTrue(main.dashboard_cards_cache_needs_immediate_rebuild())

    def test_live_cards_waits_for_stale_cache_refresh(self):
        with main._dashboard_cards_cache_lock:
            main._dashboard_cards_cache["html"] = "<section>stale</section>"
            main._dashboard_cards_cache["expires_at"] = 0.0

        async def refresh_cache(force=False):
            with main._dashboard_cards_cache_lock:
                main._dashboard_cards_cache["html"] = "<section>fresh</section>"
                main._dashboard_cards_cache["expires_at"] = 9999999999.0

        with (
            patch.object(main, "wait_for_dashboard_cards_cache_refresh", side_effect=refresh_cache) as refresh,
            patch.object(main, "get_settings", return_value={"refresh_interval": 30}),
        ):
            response = main.asyncio.run(main.live_cards_partial(None))

        refresh.assert_awaited_once_with(force=False)
        self.assertIn("fresh", response.body.decode())

    def test_completed_monitor_job_marks_cards_cache_stale(self):
        with main._dashboard_cards_cache_lock:
            main._dashboard_cards_cache["html"] = "<section>cached</section>"
            main._dashboard_cards_cache["expires_at"] = 9999999999.0

        main.handle_scheduler_job_executed(
            SimpleNamespace(job_id="monitor-7", retval={"status_changed": True})
        )

        self.assertTrue(main.dashboard_cards_cache_is_stale())

    def test_non_monitor_job_keeps_cards_cache_fresh(self):
        with main._dashboard_cards_cache_lock:
            main._dashboard_cards_cache["html"] = "<section>cached</section>"
            main._dashboard_cards_cache["expires_at"] = 9999999999.0

        main.handle_scheduler_job_executed(SimpleNamespace(job_id="db-cleanup", retval=None))

        self.assertFalse(main.dashboard_cards_cache_is_stale())

    def test_unchanged_monitor_status_keeps_cards_cache_fresh(self):
        with main._dashboard_cards_cache_lock:
            main._dashboard_cards_cache["html"] = "<section>cached</section>"
            main._dashboard_cards_cache["expires_at"] = 9999999999.0

        main.handle_scheduler_job_executed(
            SimpleNamespace(job_id="monitor-7", retval={"status_changed": False})
        )

        self.assertFalse(main.dashboard_cards_cache_is_stale())

    def test_invalidation_during_render_cannot_publish_old_cards(self):
        with main._dashboard_cards_cache_lock:
            main._dashboard_cards_cache["html"] = "<section>previous</section>"
            main._dashboard_cards_cache["expires_at"] = 0.0

        def build_payload_and_invalidate():
            main.mark_dashboard_cards_cache_stale()
            return {"monitors": [], "settings": {}}

        with (
            patch.object(main, "build_dashboard_cards_payload", side_effect=build_payload_and_invalidate),
            patch.object(main, "render_template_content", return_value="<section>outdated</section>"),
        ):
            result = main.get_dashboard_cards_html(force_refresh=True)

        self.assertEqual(result, "<section>previous</section>")
        self.assertEqual(main.peek_dashboard_cards_html(), "<section>previous</section>")
        self.assertTrue(main.dashboard_cards_cache_is_stale())

    def test_cache_invalidation_advances_snapshot_version(self):
        before = main.get_dashboard_snapshot_version()

        main.mark_dashboard_cards_cache_stale()

        self.assertGreater(main.get_dashboard_snapshot_version(), before)

    def test_dashboard_snapshot_renders_top_and_cards_from_same_context(self):
        context = {"summary": {"total": 2}, "monitors": [{"id": 1}, {"id": 2}]}

        def render(name, received):
            self.assertIs(received["summary"], context["summary"])
            self.assertIs(received["monitors"], context["monitors"])
            return received["partial"]

        with (
            patch.object(main, "build_dashboard_context", return_value=context),
            patch.object(main, "render_template_content", side_effect=render),
        ):
            with main._dashboard_snapshot_cache_lock:
                main._dashboard_snapshot_cache.update(version=0, expires_at=0.0, payload=None)
            snapshot = main.build_dashboard_snapshot(SimpleNamespace())

        self.assertEqual(snapshot["top_html"], "top")
        self.assertEqual(snapshot["cards_html"], "cards")
        self.assertEqual(snapshot["summary"], {"total": 2})

    def test_status_wall_uses_confirmed_monitor_statuses(self):
        monitors = [
            {"id": 1, "name": "A", "target": "a", "category": "Web", "enabled": 1, "status": "up", "display_status": "up", "history": [], "last_response_time": 12, "last_checked_at": "now"},
            {"id": 2, "name": "B", "target": "b", "category": "Web", "enabled": 1, "status": "down", "display_status": "down", "history": [], "last_response_time": 30, "last_checked_at": "now"},
        ]
        with patch.object(main, "build_dashboard_cards_payload", return_value={"monitors": monitors, "settings": {}}):
            payload = main.build_status_wall_payload()

        self.assertEqual(payload["summary"]["up"], 1)
        self.assertEqual(payload["summary"]["down"], 1)
        self.assertEqual([item["status"] for item in payload["monitors"]], ["up", "down"])


if __name__ == "__main__":
    unittest.main()
