import unittest

import main


class DashboardCacheTests(unittest.TestCase):
    def tearDown(self):
        with main._dashboard_cards_cache_lock:
            main._dashboard_cards_cache["html"] = None
            main._dashboard_cards_cache["expires_at"] = 0.0

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


if __name__ == "__main__":
    unittest.main()
