from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

from ogent_references import ReferenceAttachment  # noqa: E402
from ogent_retained_attachments import (  # noqa: E402
    RetainedAttachmentError,
    RetainedAttachmentStore,
)


class RetainedAttachmentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.memory_root = root / "memory" / "deadbeef"
        self.memory_root.mkdir(parents=True)
        self.store = RetainedAttachmentStore(
            self.memory_root,
            root / "runs" / "deadbeef",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def upload(self, identifier: str, content: bytes) -> ReferenceAttachment:
        incoming = self.store.begin_upload(identifier)
        source = incoming / "source.txt"
        source.write_bytes(content)
        pending = ReferenceAttachment(
            attachment_id=identifier,
            original_name="notes.txt",
            source_path=source,
            detected_type="text/plain",
            kind="Text",
            byte_size=len(content),
            uploaded_at="2026-03-04T05:06:07Z",
        )
        return self.store.commit_upload(source, pending)

    def test_validated_upload_becomes_canonical_and_run_copy_is_independent(
        self,
    ) -> None:
        identifier = "1" * 32
        canonical = self.upload(identifier, b"retained evidence")
        run_id = "2" * 32
        copies, bundle = self.store.materialize([canonical], run_id)
        self.assertIsNotNone(bundle)
        self.assertNotEqual(copies[0].source_path, canonical.source_path)
        self.assertEqual(
            hashlib.sha256(copies[0].source_path.read_bytes()).hexdigest(),
            hashlib.sha256(canonical.source_path.read_bytes()).hexdigest(),
        )
        copies[0].source_path.write_bytes(b"provider mutation")
        self.assertEqual(canonical.source_path.read_bytes(), b"retained evidence")
        self.assertTrue(self.store.cleanup_run(run_id))
        self.assertFalse(bundle.exists())  # type: ignore[union-attr]
        self.assertTrue(canonical.source_path.exists())

    def test_rejected_upload_and_forget_are_contained(self) -> None:
        incoming = self.store.begin_upload("3" * 32)
        (incoming / "source.txt").write_text("invalid", encoding="utf-8")
        self.store.reject_upload(incoming)
        self.assertFalse(incoming.exists())

        canonical = self.upload("4" * 32, b"forget me")
        self.store.forget(canonical)
        self.assertFalse(canonical.source_path.exists())
        with self.assertRaises(RetainedAttachmentError):
            self.store.forget(
                ReferenceAttachment(
                    attachment_id="5" * 32,
                    original_name="bad.txt",
                    source_path=Path(self.temp.name).parent / "outside.txt",
                    detected_type="text/plain",
                    kind="Text",
                    byte_size=1,
                    uploaded_at="now",
                    canonical_attachment_id="5" * 32,
                )
            )

    def test_prepared_derivatives_are_retained_but_not_the_run_path(self) -> None:
        canonical = self.upload("6" * 32, b"source")
        copies, bundle = self.store.materialize([canonical], "7" * 32)
        assert bundle is not None
        derived = bundle / "agent-derived"
        derived.mkdir()
        extracted = derived / "extracted.txt"
        extracted.write_text("safe extracted text", encoding="utf-8")
        image = derived / "image.png"
        image.write_bytes(b"png")
        prepared = [
            __import__("dataclasses").replace(
                copies[0],
                extracted_text_path=extracted,
                image_paths=[image],
            )
        ]
        self.store.cache_prepared([canonical], prepared)
        cache = canonical.source_path.parent / "derived"
        self.assertEqual(
            (cache / "extracted.txt").read_text(encoding="utf-8"),
            "safe extracted text",
        )
        self.assertTrue((cache / "derived-cache.json").is_file())

        self.store.cleanup_run("7" * 32)
        second_copies, second_bundle = self.store.materialize(
            [canonical],
            "8" * 32,
        )
        assert second_bundle is not None
        second_derived = second_bundle / "agent-derived"
        second_derived.mkdir()
        restored = self.store.restore_cached(
            second_copies[0],
            second_derived,
            require_visual=False,
        )
        assert restored is not None
        assert restored.extracted_text_path is not None
        self.assertEqual(
            restored.extracted_text_path.read_text(encoding="utf-8"),
            "safe extracted text",
        )
        self.assertEqual(len(restored.image_paths), 1)
        self.assertTrue(restored.extracted_text_path.is_relative_to(second_bundle))
        self.assertFalse(
            restored.extracted_text_path.is_relative_to(self.store.canonical_root)
        )
        self.assertTrue(self.store.cleanup_run("8" * 32))


if __name__ == "__main__":
    unittest.main()
