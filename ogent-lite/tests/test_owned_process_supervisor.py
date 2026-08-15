from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

from ogent_app.infrastructure.processes import (  # noqa: E402
    SubprocessOwnedProcessSupervisor,
)


class FakeProcess:
    def __init__(self, *, pid: int = 1234, exit_after_wait: bool = True) -> None:
        self.pid = pid
        self.return_code: int | None = None
        self.exit_after_wait = exit_after_wait
        self.wait_timeouts: list[float | None] = []
        self.kill_count = 0

    def poll(self) -> int | None:
        return self.return_code

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if not self.exit_after_wait:
            raise subprocess.TimeoutExpired("fixture", timeout)
        self.return_code = 0
        return 0

    def kill(self) -> None:
        self.kill_count += 1
        self.return_code = -9


class OwnedProcessSupervisorTests(unittest.TestCase):
    def test_windows_termination_targets_exact_owned_pid_and_tree(self) -> None:
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
        process = FakeProcess(pid=4321)
        supervisor = SubprocessOwnedProcessSupervisor(
            platform_name="nt",
            runner=runner,
            create_no_window=99,
        )

        supervisor.terminate(process, grace_seconds=2.5)

        arguments = runner.call_args.args[0]
        self.assertEqual(
            list(arguments),
            ["taskkill", "/PID", "4321", "/T", "/F"],
        )
        self.assertFalse(runner.call_args.kwargs["check"])
        self.assertEqual(runner.call_args.kwargs["creationflags"], 99)
        self.assertEqual(process.wait_timeouts, [2.5])
        self.assertEqual(process.kill_count, 0)

    def test_posix_termination_uses_owned_process_group_then_kills_on_timeout(
        self,
    ) -> None:
        killed_groups: list[tuple[int, int]] = []
        process = FakeProcess(exit_after_wait=False)
        supervisor = SubprocessOwnedProcessSupervisor(
            platform_name="posix",
            get_process_group=lambda pid: pid + 10,
            kill_process_group=lambda group, sig: killed_groups.append((group, sig)),
        )

        supervisor.terminate(process, grace_seconds=0.25)

        self.assertEqual(killed_groups[0][0], process.pid + 10)
        self.assertEqual(process.wait_timeouts, [0.25])
        self.assertEqual(process.kill_count, 1)

    def test_already_exited_or_missing_process_is_ignored(self) -> None:
        runner = mock.Mock()
        supervisor = SubprocessOwnedProcessSupervisor(
            platform_name="nt",
            runner=runner,
        )
        process = FakeProcess()
        process.return_code = 0

        supervisor.terminate(None)
        supervisor.terminate(process)

        runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
