# Changelog

## v1.0.0 - 2026-07-31

First public source and binary release of PC-REHD Code X.

- Published the Launcher, import/export bridges, FBX probe, Blender support, and runtime bootstrap sources.
- Published the pinned `PY依赖 PY Libs` dependency snapshot used by the distribution.
- Added the Windows release archive and a SHA-256 verification file to GitHub Releases.
- Kept generated caches, embedded Python runtime files, historical V4 backups, diagnostic output, and game assets out of source control.

### 2026-08-01 repairs

- Fixed Blender hierarchy import recovery: unmatched LOD or parent repair targets no longer fail an otherwise completed import, and unmatched Meshes remain unchanged.
- Fixed MAX `Export UV Map 2` on 3ds Max 2026: for each selected Mesh with `Unwrap UVW`, the Agent creates a local-data copy, inserts that copy at the bottom of the modifier stack, collapses only the copy into `Editable Mesh`, and preserves the original modifier.
- Export UV2 consumes the Map 2 data from the exported FBX after the copy-collapse operation; it does not rebuild or replace the user's original UV2 layout.
- Added bilingual orange warning text to the MAX UV Map 2 help and enable dialog.
- The GitHub SHA-256 update check compares the published identity SHA, which ignores only the editable Message Editor payload. Test-only update forcing is disabled; a title-bar update indicator appears only when the real published identity differs.
