from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "PC-REHD Code X Launcher.py"
MODULE_NAME = "pc_rehd_launcher_writer_preload_test"


def load_launcher_module():
    existing = sys.modules.get(MODULE_NAME)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(MODULE_NAME, MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load Launcher source: {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


class _RetryHarness:
    def __init__(self, module, failures: int) -> None:
        self.module = module
        self.failures = failures
        self.attempts = 0

    def _run_writer_request_once(self, _request):
        self.attempts += 1
        if self.attempts <= self.failures:
            raise RuntimeError(
                "Writer process terminated before READY "
                f"(worker_pid={1000 + self.attempts}, exit_code=None, "
                "error=TimeoutError: Writer preload did not reach READY)"
            )
        return {"status": "OK", "output_mod": "output.NewMOD"}


class WriterPreloadRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_launcher_module()

    def test_preload_timeout_is_retryable_before_a_write_request(self):
        harness = _RetryHarness(self.module, failures=1)
        result = self.module.LauncherApp._run_writer_in_process(
            harness,
            {"request_id": "a" * 32},
        )
        self.assertEqual(result["status"], "OK")
        self.assertEqual(harness.attempts, 2)
        recovery = result.get("writer_transport_recovery")
        self.assertIsInstance(recovery, dict)
        self.assertEqual(recovery.get("retry_count"), 1)

    def test_final_preload_failure_is_written_to_primary_export_txt(self):
        harness = _RetryHarness(self.module, failures=99)
        with tempfile.TemporaryDirectory() as raw_root:
            request = {
                "request_id": "b" * 32,
                "target_max_pid": 58192,
                "fbx_path": str(Path(raw_root) / ("export_pid58192_" + "b" * 32 + ".fbx")),
                "export_options": {"log_dir": raw_root},
            }
            with self.assertRaises(RuntimeError) as raised:
                self.module.LauncherApp._run_writer_in_process(harness, request)
            report = getattr(raised.exception, "_pc_rehd_bug_control", {})
            self.assertEqual(report.get("diagnosis", {}).get("code"), "EXPORT_WRITER_PRELOAD_TIMEOUT")
            self.assertEqual(report.get("failure_stage"), "writer_preload")
            self.assertEqual(report.get("extra", {}).get("retry_count"), 1)
            primary = Path(raw_root) / ("export_pid58192_" + "b" * 32 + ".txt")
            self.assertTrue(primary.is_file())
            text = primary.read_text(encoding="utf-8-sig")
            self.assertIn("CODE=EXPORT_WRITER_PRELOAD_TIMEOUT", text)
            self.assertIn("FAILURE_STAGE=writer_preload", text)


if __name__ == "__main__":
    unittest.main()
