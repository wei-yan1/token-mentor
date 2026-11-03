"""Guard the six-layer dependency boundaries without testing implementation details."""
from __future__ import annotations

import ast
from importlib import import_module
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1] / "src" / "token_mentor"
LAYERS = {"context", "tools", "orchestration", "state", "observability", "constraints"}
ALLOWED = {
    "constraints": set(),
    "context": {"constraints"},
    "tools": {"constraints"},
    "state": {"constraints"},
    "orchestration": LAYERS,
    "observability": {"context", "constraints", "orchestration"},
}


class ArchitectureTests(unittest.TestCase):
    def test_internal_imports_respect_layer_boundaries(self):
        for layer in sorted(LAYERS):
            for path in (ROOT / layer).rglob("*.py"):
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                    if isinstance(node, ast.ImportFrom):
                        self.assertEqual(node.level, 0, f"Use canonical imports: {path}:{node.lineno}")
                        modules = [node.module or ""]
                    elif isinstance(node, ast.Import):
                        modules = [item.name for item in node.names]
                    else:
                        continue
                    for module in modules:
                        if not module.startswith("token_mentor."):
                            continue
                        target = module.split(".")[1]
                        self.assertIn(target, LAYERS | {"models"}, f"Compatibility/entrypoint import: {path}:{node.lineno}")
                        if target in LAYERS and target != layer:
                            self.assertIn(target, ALLOWED[layer], f"Forbidden dependency {layer} -> {target}: {path}:{node.lineno}")

    def test_data_contracts_do_not_depend_on_layers(self):
        tree = ast.parse((ROOT / "models.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("token_mentor."))
                self.assertEqual(node.level, 0)

    def test_legacy_imports_retain_module_identity(self):
        aliases = {
            "analysis": "context.analysis", "agents": "orchestration.agents",
            "collector": "orchestration.collector", "service": "orchestration.service",
            "ranking": "orchestration.ranking", "report": "orchestration.report",
            "storage": "state.storage", "evaluation": "observability.evaluation",
            "sources.mcp": "tools.mcp",
        }
        for old, new in aliases.items():
            self.assertIs(import_module("token_mentor." + old), import_module("token_mentor." + new))
