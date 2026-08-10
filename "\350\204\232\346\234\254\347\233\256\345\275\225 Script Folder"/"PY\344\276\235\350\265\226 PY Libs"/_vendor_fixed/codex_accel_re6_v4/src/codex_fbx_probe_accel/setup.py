# AI MAINTENANCE GATE: this build metadata is one unit with the package
# __init__.py and codex_python_runtime_bootstrap.py. Never edit only setup.py.
# Search for "AI MAINTENANCE GATE" and update every synchronized site.

import ast
from pathlib import Path

from setuptools import Extension, setup


PACKAGE_NAME = "codex_fbx_probe_accel"
PACKAGE_INIT = Path(__file__).resolve().parent / PACKAGE_NAME / "__init__.py"
PACKAGE_TREE = ast.parse(PACKAGE_INIT.read_text(encoding="utf-8"), filename=str(PACKAGE_INIT))
PACKAGE_VERSION = ""
for statement in PACKAGE_TREE.body:
    if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
        continue
    target = statement.targets[0]
    if isinstance(target, ast.Name) and target.id == "__version__" and isinstance(statement.value, ast.Constant):
        PACKAGE_VERSION = str(statement.value.value)
        break
if PACKAGE_VERSION == "":
    raise RuntimeError(f"Missing __version__ in {PACKAGE_INIT}")


setup(
    name=PACKAGE_NAME,
    version=PACKAGE_VERSION,
    packages=["codex_fbx_probe_accel"],
    ext_modules=[
        Extension(
            "codex_fbx_probe_accel._fbx_geometry_core",
            ["codex_fbx_probe_accel/_fbx_geometry_core.c"],
        ),
    ],
    zip_safe=False,
)
