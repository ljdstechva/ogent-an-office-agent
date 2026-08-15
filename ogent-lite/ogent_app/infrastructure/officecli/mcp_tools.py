"""MCP schemas for the restricted typed OfficeCLI gateway."""

from __future__ import annotations

from typing import Any


TYPED_SCHEMAS: dict[str, tuple[str, dict[str, Any], tuple[str, ...], bool]] = {
    "load_document_skill": (
        "Load the server-selected OfficeCLI format skill.",
        {"skill": {"type": "string", "enum": ["word", "excel", "pptx"]}},
        ("skill",),
        False,
    ),
    "inspect_document": (
        "Inspect the active document using a bounded read-only view.",
        {
            "mode": {
                "type": "string",
                "enum": ["stats", "outline", "issues", "text", "annotated", "forms"],
                "default": "stats",
            }
        },
        (),
        False,
    ),
    "read_nodes": (
        "Read one or more authorized OfficeCLI stable paths.",
        {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 100,
            },
            "depth": {"type": "integer", "minimum": 0, "maximum": 8},
        },
        ("paths",),
        False,
    ),
    "query_nodes": (
        "Query nodes in the active document with an OfficeCLI selector.",
        {"selector": {"type": "string", "minLength": 1, "maxLength": 4096}},
        ("selector",),
        False,
    ),
    "apply_atomic_batch": (
        "Apply an atomic OfficeCLI batch to the authorized scope.",
        {
            "commands": {
                "type": "array",
                "items": {"type": "object"},
                "minItems": 1,
                "maxItems": 500,
            }
        },
        ("commands",),
        True,
    ),
    "validate_document": (
        "Validate the active Office package.",
        {},
        (),
        False,
    ),
    "refresh_fields": (
        "Refresh supported DOCX fields, then save atomically.",
        {},
        (),
        True,
    ),
}


def tool_definitions(*, allow_mutations: bool) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for name, (description, properties, required, mutates) in TYPED_SCHEMAS.items():
        if mutates and not allow_mutations:
            continue
        tools.append(
            {
                "name": name,
                "title": name.replace("_", " ").title(),
                "description": description,
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": list(required),
                    "additionalProperties": False,
                },
                "annotations": {
                    "readOnlyHint": not mutates,
                    "destructiveHint": False,
                    "idempotentHint": not mutates,
                    "openWorldHint": False,
                },
            }
        )
    tools.append(
        {
            "name": "officecli",
            "title": "Restricted OfficeCLI Escape Hatch",
            "description": (
                "Run one documented OfficeCLI command only when no typed "
                "operation can express the required action."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": (
                            "Arguments without the leading officecli executable."
                        ),
                    }
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            "annotations": {
                "readOnlyHint": not allow_mutations,
                "destructiveHint": False,
                "idempotentHint": not allow_mutations,
                "openWorldHint": False,
            },
        }
    )
    return tools
