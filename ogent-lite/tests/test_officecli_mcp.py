from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock


OGENT_DIR = Path(__file__).resolve().parents[1]
if str(OGENT_DIR) not in sys.path:
    sys.path.insert(0, str(OGENT_DIR))

from ogent_app.domain.run import ScopeMode  # noqa: E402
from ogent_officecli_mcp import (  # noqa: E402
    GatewayResult,
    OfficeCLIGate,
    OfficeCLIGatewayError,
    OfficeCLIMCPServer,
    split_command,
)


class OfficeCLIGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.document = self.root / "active document.docx"
        self.document.write_bytes(b"fixture")
        self.other = self.root / "other.docx"
        self.other.write_bytes(b"other")
        self.references = self.root / "run-references"
        self.references.mkdir()
        self.image = self.references / "diagram.png"
        self.image.write_bytes(b"png")
        self.gate = OfficeCLIGate(
            self.document,
            read_roots=[self.references],
            executable=Path(sys.executable),
            allow_mutations=True,
            scope_mode=ScopeMode.WHOLE_DOCUMENT,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_split_command_preserves_quoted_windows_path(self) -> None:
        arguments = split_command(f'get "{self.document}" "/body/p[2]" --json')
        self.assertEqual(arguments[0], "get")
        self.assertEqual(Path(arguments[1]), self.document)
        self.assertEqual(arguments[2], "/body/p[2]")

    def test_only_active_document_is_accepted(self) -> None:
        prepared = self.gate.prepare(f'get "{self.document}" "/body/p[2]" --json')
        self.assertEqual(prepared[1], "get")
        self.assertEqual(Path(prepared[2]), self.document.resolve())
        with self.assertRaisesRegex(
            OfficeCLIGatewayError,
            "restricted to the active",
        ):
            self.gate.prepare(f'get "{self.other}" "/body/p[1]" --json')

    def test_every_typed_document_operation_is_identity_and_scope_bound(
        self,
    ) -> None:
        calls: list[list[str]] = []

        def runner(
            arguments: list[str],
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            calls.append(arguments)
            return subprocess.CompletedProcess(arguments, 0, '{"success":true}', "")

        whole_document = OfficeCLIGate(
            self.document,
            executable=Path(sys.executable),
            runner=runner,
            allow_mutations=True,
            scope_mode=ScopeMode.WHOLE_DOCUMENT,
        )
        operations = (
            ("inspect_document", {"mode": "stats"}),
            ("read_nodes", {"paths": ["/body/p[1]"]}),
            ("query_nodes", {"selector": "/body/p[1]"}),
            (
                "apply_atomic_batch",
                {
                    "commands": [
                        {
                            "command": "set",
                            "path": "/body/p[1]",
                            "props": {"text": "safe"},
                        }
                    ]
                },
            ),
            ("validate_document", {}),
            ("refresh_fields", {}),
        )
        for operation, parameters in operations:
            with self.subTest(operation=operation, scope="whole_document"):
                whole_document.execute_typed(operation, parameters)
                self.assertEqual(
                    Path(calls[-1][2]),
                    self.document.resolve(),
                )

        selected_only = OfficeCLIGate(
            self.document,
            executable=Path(sys.executable),
            runner=runner,
            allow_mutations=True,
            scope_mode=ScopeMode.SELECTED_ONLY,
            allowed_document_paths=("/body/p[1]",),
        )
        rejected = (
            ("inspect_document", {"mode": "text"}),
            ("read_nodes", {"paths": ["/body/p[2]"]}),
            ("query_nodes", {"selector": "/body/p[2]"}),
            (
                "apply_atomic_batch",
                {
                    "commands": [
                        {
                            "command": "set",
                            "path": "/body/p[2]",
                            "props": {"text": "outside scope"},
                        }
                    ]
                },
            ),
            ("refresh_fields", {}),
        )
        for operation, parameters in rejected:
            with self.subTest(operation=operation, scope="selected_only"):
                with self.assertRaises(OfficeCLIGatewayError):
                    selected_only.execute_typed(operation, parameters)

        selected_only.execute_typed("validate_document", {})
        self.assertEqual(Path(calls[-1][2]), self.document.resolve())
        whole_document.execute_typed(
            "load_document_skill",
            {"skill": "word"},
        )
        self.assertNotIn(str(self.other), " ".join(calls[-1]))

    def test_read_only_gate_rejects_mutations_but_allows_inspection(self) -> None:
        gate = OfficeCLIGate(
            self.document,
            executable=Path(sys.executable),
            allow_mutations=False,
            scope_mode=ScopeMode.WHOLE_DOCUMENT,
        )

        inspection = gate.prepare(f'get "{self.document}" "/body/p[1]" --json')
        self.assertEqual(inspection[1], "get")
        with self.assertRaisesRegex(
            OfficeCLIGatewayError,
            "read-only",
        ):
            gate.prepare(f'set "{self.document}" "/body/p[1]" --text "changed"')

    def test_analysis_only_gate_rejects_document_commands(self) -> None:
        gate = OfficeCLIGate(
            None,
            executable=Path(sys.executable),
        )
        self.assertEqual(gate.prepare("help docx paragraph")[1], "help")
        with self.assertRaisesRegex(
            OfficeCLIGatewayError,
            "restricted to the active",
        ):
            gate.prepare(f'get "{self.document}" "/body/p[1]"')

    def test_selected_scope_rejects_broad_and_out_of_scope_reads(self) -> None:
        gate = OfficeCLIGate(
            self.document,
            executable=Path(sys.executable),
            allow_mutations=True,
            scope_mode=ScopeMode.SELECTED_ONLY,
            allowed_document_paths=("/body/p[2]",),
        )

        selected = gate.prepare(f'get "{self.document}" "/body/p[2]" --depth 2 --json')
        descendant = gate.prepare(
            f'set "{self.document}" "/body/p[2]/r[1]" --text "updated"'
        )
        stats = gate.prepare(f'view "{self.document}" stats --json')
        self.assertIn("/body/p[2]", selected)
        self.assertIn("/body/p[2]/r[1]", descendant)
        self.assertIn("stats", stats)

        for command in (
            f'view "{self.document}" text',
            f'get "{self.document}" "/" --json',
            f'get "{self.document}" "/body/p[3]" --json',
            f'query "{self.document}" "*" --json',
        ):
            with self.subTest(command=command):
                with self.assertRaises(OfficeCLIGatewayError):
                    gate.prepare(command)

    def test_attachments_only_scope_rejects_all_document_commands(self) -> None:
        gate = OfficeCLIGate(
            self.document,
            executable=Path(sys.executable),
            scope_mode=ScopeMode.ATTACHMENTS_ONLY,
        )

        for command in (
            f'view "{self.document}" stats --json',
            f'validate "{self.document}" --json',
            f'get "{self.document}" "/body/p[1]" --json',
        ):
            with self.subTest(command=command):
                with self.assertRaisesRegex(
                    OfficeCLIGatewayError,
                    "attachments-only",
                ):
                    gate.prepare(command)

    def test_external_inputs_are_limited_to_read_roots(self) -> None:
        prepared = self.gate.prepare(
            f'add "{self.document}" /body --type picture --prop image="{self.image}"'
        )
        self.assertIn(f"image={self.image}", prepared)
        outside = self.root / "outside.png"
        outside.write_bytes(b"outside")
        with self.assertRaisesRegex(
            OfficeCLIGatewayError,
            "read-only references",
        ):
            self.gate.prepare(
                f'add "{self.document}" /body --type picture --prop image="{outside}"'
            )

    def test_relative_external_inputs_are_resolved_before_containment(self) -> None:
        prepared = self.gate.prepare(
            f'add "{self.document}" /body --type picture '
            '--prop image="run-references/diagram.png"'
        )
        self.assertIn("image=run-references/diagram.png", prepared)
        text_prepared = self.gate.prepare(
            f'set "{self.document}" /body/p[1] --prop text="and/or"'
        )
        self.assertIn("text=and/or", text_prepared)
        outside = self.root.parent / f"{self.root.name}-outside-relative.png"
        outside.write_bytes(b"outside")
        self.addCleanup(outside.unlink, missing_ok=True)
        with self.assertRaisesRegex(
            OfficeCLIGatewayError,
            "read-only references",
        ):
            self.gate.prepare(
                f'add "{self.document}" /body --type picture '
                f'--prop image="../{outside.name}"'
            )

    def test_forbidden_commands_and_output_options_fail_closed(self) -> None:
        cases = (
            f'watch "{self.document}"',
            f'create "{self.document}" --force',
            f'view "{self.document}" html',
            f'view "{self.document}" text --out "{self.root / "leak.txt"}"',
            f'batch "{self.document}" --input "{self.root / "batch.json"}"',
            f'batch "{self.document}" --commands "[]" --best-effort',
        )
        for command in cases:
            with self.subTest(command=command):
                with self.assertRaises(OfficeCLIGatewayError):
                    self.gate.prepare(command)

    def test_forbidden_options_reject_equals_and_short_attached_forms(self) -> None:
        output = self.root / "leak.json"
        batch = self.root / "batch.json"
        cases = (
            (
                f'batch "{self.document}" --commands "[]" --best-effort=true',
                "--best-effort",
            ),
            (
                f'query "{self.document}" /body --browser=true',
                "--browser",
            ),
            (
                f'set "{self.document}" /body/p[1] --prop text=changed --force=true',
                "--force",
            ),
            (
                f'batch "{self.document}" --input="{batch}"',
                "--input",
            ),
            (
                f'dump "{self.document}" --out="{output}"',
                "--out",
            ),
            (
                f'dump "{self.document}" -o="{output}"',
                "-o",
            ),
            (
                f'dump "{self.document}" -o"{output}"',
                "-o",
            ),
        )
        for command, option in cases:
            with self.subTest(command=command):
                with self.assertRaisesRegex(
                    OfficeCLIGatewayError,
                    re.escape(f"option {option} "),
                ):
                    self.gate.prepare(command)

    def test_nested_batch_paths_are_validated(self) -> None:
        commands = json.dumps(
            [
                {
                    "op": "add",
                    "path": "/body",
                    "props": {"image": str(self.other)},
                }
            ],
            separators=(",", ":"),
        )
        with self.assertRaisesRegex(
            OfficeCLIGatewayError,
            "read-only references",
        ):
            self.gate.prepare(
                subprocess.list2cmdline(
                    [
                        "batch",
                        str(self.document),
                        "--commands",
                        commands,
                    ]
                )
            )

    def test_execute_uses_no_shell_and_direct_mode(self) -> None:
        captured: dict[str, Any] = {}

        def runner(
            arguments: list[str],
            **kwargs: Any,
        ) -> subprocess.CompletedProcess[str]:
            captured["arguments"] = arguments
            captured.update(kwargs)
            return subprocess.CompletedProcess(
                arguments,
                0,
                stdout="verified",
                stderr="",
            )

        gate = OfficeCLIGate(
            self.document,
            executable=Path(sys.executable),
            runner=runner,
            scope_mode=ScopeMode.WHOLE_DOCUMENT,
        )
        result = gate.execute(f'validate "{self.document}" --json')
        self.assertEqual(result, GatewayResult(0, "verified"))
        self.assertFalse(captured["shell"])
        self.assertTrue(Path(captured["cwd"]).samefile(self.document.parent))
        self.assertEqual(
            captured["env"]["OFFICECLI_NO_AUTO_RESIDENT"],
            "1",
        )


class OfficeCLIMCPServerTests(unittest.TestCase):
    def test_protocol_lists_typed_tools_and_restricted_escape_hatch(self) -> None:
        gate = mock.Mock()
        server = OfficeCLIMCPServer(gate)
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response is not None
        tools = response["result"]["tools"]
        self.assertEqual(
            [tool["name"] for tool in tools],
            [
                "load_document_skill",
                "inspect_document",
                "read_nodes",
                "query_nodes",
                "apply_atomic_batch",
                "validate_document",
                "refresh_fields",
                "officecli",
            ],
        )
        schema = tools[-1]["inputSchema"]
        self.assertEqual(schema["properties"]["command"]["type"], "string")
        self.assertFalse(schema["additionalProperties"])

    def test_tool_call_returns_result_and_rejects_array_command(self) -> None:
        gate = mock.Mock()
        gate.execute.return_value = GatewayResult(0, "done")
        server = OfficeCLIMCPServer(gate)
        success = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "officecli",
                    "arguments": {"command": "help docx"},
                },
            }
        )
        assert success is not None
        self.assertFalse(success["result"]["isError"])
        gate.execute.assert_called_once_with("help docx")
        rejected = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "officecli",
                    "arguments": {"command": ["help", "docx"]},
                },
            }
        )
        assert rejected is not None
        self.assertTrue(rejected["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
