from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

from ogent_app.application import (  # noqa: E402
    PathPolicyError,
    WorkspacePathPolicy,
)


class WorkspacePathPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = WorkspacePathPolicy({".docx", ".xlsx", ".pptx", ".pdf"})

    def test_upload_names_are_leaf_only_and_windows_safe(self) -> None:
        self.assertEqual(
            self.policy.safe_upload_filename(r"..\..\Quarterly Report.XLSX"),
            "Quarterly Report.xlsx",
        )
        self.assertEqual(
            self.policy.safe_upload_filename("CON.pdf"),
            "_CON.pdf",
        )
        with self.assertRaises(PathPolicyError) as caught:
            self.policy.safe_upload_filename("payload.exe")
        self.assertEqual(caught.exception.status, 415)

    def test_normalization_resolves_relative_files_but_rejects_directories(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            document = root / "report.docx"
            document.write_bytes(b"fixture")

            normalized = self.policy.normalize_existing_file(
                "report.docx",
                base_directory=root,
            )
            self.assertEqual(normalized, document.resolve())
            with self.assertRaisesRegex(PathPolicyError, "Not a file"):
                self.policy.normalize_existing_file(
                    ".",
                    base_directory=root,
                )

    def test_containment_uses_canonical_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            inside = root / "nested" / ".." / "document.docx"
            outside = root.parent / f"{root.name}-outside.docx"

            self.assertTrue(self.policy.is_within(inside, root))
            self.assertFalse(self.policy.is_within(outside, root))


if __name__ == "__main__":
    unittest.main()
