from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = ROOT / "codex_python_runtime_bootstrap.py"
LAUNCHER_PATH = ROOT / "PC-REHD Code X Launcher.py"


def _load_bootstrap():
    spec = importlib.util.spec_from_file_location("codex_runtime_bootstrap_test", BOOTSTRAP_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launcher_runtime_contract_requires_exact_python_3_14_7():
    bootstrap = _load_bootstrap()
    assert bootstrap.REQUIRED_PYTHON_RUNTIME == (3, 14, 7)
    assert bootstrap._runtime_matches_required_python((3, 14, 7), releaselevel="final")
    assert not bootstrap._runtime_matches_required_python((3, 14, 6), releaselevel="final")
    assert not bootstrap._runtime_matches_required_python((3, 15, 0), releaselevel="final")
    assert not bootstrap._runtime_matches_required_python((3, 14, 7), releaselevel="candidate")


def test_launcher_does_not_keep_running_after_bootstrap_failure():
    source = LAUNCHER_PATH.read_text(encoding="utf-8")
    assert "ensure_required_python_runtime" in source
    assert "Keep the original fixed runtimes usable" not in source


def test_exact_upgrade_reports_dynamic_version_and_restarts_only_after_success():
    source = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    assert "_RequiredRuntimeInstallProgress" in source
    assert "Python {self.required_text} 正在自动安装" in source
    assert "progress.finish(success=True)" in source
    assert "progress.finish(success=False)" in source
    assert "str(upgrade.get(\"status\", \"\")).casefold() != \"promoted\"" in source


def test_manual_install_escape_keeps_launcher_open_and_waits_for_custom_python():
    source = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    assert "Stop automatic install and choose another folder" in source
    assert "wait_for_manual_runtime" in source
    assert "_find_required_python_installer_path" in source
    assert "自动安装已停止" in source


def test_managed_install_never_targets_user_custom_python_directory():
    source = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    assert "PC_REHD_CODE_X_PYTHON" in source
    assert "Refusing to install managed Python over a user-selected directory" in source
    assert "RUNTIME_INTERPRETER_ROOT_DIR" in source


def test_custom_python_discovery_checks_registry_and_launcher_inventory():
    source = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    assert "def _discover_python_paths_via_registry" in source
    assert "PythonCore" in source
    assert "_discover_python_paths_via_launcher_list" in source
    assert "_find_exact_python_runtime_patch_candidate" in source


def test_bootstrap_popups_use_modern_tk_ui_instead_of_native_message_box():
    source = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    assert "MessageBoxW" not in source
    assert "tk.Text" in source
    assert "Microsoft YaHei" in source
    assert "Microsoft YaHei UI" not in source
    assert "#F59E0B" in source


def test_pyassimp_vendor_does_not_import_removed_distutils_on_python_314():
    helper_paths = list(ROOT.rglob("pyassimp/helper.py"))
    assert helper_paths
    source = helper_paths[0].read_text(encoding="utf-8")
    assert "from distutils.sysconfig import get_python_lib" not in source
    assert "sysconfig" in source


def test_assimp_vendor_does_not_import_removed_distutils_on_python_314():
    helper_paths = list(ROOT.rglob("assimp/helper.py"))
    assert helper_paths
    source = helper_paths[0].read_text(encoding="utf-8")
    assert "from distutils.sysconfig import get_python_lib" not in source
    assert "sysconfig" in source


def test_vendor_certifi_payload_is_current_and_pure_python():
    metadata_paths = list(ROOT.rglob("certifi-2026.7.22.dist-info/METADATA"))
    assert metadata_paths
    source = metadata_paths[0].read_text(encoding="utf-8")
    assert "Name: certifi" in source
    assert "Version: 2026.7.22" in source
