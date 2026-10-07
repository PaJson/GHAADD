"""Options given without the command they belong to are rejected instead of silently doing nothing.

Only argument parsing is exercised. Run from the project root: python -m unittest discover -s tests -t .
"""
import contextlib
import io
import unittest

from modules import cli_commands


def parse(*args: str):
    return cli_commands.parse_cli_args(list(args), "test")


def rejected(*args: str) -> str:
    """The error text printed for `args` (the parser exits with status 2)."""
    err = io.StringIO()
    with contextlib.redirect_stderr(err), self_assert_exit():
        parse(*args)
    return err.getvalue()


@contextlib.contextmanager
def self_assert_exit():
    try:
        yield
    except SystemExit as exc:
        assert exc.code == 2, exc.code
    else:
        raise AssertionError("the arguments were accepted")


class UnusedOptionTests(unittest.TestCase):
    def test_the_queue_report_flag_alone_is_rejected_and_names_the_command_it_needs(self) -> None:
        message = rejected("--queue-report")
        self.assertIn("--queue-report has no effect without --queue-status", message)
        self.assertIn("Nothing was started", message)

    def test_every_modifier_works_with_its_command(self) -> None:
        for args in (
            ("--queue-status", "--queue-report"),
            ("--queue-status", "--queue-report-only", "--queue-limit", "0"),
            ("--queue-status", "--queue-report-csv"),
            ("--queue-status", "--queue-status-filter", "PENDING", "--queue-repo-filter", "x", "--json"),
            ("--lifecycle-log", "--lifecycle-limit", "5", "--lifecycle-type", "WARNING", "--json"),
            ("--purge", "--purge-age", "0", "--purge-type", "WARNING", "--purge-repository", "x"),
            ("--purge-jobs", "--purge-oldest", "3", "--purge-status", "FAILED"),
            ("--install-autostart", "--autostart-mode", "task", "--task-trigger", "manual"),
            ("--create-shortcuts", "--shortcut-dir", "X"),
            ("--remove-shortcuts", "--shortcut-dir", "X"),
            ("--doctor", "--json"),
            ("--perf-report", "--json"),
        ):
            with self.subTest(args=args):
                parse(*args)

    def test_a_modifier_for_a_different_command_is_rejected(self) -> None:
        for args in (
            ("--purge-jobs", "--purge-age", "1", "--purge-type", "WARNING"),  # the type belongs to --purge
            ("--lifecycle-log", "--queue-limit", "5"),
            ("--doctor", "--lifecycle-limit", "5"),
            ("--poll-now", "--shortcut-dir", "X"),
        ):
            with self.subTest(args=args):
                self.assertIn("has no effect", rejected(*args))

    def test_all_problems_are_listed_at_once(self) -> None:
        message = rejected("--queue-all", "--lifecycle-limit", "3")
        self.assertIn("--queue-all", message)
        self.assertIn("--lifecycle-limit", message)

    def test_a_value_of_zero_still_counts_as_given(self) -> None:
        self.assertIn("--queue-limit has no effect", rejected("--queue-limit", "0"))
        self.assertIn("--purge-age has no effect", rejected("--purge-age", "0"))

    def test_plain_runs_and_dry_run_are_untouched(self) -> None:
        for args in ((), ("--poll",), ("--single", "--dry-run"), ("--dry-run",), ("--stop",)):
            with self.subTest(args=args):
                parse(*args)

    def test_the_autostart_defaults_are_still_filled_in(self) -> None:
        parsed = parse("--install-autostart")
        self.assertEqual((parsed.autostart_mode, parsed.task_trigger), ("auto", "logon"))
        parsed = parse("--install-autostart", "--autostart-mode", "runkey")
        self.assertEqual((parsed.autostart_mode, parsed.task_trigger), ("runkey", "logon"))

    def test_every_listed_option_and_command_really_exists(self) -> None:
        parsed = parse()
        for option, commands in cli_commands.MODIFIER_COMMANDS.items():
            self.assertTrue(hasattr(parsed, option), option)
            for command in commands:
                self.assertTrue(hasattr(parsed, command), command)


if __name__ == "__main__":
    unittest.main()
