from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from codex_usage.application.lock import ApplicationLock, ApplicationLockError


class ApplicationLockTests(unittest.TestCase):
    def test_second_mutating_command_is_rejected_until_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.sqlite"
            with ApplicationLock(state):
                with self.assertRaises(ApplicationLockError):
                    with ApplicationLock(state):
                        self.fail("the second lock must not be acquired")

            with ApplicationLock(state):
                self.assertTrue(state.with_name("state.sqlite.lock").is_file())
