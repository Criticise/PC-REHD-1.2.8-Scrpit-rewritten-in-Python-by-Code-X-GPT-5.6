# Changelog

## v1.0.0 - 2026-07-31

First public source and binary release of PC-REHD Code X.

- Published the Launcher, import/export bridges, FBX probe, Blender support, and runtime bootstrap sources.
- Published the pinned `PY依赖 PY Libs` dependency snapshot used by the distribution.
- Added the Windows release archive and a SHA-256 verification file to GitHub Releases.
- Kept generated caches, embedded Python runtime files, historical V4 backups, diagnostic output, and game assets out of source control.

### 2026-08-01 repairs

- Fixed Blender hierarchy import recovery: unmatched LOD or parent repair targets no longer fail an otherwise completed import, and unmatched Meshes remain unchanged.
- Fixed MAX `Export UV Map 2` on 3ds Max 2026: selected Bucket 3 Meshes safely move `Unwrap UVW` immediately above `Editable Mesh`, collapse it into the base, and verify that all upper modifiers remain in order.
- Export UV2 selection now follows the exported FBX, so a stale scene snapshot cannot reject a valid Map 2 created by the safe collapse step.
- Added bilingual orange warning text to the MAX UV Map 2 help and enable dialog.
- The GitHub SHA-256 update-check UI is disabled in this active local-testing build, so it makes no background network request and shows no test update indicator.
- Documented the tested Blender 5.0+ and 3ds Max 2026 platforms, plus the explicit Blender Legacy-FBX and Agent-injection compatibility limits.
- Released project-authored code under the Unlicense: it may be freely used, modified, republished, and redistributed; users evaluate the script and generated results for their own work.
