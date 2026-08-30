# FVF Skin Lane Fix Implementation Plan

> For agentic workers: use the subagent-driven-development or executing-plans skill to execute this plan task by task. Steps use checkbox syntax for tracking.

Goal: Stop the MOD exporter from writing hard-coded 0xFE/0xFF into one-bone FVF reserved bytes, and keep importer/exporter skin offsets aligned with verified original MOD data.

Architecture: Add byte-level regression assertions to the existing writer maintenance guard, run them against the current bridge to establish the failure, then make the smallest per-FVF lane changes. Update the deployed old and current bridge copies together; CBF6 uses the independently verified Albam little-endian u16 bone lane at offset 6.

Tech Stack: Python 3.14, struct, existing RE6 MOD parser/writer maintenance guards.

---

### Task 1: Add failing byte-lane regressions

Files:
- Modify: C:/Users/1/Desktop/RE6 OLD/codex_python_export_bridge.py
- Modify: C:/Users/1/Desktop/RE6 脚本/脚本目录 Script Folder/codex_python_export_bridge.py

- [x] Add assertions for B0983013/14, 0CB68015/16, A8FAB018/19, and D877801B that expect byte 7 to remain zero while byte 6 carries the one-bone value.
- [x] Add a D877 source/import regression showing original byte 6 is the varying direct-global bone lane and byte 7 is zero padding.
- [x] Run the focused maintenance guards and confirm the old hard-coded constants fail.

### Task 2: Apply minimal writer/parser fixes

Files:
- Modify: C:/Users/1/Desktop/RE6 OLD/codex_python_export_bridge.py
- Modify: C:/Users/1/Desktop/RE6 OLD/codex_re6_mod_import_fbx.py
- Modify: C:/Users/1/Desktop/RE6 脚本/脚本目录 Script Folder/codex_python_export_bridge.py
- Modify: C:/Users/1/Desktop/RE6 脚本/脚本目录 Script Folder/codex_re6_mod_import_fbx.py

- [x] Replace only the proven reserved-byte constants with zero.
- [x] Align D877801B importer/source-truth metadata to byte 6, matching the original MOD sample and writer.
- [x] Align CBF6C01A importer/source-truth metadata to a little-endian u16 bone at offset 6, confirmed by Albam Redux's MOD-21 schema and generated reader/writer.

### Task 3: Verify

Files:
- Read: both bridge copies, both importer copies, original MOD samples.

- [x] Run the old bridge's full writer suite and both importer's full maintenance suites; the current bridge still reports pre-existing unrelated suite failures.
- [x] Re-scan original MOD samples and compare output lane bytes/strides.
- [x] Check syntax and report exact changed paths and remaining unverified FVF risks.
