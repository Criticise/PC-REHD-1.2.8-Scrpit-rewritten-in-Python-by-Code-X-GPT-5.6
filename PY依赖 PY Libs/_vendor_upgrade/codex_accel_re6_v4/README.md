# V4 Accelerator Upgrade Lane

> [!IMPORTANT]
> **AI MAINTENANCE GATE:** Do not add or modify an accelerator candidate without
> checking `codex_python_runtime_bootstrap.py`, the fixed rollback package,
> `BOOTSTRAP_ACCELERATOR_CONTRACT_REVISION`, cp312/cp314 behavior, release
> copies, and the runtime maintenance charter in the same task.

Local upgrade lane for compiled V4 accelerators.

Every managed upgrade file must be registered with its SHA-256 in
`ACCELERATOR_APPROVED_UPGRADE_SHA256` inside
`codex_python_runtime_bootstrap.py`. An unregistered file intentionally fails
`--accelerator-sync-check` and is not accepted as a healthy accelerator.

Supported imports:

- `codex_fbx_probe_accel`
- `codex_uv_layout_accel`

Expected prebuilt layout:

- `cp312/codex_fbx_probe_accel/`
- `cp312/codex_uv_layout_accel/`
- `cp314/codex_fbx_probe_accel/`
- `cp314/codex_uv_layout_accel/`

Expected source fallback layout:

- `src/codex_fbx_probe_accel/`
- `src/codex_uv_layout_accel/`

Bootstrap order:

1. Upgrade prebuilt
2. Upgrade source
3. Fixed prebuilt
4. Fixed source

Within each ABI lane, higher package versions are attempted first. A failed
upgrade build/import/health check is recorded in the per-cpXXX runtime state,
skipped on later runs, and rolled back to `_vendor_fixed`. Python owns the GUI
warning; MaxScript is not part of this dependency error path.

Rollback order:

1. Fixed prebuilt
2. Fixed source

If no fixed accelerator works, export continues on the slower pure-Python path.

Source fallback compiles the two Codex C accelerators for the active Python ABI.
It does not compile `orjson`; orjson upgrades must be supplied as a matching
local cpXXX wheel under `_vendor_upgrade/orjson_re6_v4`.

Current shared contract revision is `2`. Any upgraded FBX probe accelerator
must preserve explicit corner normals, final RE6 normal-byte splits, and
inverse-transpose normal transforms. A higher package version that lacks these
capabilities is unhealthy and must be rejected/rolled back by bootstrap.
