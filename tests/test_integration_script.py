import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/integrate-futureweb.py"
spec = importlib.util.spec_from_file_location("integration_script", SCRIPT)
integration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(integration)


class InstallerTests(unittest.TestCase):
    def fixture(self, root):
        package = root / "src/intervals_mcp_server"
        package.mkdir(parents=True)
        (package / "server.py").write_text("mcp.mark_instructions()\n", encoding="utf-8")
        (package / "mcp_instance.py").write_text("def tool(): pass\n", encoding="utf-8")
        (package / "tenancy.py").write_text("def multi_user(): pass\n", encoding="utf-8")
        (root / "pyproject.toml").write_text('include = ["src/intervals_mcp_server", "README.md"]\npackages = ["src/intervals_mcp_server"]\n', encoding="utf-8")
        return package

    def test_repeat_install_is_idempotent_and_packages_both_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self.fixture(root)
            integration.integrate(root)
            first = (root / "pyproject.toml").read_text(encoding="utf-8")
            integration.integrate(root)
            self.assertEqual(first, (root / "pyproject.toml").read_text(encoding="utf-8"))
            self.assertEqual(2, first.count('"src/mcp4intervals"'))
            self.assertEqual(1, (package / "server.py").read_text(encoding="utf-8").count("import mcp4intervals.futureweb"))
            self.assertTrue((root / "src/mcp4intervals/ai.py").is_file())
            self.assertFalse((root / "src/mcp4intervals/.env").exists())

    def test_unknown_layout_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = self.fixture(root)
            (package / "server.py").write_text("unknown_layout = True\n", encoding="utf-8")
            original = (root / "pyproject.toml").read_bytes()
            with self.assertRaises(ValueError):
                integration.integrate(root)
            self.assertEqual(original, (root / "pyproject.toml").read_bytes())
            self.assertFalse((root / "src/mcp4intervals").exists())


if __name__ == "__main__":
    unittest.main()
