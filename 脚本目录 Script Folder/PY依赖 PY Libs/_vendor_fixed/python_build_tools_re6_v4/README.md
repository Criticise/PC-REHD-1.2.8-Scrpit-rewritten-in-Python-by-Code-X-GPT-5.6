# V4 Local Python Build Tools

> [!IMPORTANT]
> **AI MAINTENANCE GATE:** This folder is part of
> `codex_python_runtime_bootstrap.py`. Changing build-tool versions or C
> extension build behavior requires updating bootstrap, the accelerator bundle,
> release copies, and `PYTHON_RUNTIME_MAINTENANCE_CHARTER.md` in the same task.

This fixed local bundle supplies build backends when Bootstrap must compile
patched `ufbx` or a `codex_*_accel` source package for an A/B candidate ABI.

Approved floor:

- `setuptools 83.0.0`
- `Cython 3.2.9` for CPython 3.14 x64

Bootstrap passes this directory to pip with `--find-links`, so an offline
CPython 3.14 rebuild does not depend on PyPI. Normal exports using a matching
prebuilt `.pyd` do not install or load this bundle.
