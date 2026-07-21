import unittest
from unittest.mock import patch

from main import _changelog_cache, _format_commit_change, _format_german_date, _humanize_commit_subject, get_changelog_items


class ChangelogTests(unittest.TestCase):
    def test_known_commit_subject_is_humanized(self):
        summary = _humanize_commit_subject("Add automated CI checks")

        self.assertEqual(summary, "Automatische Tests auf GitHub wurden ergänzt.")

    def test_unknown_commit_subject_stays_readable(self):
        summary = _humanize_commit_subject("improve dashboard loading")

        self.assertEqual(summary, "Improve dashboard loading")

    def test_changelog_cache_keeps_enough_items_for_detail_page(self):
        output = "\n".join(
            f"abcde{i}\t21.07.2026 13:4{i}\tchange {i}"
            for i in range(8)
        )
        _changelog_cache["items"] = None
        _changelog_cache["expires_at"] = 0

        with patch("main._run_git_command", return_value=output):
            footer_items = get_changelog_items(limit=3)
            detail_items = get_changelog_items(limit=8)

        self.assertEqual(len(footer_items), 3)
        self.assertEqual(len(detail_items), 8)

    def test_commit_change_contains_user_summary(self):
        change = _format_commit_change("5b3d0a8abcdef", "Add automated CI checks", "2026-07-21T12:34:56Z")

        self.assertEqual(change["sha"], "5b3d0a8")
        self.assertEqual(change["summary"], "Automatische Tests auf GitHub wurden ergänzt.")
        self.assertEqual(change["committed_at"], "21.07.2026")

    def test_recent_changelog_subjects_are_humanized(self):
        self.assertEqual(
            _humanize_commit_subject("Fix changelog page theme"),
            "Die Änderungsseite nutzt jetzt wieder das dunkle KeepUp-Design.",
        )
        self.assertEqual(
            _humanize_commit_subject("Show changelog during updates"),
            "Während eines Updates werden die enthaltenen Änderungen direkt angezeigt.",
        )

    def test_iso_date_is_formatted_for_german_ui(self):
        self.assertEqual(_format_german_date("2026-07-21"), "21.07.2026")
        self.assertEqual(_format_german_date("2026-07-21T12:34:56Z"), "21.07.2026")


if __name__ == "__main__":
    unittest.main()
