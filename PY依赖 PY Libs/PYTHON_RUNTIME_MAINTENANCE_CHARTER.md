# V4 Python Runtime Maintenance Charter

> [!IMPORTANT]
> **AI MAINTENANCE GATE: DEPENDENCIES AND BOOTSTRAP MUST BE REVIEWED TOGETHER**
>
> Any AI changing a package version, capability marker, C/PYD API, build rule,
> Python ABI, upgrade order, or rollback behavior must update and verify
> `codex_python_runtime_bootstrap.py` and the matching dependency bundle in the
> same task. Search the workspace for `AI MAINTENANCE GATE`; do not finish after
> editing only one side. The cp314 release floor and every A/B candidate must
> each pass the same self-checks.
>
> Accelerator wrappers and bootstrap share
> `BOOTSTRAP_ACCELERATOR_CONTRACT_REVISION`. A revision mismatch intentionally
> disables the accelerator and forces rollback/pure-Python fallback.


## Purpose

This runtime is local-first and self-repairing. A complete release must work
offline, but ordinary allowlisted dependencies may use the official Python
package index when the matching local payload is absent or when a cached
upgrade check discovers a newer candidate. Network access is never required
for an already healthy export runtime.

The runtime uses three safety layers:

- `_vendor_fixed`: known-good rollback floor
- `_vendor_upgrade`: newer local candidates that bootstrap may auto-follow
- `_vendor_py/cpXXX`: packaged ABI lane and active import source

Every candidate is installed into staging and accepted only after a fresh-child
health probe. A bad upgrade cannot replace the active or last-known-good lane.

## Dependency Classes

Pinned local-only:

- `ufbx`
- `codex_fbx_probe_accel`
- `codex_uv_layout_accel`

Allowlisted for local-first, online-fallback upgrades:

- `numpy`
- `Pillow`
- `orjson`

Transitive security payload:

- `certifi` is shipped with `requests` as a pure-Python CA bundle. Refreshes
  must keep the package ABI-neutral and pass `certifi.where()` plus an HTTPS
  import-health check. The current release payload is `certifi 2026.7.22`.

The two `codex_*_accel` packages may still advance from a newer local prebuilt
or source candidate. Their names must never be resolved from a public index.

## Folder Layout

```text
_vendor_fixed/
  numpy_re6_v4/
    numpy-2.5.1-cp314-cp314-win_amd64.whl
  pyufbx_re6_v4/
  python_runtime_re6_v4/
  pillow_re6_v4/
  orjson_re6_v4/
  codex_accel_re6_v4/
    cp314/
    src/
  python_build_tools_re6_v4/
    setuptools-83.0.0-py3-none-any.whl
    cython-3.2.9-cp314-cp314-win_amd64.whl

_vendor_upgrade/
  pillow_re6_v4/
  orjson_re6_v4/
  codex_accel_re6_v4/
    cp314/
    src/

Generated at runtime, not shipped:
  _vendor_py/cpXXX/
    _codex_runtime_state.json
```

Directory roles:

- `_vendor_fixed`: stable local floor, and the only rollback target
- `_vendor_upgrade`: newer local candidates that bootstrap may follow automatically
- `_vendor_py`: generated live per-Python runtime install/output area; it is not a release payload
- `_vendor_src`: development-only source reference/cache; it is not a release payload

## Bootstrap Rules

1. Detect the active Python tag, currently `cp314` for the release baseline.
2. Resolve the dependency root and validate the exact ABI payload contract
   before attempting repair. A directory name by itself is not evidence that
   the bundle is complete.
3. Candidate order is packaged ABI lane, local upgrade, local fixed floor, then
   the official-index fallback for explicitly allowlisted ordinary packages.
4. Compute `preferred_version`, `floor_version`, and a stable artifact
   fingerprint. A local file uses SHA256; a source tree uses a deterministic
   tree fingerprint; a downloaded wheel records its SHA256.
5. Install every candidate into a staging lane. Commit atomically only after a
   fresh child imports the package from that lane and passes its functional
   health check. `numpy` must also pass the patched `ufbx` joint probe.
6. A failed candidate is blocked by package + version + artifact fingerprint.
   The exact same artifact is skipped on later runs, while a newer version or a
   rebuilt same-version artifact with a different fingerprint is eligible.
7. Upgrade discovery is TTL-cached. Network timeout or index failure is a
   maintenance warning and must not interrupt a healthy export.
8. Rollback prefers the last-known-good lane, then `_vendor_fixed`. Neither a
   failed staging install nor a failed online check may mutate those copies.
9. For accelerators:
   - prefer prebuilt package dirs over source builds
   - if no prebuilt matches, try local source build
   - source builds must bootstrap setuptools from `_vendor_fixed/python_build_tools_re6_v4`
   - pass the live `_vendor_py/cpXXX` path to the child build through `PYTHONPATH`
   - if accelerator still fails, continue export on the slower pure-Python path
   - retry automatically when a higher local version or a different source
     fingerprint appears
   - never download a public package with a `codex_*_accel` distribution name
   - every fixed managed file must match `ACCELERATOR_FIXED_SOURCE_SHA256`
   - every upgrade file must be explicitly registered in `ACCELERATOR_APPROVED_UPGRADE_SHA256`
   - run `codex_python_runtime_bootstrap.py --accelerator-sync-check --json` on cp314 and each A/B candidate ABI
   - runtime import health checks only the active local dependency tree; it must
     never reject a working user accelerator because a developer-only release
     packaging copy is absent or incomplete
   - the explicit `--accelerator-sync-check` maintenance command may additionally
     audit release copies, but that result is not runtime import authority
10. Optional runtime dependency warnings belong to Python itself:
   - Python may show its own GUI warning with the reason and suggested fix
   - MaxScript must not be the UI owner for `orjson` / `Pillow` / `codex_*_accel` runtime advisories
   - export result sidecars should not route those advisories back into MS warning popups
11. `ufbx` stays locked to `_vendor_fixed/pyufbx_re6_v4` and is never upgraded
    from a public index. Its required NumPy runtime may be repaired separately,
    but both must pass a joint fresh-child probe.

## Operation Fault Domains

Launcher is the lightweight dispatcher. It loads only the modules required by
the requested operation; Bootstrap owns explicit startup, health, repair, and
upgrade work. Do not add a second dispatcher file.

- `import_mod`: Max Agent + `codex_re6_mod_import_fbx`
- `export_mod`: Max Agent + `codex_python_export_bridge`; `ufbx` is required
  when the writer reads the exported FBX
- `texture`: `codex_re6_tex_decode` and its own optional Pillow path
- `auxiliary`: `codex_re6_auxiliary_max_bridge` for SBC/ADR/EMS only
- `max_agent`: exact-PID Max communication only

The failure contract is strict:

1. Only a missing required dependency or failed module in the current operation
   may fail that operation.
2. An optional accelerator failure is `DEGRADED`; use the pure-Python fallback.
3. TEX or AUX failure never changes MOD import/export status. Import failure
   never changes export status, and export failure never changes import status.
4. `runtime_json_*` and ordinary business hot paths may load an installed
   optional module, but must not install, repair, upgrade, or access the network.
5. Installation and A/B repair run only through explicit Bootstrap health,
   BAT/PS1 initialization, or the resident health supervisor while no user
   operation is active.
6. A full release health report may list several failed domains. Launcher must
   not reuse that aggregate result as the success gate for one user operation.

Bootstrap exposes `get_operation_runtime_domain_contract()` and
`get_operation_runtime_domain_report()` for structured diagnostics. These APIs
do not import unrelated business modules and default to no repair.

## How To Add A New Upgrade

### Wheel dependencies (`numpy`, `Pillow`, `orjson`)

1. Build or collect a wheel for the exact Python tag.
2. Drop it under the matching `_vendor_upgrade/...` folder.
3. Keep the old stable wheel in `_vendor_fixed/...`.
4. Start V4 or run the bootstrap runtime report.
5. Confirm `preferred_version` moved forward and `floor_version` stayed on the fixed bundle.

Bootstrap selects the highest local wheel matching the active cpXXX ABI.
After the upgrade-check TTL expires it may inspect the official index for an
allowlisted newer wheel. The downloaded wheel remains a staged candidate until
its package-specific health probe passes; it never deletes the fixed floor.
`orjson` is a Rust extension and is deliberately not compiled on an end-user
machine; automatic handling means selecting/installing a matching local wheel,
running a real `dumps`/`loads`/`OPT_INDENT_2` round-trip health check, and rolling
back to the fixed wheel if it fails.

### Accelerator prebuilt packages

1. Build the accelerator against the target Python minor.
2. Place the package directory under `_vendor_upgrade/codex_accel_re6_v4/cpXXX/<import_name>/`.
3. Keep a known-good package or source tree in `_vendor_fixed/codex_accel_re6_v4/...`.
4. Start V4 or run the runtime report.
5. Confirm the report shows the new preferred version and still has a fixed floor.

### Accelerator source fallback

1. Put the source package under `_vendor_upgrade/codex_accel_re6_v4/src/<import_name>/`.
2. Include `setup.py`, `setup.cfg`, or `pyproject.toml`.
3. Keep a fixed prebuilt or fixed source floor in `_vendor_fixed/codex_accel_re6_v4/...`.
4. If local build fails, bootstrap must fall back without aborting export.

## Rollback Policy

Rollback only targets `_vendor_fixed`.

That means:

- a broken upgrade wheel never becomes its own repair target
- a broken upgrade accelerator never becomes its own repair target
- the blocked version record prevents repeated bad reuse

If `_vendor_fixed` is empty for an upgradeable package, the release layout is
incomplete even when online repair is possible. Network repair is resilience,
not permission to ship an untested bundle.

## Blocked Version State

Blocked artifacts are tracked per Python minor in the generated
`_vendor_py/cpXXX/_codex_runtime_state.json`.

Use this file to answer:

- which version failed
- why it failed
- which Python minor saw the failure

Do not require manual clearing after a rebuild. The blocked key includes the
artifact fingerprint, so a changed build is retried automatically. Manual state
deletion is only a diagnostic override.

## Python Version Changes

First-run user setup now lives next to the V4 script:

- `一定要先点我安装Python  - Click to Install Python First.bat`
- `先点Bat文件 - Click Bat First.ps1`
- `python-3.14.7-amd64.exe`

The helper prefers the bundled `python-3.14.7-amd64.exe` for a local/offline
install, then falls back to `winget` or the official python.org download path if
the EXE is missing. Runtime discovery and selection stay inside the BAT/PS1 and
Python layers; Launcher and 3ds Max do not own runtime promotion or rollback.

1. The fixed release baseline is Python 3.14.7 x64 (`cp314`).
2. A newer Python, including a future major version, is tested in the inactive
   A/B slot and becomes active only after the full Bootstrap contract passes.
3. The previously approved runtime remains the rollback slot.
4. Candidate mode does not assume binary accelerators or ufbx exist.
5. If the Python minor changes:
   - wheel packages need a matching wheel when ABI-specific
   - accelerator prebuilt packages need a matching `cpXXX` folder
   - otherwise bootstrap may try local source build
   - if source build fails, export falls back to pure Python

### Approving A New Python Minor

1. Add the version to `SUPPORTED_PYTHON_MINORS`; do not only change the
   installer search list.
2. Add whatever matching wheels/prebuilts are available. Missing ABI artifacts
   are warnings, not synchronization failures.
3. Confirm optional dependencies degrade correctly: standard-library JSON for
   orjson, fallback image handling for Pillow, and pure Python for accelerators.
4. For required patched ufbx, confirm this order: prebuilt copy, local source
   build, then automatic relaunch under another supported Python ABI.
5. Update bootstrap hashes, fixed/upgrade READMEs, and release copies for every
   artifact that is actually shipped.
6. Run syntax, dependency sync, import-health, repair, and rollback simulations
   on the new ABI and every retained ABI.

## Promotion Workflow

Once an upgrade has proven stable:

1. Copy the winning upgrade artifact from `_vendor_upgrade` into `_vendor_fixed`.
2. Keep versions aligned across supported Python minors when possible.
3. Remove stale upgrade artifacts only after the fixed floor is in place.
4. Clear any now-obsolete blocked-version entry if it refers to the same repaired version.

## Observed Runtime Impact

### 2026-07-11 enhanced normal accelerator contract

`codex_fbx_probe_accel 0.3.0` and the shared accelerator contract revision `2`
add three mandatory normal capabilities: explicit corner-normal preservation,
final RE6 normal-byte split preservation, and inverse-transpose normal
transforms. The pure-Python and C paths must match at the final `.MOD` normal
byte level. A build that imports successfully but lacks any capability is not
healthy and must fall back.

This release revision is compiled for cp314. Its supplied FBX normal samples
passed C-versus-Python, UV1/UV2, HALF SAFE, and final writer-space byte checks
with zero mismatches. Tangent policy remains in the Python writer and is locked
to PC_REHD 1.2.8 for known RE6 FVF fields; it is not delegated to this C module.

### 2026-07-09 `orjson` + `codex_*_accel` practical result

Observed export timing after the local `orjson` lane and C++ accelerator lane were compiled and accepted:

- `log_mode` export improved clearly:
  - before: about `38s`
  - after: about `23s`
  - result: now close to the same wall-clock time as ordinary non-log export
- ordinary non-log export did not change much:
  - before: about `23s`
  - after: about `23s`

Interpretation:

- the dependency upgrade helped mainly with the extra JSON/diagnostic sidecar and accelerated helper overhead
- it did **not** materially shorten the main export critical path yet
- do not oversell `orjson` / `codex_*_accel` as a blanket “main export got much faster” change

## Next Speed Focus

If the next goal is to reduce ordinary export time, investigate the phases that still dominate total runtime instead of spending another round only on dependency packaging.

Start from the bridge timing keys in the result sidecar:

- `TIMING_COLLECT_FBX_HANDOFF_SECONDS`
- `TIMING_PREPARE_JOB_CONTRACT_SECONDS`
- `TIMING_WRITE_OUTPUT_MOD_SECONDS`
- `TIMING_POST_WRITE_VERIFY_SECONDS`

Likely next bottleneck families:

- `FBX handoff / probe`
  - reading and summarizing the FBX scene
  - `codex_fbx_probe` hot loops
- `prepare_job_contract`
  - route-table interpretation
  - mesh-plan rebuild and contract normalization
- `write_output_mod`
  - actual mesh/header/bone serialization and payload rebuild
- `post_write_verify`
  - diff dump generation
  - verify diagnostics
  - extra artifact persistence when `log_mode` is on

Performance rule:

- if `log_mode` and non-log export are now roughly equal, the old “logging tax” was reduced successfully
- the next real speed win must come from the main bridge/write/verify pipeline, not from JSON dependency swaps alone

## Non-Negotiable Rules

- No network dependency for an already healthy runtime
- No public-index lookup outside the explicit ordinary-package allowlist
- No auto-downgrade below the fixed floor
- No upgrade lane for patched `ufbx`
- No public-index package for either `codex_*_accel` module
- No release layout without a fixed rollback floor
- No new Python ABI without a tested repair/degrade/relaunch path
- No candidate promotion without staging, fresh-child validation, and an
  artifact fingerprint
