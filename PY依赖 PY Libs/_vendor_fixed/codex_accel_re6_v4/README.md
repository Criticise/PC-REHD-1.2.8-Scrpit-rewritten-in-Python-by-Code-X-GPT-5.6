# V4 Local Accelerator Bundle

> [!IMPORTANT]
> **AI MAINTENANCE GATE:** This bundle and
> `codex_python_runtime_bootstrap.py` are one compatibility unit. Any change to
> versions, capability flags, C/PYD APIs, cpXXX builds, or fallback behavior
> must update bootstrap, package wrappers, setup.py, release copies, and the
> runtime maintenance charter in the same task. Search for
> `AI MAINTENANCE GATE` before finishing.

Expected local accelerator bundle layout:

- `cp314/codex_fbx_probe_accel/`
- `cp314/codex_uv_layout_accel/`

The formal fixed bundle ships cp314 as the Python 3.14.6 release baseline.
Future ABIs are compiled and tested in the inactive A/B slot before promotion;
the previously approved runtime remains the rollback slot.

All managed source, wrapper, and `.pyd` files in this fixed bundle are pinned
by `ACCELERATOR_FIXED_SOURCE_SHA256` in bootstrap. After an intentional edit,
update bootstrap and run `--accelerator-sync-check --json` on cp314 and on any
new A/B candidate ABI before promotion.
The explicit maintenance check may inspect release packaging copies. Runtime
accelerator health deliberately checks only the active local dependency tree;
an incomplete developer release folder must never disable a user accelerator.

Each prebuilt package directory should contain at least:

- `__init__.py`
- compiled `.pyd` files for the matching CPython tag

Required synchronized source fallback layout:

- `src/codex_fbx_probe_accel/`
- `src/codex_uv_layout_accel/`

Those source directories are part of the bootstrap SHA-256 maintenance gate and
must ship with the dependency bundle. They contain `setup.py` and package source.
The runtime bootstrap prefers current-version prebuilt packages first and falls
back to local source builds only when a matching prebuilt package is missing.
Source installs are local-only: bootstrap routes them through `pip install`
with `--target <_vendor_py/cpXXX>`, `--no-deps`, and `--no-build-isolation`.
If the source build fails, the bridge keeps running on the built-in pure-Python
fallback path instead of aborting export.

Adding a future cp315/cp316 lane requires updating `SUPPORTED_PYTHON_MINORS`
and running sync/import checks on every retained ABI. A partial lane is allowed:
missing prebuilts are reported as warnings, then bootstrap tries local repair,
source compilation, a slower Python implementation, or another Python ABI.

## Current Geometry Contract

- Shared bootstrap contract revision: `2`
- `codex_fbx_probe_accel`: `0.3.0`
- Required normal capabilities:
  - `PRESERVES_CORNER_NORMALS`
  - `PRESERVES_RE6_NORMAL_BYTES`
  - `USES_INVERSE_TRANSPOSE_NORMALS`

The FBX accelerator must match pure Python at the final RE6 normal-byte level,
not only at vertex-count level. It preserves explicit corner normals, uses
inverse-transpose normal transforms, avoids early six-decimal normal rounding,
and pre-splits rows by the final normal bytes that `.MOD` can store.
