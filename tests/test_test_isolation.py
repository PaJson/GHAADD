"""Guards for how the tests are written, so they can never reach a real viewer, mailbox or file of the user.

A test that runs the polling loop reads the settings of the REAL config.json unless it replaces them. Once a user switches
the viewer on there, such a test would start a real sender and talk to the real viewer under the real daemon name (found
when it happened: the suite made a user's viewer show "daemon stopped" for a moment).
Run from the project root: python -m unittest discover -s tests -t .
"""
import os
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))


def _sources() -> dict[str, str]:
    result = {}
    for name in sorted(os.listdir(TESTS)):
        if name.startswith("test_") and name.endswith(".py"):
            with open(os.path.join(TESTS, name), encoding="utf-8") as handle:
                result[name] = handle.read()
    return result


class PollingLoopTestsAreIsolated(unittest.TestCase):
    def test_every_test_module_that_runs_the_polling_loop_replaces_the_viewer_settings(self) -> None:
        offenders = [
            name for name, text in _sources().items()
            if "run_polling_loop(" in text and "get_viewer_settings" not in text
        ]
        self.assertEqual(offenders, [], "these run main.run_polling_loop with the real viewer settings: patch main.get_viewer_settings")

    def test_no_test_builds_a_real_sender_with_a_real_address(self) -> None:
        # A real ViewerPusher is fine in tests only with the fake post function; the default one posts to the network.
        offenders = []
        for name, text in _sources().items():
            if name == os.path.basename(__file__):
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                if "ViewerPusher(" in line and "post=" not in line and "FakeViewerPusher" not in line and "class " not in line:
                    # multi-line constructions pass post= on a later line: look a few lines ahead
                    window = "\n".join(text.splitlines()[line_number - 1:line_number + 6])
                    if "post=" not in window:
                        offenders.append(f"{name}:{line_number}")
        self.assertEqual(offenders, [], "a ViewerPusher without post= would use the network")


if __name__ == "__main__":
    unittest.main()
