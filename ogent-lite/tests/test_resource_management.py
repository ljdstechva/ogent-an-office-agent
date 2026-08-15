from __future__ import annotations

import os
import tempfile
import types
import unittest
from pathlib import Path

from ogent_app.infrastructure.sqlite import ContentAddressedBlobStore
from ogent_app.infrastructure.storage import (
    StorageQuotaError,
    StorageResourceManager,
)
from ogent_app.settings import (
    GIB,
    MIB,
    OgentSettings,
    ResourceQuotas,
    SettingsError,
)


class SettingsTests(unittest.TestCase):
    def test_defaults_preserve_current_public_limits(self) -> None:
        settings = OgentSettings.load({})
        self.assertEqual(settings.quotas.max_inline_turn_characters, 200_000)
        self.assertEqual(settings.quotas.max_reference_file_bytes, 50 * MIB)
        self.assertEqual(settings.quotas.max_document_upload_bytes, 128 * MIB)
        self.assertTrue(settings.features.large_text_assets)
        self.assertFalse(settings.features.fault_injection)

    def test_environment_overrides_are_typed_and_bounded(self) -> None:
        settings = OgentSettings.load(
            {
                "OGENT_MAX_INLINE_TURN_CHARACTERS": "250000",
                "OGENT_MAX_JSON_BODY_BYTES": str(2 * MIB),
                "OGENT_MAX_LOCAL_DATA_BYTES": str(12 * GIB),
                "OGENT_FEATURE_WARM_PROVIDER_TRANSPORT": "true",
            }
        )
        self.assertEqual(
            settings.quotas.max_inline_turn_characters,
            250_000,
        )
        self.assertEqual(settings.quotas.max_local_data_bytes, 12 * GIB)
        self.assertTrue(settings.features.warm_provider_transport)

    def test_invalid_cross_quota_relationship_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            SettingsError,
            "per-file reference limit",
        ):
            OgentSettings.load(
                {
                    "OGENT_MAX_REFERENCE_FILE_BYTES": str(20 * MIB),
                    "OGENT_MAX_REFERENCE_TURN_BYTES": str(10 * MIB),
                }
            )


class StorageResourceManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "managed"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def quotas(self, **changes: int) -> ResourceQuotas:
        defaults = ResourceQuotas()
        values = {
            field.name: getattr(defaults, field.name)
            for field in defaults.__dataclass_fields__.values()
        }
        values.update(changes)
        return ResourceQuotas(**values)

    def test_forecast_accounts_for_managed_and_physical_capacity(self) -> None:
        self.root.mkdir()
        (self.root / "state.bin").write_bytes(b"x" * 512)
        manager = StorageResourceManager(
            self.root,
            self.quotas(
                max_local_data_bytes=1024,
                minimum_free_disk_bytes=256,
            ),
            disk_usage=lambda _: types.SimpleNamespace(free=2048),
        )
        accepted = manager.forecast(256)
        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.projected_managed_bytes, 768)
        rejected = manager.forecast(600)
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "managed_quota_exceeded")
        with self.assertRaises(StorageQuotaError):
            manager.ensure_capacity(600, purpose="test artifact")

    def test_free_space_reserve_is_independent_of_managed_quota(self) -> None:
        manager = StorageResourceManager(
            self.root,
            self.quotas(
                max_local_data_bytes=4096,
                minimum_free_disk_bytes=512,
            ),
            disk_usage=lambda _: types.SimpleNamespace(free=700),
        )
        forecast = manager.forecast(256)
        self.assertFalse(forecast.accepted)
        self.assertEqual(forecast.reason, "free_disk_reserve_exceeded")

    def test_cleanup_removes_only_stale_owned_partials(self) -> None:
        self.root.mkdir()
        stale = self.root / ".blob.partial"
        fresh = self.root / ".current.partial"
        ordinary = self.root / "document.docx"
        stale.write_bytes(b"old")
        fresh.write_bytes(b"new")
        ordinary.write_bytes(b"keep")
        os.utime(stale, (100, 100))
        os.utime(fresh, (9_900, 9_900))
        manager = StorageResourceManager(
            self.root,
            self.quotas(partial_retention_seconds=1000),
            disk_usage=lambda _: types.SimpleNamespace(free=10 * GIB),
            now=lambda: 10_000,
        )
        result = manager.cleanup_partials()
        self.assertEqual(result, {"deleted": 1, "reclaimed_bytes": 3})
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(ordinary.exists())

    def test_blob_store_forecasts_only_new_content(self) -> None:
        calls: list[int] = []
        store = ContentAddressedBlobStore(
            self.root,
            capacity_guard=calls.append,
        )
        first = store.put_bytes(b"lossless")
        second = store.put_bytes(b"lossless")
        self.assertEqual(first.blob_id, second.blob_id)
        self.assertEqual(calls, [8])


if __name__ == "__main__":
    unittest.main()
