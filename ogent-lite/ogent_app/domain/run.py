"""Typed run intent and scope policy for one Ogent turn."""

from __future__ import annotations

import dataclasses
import enum
import re
from collections.abc import Iterable


class RunMode(str, enum.Enum):
    ANALYZE = "analyze"
    REVIEW = "review"
    EDIT = "edit"
    GENERATE = "generate"
    COMPARE = "compare"


class ScopeMode(str, enum.Enum):
    SELECTED_ONLY = "selected_only"
    LOCAL_REGION = "local_region"
    WHOLE_DOCUMENT = "whole_document"
    SPECIFIED_SECTIONS = "specified_sections"
    SPECIFIED_SHEETS = "specified_sheets"
    SPECIFIED_SLIDES = "specified_slides"
    ATTACHMENTS_ONLY = "attachments_only"


@dataclasses.dataclass(frozen=True)
class RunContract:
    mode: RunMode
    scope: ScopeMode
    selected_paths: tuple[str, ...] = ()

    @property
    def requires_mutation(self) -> bool:
        return self.mode in {RunMode.EDIT, RunMode.GENERATE}

    @property
    def analysis_only(self) -> bool:
        return not self.requires_mutation

    def public(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "scope": self.scope.value,
            "selected_paths": list(self.selected_paths),
            "requires_mutation": self.requires_mutation,
            "analysis_only": self.analysis_only,
        }


EXPLICIT_READ_ONLY = re.compile(
    r"(?is)"
    r"\b(?:read[- ]only|analysis only)\b"
    r"|\bwithout\s+(?:editing|changing|modifying|rewriting|updating)\b"
    r"|\b(?:do\s+not|don['’]?t|dont)\s+"
    r"(?:edit|change|modify|rewrite|update|alter)\b"
    r"|\b(?:suggest|recommend|advise)\b.{0,80}\b(?:only|without editing)?\b"
)
MUTATION_REQUEST = re.compile(
    r"(?is)"
    r"\b(?:edit|change|apply|alter|modify|revise|rewrite|replace|update|"
    r"fix|correct|reformat|"
    r"format|insert|add|append|delete|remove|rename|populate|fill\s+in|"
    r"adjust|restyle)\b"
    r"|\b(?:make|set)\b.{0,80}\b(?:bold|italic|blue|red|green|font|style|"
    r"color|colour|size|alignment|heading|title|formula|value)\b"
    r"|\b(?:create|generate)\b.{0,80}\b(?:section|table|row|column|sheet|"
    r"slide|page|chart|workbook|presentation)\b"
    r"|\bimprove\b.{0,80}\b(?:document|workbook|presentation|section|"
    r"sheet|slide|paragraph|table|chart)\b"
)
ADVISORY_OR_CONDITIONAL = re.compile(
    r"(?is)"
    r"^\s*(?:please\s+)?"
    r"(?:how|what|why|when|where|which|who|whose|"
    r"should|would|could|can|may|might|do|does|did|is|are|will)\b"
    r"|^\s*(?:if|assuming|suppose|supposing|hypothetically)\b"
    r"|\b(?:explain|describe|show|tell\s+me)\b.{0,100}"
    r"\b(?:how|whether)\b"
    r"|\b(?:if|whether)\b.{0,100}\b(?:edit|change|modify|rewrite|"
    r"replace|update|fix|correct|format|polish|bold|set)\b"
)
DIRECT_MUTATION_REQUEST = re.compile(
    r"(?is)"
    r"(?:^|[.!;\n]\s*|\b(?:please|kindly|then)\s+)"
    r"(?:please\s+|kindly\s+)?"
    r"(?:edit|change|apply|alter|modify|revise|rewrite|replace|update|"
    r"fix|correct|reformat|format|insert|add|append|delete|remove|"
    r"rename|populate|fill\s+in|adjust|restyle|polish|bold|italicize|"
    r"underline)\b"
    r"|\b(?:review|check|inspect|proofread)\b.{0,100}"
    r"\b(?:and|then)\s+(?:edit|change|apply|modify|revise|rewrite|"
    r"replace|update|fix|correct|format|polish)\b"
    r"|\b(?:make|set)\b.{0,80}\b(?:bold|italic|blue|red|green|font|style|"
    r"color|colour|size|alignment|heading|title|formula|value|cell)\b"
    r"|\b(?:create|generate)\b.{0,80}\b(?:section|table|row|column|sheet|"
    r"slide|page|chart|workbook|presentation)\b"
    r"|\bimprove\b.{0,80}\b(?:document|workbook|presentation|section|"
    r"sheet|slide|paragraph|table|chart)\b"
)
DIRECT_GENERATION_REQUEST = re.compile(
    r"(?is)\b(?:create|generate)\b.{0,80}\b"
    r"(?:section|table|row|column|sheet|slide|page|chart|"
    r"workbook|presentation)\b"
)
COMPARE_REQUEST = re.compile(r"(?i)\b(?:compare|contrast|difference|versus|vs\.?)\b")
REVIEW_REQUEST = re.compile(
    r"(?i)\b(?:review|audit|check|inspect|evaluate|proofread|critique|"
    r"recommend|suggest|identify|find)\b"
)
WHOLE_DOCUMENT_REQUEST = re.compile(
    r"(?is)"
    r"\b(?:entire|whole|full)[ -](?:document|workbook|presentation|deck|file)\b"
    r"|\bthroughout\s+(?:the\s+)?(?:document|workbook|presentation|deck)\b"
    r"|\bacross\s+(?:the\s+)?(?:document|workbook|presentation|deck)\b"
    r"|\ball\s+(?:sections|headings|tables|figures|charts|sheets|slides|pages)\b"
    r"|\bevery\s+(?:section|heading|table|figure|chart|sheet|slide|page)\b"
)
NEGATED_WHOLE_DOCUMENT_REQUEST = re.compile(
    r"(?is)"
    r"\b(?:do\s+not|don['’]?t|dont|not)\b.{0,80}"
    r"\b(?:entire|whole|full)[ -](?:document|workbook|presentation|deck|file)\b"
    r"|\bonly\s+(?:the\s+)?(?:selection|selected\s+(?:text|targets?|items?))\b"
)
ATTACHMENTS_ONLY_REQUEST = re.compile(r"(?i)\b(?:attachments?|references?)\s+only\b")
SPECIFIED_SHEETS_REQUEST = re.compile(
    r"(?i)\b(?:specified|named|listed|these|following)\s+sheets?\b"
    r"|\bsheets?\s+(?:named|called)\b"
    r"|\bsheet\d+\b"
    r"|\b(?:the\s+)?[A-Za-z][A-Za-z0-9_-]*(?:\s+[A-Za-z0-9_-]+){0,4}"
    r"\s+(?:sheet|worksheet)\b"
)
SPECIFIED_SLIDES_REQUEST = re.compile(
    r"(?i)\b(?:specified|listed|these|following)\s+slides?\b"
    r"|\bslides?\s+(?:numbered|named)\b"
    r"|\bslides?\s+\d+(?:\s*[-–]\s*\d+)?"
    r"(?:\s*(?:,|and)\s*\d+)*\b"
)
SPECIFIED_SECTIONS_REQUEST = re.compile(
    r"(?i)\b(?:specified|named|listed|these|following)\s+sections?\b"
    r"|\bsections?\s+(?:named|called)\b"
    r"|\b(?:the\s+)?[A-Za-z][A-Za-z0-9_-]*(?:\s+[A-Za-z0-9_-]+){0,5}"
    r"\s+section\b"
)


def _resolve_mode(message: str, *, has_active_document: bool) -> RunMode:
    if COMPARE_REQUEST.search(message):
        comparison_mode = RunMode.COMPARE
    elif REVIEW_REQUEST.search(message):
        comparison_mode = RunMode.REVIEW
    else:
        comparison_mode = RunMode.ANALYZE
    if (
        not has_active_document
        or EXPLICIT_READ_ONLY.search(message)
        or ADVISORY_OR_CONDITIONAL.search(message)
    ):
        return comparison_mode
    if DIRECT_MUTATION_REQUEST.search(message):
        if DIRECT_GENERATION_REQUEST.search(message):
            return RunMode.GENERATE
        return RunMode.EDIT
    return comparison_mode


def _resolve_scope(
    message: str,
    *,
    has_active_document: bool,
    has_attachments: bool,
    selected_paths: tuple[str, ...],
) -> ScopeMode:
    if not has_active_document:
        return ScopeMode.ATTACHMENTS_ONLY
    if has_attachments and ATTACHMENTS_ONLY_REQUEST.search(message):
        return ScopeMode.ATTACHMENTS_ONLY
    if selected_paths and NEGATED_WHOLE_DOCUMENT_REQUEST.search(message):
        return ScopeMode.SELECTED_ONLY
    if WHOLE_DOCUMENT_REQUEST.search(message):
        return ScopeMode.WHOLE_DOCUMENT
    if SPECIFIED_SHEETS_REQUEST.search(message):
        return ScopeMode.SPECIFIED_SHEETS
    if SPECIFIED_SLIDES_REQUEST.search(message):
        return ScopeMode.SPECIFIED_SLIDES
    if SPECIFIED_SECTIONS_REQUEST.search(message):
        return ScopeMode.SPECIFIED_SECTIONS
    if selected_paths:
        return ScopeMode.SELECTED_ONLY
    return ScopeMode.WHOLE_DOCUMENT


def resolve_run_contract(
    message: str,
    *,
    has_active_document: bool,
    has_attachments: bool,
    selected_paths: Iterable[str] = (),
) -> RunContract:
    """Resolve a deterministic contract before provider launch.

    The current compatibility UI has no explicit run-mode control, so explicit
    read-only language wins and mutations require a recognized edit request.
    """

    paths = tuple(str(path) for path in selected_paths if str(path))
    return RunContract(
        mode=_resolve_mode(str(message), has_active_document=has_active_document),
        scope=_resolve_scope(
            str(message),
            has_active_document=has_active_document,
            has_attachments=has_attachments,
            selected_paths=paths,
        ),
        selected_paths=paths,
    )
