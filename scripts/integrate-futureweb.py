"""Install into a local source checkout; no SSH, container restart or live deployment.

The adapter reuses the host OAuth and @tool permission checks. Refuse unknown layouts
instead of guessing how to replace the running server.
"""
from __future__ import annotations

import argparse
import ast
import shutil
from pathlib import Path


IMPORT = "import mcp4intervals.futureweb  # noqa: F401,E402  # MCP4Intervals integration\n"


def integrate(target: Path):
    target = target.resolve()
    source = Path(__file__).resolve().parents[1] / "mcp4intervals"
    server = target / "src/intervals_mcp_server/server.py"
    permissions = target / "src/intervals_mcp_server/mcp_instance.py"
    tenancy = target / "src/intervals_mcp_server/tenancy.py"
    project = target / "pyproject.toml"
    if not all(path.is_file() for path in (server, permissions, tenancy, project)):
        raise ValueError("Target is not a supported futureweb/intervals-mcp-server source checkout.")
    if source.resolve().is_relative_to(target) or target.is_relative_to(source.resolve()):
        raise ValueError("Source and target must be separate directories.")
    text = server.read_text(encoding="utf-8")
    if "def tool(" not in permissions.read_text(encoding="utf-8") or "def multi_user(" not in tenancy.read_text(encoding="utf-8"):
        raise ValueError("Host permission/tenancy hooks are missing; update or adapt the integration manually.")
    marker = "mcp.mark_instructions()"
    if marker not in text:
        raise ValueError("Host server registration marker is missing; no files changed.")
    project_text = project.read_text(encoding="utf-8")
    packages = 'packages = ["src/intervals_mcp_server"]'
    updated_packages = 'packages = ["src/intervals_mcp_server", "src/mcp4intervals"]'
    if packages not in project_text and updated_packages not in project_text:
        raise ValueError("Host wheel package layout changed; no files changed.")
    include = 'include = ["src/intervals_mcp_server",'
    updated_include = 'include = ["src/intervals_mcp_server", "src/mcp4intervals",'
    if include not in project_text and updated_include not in project_text:
        raise ValueError("Host sdist package layout changed; no files changed.")
    new_server = text if IMPORT.strip() in text else text.replace(marker, IMPORT + "\n" + marker, 1)
    ast.parse(new_server)
    project_text = project_text.replace(packages, updated_packages, 1)
    if updated_include not in project_text:
        project_text = project_text.replace(include, updated_include, 1)
    destination = target / "src/mcp4intervals"
    if destination.exists():
        # An idempotent rerun may update our files, but never erase unknown local files.
        existing = {p.name for p in destination.iterdir() if p.is_file()}
        ours = {p.name for p in source.glob("*.py")}
        if existing - ours or IMPORT.strip() not in text:
            raise ValueError("Target package already exists without a matching integration; no files changed.")
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.glob("*.py"):
        shutil.copy2(path, destination / path.name)
    server.write_text(new_server, encoding="utf-8")
    project.write_text(project_text, encoding="utf-8")
    print(f"Integrated source: {target}")
    print("Next: uv lock, uv sync --all-extras, run checks, then build with the existing Dockerfile.")
    print("Keep the existing OAuth settings and state volume. Use MCP_TOOLSET=full and MCP_PERMISSIONS=read,write.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    args = parser.parse_args()
    try:
        integrate(args.checkout)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Integration failed: {exc}\n")
