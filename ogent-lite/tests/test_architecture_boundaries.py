from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "ogent_app"


def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _production_modules() -> dict[str, Path]:
    return {
        _module_name(path): path
        for path in PACKAGE_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    }


def _internal_dependencies(
    module: str,
    path: Path,
    modules: dict[str, Path],
) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    dependencies: set[str] = set()
    for node in ast.walk(tree):
        candidates: list[str] = []
        if isinstance(node, ast.Import):
            candidates.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = package.split(".")
                ascents = node.level - 1
                if ascents:
                    anchor = anchor[:-ascents]
                base = ".".join([*anchor, *(node.module or "").split(".")]).rstrip(".")
            else:
                base = node.module or ""
            if base:
                candidates.append(base)
            candidates.extend(
                f"{base}.{alias.name}".strip(".")
                for alias in node.names
                if alias.name != "*"
            )
        for candidate in candidates:
            if candidate in modules and candidate != module:
                dependencies.add(candidate)
    return dependencies


def _first_cycle(graph: dict[str, set[str]]) -> tuple[str, ...]:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(module: str) -> tuple[str, ...]:
        state[module] = 1
        stack.append(module)
        for dependency in sorted(graph[module]):
            if state.get(dependency, 0) == 0:
                cycle = visit(dependency)
                if cycle:
                    return cycle
            elif state.get(dependency) == 1:
                start = stack.index(dependency)
                return (*stack[start:], dependency)
        stack.pop()
        state[module] = 2
        return ()

    for module in sorted(graph):
        if state.get(module, 0) == 0:
            cycle = visit(module)
            if cycle:
                return cycle
    return ()


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_production_module_graph_has_no_import_cycles(self) -> None:
        modules = _production_modules()
        graph = {
            module: _internal_dependencies(module, path, modules)
            for module, path in modules.items()
        }
        cycle = _first_cycle(graph)
        self.assertEqual(cycle, (), " -> ".join(cycle))

    def test_domain_and_ports_do_not_depend_on_outer_layers(self) -> None:
        violations: list[str] = []
        allowed_internal = ("ogent_app.domain", "ogent_app.ports")
        for boundary in ("domain", "ports"):
            for path in (PACKAGE_ROOT / boundary).rglob("*.py"):
                tree = ast.parse(
                    path.read_text(encoding="utf-8"),
                    filename=str(path),
                )
                for node in ast.walk(tree):
                    imported: list[str] = []
                    if isinstance(node, ast.Import):
                        imported = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.level == 0:
                        imported = [node.module or ""]
                    for name in imported:
                        root = name.split(".", 1)[0]
                        internal_allowed = name.startswith(allowed_internal)
                        if (name.startswith("ogent_app") and not internal_allowed) or (
                            root
                            and root not in sys.stdlib_module_names
                            and root != "ogent_app"
                        ):
                            violations.append(
                                f"{path.relative_to(ROOT)}:{node.lineno} -> {name}"
                            )
        self.assertEqual(violations, [])

    def test_broad_exceptions_are_not_silently_suppressed(self) -> None:
        violations: list[str] = []
        silent_handler = re.compile(
            r"except\s+Exception(?:\s+as\s+\w+)?\s*:\s*\n\s+pass\b"
        )
        for pattern in ("*.py", "*.pyfrag"):
            for path in PACKAGE_ROOT.rglob(pattern):
                source = path.read_text(encoding="utf-8")
                if "contextlib.suppress(Exception)" in source:
                    violations.append(
                        f"{path.relative_to(ROOT)} uses suppress(Exception)"
                    )
                if silent_handler.search(source):
                    violations.append(
                        f"{path.relative_to(ROOT)} silently passes Exception"
                    )
        self.assertEqual(violations, [])

    def test_production_sources_stay_inside_extraction_size_gates(self) -> None:
        oversized: list[str] = []
        oversized_application: list[str] = []
        for pattern in ("*.py", "*.pyfrag"):
            for path in PACKAGE_ROOT.rglob(pattern):
                lines = len(path.read_text(encoding="utf-8").splitlines())
                if lines > 1_000:
                    oversized.append(f"{path.relative_to(ROOT)} ({lines})")
                if (
                    path.parent == PACKAGE_ROOT / "application"
                    and lines > 400
                    and path.name != "turn_coordinator.py"
                ):
                    oversized_application.append(f"{path.relative_to(ROOT)} ({lines})")
        self.assertEqual(oversized, [])
        self.assertEqual(oversized_application, [])


if __name__ == "__main__":
    unittest.main()
