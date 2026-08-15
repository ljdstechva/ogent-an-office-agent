from __future__ import annotations

import sys
import unittest
from pathlib import Path


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

from ogent_app.domain.run import (  # noqa: E402
    RunMode,
    ScopeMode,
    resolve_run_contract,
)


class RunContractTests(unittest.TestCase):
    def test_mutation_intent_corpus_fails_closed_for_advisory_language(self) -> None:
        cases = {
            "How would you edit this paragraph?": RunMode.ANALYZE,
            "Should I change the title?": RunMode.ANALYZE,
            "Could you bold the title?": RunMode.ANALYZE,
            "If the title is unclear, rewrite it.": RunMode.ANALYZE,
            "Compare the document before and after the update.": RunMode.COMPARE,
            "Explain how to format this table.": RunMode.ANALYZE,
            "Polish this paragraph.": RunMode.EDIT,
            "Bold the title.": RunMode.EDIT,
            "Set cell A1 to 42.": RunMode.EDIT,
            "Please rewrite the Executive Summary.": RunMode.EDIT,
            "Review the document and correct spelling errors.": RunMode.EDIT,
            "Generate a new summary slide.": RunMode.GENERATE,
        }

        for message, expected_mode in cases.items():
            with self.subTest(message=message):
                contract = resolve_run_contract(
                    message,
                    has_active_document=True,
                    has_attachments=False,
                )
                self.assertEqual(contract.mode, expected_mode)
                self.assertEqual(
                    contract.requires_mutation,
                    expected_mode in {RunMode.EDIT, RunMode.GENERATE},
                )

    def test_named_document_regions_resolve_to_typed_scope(self) -> None:
        cases = {
            "Review slides 3-5.": ScopeMode.SPECIFIED_SLIDES,
            "Review Sheet1.": ScopeMode.SPECIFIED_SHEETS,
            "Review the Budget sheet.": ScopeMode.SPECIFIED_SHEETS,
            "Review the Executive Summary section.": ScopeMode.SPECIFIED_SECTIONS,
        }

        for message, expected_scope in cases.items():
            with self.subTest(message=message):
                contract = resolve_run_contract(
                    message,
                    has_active_document=True,
                    has_attachments=False,
                )
                self.assertEqual(contract.scope, expected_scope)

    def test_explicit_whole_document_request_overrides_preview_selection(self) -> None:
        contract = resolve_run_contract(
            "Review the entire document and list every inconsistency.",
            has_active_document=True,
            has_attachments=False,
            selected_paths=("/body/p[2]",),
        )

        self.assertEqual(contract.mode, RunMode.REVIEW)
        self.assertEqual(contract.scope, ScopeMode.WHOLE_DOCUMENT)
        self.assertFalse(contract.requires_mutation)

    def test_selected_edit_remains_selected_only(self) -> None:
        contract = resolve_run_contract(
            "Rewrite this heading to be more concise.",
            has_active_document=True,
            has_attachments=False,
            selected_paths=("/body/p[2]",),
        )

        self.assertEqual(contract.mode, RunMode.EDIT)
        self.assertEqual(contract.scope, ScopeMode.SELECTED_ONLY)
        self.assertTrue(contract.requires_mutation)

    def test_explicit_read_only_language_wins_over_ambiguous_edit_words(self) -> None:
        contract = resolve_run_contract(
            "Suggest improvements, but do not edit or change the document.",
            has_active_document=True,
            has_attachments=False,
        )

        self.assertEqual(contract.mode, RunMode.REVIEW)
        self.assertFalse(contract.requires_mutation)

    def test_attachment_only_turn_is_never_a_document_mutation(self) -> None:
        contract = resolve_run_contract(
            "Compare the attached reports.",
            has_active_document=False,
            has_attachments=True,
        )

        self.assertEqual(contract.mode, RunMode.COMPARE)
        self.assertEqual(contract.scope, ScopeMode.ATTACHMENTS_ONLY)
        self.assertFalse(contract.requires_mutation)

    def test_common_change_and_apply_requests_are_mutations(self) -> None:
        changed = resolve_run_contract(
            "Change the title to blue.",
            has_active_document=True,
            has_attachments=False,
        )
        applied = resolve_run_contract(
            "Apply this formatting to the whole document.",
            has_active_document=True,
            has_attachments=False,
            selected_paths=("/body/p[2]",),
        )

        self.assertTrue(changed.requires_mutation)
        self.assertTrue(applied.requires_mutation)
        self.assertEqual(applied.scope, ScopeMode.WHOLE_DOCUMENT)

    def test_contracted_no_edit_and_negated_whole_document_are_read_only_selected(
        self,
    ) -> None:
        contract = resolve_run_contract(
            "Don't edit or review the entire document, only the selection.",
            has_active_document=True,
            has_attachments=False,
            selected_paths=("/body/p[2]",),
        )

        self.assertFalse(contract.requires_mutation)
        self.assertEqual(contract.scope, ScopeMode.SELECTED_ONLY)


if __name__ == "__main__":
    unittest.main()
