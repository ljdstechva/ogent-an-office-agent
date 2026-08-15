from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

import ogent
from ogent_app.application.provider_execution import execute_provider
from ogent_app.infrastructure.fault_injection import (
    FaultInjector,
    FaultPoint,
    InjectedFault,
)
from ogent_app.infrastructure.officecli import (
    OfficeCliExecutionError,
    OfficeCliExecutor,
)
from ogent_app.infrastructure.sqlite import SqliteDatabase
from ogent_app.infrastructure.storage import (
    StorageQuotaError,
    StorageResourceManager,
)
from ogent_app.settings import FeatureFlags, ResourceQuotas, SettingsError
from ogent_preview_sync import PreviewRunBaseline


class FaultInjectorTests(unittest.TestCase):
    def test_points_require_the_explicit_feature_flag(self) -> None:
        with self.assertRaises(SettingsError):
            FaultInjector.load(
                FeatureFlags(fault_injection=False),
                {"OGENT_FAULT_POINTS": "provider_crash"},
            )

    def test_fault_is_one_shot(self) -> None:
        injector = FaultInjector({FaultPoint.PROVIDER_CRASH})
        with self.assertRaises(InjectedFault):
            injector.trigger(FaultPoint.PROVIDER_CRASH)
        injector.trigger(FaultPoint.PROVIDER_CRASH)
        self.assertEqual(injector.pending(), ())

    def test_provider_crash_trips_before_provider_process_launch(self) -> None:
        runtime = types.SimpleNamespace(
            FAULT_INJECTOR=FaultInjector({FaultPoint.PROVIDER_CRASH})
        )
        with self.assertRaises(InjectedFault):
            execute_provider(
                runtime,
                session=object(),
                provider="codex",
                prompt="test",
                working_directory=Path.cwd(),
                model="test",
                effort="automatic",
                run_id="a" * 32,
                image_paths=[],
                sandbox="read-only",
                writable_directories=[],
                document=None,
                references=[],
                timing=object(),
                run_contract=object(),
                audit_log_path=None,
                capability=None,
                initial_package_sha256=None,
                run_root=None,
                conversation_generation=1,
            )

    def test_officecli_crash_and_word_lock_do_not_call_runner(self) -> None:
        calls: list[list[str]] = []

        def runner(command: list[str], **_: object) -> object:
            calls.append(command)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        crash = OfficeCliExecutor(
            executable=Path(sys.executable),
            runner=runner,
            fault_injector=FaultInjector({FaultPoint.OFFICECLI_CRASH}),
        )
        with self.assertRaises(OfficeCliExecutionError):
            crash.execute(["view", "document.docx", "text"])

        locked = OfficeCliExecutor(
            executable=Path(sys.executable),
            runner=runner,
            fault_injector=FaultInjector({FaultPoint.WORD_LOCK}),
        )
        with self.assertRaisesRegex(OfficeCliExecutionError, "locked"):
            locked.execute(["set", "document.docx", "/body/p[1]"])
        self.assertEqual(calls, [])

    def test_database_lock_and_disk_full_trip_at_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SqliteDatabase(root / "state.sqlite3")
            database.initialize()
            database.fault_injector = FaultInjector({FaultPoint.DATABASE_LOCK})
            with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                database.connect()

            manager = StorageResourceManager(
                root / "storage",
                ResourceQuotas(),
                disk_usage=lambda _: types.SimpleNamespace(free=10**12),
                fault_injector=FaultInjector({FaultPoint.DISK_FULL}),
            )
            with self.assertRaises(StorageQuotaError):
                manager.forecast(1)

    def test_preview_mismatch_forces_degraded_confirmation(self) -> None:
        original = ogent.FAULT_INJECTOR
        session = types.SimpleNamespace(
            lock=threading.RLock(),
            closed=False,
            conversation_generation=1,
            preview_update_status="",
            preview_update_message="",
            preview_confirmation=None,
            preview_sync=types.SimpleNamespace(
                matching_mutation=lambda *_: None,
            ),
            emit=lambda *_args, **_kwargs: None,
        )
        baseline = PreviewRunBaseline(
            document_id="d" * 32,
            watch_generation="watch",
            mutation_sequence=0,
            package_sha256="a" * 64,
            client_id=None,
        )
        try:
            ogent.FAULT_INJECTOR = FaultInjector({FaultPoint.PREVIEW_MISMATCH})
            confirmation = ogent.confirm_word_preview(
                session,
                Path("document.docx"),
                baseline,
                "b" * 64,
                expected_generation=1,
            )
        finally:
            ogent.FAULT_INJECTOR = original
        self.assertFalse(confirmation.confirmed)
        self.assertEqual(confirmation.status, "degraded")


if __name__ == "__main__":
    unittest.main()
