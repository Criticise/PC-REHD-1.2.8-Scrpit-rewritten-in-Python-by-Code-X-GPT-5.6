Local upgrade lane for V4 Python runtime dependencies.

Authority note:

- For the full dependency maintenance policy, promotion workflow, observed runtime impact, and next performance focus, see `../PYTHON_RUNTIME_MAINTENANCE_CHARTER.md`.

Rules:

- Local-only. Do not put network package specs here.
- This layer is optional and always sits above `_vendor_fixed`.
- Bootstrap treats `_vendor_fixed` as the rollback floor.
- If an upgrade candidate fails to import or build, bootstrap rolls back to `_vendor_fixed`.
- Patched `ufbx` does not belong here. Keep it locked in `_vendor_fixed/pyufbx_re6_v4`.

Expected subfolders:

- `pillow_re6_v4/`
- `orjson_re6_v4/`
- `codex_accel_re6_v4/`
