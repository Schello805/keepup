import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main


class PerformanceBudgetTests(unittest.TestCase):
    def tearDown(self):
        main.invalidate_monitor_detail_cache()

    def test_cached_monitor_details_stay_below_25ms_for_100_reads(self):
        with (
            patch.object(main, "build_monitor_detail_context", return_value={"monitor": {"id": 7}}),
            patch.object(main, "render_template_content", return_value="<section>detail</section>"),
        ):
            main.get_monitor_detail_html(SimpleNamespace(), 7)
            started = time.perf_counter()
            for _ in range(100):
                main.get_monitor_detail_html(SimpleNamespace(), 7)
            elapsed_ms = (time.perf_counter() - started) * 1000

        self.assertLess(elapsed_ms, 25, f"Detail cache took {elapsed_ms:.1f} ms")

    def test_frontend_bootstrap_assets_stay_small_and_local(self):
        root = Path(__file__).resolve().parents[1]
        core = root / "static" / "dashboard-core.js"
        template = (root / "templates" / "index.html").read_text(encoding="utf-8")

        self.assertLess(core.stat().st_size, 12 * 1024)
        self.assertIn('src="/static/dashboard-core.js"', template)
        self.assertNotIn("unpkg.com", template)


if __name__ == "__main__":
    unittest.main()
