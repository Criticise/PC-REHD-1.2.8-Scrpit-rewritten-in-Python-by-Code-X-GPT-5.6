from __future__ import annotations

# =============================================================================
# AI MAINTENANCE GATE: PY DEPENDENCIES <-> BOOTSTRAP
# READ THIS BEFORE EDITING THIS FILE OR ANY PACKAGE UNDER "PY依赖 PY Libs".
#
# The bootstrap and local dependency bundles are one compatibility unit. If an
# AI changes an accelerator version, capability flag, compiled API, build rule,
# candidate order, Python ABI policy, or rollback behavior, it MUST inspect and
# update both sides in the same task:
#   1. codex_python_runtime_bootstrap.py
#   2. _vendor_fixed/codex_accel_re6_v4/src/*/package/__init__.py
#   3. matching setup.py, prebuilt cpXXX wrappers, release copies, and READMEs
#   4. PYTHON_RUNTIME_MAINTENANCE_CHARTER.md
#
# Do not finish such a task until the current cp314 release floor and any A/B
# candidate both produce equivalent accelerated and pure-Python output.
# Search for "AI MAINTENANCE GATE" to find every synchronized maintenance site.
# =============================================================================

import argparse
import ast
import copy
from contextlib import contextmanager
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import mmap
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable


def _run_hidden_subprocess(*popenargs: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Run non-interactive Bootstrap children without allocating Windows consoles."""
    hide_window = bool(kwargs.pop("hide_window", True))
    if os.name == "nt":
        kwargs["creationflags"] = int(kwargs.get("creationflags", 0) or 0) | int(
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        if hide_window and kwargs.get("startupinfo") is None:
            startup_info = subprocess.STARTUPINFO()
            startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup_info.wShowWindow = subprocess.SW_HIDE
            kwargs["startupinfo"] = startup_info
    return subprocess.run(*popenargs, **kwargs)


BASE_DIR = Path(__file__).resolve().parent
RELEASE_DEPENDENCY_DIR_NAMES = (
    "发行版PY 依赖库 - 把文件夹内的文件夹打包7-ZIP",
    "发行版PY依赖库",
)
DEPENDENCY_WRAPPER_DIR_NAMES = (
    "PY依赖 PY Libs",
    "PY依赖",
    "PY Libs",
)


def _dependency_dir_contains_vendors(candidate: Path) -> bool:
    return (
        (candidate / "_vendor_fixed").exists()
        or (candidate / "_vendor_upgrade").exists()
        or (candidate / "_vendor_py").exists()
    )


def _dependency_candidate_score(candidate: Path) -> tuple[int, int, int, int]:
    """Prefer a usable fixed contract over a merely present wrapper directory."""
    fixed_root = candidate / "_vendor_fixed"
    packaged_abi_root = candidate / "_vendor_py" / f"cp{sys.version_info.major}{sys.version_info.minor}"
    patched_ufbx_root = fixed_root / "pyufbx_re6_v4" / "pyufbx-0.0.7"
    prebuilt_ufbx_root = (
        patched_ufbx_root
        / "build"
        / f"lib.win-amd64-cpython-{sys.version_info.major}{sys.version_info.minor}"
        / "ufbx"
    )
    return (
        1 if (prebuilt_ufbx_root / "__init__.py").is_file() else 0,
        1 if patched_ufbx_root.is_dir() else 0,
        1 if fixed_root.is_dir() else 0,
        1 if packaged_abi_root.is_dir() else 0,
    )


def _iter_dependency_base_dir_candidates(base_dir: Path) -> list[Path]:
    candidates: list[Path] = []

    def add_candidate(path_value: Path) -> None:
        normalized = path_value.resolve()
        if normalized not in candidates:
            candidates.append(normalized)

    for wrapper_dir_name in DEPENDENCY_WRAPPER_DIR_NAMES:
        add_candidate(base_dir / wrapper_dir_name)

    add_candidate(base_dir)

    for release_dir_name in RELEASE_DEPENDENCY_DIR_NAMES:
        release_dir = base_dir / release_dir_name
        for wrapper_dir_name in DEPENDENCY_WRAPPER_DIR_NAMES:
            add_candidate(release_dir / wrapper_dir_name)
            add_candidate(release_dir / wrapper_dir_name / wrapper_dir_name)
        add_candidate(release_dir)

    # Some archive tools add one redundant wrapper directory. Keep discovery
    # bounded to known names so a large MOD tree is never recursively scanned.
    for wrapper_dir_name in DEPENDENCY_WRAPPER_DIR_NAMES:
        add_candidate(base_dir / wrapper_dir_name / wrapper_dir_name)

    return candidates


def _resolve_dependency_base_dir(base_dir: Path) -> Path:
    candidates = [
        candidate
        for candidate in _iter_dependency_base_dir_candidates(base_dir)
        if _dependency_dir_contains_vendors(candidate)
    ]
    if candidates:
        return max(
            candidates,
            key=lambda candidate: (
                _dependency_candidate_score(candidate),
                -_iter_dependency_base_dir_candidates(base_dir).index(candidate),
            ),
        )
    return base_dir


DEPENDENCY_BASE_DIR = _resolve_dependency_base_dir(BASE_DIR)
FIXED_VENDOR_ROOT_DIR = DEPENDENCY_BASE_DIR / "_vendor_fixed"
UPGRADE_VENDOR_ROOT_DIR = DEPENDENCY_BASE_DIR / "_vendor_upgrade"
PACKAGED_VENDOR_ROOT_OVERRIDE_ENV = "CODEX_V4_PACKAGED_VENDOR_ROOT"
PACKAGED_VENDOR_PY_ROOT_DIR = Path(
    str(os.environ.get(PACKAGED_VENDOR_ROOT_OVERRIDE_ENV, "") or "").strip()
    or str(DEPENDENCY_BASE_DIR / "_vendor_py")
).expanduser()
RUNTIME_ROOT_OVERRIDE_ENV = "CODEX_V4_RUNTIME_ROOT"


def _resolve_runtime_root(dependency_base_dir: Path) -> Path:
    override_text = str(os.environ.get(RUNTIME_ROOT_OVERRIDE_ENV, "") or "").strip()
    if override_text:
        return Path(override_text).expanduser()
    local_root = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir())
    release_key = hashlib.sha1(
        str(dependency_base_dir).casefold().encode("utf-8", errors="replace"),
    ).hexdigest()[:16]
    return local_root / "CodexV4" / "RE6" / release_key


RUNTIME_ROOT_DIR = _resolve_runtime_root(DEPENDENCY_BASE_DIR)
VENDOR_PY_ROOT_DIR = RUNTIME_ROOT_DIR / "vendor"
VENDOR_PY_DIR = VENDOR_PY_ROOT_DIR / f"cp{sys.version_info.major}{sys.version_info.minor}"
PACKAGED_VENDOR_PY_DIR = PACKAGED_VENDOR_PY_ROOT_DIR / f"cp{sys.version_info.major}{sys.version_info.minor}"
RUNTIME_STATE_ROOT_DIR = RUNTIME_ROOT_DIR / "state"
RUNTIME_STATE_PATH = RUNTIME_STATE_ROOT_DIR / f"cp{sys.version_info.major}{sys.version_info.minor}.json"
BOOTSTRAP_RUNTIME_STATE_PATH = RUNTIME_STATE_ROOT_DIR / "bootstrap.json"
RUNTIME_LOCK_ROOT_DIR = RUNTIME_ROOT_DIR / "locks"
RUNTIME_STAGING_ROOT_DIR = RUNTIME_ROOT_DIR / "staging"
RUNTIME_QUARANTINE_ROOT_DIR = RUNTIME_ROOT_DIR / "quarantine"
RUNTIME_LAST_KNOWN_GOOD_ROOT_DIR = RUNTIME_ROOT_DIR / "last-known-good"
RUNTIME_LAST_KNOWN_GOOD_DIR = RUNTIME_LAST_KNOWN_GOOD_ROOT_DIR / f"cp{sys.version_info.major}{sys.version_info.minor}"
RUNTIME_BACKGROUND_UPGRADE_DIR = (
    RUNTIME_ROOT_DIR
    / "diagnostics"
    / "background-upgrade"
    / f"cp{sys.version_info.major}{sys.version_info.minor}"
)
RUNTIME_INTERPRETER_ROOT_DIR = RUNTIME_ROOT_DIR / "interpreters"
RUNTIME_INTERPRETER_QUARANTINE_DIR = RUNTIME_INTERPRETER_ROOT_DIR / "quarantine"
RUNTIME_INTERPRETER_DOWNLOAD_DIR = RUNTIME_ROOT_DIR / "downloads" / "python"
RUNTIME_INTERPRETER_UPDATE_LOCK_PATH = RUNTIME_LOCK_ROOT_DIR / "python_runtime_ab.lock"
RUNTIME_INSTALL_LOCK_PATH = RUNTIME_LOCK_ROOT_DIR / (
    "runtime_"
    + hashlib.sha1(str(RUNTIME_ROOT_DIR).casefold().encode("utf-8", errors="replace")).hexdigest()[:16]
    + f"_cp{sys.version_info.major}{sys.version_info.minor}"
    + ".lock"
)
BOOTSTRAP_STATE_LOCK_PATH = RUNTIME_LOCK_ROOT_DIR / "bootstrap_state.lock"
RUNTIME_STATE_LOCK_PATH = RUNTIME_LOCK_ROOT_DIR / f"state_cp{sys.version_info.major}{sys.version_info.minor}.lock"
PATCHED_UFBX_BUNDLE_DIR = FIXED_VENDOR_ROOT_DIR / "pyufbx_re6_v4"
PATCHED_UFBX_SOURCE_DIR = PATCHED_UFBX_BUNDLE_DIR / "pyufbx-0.0.7"
PATCHED_UFBX_ARCHIVE_PATH = PATCHED_UFBX_BUNDLE_DIR / "pyufbx-0.0.7-codex.tar.gz"
PATCHED_UFBX_MARKER = "re6_v4_bridge_20260709_uvsets"
PATCHED_UFBX_VERSION = "0.0.7"
PATCHED_UFBX_CONTRACT_DIR = PATCHED_UFBX_BUNDLE_DIR / "contract"
PATCHED_UFBX_CONTRACT_FBX_PATH = PATCHED_UFBX_CONTRACT_DIR / "pl0600_patched_ufbx_contract.fbx"
PATCHED_UFBX_CONTRACT_BASELINE_PATH = PATCHED_UFBX_CONTRACT_DIR / "pl0600_patched_ufbx_contract.json"
PATCHED_UFBX_CONTRACT_FBX_SHA256 = "c7ee247dae12285f2a9ef99846b7c5a848602baac6a260754fb3915a85d6e994"
PATCHED_UFBX_CONTRACT_BASELINE_SHA256 = "79b2d84a8fa916bd7371cf015078b915aeddf15ea034afdf943a607efc0aeecb"
PATCHED_UFBX_APPROVED_SOURCE_FINGERPRINT = "tree-sha256:72eecc89c8a758e1ba816d92830bfaed37f754bc094c4963a9f25b3e5f063c01:18"
MIN_DELETE_SELECTED_STABLE_SLOT_CONTRACT_REVISION = 2
REQUIRED_RE6_MOD_IMPORT_FBX_CONTRACT_REVISION = 12
EXPORT_BRIDGE_REQUIRED_REGRESSION_STATUSES = (
    "OUTPUT_PATH_MUTEX_REGRESSION_STATUS",
    "SOURCE_ORDERED_SKIN_SELECTION_REGRESSION_STATUS",
    "PREPARED_64593023_WRITER_PASSTHROUGH_REGRESSION_STATUS",
    "LEGACY_COMPACTED_RECOVERY_REGRESSION_STATUS",
    "SOURCE_SKIN_TRUTH_REGRESSION_STATUS",
    "SEMANTIC_WRITER_LAYOUT_REGRESSION_STATUS",
    "ORDINARY_SKIN_ROUND_TRIP_REGRESSION_STATUS",
    "REAL_PL0600_SKIN_REGRESSION_STATUS",
    "ROUND_TRIP_BYTE_ALLOWLIST_REGRESSION_STATUS",
    "HEADER_BUCKET_AUTHORITY_REGRESSION_STATUS",
    "REQUIRED_FBX_GEOMETRY_REGRESSION_STATUS",
    "SPECIAL_MESH_SOURCE_FALLBACK_REGRESSION_STATUS",
    "STANDARD_RE6_EXPORT_MESH_NAME_REGRESSION_STATUS",
    "MESH_SLOT_LIMIT_REGRESSION_STATUS",
    "DELETE_SELECTED_STABLE_SLOT_REGRESSION_STATUS",
    "UINT16_VERTEX_GROUP_ROLLOVER_REGRESSION_STATUS",
    "PYMXS_GEOMETRY_BOUNDARY_REGRESSION_STATUS",
    "MEMORY_SCENE_CONTRACT_REGRESSION_STATUS",
)
EXPORT_BRIDGE_OPTIONAL_FIXTURE_REGRESSION_STATUSES = frozenset(
    {"REAL_PL0600_SKIN_REGRESSION_STATUS"}
)
LOCAL_PILLOW_BUNDLE_DIR = FIXED_VENDOR_ROOT_DIR / "pillow_re6_v4"
UPGRADE_PILLOW_BUNDLE_DIR = UPGRADE_VENDOR_ROOT_DIR / "pillow_re6_v4"
LOCAL_NUMPY_BUNDLE_DIR = FIXED_VENDOR_ROOT_DIR / "numpy_re6_v4"
LOCAL_ORJSON_BUNDLE_DIR = FIXED_VENDOR_ROOT_DIR / "orjson_re6_v4"
UPGRADE_ORJSON_BUNDLE_DIR = UPGRADE_VENDOR_ROOT_DIR / "orjson_re6_v4"
LOCAL_ACCELERATOR_BUNDLE_DIR = FIXED_VENDOR_ROOT_DIR / "codex_accel_re6_v4"
UPGRADE_ACCELERATOR_BUNDLE_DIR = UPGRADE_VENDOR_ROOT_DIR / "codex_accel_re6_v4"
LOCAL_PYTHON_BUILD_TOOLS_DIR = FIXED_VENDOR_ROOT_DIR / "python_build_tools_re6_v4"
LOCAL_ONLY_RUNTIME_INSTALL = False
NETWORK_REPAIR_ENABLED = True
OFFICIAL_PYPI_INDEX_URL = "https://pypi.org/simple"
DEFAULT_RUNTIME_LOCK_TIMEOUT_SECONDS = 45.0
DEFAULT_UPGRADE_CHECK_TTL_SECONDS = 86400.0
FAILED_UPGRADE_CHECK_TTL_SECONDS = 3600.0
ONLINE_BLOCKED_ARTIFACT_RECHECK_SECONDS = 21600.0
BACKGROUND_UPGRADE_LEASE_SECONDS = 300.0
BACKGROUND_UPGRADE_STATE_LOCK_TIMEOUT_SECONDS = 0.1
BACKGROUND_UPGRADE_ARTIFACT_RETENTION_SECONDS = 7.0 * 86400.0
BACKGROUND_UPGRADE_ARTIFACT_MAX_FILES = 64
BACKGROUND_UPGRADE_TEST_DELAY_ENV = "CODEX_V4_BACKGROUND_UPGRADE_TEST_DELAY"
BACKGROUND_UPGRADE_TEST_STATUS_ENV = "CODEX_V4_BACKGROUND_UPGRADE_TEST_STATUS"
SUPPORTED_PYTHON_MINORS = (
    (3, 14),
)
RECOMMENDED_PYTHON = (3, 14)
# Launcher activation is pinned to this full release.  The minor-level ABI
# policy above remains useful for dependency probing, but it must not allow a
# different patch release to start the user-facing Launcher.
REQUIRED_PYTHON_RUNTIME = (3, 14, 7)
ISOLATED_PYTHON_ENVIRONMENT = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
}
ALLOW_UNSUPPORTED_PYTHON_ENV = "CODEX_V4_ALLOW_UNSUPPORTED_PYTHON"
BOOTSTRAP_PYTHON_HINT_ENV = "CODEX_V4_PYTHON_EXE"
BOOTSTRAP_REEXEC_PATH_ENV = "CODEX_V4_BOOTSTRAP_ACTIVE_PYTHON"
BOOTSTRAP_REEXEC_DEPTH_ENV = "CODEX_V4_BOOTSTRAP_REEXEC_DEPTH"
BOOTSTRAP_TRIED_PYTHONS_ENV = "CODEX_V4_BOOTSTRAP_TRIED_PYTHONS"
RUNTIME_UI_LANGUAGE_ENV = "CODEX_V4_UI_LANGUAGE"
MINIMUM_SUPPORTED_PYTHON = min(SUPPORTED_PYTHON_MINORS)
RUNTIME_CANDIDATE_QUARANTINE_STATE_KEY = "runtime_candidate_quarantine"
RUNTIME_CANDIDATE_REHABILITATION_STATE_KEY = "runtime_candidate_rehabilitation"
PYTHON_RUNTIME_AB_STATE_KEY = "python_runtime_ab"
PYTHON_RUNTIME_AB_SCHEMA = "pc-rehd-code-x-python-runtime-ab-v1"
PYTHON_RUNTIME_CANDIDATE_TOKEN_ENV = "PC_REHD_CODE_X_RUNTIME_CANDIDATE_TOKEN"
PYTHON_RUNTIME_CANDIDATE_PATH_ENV = "PC_REHD_CODE_X_RUNTIME_CANDIDATE_PATH"
PYTHON_RUNTIME_RELEASE_INDEX_URL = "https://www.python.org/ftp/python/"
PYTHON_RUNTIME_UPDATE_CHECK_SECONDS = 24.0 * 3600.0
PYTHON_RUNTIME_UPDATE_FAILURE_RETRY_SECONDS = 6.0 * 3600.0
PYTHON_RUNTIME_FAILED_VERSION_RETRY_SECONDS = 7.0 * 86400.0
PYTHON_RUNTIME_DOWNLOAD_TIMEOUT_SECONDS = 120.0
PYTHON_RUNTIME_INSTALL_TIMEOUT_SECONDS = 1800.0
PYTHON_RUNTIME_CONTRACT_TIMEOUT_SECONDS = 1800.0
DEFAULT_RUNTIME_CANDIDATE_COOLDOWN_SECONDS = 3600.0
MAX_RUNTIME_CANDIDATE_COOLDOWN_SECONDS = 86400.0
_CONFIGURED_VENDOR_PATHS: list[str] = []
_VENDOR_LANE_CONTEXT: Path | None = None
_VENDOR_INCLUDE_PACKAGED_CONTEXT: bool | None = None
_EXPORT_BRIDGE_CONTRACT_CACHE: dict[tuple[str, ...], dict[str, object]] = {}


def _isolated_python_child_environment(
    base_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    environment = dict(os.environ if base_environment is None else base_environment)
    for variable_name in tuple(environment):
        if str(variable_name).upper().startswith("PYTHON"):
            environment.pop(variable_name, None)
    environment.update(ISOLATED_PYTHON_ENVIRONMENT)
    return environment


class DependencyBundleBrokenError(RuntimeError):
    error_type = "DEPENDENCY_BUNDLE_BROKEN"

    def __init__(self, message: str, report: dict[str, object] | None = None):
        super().__init__(message)
        self.report = dict(report or {})


class OnlineDependencyRepairError(RuntimeError):
    error_type = "ONLINE_DEPENDENCY_REPAIR_FAILED"

    def __init__(self, message: str, report: dict[str, object]):
        super().__init__(message)
        self.report = dict(report)


class RuntimeInstallLockTimeout(TimeoutError):
    error_type = "RUNTIME_LOCK_TIMEOUT"

    def __init__(self, message: str, report: dict[str, object]):
        super().__init__(message)
        self.report = dict(report)


def _atomic_write_runtime_bytes(path: str | Path, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(bytes(data))
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _sha256_file(path: Path, *, use_cache: bool = False) -> str:
    # Filesystem timestamps and file IDs are not content identities on Windows.
    # Keep the keyword for caller compatibility, but always hash actual bytes.
    del use_cache
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _runtime_lock_metadata_from_file(lock_file: Any) -> dict[str, object]:
    try:
        lock_file.seek(1)
        raw_payload = lock_file.read().decode("utf-8", errors="replace").strip().strip("\0")
        payload = json.loads(raw_payload) if raw_payload else {}
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _write_runtime_lock_metadata(lock_file: Any, payload: dict[str, object] | None) -> None:
    encoded = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if payload else b""
    lock_file.seek(0)
    lock_file.write(b"\0" + encoded)
    lock_file.truncate()
    lock_file.flush()
    os.fsync(lock_file.fileno())


def _process_is_alive(pid_value: object) -> bool | None:
    try:
        pid = int(pid_value)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            still_active = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information,
                False,
                pid,
            )
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            try:
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return None
                return int(exit_code.value) == still_active
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def _classify_runtime_lock_owner(
    owner: dict[str, object] | None,
    *,
    lock_path: Path = RUNTIME_INSTALL_LOCK_PATH,
) -> dict[str, object]:
    payload = dict(owner or {})
    alive = _process_is_alive(payload.get("pid"))
    if not payload:
        state = "unowned"
    elif alive is True:
        state = "active"
    elif alive is False:
        state = "stale"
    else:
        state = "unknown"
    return {
        "state": state,
        "owner_alive": alive,
        "owner": payload or None,
        "lock_path": str(lock_path),
    }


@contextmanager
def _runtime_install_lock(
    timeout_seconds: float = DEFAULT_RUNTIME_LOCK_TIMEOUT_SECONDS,
    *,
    lock_path: Path = RUNTIME_INSTALL_LOCK_PATH,
):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Do not use append mode here.  ``a+b`` forces every write to the end of
    # the file on Windows, so the owner record and the release marker were
    # concatenated forever despite seek(0)/truncate().  That eventually made
    # the metadata unreadable and hid the process holding the shared lock.
    lock_fd = os.open(
        str(lock_path),
        os.O_CREAT | os.O_RDWR,
        0o666,
    )
    lock_file = os.fdopen(lock_fd, "r+b")
    acquired = False
    windows_lock = None
    posix_lock = None
    try:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() <= 0:
            lock_file.write(b"\0")
            lock_file.flush()
        started_monotonic = time.monotonic()
        deadline = started_monotonic + max(0.05, float(timeout_seconds))
        last_owner_report = _classify_runtime_lock_owner(
            _runtime_lock_metadata_from_file(lock_file),
            lock_path=lock_path,
        )
        if os.name == "nt":
            import msvcrt

            windows_lock = msvcrt
            while not acquired:
                lock_file.seek(0)
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    last_owner_report = _classify_runtime_lock_owner(
                        _runtime_lock_metadata_from_file(lock_file),
                        lock_path=lock_path,
                    )
                    if time.monotonic() >= deadline:
                        timeout_report = {
                            **last_owner_report,
                            "state": "timeout",
                            "owner_state": last_owner_report.get("state"),
                            "waited_seconds": round(time.monotonic() - started_monotonic, 3),
                            "timeout_seconds": float(timeout_seconds),
                        }
                        raise RuntimeInstallLockTimeout(
                            "Timed out waiting for the shared Codex runtime install lock: "
                            + str(lock_path)
                            + " | owner="
                            + json.dumps(last_owner_report, ensure_ascii=False),
                            timeout_report,
                        )
                    time.sleep(0.1)
        else:
            import fcntl

            posix_lock = fcntl
            while not acquired:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    last_owner_report = _classify_runtime_lock_owner(
                        _runtime_lock_metadata_from_file(lock_file),
                        lock_path=lock_path,
                    )
                    if time.monotonic() >= deadline:
                        timeout_report = {
                            **last_owner_report,
                            "state": "timeout",
                            "owner_state": last_owner_report.get("state"),
                            "waited_seconds": round(time.monotonic() - started_monotonic, 3),
                            "timeout_seconds": float(timeout_seconds),
                        }
                        raise RuntimeInstallLockTimeout(
                            "Timed out waiting for the shared Codex runtime install lock: "
                            + str(lock_path)
                            + " | owner="
                            + json.dumps(last_owner_report, ensure_ascii=False),
                            timeout_report,
                        )
                    time.sleep(0.1)
        lock_report = {
            "state": "acquired",
            "previous_owner_state": last_owner_report.get("state"),
            "waited_seconds": round(time.monotonic() - started_monotonic, 3),
            "timeout_seconds": float(timeout_seconds),
            "lock_path": str(lock_path),
            "owner": {
                "pid": os.getpid(),
                "python_executable": sys.executable,
                "python_tag": f"cp{sys.version_info.major}{sys.version_info.minor}",
                "acquired_epoch": time.time(),
                "token": uuid.uuid4().hex,
            },
        }
        _write_runtime_lock_metadata(lock_file, dict(lock_report["owner"]))
        yield lock_report
    finally:
        if acquired:
            try:
                _write_runtime_lock_metadata(lock_file, None)
            except OSError:
                pass
        if acquired and windows_lock is not None:
            try:
                lock_file.seek(0)
                windows_lock.locking(lock_file.fileno(), windows_lock.LK_UNLCK, 1)
            except OSError:
                pass
        if acquired and posix_lock is not None:
            try:
                posix_lock.flock(lock_file.fileno(), posix_lock.LOCK_UN)
            except OSError:
                pass
        lock_file.close()


def get_runtime_lock_report(*, lock_path: Path = RUNTIME_INSTALL_LOCK_PATH) -> dict[str, object]:
    if lock_path.is_file() is not True:
        return {
            "state": "not-created",
            "lock_path": str(lock_path),
            "owner": None,
            "owner_alive": None,
        }
    try:
        lock_file = lock_path.open("r+b")
    except OSError as exc:
        return {
            "state": "unreadable",
            "lock_path": str(lock_path),
            "owner": None,
            "owner_alive": None,
            "error": str(exc),
        }
    acquired = False
    windows_lock = None
    posix_lock = None
    try:
        owner_report = _classify_runtime_lock_owner(
            _runtime_lock_metadata_from_file(lock_file),
            lock_path=lock_path,
        )
        if os.name == "nt":
            import msvcrt

            windows_lock = msvcrt
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl

            posix_lock = fcntl
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        if acquired:
            return {
                **owner_report,
                "state": "stale-metadata" if owner_report.get("owner") else "unlocked",
                "os_lock_held": False,
            }
        owner_state = str(owner_report.get("state", "unknown"))
        return {
            **owner_report,
            "state": "active" if owner_state == "active" else "contended-" + owner_state,
            "os_lock_held": True,
        }
    finally:
        if acquired and windows_lock is not None:
            try:
                lock_file.seek(0)
                windows_lock.locking(lock_file.fileno(), windows_lock.LK_UNLCK, 1)
            except OSError:
                pass
        if acquired and posix_lock is not None:
            try:
                posix_lock.flock(lock_file.fileno(), posix_lock.LOCK_UN)
            except OSError:
                pass
        lock_file.close()


def run_runtime_lock_metadata_regression() -> dict[str, object]:
    """Strict maintenance check: lock ownership must overwrite, never append."""
    with tempfile.TemporaryDirectory(prefix="pc-rehd-runtime-lock-regression-") as raw_root:
        lock_path = Path(raw_root) / "runtime.lock"
        sizes: list[int] = []
        for _ in range(4):
            with _runtime_install_lock(timeout_seconds=2.0, lock_path=lock_path):
                pass
            sizes.append(lock_path.stat().st_size)
        released_metadata = _runtime_lock_metadata_from_file(lock_path)
        released_report = get_runtime_lock_report(lock_path=lock_path)
    size_stable = bool(sizes) and max(sizes) - min(sizes) <= 8
    released = (
        released_metadata == {}
        and released_report.get("state") == "unlocked"
        and released_report.get("os_lock_held") is False
    )
    return {
        "status": "PASS" if size_stable and released else "FAIL",
        "sizes": sizes,
        "size_stable": size_stable,
        "released_metadata": released_metadata,
        "released_report": released_report,
    }


@contextmanager
def _bootstrap_state_lock(timeout_seconds: float = 10.0):
    with _runtime_install_lock(
        timeout_seconds=timeout_seconds,
        lock_path=BOOTSTRAP_STATE_LOCK_PATH,
    ) as report:
        yield report


@contextmanager
def _abi_state_lock(timeout_seconds: float = 10.0):
    with _runtime_install_lock(
        timeout_seconds=timeout_seconds,
        lock_path=RUNTIME_STATE_LOCK_PATH,
    ) as report:
        yield report


def get_runtime_ui_language() -> str:
    override = str(os.environ.get(RUNTIME_UI_LANGUAGE_ENV, "") or "").strip().lower()
    if override in {"zh", "zh-cn", "zh-hans", "cn", "chinese"}:
        return "zh"
    if override in {"en", "english"}:
        return "en"
    if os.name == "nt":
        try:
            import ctypes

            language_id = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())
            if (language_id & 0x03FF) == 0x0004:  # LANG_CHINESE
                return "zh"
            return "en"
        except Exception:
            pass
    try:
        import locale

        locale_name = str(locale.getlocale()[0] or "").lower()
        if locale_name.startswith("zh") or "chinese" in locale_name:
            return "zh"
    except Exception:
        pass
    return "en"


def runtime_ui_is_chinese() -> bool:
    return get_runtime_ui_language() == "zh"


def runtime_ui_text(chinese_text: str, english_text: str) -> str:
    return str(chinese_text if runtime_ui_is_chinese() else english_text)

LOCAL_ACCELERATOR_IMPORTS = (
    "codex_fbx_probe_accel",
    "codex_uv_layout_accel",
)
LOCAL_ACCELERATOR_HEALTH_REQUIREMENTS = {
    "codex_fbx_probe_accel": {
        "minimum_version": "0.3.0",
        "capabilities": (
            "PRESERVES_CORNER_NORMALS",
            "PRESERVES_RE6_NORMAL_BYTES",
            "USES_INVERSE_TRANSPOSE_NORMALS",
        ),
        "contract_revision": 2,
    },
    "codex_uv_layout_accel": {
        "minimum_version": "0.2.0",
        "capabilities": ("PRESERVES_EXPORT_VERTEX_SPLITS",),
        "contract_revision": 2,
    },
}
# AI MAINTENANCE GATE: changing any fixed accelerator/build-tool source file
# requires updating its SHA-256 here. A one-sided dependency edit must make the
# sync regression fail until bootstrap and release copies are reviewed.
ACCELERATOR_FIXED_SOURCE_SHA256 = {
    "codex_accel_re6_v4/src/codex_fbx_probe_accel/setup.py": "a84faa8aaff47c55be3eb350d25e9340a7e5adc14910d971395449c741e1c238",
    "codex_accel_re6_v4/src/codex_fbx_probe_accel/codex_fbx_probe_accel/__init__.py": "c8fec3d2090914e9b296df2c04769b9f0432a2b56bad3d9e22e925a621e7a0f9",
    "codex_accel_re6_v4/src/codex_fbx_probe_accel/codex_fbx_probe_accel/_fbx_geometry_core.c": "0604d99d0f9b6a346837118f3a60ed1740cc3f81af4e1a8edac024319ccf50e5",
    "codex_accel_re6_v4/src/codex_uv_layout_accel/setup.py": "a2c54381fe426e511ccc41090e712b426e2fc262f7bcef9f1536f08dbb69b6c1",
    "codex_accel_re6_v4/src/codex_uv_layout_accel/codex_uv_layout_accel/__init__.py": "1720c9fd449073dae57666f0069ed2f2d5faf570a5f0aa354fc60121e359bad8",
    "codex_accel_re6_v4/src/codex_uv_layout_accel/codex_uv_layout_accel/_uv_layout_core.c": "8cee9834bae36711eac52ff529284cdb0e325735f6a4c7e1617d25073edc93e6",
    "codex_accel_re6_v4/cp314/codex_fbx_probe_accel/__init__.py": "c8fec3d2090914e9b296df2c04769b9f0432a2b56bad3d9e22e925a621e7a0f9",
    "codex_accel_re6_v4/cp314/codex_fbx_probe_accel/_fbx_geometry_core.cp314-win_amd64.pyd": "6b8dc4d8165ba674f2f3f0ff8cf450b1e755fa11237eded647d23a21d2c59f74",
    "codex_accel_re6_v4/cp314/codex_uv_layout_accel/__init__.py": "1720c9fd449073dae57666f0069ed2f2d5faf570a5f0aa354fc60121e359bad8",
    "codex_accel_re6_v4/cp314/codex_uv_layout_accel/_uv_layout_core.cp314-win_amd64.pyd": "fa0e782041537f20672cb0c8262bf374549e209a49030036bd27da8a843a422c",
    "python_build_tools_re6_v4/cython-3.2.9-cp314-cp314-win_amd64.whl": "56d95c0674c25f281c6ae8f1d17bd425d6c2818bb304ff781831bb5d00d04b0b",
    "python_build_tools_re6_v4/setuptools-83.0.0-py3-none-any.whl": "29b23c360f22f414dc7336bb39178cc7bcbf6021ed2733cde173f09dba19abb3",
}
# Upgrade candidates are intentionally empty today. Adding any managed file
# under _vendor_upgrade/codex_accel_re6_v4 without registering its SHA-256 here
# makes --accelerator-sync-check fail and prevents one-sided upgrades.
ACCELERATOR_APPROVED_UPGRADE_SHA256: dict[str, str] = {}
ACCELERATOR_SYNC_REPORT_CACHE: dict[bool, dict[str, object]] = {}
NUMPY_FIXED_WHEEL_CONTRACT = {
    "cp314": {
        "filename": "numpy-2.5.1-cp314-cp314-win_amd64.whl",
        "sha256": "24d0eb82c0541d3415a33425db64ae439dffccd7b4dbcb30e7c35120205c506a",
    },
}
IMPORT_POLICY = {
    "numpy": {
        "allow_upgrade": True,
        "distribution_name": "numpy",
        "policy_label": "abi-hash-fixed-floor",
        "repair_mode": "online-repair",
        "online_spec": "numpy>=2.5.1,<3",
    },
    "ufbx": {
        "allow_upgrade": False,
        "distribution_name": "ufbx",
        "policy_label": "patched-local-locked",
        "repair_mode": "pinned-local-only",
        "runtime_dependencies": ("numpy",),
    },
    "PIL": {
        "allow_upgrade": True,
        "distribution_name": "Pillow",
        "policy_label": "local-upgrade-floor",
        "repair_mode": "online-repair",
        "online_spec": "Pillow>=12.3,<13",
    },
    "orjson": {
        "allow_upgrade": True,
        "distribution_name": "orjson",
        "policy_label": "local-upgrade-floor",
        "repair_mode": "online-repair",
        "online_spec": "orjson>=3.11.9,<4",
    },
    "codex_fbx_probe_accel": {
        "allow_upgrade": True,
        "distribution_name": "codex_fbx_probe_accel",
        "policy_label": "local-compiled-upgrade-floor",
        "repair_mode": "pinned-local-only",
        "minimum_compatible_version": "0.2.0",
        "contract_revision": 2,
    },
    "codex_uv_layout_accel": {
        "allow_upgrade": True,
        "distribution_name": "codex_uv_layout_accel",
        "policy_label": "local-compiled-upgrade-floor",
        "repair_mode": "pinned-local-only",
        "minimum_compatible_version": "0.2.0",
        "contract_revision": 2,
    },
}
APPROVED_IMPORTS = (
    "numpy",
    "ufbx",
    "PIL",
    "orjson",
)
OPTIONAL_RUNTIME_IMPORTS = (
    "orjson",
    *LOCAL_ACCELERATOR_IMPORTS,
)


def _python_tag(version_info: object | None = None) -> str:
    if version_info is None:
        major, minor = (sys.version_info.major, sys.version_info.minor)
    elif hasattr(version_info, "major") and hasattr(version_info, "minor"):
        major, minor = (int(getattr(version_info, "major")), int(getattr(version_info, "minor")))
    elif isinstance(version_info, (tuple, list)) and len(version_info) >= 2:
        major, minor = (int(version_info[0]), int(version_info[1]))
    else:
        raise TypeError(f"Unsupported version_info payload: {version_info!r}")
    return f"cp{major}{minor}"


def _find_local_wheel_candidates(
    bundle_dir: Path,
    distribution_name: str,
    *,
    version_info: object | None = None,
) -> tuple[str, ...]:
    candidates: list[str] = []
    tag = _python_tag(version_info)
    if bundle_dir.exists():
        for wheel_path in sorted(bundle_dir.glob(f"{distribution_name}-*-{tag}-{tag}-win_amd64.whl")):
            candidates.append(str(wheel_path))
        for wheel_path in sorted(bundle_dir.glob(f"{distribution_name}-*-win_amd64.whl")):
            wheel_name = wheel_path.name.casefold()
            abi_match = re.search(r"-cp(\d{2,3})-cp\1-", wheel_name)
            if abi_match is not None and f"-cp{abi_match.group(1)}-cp{abi_match.group(1)}-" != f"-{tag}-{tag}-":
                continue
            wheel_text = str(wheel_path)
            if wheel_text not in candidates:
                candidates.append(wheel_text)
    return tuple(candidates)


def _merge_candidate_groups(*candidate_groups: tuple[str, ...]) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for candidate_group in candidate_groups:
        for candidate in candidate_group:
            normalized = str(candidate)
            if normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
    return tuple(merged)


def _find_local_fixed_pillow_candidates() -> tuple[str, ...]:
    return _find_local_wheel_candidates(LOCAL_PILLOW_BUNDLE_DIR, "Pillow")


def _fixed_numpy_wheel_contract(version_info: object | None = None) -> dict[str, str] | None:
    payload = NUMPY_FIXED_WHEEL_CONTRACT.get(_python_tag(version_info))
    return dict(payload) if isinstance(payload, dict) else None


def _get_fixed_numpy_wheel_report(version_info: object | None = None) -> dict[str, object]:
    python_tag = _python_tag(version_info)
    contract = _fixed_numpy_wheel_contract(version_info)
    if contract is None:
        return {
            "python_tag": python_tag,
            "path": None,
            "present": False,
            "hash_ready": False,
            "expected_sha256": None,
            "actual_sha256": None,
            "error": f"No fixed NumPy hash contract is registered for {python_tag}.",
        }
    wheel_path = LOCAL_NUMPY_BUNDLE_DIR / contract["filename"]
    actual_sha256 = _sha256_file(wheel_path, use_cache=False) if wheel_path.is_file() else None
    expected_sha256 = contract["sha256"].lower()
    hash_ready = actual_sha256 is not None and actual_sha256.lower() == expected_sha256
    error = ""
    if wheel_path.is_file() is not True:
        error = "Fixed NumPy wheel is missing."
    elif hash_ready is not True:
        error = "Fixed NumPy wheel SHA-256 does not match its ABI contract."
    return {
        "python_tag": python_tag,
        "path": str(wheel_path),
        "present": wheel_path.is_file(),
        "hash_ready": hash_ready,
        "expected_sha256": expected_sha256,
        "actual_sha256": actual_sha256,
        "error": error,
    }


def _find_local_fixed_numpy_candidates(*, version_info: object | None = None) -> tuple[str, ...]:
    report = _get_fixed_numpy_wheel_report(version_info)
    path_text = str(report.get("path", "") or "")
    return (path_text,) if report.get("hash_ready") is True and path_text else tuple()


def _find_local_upgrade_pillow_candidates() -> tuple[str, ...]:
    return _find_local_wheel_candidates(UPGRADE_PILLOW_BUNDLE_DIR, "Pillow")


def _find_local_pillow_candidates() -> tuple[str, ...]:
    return _merge_candidate_groups(
        _find_local_upgrade_pillow_candidates(),
        _find_local_fixed_pillow_candidates(),
    )


def _find_local_fixed_orjson_candidates() -> tuple[str, ...]:
    return _find_local_wheel_candidates(LOCAL_ORJSON_BUNDLE_DIR, "orjson")


def _find_local_upgrade_orjson_candidates() -> tuple[str, ...]:
    return _find_local_wheel_candidates(UPGRADE_ORJSON_BUNDLE_DIR, "orjson")


def _find_local_orjson_candidates() -> tuple[str, ...]:
    return _merge_candidate_groups(
        _find_local_upgrade_orjson_candidates(),
        _find_local_fixed_orjson_candidates(),
    )


def _is_local_package_dir(path: Path, *, allow_prebuilt_only: bool = False) -> bool:
    if path.exists() is not True or path.is_dir() is not True:
        return False
    if (path / "__init__.py").exists():
        return True
    if allow_prebuilt_only is True:
        return False
    return any((path / marker).exists() for marker in ("pyproject.toml", "setup.py", "setup.cfg"))


def _is_local_source_package_dir(path: Path) -> bool:
    if path.exists() is not True or path.is_dir() is not True:
        return False
    if (path / "__init__.py").exists():
        return False
    return any((path / marker).exists() for marker in ("pyproject.toml", "setup.py", "setup.cfg"))


def _accelerator_candidate_paths(
    bundle_dir: Path,
    import_name: str,
    *,
    version_info: object | None = None,
) -> tuple[Path, ...]:
    py_tag = _python_tag(version_info)
    return (
        bundle_dir / py_tag / import_name,
        bundle_dir / import_name / py_tag,
        bundle_dir / "src" / import_name,
        bundle_dir / import_name / "src",
    )


def _find_accelerator_candidates_in_bundle(
    bundle_dir: Path,
    import_name: str,
    *,
    version_info: object | None = None,
) -> tuple[str, ...]:
    candidates: list[str] = []
    for candidate_path in _accelerator_candidate_paths(bundle_dir, import_name, version_info=version_info):
        if _is_local_package_dir(candidate_path):
            candidates.append(str(candidate_path))
    return tuple(candidates)


def _find_local_fixed_accelerator_candidates(import_name: str, *, version_info: object | None = None) -> tuple[str, ...]:
    return _find_accelerator_candidates_in_bundle(
        LOCAL_ACCELERATOR_BUNDLE_DIR,
        import_name,
        version_info=version_info,
    )


def _find_local_upgrade_accelerator_candidates(import_name: str, *, version_info: object | None = None) -> tuple[str, ...]:
    return _find_accelerator_candidates_in_bundle(
        UPGRADE_ACCELERATOR_BUNDLE_DIR,
        import_name,
        version_info=version_info,
    )


def _find_local_accelerator_candidates(import_name: str, *, version_info: object | None = None) -> tuple[str, ...]:
    return _merge_candidate_groups(
        _find_local_upgrade_accelerator_candidates(import_name, version_info=version_info),
        _find_local_fixed_accelerator_candidates(import_name, version_info=version_info),
    )


def _build_fixed_package_candidate_map() -> dict[str, tuple[str, ...]]:
    return {
        "numpy": _find_local_fixed_numpy_candidates(),
        "ufbx": (str(PATCHED_UFBX_SOURCE_DIR), str(PATCHED_UFBX_ARCHIVE_PATH)),
        "PIL": _find_local_fixed_pillow_candidates(),
        "orjson": _find_local_fixed_orjson_candidates(),
        "codex_fbx_probe_accel": _find_local_fixed_accelerator_candidates("codex_fbx_probe_accel"),
        "codex_uv_layout_accel": _find_local_fixed_accelerator_candidates("codex_uv_layout_accel"),
    }


def _build_upgrade_package_candidate_map() -> dict[str, tuple[str, ...]]:
    return {
        "numpy": tuple(),
        "PIL": _find_local_upgrade_pillow_candidates(),
        "orjson": _find_local_upgrade_orjson_candidates(),
        "codex_fbx_probe_accel": _find_local_upgrade_accelerator_candidates("codex_fbx_probe_accel"),
        "codex_uv_layout_accel": _find_local_upgrade_accelerator_candidates("codex_uv_layout_accel"),
    }


def _build_package_candidate_map() -> dict[str, tuple[str, ...]]:
    fixed_map = _build_fixed_package_candidate_map()
    upgrade_map = _build_upgrade_package_candidate_map()
    return {
        "numpy": fixed_map["numpy"],
        "ufbx": fixed_map["ufbx"],
        "PIL": _merge_candidate_groups(upgrade_map.get("PIL", tuple()), fixed_map.get("PIL", tuple())),
        "orjson": _merge_candidate_groups(upgrade_map.get("orjson", tuple()), fixed_map.get("orjson", tuple())),
        "codex_fbx_probe_accel": _merge_candidate_groups(
            upgrade_map.get("codex_fbx_probe_accel", tuple()),
            fixed_map.get("codex_fbx_probe_accel", tuple()),
        ),
        "codex_uv_layout_accel": _merge_candidate_groups(
            upgrade_map.get("codex_uv_layout_accel", tuple()),
            fixed_map.get("codex_uv_layout_accel", tuple()),
        ),
    }


PACKAGE_BY_IMPORT_NAME = _build_package_candidate_map()


def refresh_package_candidates() -> dict[str, tuple[str, ...]]:
    PACKAGE_BY_IMPORT_NAME.clear()
    PACKAGE_BY_IMPORT_NAME.update(_build_package_candidate_map())
    return dict(PACKAGE_BY_IMPORT_NAME)


def _get_fixed_install_candidates(import_name: str) -> tuple[str, ...]:
    return tuple(_build_fixed_package_candidate_map().get(import_name, ()))


def _get_upgrade_install_candidates(import_name: str) -> tuple[str, ...]:
    return tuple(_build_upgrade_package_candidate_map().get(import_name, ()))

MODULE_REQUIREMENTS = {
    "codex_re6_scene_compatibility": (),
    "codex_python_export_bridge": (),
    "codex_fbx_probe": ("ufbx",),
    # AI MAINTENANCE GATE: this standard-library module still belongs to the
    # clean source/release contract.  Changing its imports requires updating
    # this policy and the clean-copy probe in the same change.
    "codex_re6_mod_import_fbx": (),
    "codex_re6_tex_decode": (),
}

IMPORT_RUNTIME_MODULES = (
    "codex_re6_scene_compatibility",
    "codex_re6_mod_import_fbx",
)
EXPORT_RUNTIME_MODULES = (
    "codex_re6_scene_compatibility",
    "codex_python_export_bridge",
    "codex_fbx_probe",
)
TEXTURE_RUNTIME_MODULES = ("codex_re6_tex_decode",)
AUXILIARY_RUNTIME_MODULES = ()
AUXILIARY_PROBE_RUNTIME_MODULES = ("codex_fbx_probe",)
ALL_RUNTIME_MODULES = tuple(
    dict.fromkeys(
        (
            *IMPORT_RUNTIME_MODULES,
            *EXPORT_RUNTIME_MODULES,
            *TEXTURE_RUNTIME_MODULES,
            *AUXILIARY_RUNTIME_MODULES,
            *AUXILIARY_PROBE_RUNTIME_MODULES,
        )
    )
)

# Operation health is intentionally narrower than release health. A user action
# may only depend on its own required lane; optional accelerators and unrelated
# tools are diagnostics and must never turn that action into a failure.
OPERATION_RUNTIME_DOMAIN_SCHEMA = "pc-rehd-code-x-runtime-domain-v1"
OPERATION_RUNTIME_RECEIPT_SCHEMA = "pc-rehd-code-x-operation-receipt-v1"
OPERATION_RUNTIME_DOMAINS: dict[str, dict[str, object]] = {
    "import_mod": {
        "failure_domain": "mod_import",
        "modules": IMPORT_RUNTIME_MODULES,
        "required_imports": (),
        "optional_imports": ("orjson",),
    },
    "export_mod": {
        "failure_domain": "mod_export",
        "modules": EXPORT_RUNTIME_MODULES,
        "required_imports": ("ufbx",),
        "optional_imports": (
            "orjson",
            "codex_fbx_probe_accel",
            "codex_uv_layout_accel",
        ),
    },
    "texture": {
        "failure_domain": "texture",
        "modules": TEXTURE_RUNTIME_MODULES,
        "required_imports": (),
        "optional_imports": ("PIL",),
    },
    "auxiliary": {
        "failure_domain": "auxiliary",
        "modules": AUXILIARY_RUNTIME_MODULES,
        "required_imports": (),
        "optional_imports": (),
    },
    "auxiliary_probe": {
        "failure_domain": "auxiliary_probe",
        "modules": AUXILIARY_PROBE_RUNTIME_MODULES,
        "required_imports": ("ufbx",),
        "optional_imports": ("codex_fbx_probe_accel",),
    },
    "max_agent": {
        "failure_domain": "max_agent",
        "modules": (),
        "required_imports": (),
        "optional_imports": (),
    },
}
OPERATION_RUNTIME_ALIASES = {
    "import": "import_mod",
    "mod_import": "import_mod",
    "export": "export_mod",
    "mod_export": "export_mod",
    "tex": "texture",
    "sbc": "auxiliary",
    "adr": "auxiliary",
    "ems": "auxiliary",
    "aux_probe": "auxiliary_probe",
    "auxiliary_fbx_probe": "auxiliary_probe",
    "agent": "max_agent",
}

def _expand_policy_import_dependencies(import_names: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    visiting: set[str] = set()

    def add_import(import_name: str) -> None:
        if import_name in ordered:
            return
        if import_name in visiting:
            raise RuntimeError(f"Cyclic runtime dependency policy detected at {import_name}.")
        visiting.add(import_name)
        for dependency_name in IMPORT_POLICY.get(import_name, {}).get("runtime_dependencies", ()):
            add_import(str(dependency_name))
        visiting.remove(import_name)
        ordered.append(import_name)

    for requested_name in import_names:
        add_import(str(requested_name))
    return tuple(ordered)


REPAIR_REQUIRED_IMPORTS = _expand_policy_import_dependencies(
    tuple(
        dict.fromkeys(
            import_name
            for module_name in EXPORT_RUNTIME_MODULES
            for import_name in MODULE_REQUIREMENTS[module_name]
        )
    )
)
REPAIR_HEALTH_IMPORTS = tuple(
    dict.fromkeys((*REPAIR_REQUIRED_IMPORTS, *APPROVED_IMPORTS, *LOCAL_ACCELERATOR_IMPORTS))
)
REPAIR_IMPORT_DEPENDENCY_CLOSURE = {
    import_name: tuple(str(value) for value in policy.get("runtime_dependencies", ()))
    for import_name, policy in IMPORT_POLICY.items()
    if policy.get("runtime_dependencies")
}
PACKAGED_IMPORT_ARTIFACT_PATTERNS = {
    "numpy": ("numpy", "numpy.libs", "numpy-*.dist-info"),
}

ImportChecker = Callable[[str], bool]
Installer = Callable[[tuple[str, ...]], None]
RuntimePopup = Callable[[str, str], object]
OPTIONAL_RUNTIME_MODULE_CACHE: dict[str, Any | None] = {}
IMPORT_HEALTH_ERRORS: dict[str, str] = {}
RUNTIME_ADVISORIES: list[dict[str, Any]] = []
RUNTIME_ADVISORY_POPUP_SIGNATURES: set[str] = set()
LAST_INSTALL_PROVENANCE: dict[str, dict[str, object]] = {}


def _coerce_version_info(version_info: object | None = None) -> tuple[int, int]:
    if version_info is None:
        return (sys.version_info.major, sys.version_info.minor)
    if hasattr(version_info, "major") and hasattr(version_info, "minor"):
        return (int(getattr(version_info, "major")), int(getattr(version_info, "minor")))
    if isinstance(version_info, (tuple, list)) and len(version_info) >= 2:
        return (int(version_info[0]), int(version_info[1]))
    raise TypeError(f"Unsupported version_info payload: {version_info!r}")


def _runtime_architecture_report(
    *,
    pointer_bits: object | None = None,
    machine: object | None = None,
    platform_tag: object | None = None,
) -> dict[str, object]:
    bits = int(pointer_bits) if pointer_bits is not None else struct.calcsize("P") * 8
    machine_text = str(machine if machine is not None else platform.machine() or "").strip()
    platform_text = str(platform_tag if platform_tag is not None else sysconfig.get_platform() or "").strip()
    machine_key = machine_text.casefold().replace("-", "_")
    platform_key = platform_text.casefold().replace("-", "_")
    windows_amd64 = (
        os.name == "nt"
        and bits == 64
        and machine_key in {"amd64", "x86_64"}
        and ("win_amd64" in platform_key or platform_key == "win_amd64")
    )
    return {
        "pointer_bits": bits,
        "machine": machine_text,
        "platform_tag": platform_text,
        "windows_amd64": windows_amd64,
        "supported": windows_amd64,
    }


def _format_supported_python_minors() -> str:
    return ", ".join(f"{major}.{minor}" for major, minor in SUPPORTED_PYTHON_MINORS)


def get_managed_import_bundle_report(import_name: str) -> dict[str, object]:
    refresh_package_candidates()
    candidates = list(PACKAGE_BY_IMPORT_NAME.get(import_name, ()))
    fixed_candidates = list(_get_fixed_install_candidates(import_name))
    upgrade_candidates = list(_get_upgrade_install_candidates(import_name))
    strategy = "no-local-bundle"
    if import_name == "ufbx":
        strategy = "copy-prebuilt-or-install-local-source"
    elif import_name in LOCAL_ACCELERATOR_IMPORTS:
        if candidates:
            first_candidate = Path(candidates[0])
            strategy = "copy-prebuilt-extension" if (first_candidate / "__init__.py").exists() else "build-local-source"
    elif candidates:
        strategy = "install-local-wheel"
    return {
        "import_name": import_name,
        "strategy": strategy,
        "candidates": candidates,
        "upgrade_candidates": upgrade_candidates,
        "fixed_candidates": fixed_candidates,
        "candidate_contracts": [
            get_candidate_artifact_report(import_name, candidate)
            for candidate in candidates
        ],
        "preferred_version": _get_best_candidate_version(tuple(candidates)),
        "floor_version": _get_best_candidate_version(tuple(fixed_candidates)) or _get_best_candidate_version(tuple(candidates)),
        "policy": dict(IMPORT_POLICY.get(import_name, {})),
        "upgrade_check_state": _get_upgrade_check_state(import_name),
    }


def get_runtime_bundle_report() -> dict[str, object]:
    refresh_package_candidates()
    managed_imports = list(dict.fromkeys((*APPROVED_IMPORTS, *LOCAL_ACCELERATOR_IMPORTS)))
    return {
        "managed_imports": [get_managed_import_bundle_report(import_name) for import_name in managed_imports],
        "dependency_contract": get_dependency_bundle_contract_report(include_runtime_health=False),
        "runtime_state": get_runtime_state_report(),
    }


def get_runtime_state_report() -> dict[str, object]:
    payload = _load_runtime_state()
    blocked_artifacts = payload.get("blocked_artifacts", {})
    upgrade_checks = payload.get("upgrade_checks", {})
    return {
        "state_path": str(RUNTIME_STATE_PATH),
        "state_exists": RUNTIME_STATE_PATH.is_file(),
        "blocked_artifacts": dict(blocked_artifacts) if isinstance(blocked_artifacts, dict) else {},
        "upgrade_checks": dict(upgrade_checks) if isinstance(upgrade_checks, dict) else {},
        "last_export_runtime_repair": (
            dict(payload.get("last_export_runtime_repair", {}))
            if isinstance(payload.get("last_export_runtime_repair"), dict)
            else None
        ),
        "bootstrap_state_path": str(BOOTSTRAP_RUNTIME_STATE_PATH),
        "state_files": [
            _state_file_diagnostic(BOOTSTRAP_RUNTIME_STATE_PATH),
            _state_file_diagnostic(RUNTIME_STATE_PATH),
        ],
    }


def _state_file_diagnostic(path: Path) -> dict[str, object]:
    if path.is_file() is not True:
        return {"path": str(path), "exists": False, "valid": None, "error": ""}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("State root must be a JSON object.")
    except Exception as exc:
        return {"path": str(path), "exists": True, "valid": False, "error": str(exc)}
    return {"path": str(path), "exists": True, "valid": True, "error": ""}


def _dependency_resolution_report() -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    selected_key = os.path.normcase(os.path.abspath(str(DEPENDENCY_BASE_DIR)))
    for candidate in _iter_dependency_base_dir_candidates(BASE_DIR):
        candidate_key = os.path.normcase(os.path.abspath(str(candidate)))
        reports.append(
            {
                "path": str(candidate),
                "exists": candidate.exists(),
                "contains_vendors": _dependency_dir_contains_vendors(candidate),
                "score": list(_dependency_candidate_score(candidate)),
                "selected": candidate_key == selected_key,
            }
        )
    return reports


def _payload_contract_report(
    import_name: str,
    *,
    include_runtime_health: bool,
) -> dict[str, object]:
    policy = dict(IMPORT_POLICY.get(import_name, {}))
    repair_mode = str(policy.get("repair_mode", "pinned-local-only") or "pinned-local-only")
    fixed_candidates = list(_get_fixed_install_candidates(import_name))
    packaged_root = _find_packaged_import_source_root(import_name)
    packaged_payload_present = packaged_root is not None and _lane_contains_import_payload(packaged_root, import_name)
    fixed_payload: dict[str, object]
    if import_name == "numpy":
        fixed_payload = _get_fixed_numpy_wheel_report()
    elif import_name == "ufbx":
        prebuilt_dir = _prebuilt_ufbx_package_dir()
        expected_binary = (
            list(prebuilt_dir.glob(f"_ufbx.{_python_tag()}-win_amd64.pyd"))
            if prebuilt_dir is not None
            else []
        )
        fixed_payload = {
            "path": str(prebuilt_dir) if prebuilt_dir is not None else str(
                PATCHED_UFBX_SOURCE_DIR
                / "build"
                / f"lib.win-amd64-cpython-{sys.version_info.major}{sys.version_info.minor}"
                / "ufbx"
            ),
            "present": prebuilt_dir is not None,
            "abi_ready": prebuilt_dir is not None and len(expected_binary) > 0,
            "binary_paths": [str(path_value) for path_value in expected_binary],
            "source_present": PATCHED_UFBX_SOURCE_DIR.is_dir(),
            "archive_present": PATCHED_UFBX_ARCHIVE_PATH.is_file(),
            "error": "" if prebuilt_dir is not None and expected_binary else "Patched ufbx prebuilt ABI payload is missing.",
        }
    else:
        fixed_payload = {
            "paths": fixed_candidates,
            "present": any(Path(candidate).exists() for candidate in fixed_candidates),
            "error": "" if any(Path(candidate).exists() for candidate in fixed_candidates) else "No fixed local payload is registered.",
        }

    if "hash_ready" in fixed_payload:
        fixed_ready = fixed_payload.get("hash_ready") is True
    elif import_name == "ufbx":
        fixed_ready = bool(
            fixed_payload.get("abi_ready") is True
            or fixed_payload.get("source_present") is True
            or fixed_payload.get("archive_present") is True
        )
    else:
        fixed_ready = fixed_payload.get("present") is True
    runtime_ready: bool | None = None
    runtime_error = ""
    runtime_origins: list[str] = []
    if include_runtime_health:
        runtime_ready = _default_import_checker(import_name) is True
        runtime_error = "" if runtime_ready else get_last_import_error_text(import_name)
        runtime_origins = [str(path_value) for path_value in _module_origin_paths(import_name)] if runtime_ready else []

    if runtime_ready is True:
        planned_repair = "ready"
    elif fixed_ready or packaged_payload_present:
        planned_repair = "offline-local"
    elif repair_mode == "online-repair" and NETWORK_REPAIR_ENABLED:
        planned_repair = "online-repair"
    else:
        planned_repair = "pinned-local-only"

    bundle_error = ""
    error_type = None
    if repair_mode == "pinned-local-only" and fixed_ready is not True and packaged_payload_present is not True:
        error_type = DependencyBundleBrokenError.error_type
        bundle_error = (
            f"Pinned local dependency {import_name} has no usable {_python_tag()} payload under "
            + str(DEPENDENCY_BASE_DIR)
        )
    return {
        "import_name": import_name,
        "distribution_name": str(policy.get("distribution_name", import_name) or import_name),
        "python_tag": _python_tag(),
        "policy": policy,
        "repair_mode": repair_mode,
        "planned_repair": planned_repair,
        "fixed_payload": fixed_payload,
        "fixed_candidates": fixed_candidates,
        "packaged_abi_root": str(PACKAGED_VENDOR_PY_DIR),
        "packaged_source_root": str(packaged_root) if packaged_root is not None else None,
        "packaged_payload_present": packaged_payload_present,
        "runtime_ready": runtime_ready,
        "runtime_origins": runtime_origins,
        "health_error": runtime_error,
        "metadata_contract": get_vendor_metadata_contract_report(import_name),
        "bundle_error": bundle_error,
        "error_type": error_type,
        "repairable": runtime_ready is True or fixed_ready or packaged_payload_present or planned_repair == "online-repair",
    }


def get_dependency_bundle_contract_report(*, include_runtime_health: bool = True) -> dict[str, object]:
    required_payloads = [
        _payload_contract_report(import_name, include_runtime_health=include_runtime_health)
        for import_name in REPAIR_REQUIRED_IMPORTS
    ]
    managed_payloads = [
        _payload_contract_report(import_name, include_runtime_health=False)
        for import_name in dict.fromkeys((*APPROVED_IMPORTS, *LOCAL_ACCELERATOR_IMPORTS))
        if import_name not in REPAIR_REQUIRED_IMPORTS
    ]
    errors = [
        str(payload["bundle_error"])
        for payload in required_payloads
        if str(payload.get("bundle_error", "") or "")
    ]
    return {
        "ready": len(errors) == 0 and all(payload.get("repairable") is True for payload in required_payloads),
        "effective_runtime_ready": (
            all(payload.get("runtime_ready") is True for payload in required_payloads)
            if include_runtime_health
            else None
        ),
        "base_dir": str(BASE_DIR),
        "dependency_base_dir": str(DEPENDENCY_BASE_DIR),
        "dependency_resolution_candidates": _dependency_resolution_report(),
        "resolved_roots": {
            "fixed": str(FIXED_VENDOR_ROOT_DIR),
            "upgrade": str(UPGRADE_VENDOR_ROOT_DIR),
            "packaged_vendor_root": str(PACKAGED_VENDOR_PY_ROOT_DIR),
            "packaged_abi": str(PACKAGED_VENDOR_PY_DIR),
            "runtime_abi": str(VENDOR_PY_DIR),
            "last_known_good_abi": str(RUNTIME_LAST_KNOWN_GOOD_DIR),
        },
        "python_tag": _python_tag(),
        "python_executable": sys.executable,
        "network_repair_enabled": NETWORK_REPAIR_ENABLED,
        "official_index": OFFICIAL_PYPI_INDEX_URL,
        "runtime_lock": get_runtime_lock_report(),
        "state_files": [
            _state_file_diagnostic(BOOTSTRAP_RUNTIME_STATE_PATH),
            _state_file_diagnostic(RUNTIME_STATE_PATH),
        ],
        "required_payloads": required_payloads,
        "managed_optional_payloads": managed_payloads,
        "supported_abi_contracts": get_supported_abi_bundle_contracts(),
        "errors": errors,
    }


def get_supported_abi_bundle_contracts() -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    for version_info in SUPPORTED_PYTHON_MINORS:
        python_tag = _python_tag(version_info)
        packaged_abi_root = PACKAGED_VENDOR_PY_ROOT_DIR / python_tag
        prebuilt_ufbx = _prebuilt_ufbx_package_dir(version_info)
        accelerator_reports: list[dict[str, object]] = []
        for import_name in LOCAL_ACCELERATOR_IMPORTS:
            fixed_candidates = list(
                _find_local_fixed_accelerator_candidates(import_name, version_info=version_info)
            )
            accelerator_reports.append(
                {
                    "import_name": import_name,
                    "fixed_candidates": fixed_candidates,
                    "fixed_payload_present": bool(fixed_candidates),
                    "metadata_contract": get_vendor_metadata_contract_report(
                        import_name,
                        version_info=version_info,
                    ),
                }
            )
        numpy_report = _get_fixed_numpy_wheel_report(version_info)
        reports.append(
            {
                "python": f"{version_info[0]}.{version_info[1]}",
                "python_tag": python_tag,
                "packaged_abi_root": str(packaged_abi_root),
                "packaged_abi_present": packaged_abi_root.is_dir(),
                "fixed_numpy": numpy_report,
                "patched_ufbx": {
                    "path": str(prebuilt_ufbx) if prebuilt_ufbx is not None else None,
                    "present": prebuilt_ufbx is not None,
                },
                "accelerators": accelerator_reports,
                "pinned_contract_ready": prebuilt_ufbx is not None and all(
                    report["fixed_payload_present"] is True
                    for report in accelerator_reports
                ),
            }
        )
    return reports


def get_runtime_support_report(version_info: object | None = None) -> dict[str, object]:
    current_version = _coerce_version_info(version_info)
    override_raw = str(os.environ.get(ALLOW_UNSUPPORTED_PYTHON_ENV, "")).strip().lower()
    override_enabled = override_raw in {"1", "true", "yes", "on"}
    candidate_session = _runtime_candidate_session_report() if version_info is None else {"authorized": False}
    ab_approved = (
        _runtime_ab_path_is_approved(sys.executable, _python_runtime_version_tuple())
        if version_info is None
        else False
    )
    dynamic_approval = ab_approved or candidate_session.get("authorized") is True
    exact_bundle_supported = current_version in SUPPORTED_PYTHON_MINORS or dynamic_approval
    architecture = _runtime_architecture_report()
    same_major = current_version[0] == MINIMUM_SUPPORTED_PYTHON[0]
    forward_compat_mode = same_major and current_version[1] >= MINIMUM_SUPPORTED_PYTHON[1] and exact_bundle_supported is not True
    native_abi_supported = exact_bundle_supported and architecture["supported"] is True
    supported = native_abi_supported
    recommended = current_version == RECOMMENDED_PYTHON
    return {
        "current_python": f"{current_version[0]}.{current_version[1]}",
        "current_python_tuple": current_version,
        "supported": supported,
        "exact_bundle_supported": exact_bundle_supported,
        "baseline_bundle_supported": current_version in SUPPORTED_PYTHON_MINORS,
        "ab_approved": ab_approved,
        "candidate_session": candidate_session.get("authorized") is True,
        "native_abi_supported": native_abi_supported,
        "architecture": architecture,
        "forward_compat_mode": forward_compat_mode,
        "recommended": recommended,
        "recommended_python": f"{RECOMMENDED_PYTHON[0]}.{RECOMMENDED_PYTHON[1]}",
        "minimum_supported_python": f"{MINIMUM_SUPPORTED_PYTHON[0]}.{MINIMUM_SUPPORTED_PYTHON[1]}",
        "supported_python_versions": [f"{major}.{minor}" for major, minor in SUPPORTED_PYTHON_MINORS],
        "override_env": ALLOW_UNSUPPORTED_PYTHON_ENV,
        "override_enabled": override_enabled,
        "local_only_runtime_install": LOCAL_ONLY_RUNTIME_INSTALL,
        "network_repair_enabled": NETWORK_REPAIR_ENABLED,
        "official_index": OFFICIAL_PYPI_INDEX_URL,
        "vendor_dir": str(VENDOR_PY_DIR),
        "packaged_vendor_dir": str(PACKAGED_VENDOR_PY_DIR),
        "runtime_root": str(RUNTIME_ROOT_DIR),
    }


def _normalize_existing_python_path(path_value: object | None) -> Path | None:
    normalized_text = str(path_value or "").strip().strip("\"")
    if normalized_text == "":
        return None
    try:
        candidate = Path(normalized_text).expanduser()
    except Exception:
        return None
    try:
        if candidate.exists() is not True or candidate.is_file() is not True:
            return None
    except Exception:
        return None
    if candidate.name.lower() not in {"python.exe", "pythonw.exe"}:
        return None
    try:
        return candidate.resolve()
    except Exception:
        return candidate


def _load_bootstrap_runtime_state() -> dict[str, object]:
    if BOOTSTRAP_RUNTIME_STATE_PATH.exists() is not True:
        return {}
    try:
        payload = json.loads(BOOTSTRAP_RUNTIME_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _save_bootstrap_runtime_state(payload: dict[str, object]) -> bool:
    try:
        _atomic_write_runtime_bytes(
            BOOTSTRAP_RUNTIME_STATE_PATH,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    except Exception:
        return False
    return True


def _python_runtime_version_tuple(value: object | None = None) -> tuple[int, int, int]:
    if value is None:
        return (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)
    if isinstance(value, (tuple, list)) and len(value) >= 2:
        return (
            int(value[0]),
            int(value[1]),
            int(value[2]) if len(value) >= 3 else 0,
        )
    text_value = str(value or "").strip()
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text_value)
    if match is None:
        raise ValueError(f"Unsupported Python version label: {value!r}")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def _python_runtime_version_text(value: object | None = None) -> str:
    major, minor, micro = _python_runtime_version_tuple(value)
    return f"{major}.{minor}.{micro}"


def _runtime_matches_required_python(
    version_info: object | None = None,
    *,
    releaselevel: object | None = None,
) -> bool:
    """Return True only for the exact stable Launcher runtime release."""
    try:
        version = _python_runtime_version_tuple(version_info)
    except Exception:
        return False
    level = (
        getattr(sys.version_info, "releaselevel", "final")
        if releaselevel is None
        else releaselevel
    )
    return version == REQUIRED_PYTHON_RUNTIME and str(level).casefold() == "final"


def _same_runtime_path(left: object | None, right: object | None) -> bool:
    left_path = _normalize_existing_python_path(left)
    right_path = _normalize_existing_python_path(right)
    if left_path is None or right_path is None:
        return False
    return os.path.normcase(os.path.abspath(str(left_path))) == os.path.normcase(
        os.path.abspath(str(right_path))
    )


def _runtime_ab_new_active_entry(
    python_exe: str | Path,
    *,
    slot: str,
    version: object | None = None,
    managed: bool,
) -> dict[str, object]:
    python_path = _normalize_existing_python_path(python_exe)
    if python_path is None:
        raise RuntimeError(f"Python runtime entry does not exist: {python_exe}")
    version_tuple = _python_runtime_version_tuple(version)
    return {
        "status": "approved",
        "slot": str(slot).upper(),
        "python_exe": str(python_path),
        "python_version": _python_runtime_version_text(version_tuple),
        "abi": _python_tag(version_tuple),
        "managed": bool(managed),
        "approved_epoch": time.time(),
    }


def _runtime_ab_payload_from_state(state_payload: dict[str, object]) -> dict[str, object]:
    runtime_ab = state_payload.get(PYTHON_RUNTIME_AB_STATE_KEY, {})
    return dict(runtime_ab) if isinstance(runtime_ab, dict) else {}


def _runtime_ab_active_entry(state_payload: dict[str, object] | None = None) -> dict[str, object]:
    payload = state_payload if isinstance(state_payload, dict) else _load_bootstrap_runtime_state()
    runtime_ab = _runtime_ab_payload_from_state(payload)
    active = runtime_ab.get("active", {})
    return dict(active) if isinstance(active, dict) else {}


def _runtime_ab_candidate_entry(state_payload: dict[str, object] | None = None) -> dict[str, object]:
    payload = state_payload if isinstance(state_payload, dict) else _load_bootstrap_runtime_state()
    runtime_ab = _runtime_ab_payload_from_state(payload)
    candidate = runtime_ab.get("candidate", {})
    return dict(candidate) if isinstance(candidate, dict) else {}


def _runtime_ab_entry_is_usable(entry: object) -> bool:
    if not isinstance(entry, dict) or str(entry.get("status", "")).casefold() != "approved":
        return False
    return _normalize_existing_python_path(entry.get("python_exe")) is not None


def ensure_python_runtime_ab_state() -> dict[str, object]:
    with _bootstrap_state_lock(timeout_seconds=10.0):
        payload = _load_bootstrap_runtime_state()
        runtime_ab = _runtime_ab_payload_from_state(payload)
        active = runtime_ab.get("active", {})
        changed = False
        if _runtime_ab_entry_is_usable(active) is not True:
            rollback = runtime_ab.get("rollback", {})
            if _runtime_ab_entry_is_usable(rollback):
                active = dict(rollback)
                active["restored_epoch"] = time.time()
            else:
                active = _runtime_ab_new_active_entry(
                    sys.executable,
                    slot="A",
                    version=None,
                    managed=False,
                )
            runtime_ab["active"] = active
            runtime_ab["active_slot"] = str(active.get("slot", "A") or "A").upper()
            changed = True
        identity_fields = {
            "schema": PYTHON_RUNTIME_AB_SCHEMA,
            "release_root": str(BASE_DIR),
            "dependency_base_dir": str(DEPENDENCY_BASE_DIR),
        }
        if any(runtime_ab.get(name) != value for name, value in identity_fields.items()):
            runtime_ab.update(identity_fields)
            changed = True
        if not isinstance(runtime_ab.get("failed_versions"), dict):
            runtime_ab["failed_versions"] = {}
            changed = True
        if "next_check_epoch" not in runtime_ab:
            runtime_ab["next_check_epoch"] = 0.0
            changed = True
        payload[PYTHON_RUNTIME_AB_STATE_KEY] = runtime_ab
        active_path = _normalize_existing_python_path(dict(runtime_ab.get("active", {})).get("python_exe"))
        if active_path is not None and str(payload.get("preferred_python_exe", "")) != str(active_path):
            payload["preferred_python_exe"] = str(active_path)
            payload["preferred_python_version"] = str(
                dict(runtime_ab.get("active", {})).get("python_version", "")
            )
            changed = True
        if changed and _save_bootstrap_runtime_state(payload) is not True:
            raise OSError(f"Unable to initialize Python A/B runtime state: {BOOTSTRAP_RUNTIME_STATE_PATH}")
        return dict(runtime_ab)


def _runtime_candidate_session_report() -> dict[str, object]:
    token = str(os.environ.get(PYTHON_RUNTIME_CANDIDATE_TOKEN_ENV, "") or "").strip()
    path_hint = str(os.environ.get(PYTHON_RUNTIME_CANDIDATE_PATH_ENV, "") or "").strip()
    candidate = _runtime_ab_candidate_entry()
    authorized = bool(
        token
        and token == str(candidate.get("token", "") or "")
        and str(candidate.get("status", "") or "").casefold()
        in {"installed", "preparing", "testing", "contract-pass"}
        and _same_runtime_path(candidate.get("python_exe"), sys.executable)
        and (not path_hint or _same_runtime_path(path_hint, sys.executable))
        and str(candidate.get("abi", "") or "").casefold() == _python_tag().casefold()
        and _python_runtime_version_tuple(candidate.get("python_version", "0.0.0"))
        == _python_runtime_version_tuple()
    )
    return {
        "authorized": authorized,
        "candidate": candidate if authorized else {},
        "token_present": bool(token),
    }


def _runtime_ab_path_is_approved(
    python_exe: str | Path,
    version_info: object | None = None,
) -> bool:
    active = _runtime_ab_active_entry()
    if _runtime_ab_entry_is_usable(active) is not True:
        return False
    if _same_runtime_path(active.get("python_exe"), python_exe) is not True:
        return False
    if version_info is None:
        return True
    active_version = _python_runtime_version_tuple(active.get("python_version", "0.0.0"))
    requested_version = _python_runtime_version_tuple(version_info)
    return active_version == requested_version


def get_python_runtime_ab_report() -> dict[str, object]:
    runtime_ab = ensure_python_runtime_ab_state()
    active = dict(runtime_ab.get("active", {})) if isinstance(runtime_ab.get("active"), dict) else {}
    rollback = dict(runtime_ab.get("rollback", {})) if isinstance(runtime_ab.get("rollback"), dict) else {}
    candidate = dict(runtime_ab.get("candidate", {})) if isinstance(runtime_ab.get("candidate"), dict) else {}
    return {
        **runtime_ab,
        "active": active,
        "rollback": rollback,
        "candidate": candidate,
        "active_usable": _runtime_ab_entry_is_usable(active),
        "active_is_current": _runtime_ab_path_is_approved(sys.executable),
        "state_path": str(BOOTSTRAP_RUNTIME_STATE_PATH),
        "interpreter_root": str(RUNTIME_INTERPRETER_ROOT_DIR),
    }


def _normalize_python_runtime_abi(abi: object | None = None) -> str:
    if abi is None:
        return _python_tag()
    if isinstance(abi, (tuple, list)) and len(abi) >= 2:
        return f"cp{int(abi[0])}{int(abi[1])}"
    abi_text = str(abi or "").strip().casefold().replace(".", "")
    match = re.fullmatch(r"(?:cp|python)?(\d)(\d{1,2})", abi_text)
    if match is None:
        raise ValueError(f"Unsupported Python ABI label: {abi!r}")
    return f"cp{match.group(1)}{match.group(2)}"


def _runtime_candidate_path_text(python_exe: str | Path) -> str:
    normalized_text = str(python_exe or "").strip().strip('"')
    if normalized_text == "":
        raise ValueError("python_exe cannot be empty")
    return os.path.abspath(os.path.expanduser(normalized_text))


def _runtime_candidate_state_key(python_exe: str | Path, abi: object | None = None) -> str:
    path_text = os.path.normcase(_runtime_candidate_path_text(python_exe))
    return path_text.casefold() + "|" + _normalize_python_runtime_abi(abi)


def _runtime_candidate_entry_is_blocked(entry: object, *, now_epoch: float | None = None) -> bool:
    if not isinstance(entry, dict):
        return False
    status_text = str(entry.get("status", "") or "").strip().casefold()
    try:
        cooldown_until = float(entry.get("cooldown_until_epoch", 0.0) or 0.0)
    except (TypeError, ValueError):
        cooldown_until = 0.0
    return cooldown_until > (time.time() if now_epoch is None else float(now_epoch))


def _python_runtime_candidate_is_blocked(
    python_exe: str | Path,
    *,
    state_payload: dict[str, object] | None = None,
) -> bool:
    payload = state_payload if isinstance(state_payload, dict) else _load_bootstrap_runtime_state()
    quarantine_payload = payload.get(RUNTIME_CANDIDATE_QUARANTINE_STATE_KEY, {})
    if not isinstance(quarantine_payload, dict):
        return False
    path_key_prefix = os.path.normcase(_runtime_candidate_path_text(python_exe)).casefold() + "|"
    return any(
        str(candidate_key).startswith(path_key_prefix)
        and _runtime_candidate_entry_is_blocked(candidate_entry)
        for candidate_key, candidate_entry in quarantine_payload.items()
    )


def get_python_runtime_candidate_health(
    python_exe: str | Path | None = None,
    *,
    abi: object | None = None,
) -> dict[str, object]:
    payload = _load_bootstrap_runtime_state()
    quarantine_payload = payload.get(RUNTIME_CANDIDATE_QUARANTINE_STATE_KEY, {})
    rehabilitation_payload = payload.get(RUNTIME_CANDIDATE_REHABILITATION_STATE_KEY, {})
    if not isinstance(quarantine_payload, dict):
        quarantine_payload = {}
    if not isinstance(rehabilitation_payload, dict):
        rehabilitation_payload = {}
    if python_exe is None:
        return {
            "state_path": str(BOOTSTRAP_RUNTIME_STATE_PATH),
            "quarantined_candidates": dict(quarantine_payload),
            "rehabilitated_candidates": dict(rehabilitation_payload),
        }
    path_text = _runtime_candidate_path_text(python_exe)
    candidate_key = _runtime_candidate_state_key(path_text, abi)
    entry = quarantine_payload.get(candidate_key)
    return {
        "python_exe": path_text,
        "abi": _normalize_python_runtime_abi(abi),
        "candidate_key": candidate_key,
        "blocked": _runtime_candidate_entry_is_blocked(entry),
        "quarantine": dict(entry) if isinstance(entry, dict) else None,
        "last_rehabilitation": (
            dict(rehabilitation_payload[candidate_key])
            if isinstance(rehabilitation_payload.get(candidate_key), dict)
            else None
        ),
        "state_path": str(BOOTSTRAP_RUNTIME_STATE_PATH),
    }


def record_python_runtime_launch_failure(
    python_exe: str | Path,
    *,
    abi: object | None = None,
    failure_kind: str = "native-crash",
    detail: str = "",
    cooldown_seconds: float = DEFAULT_RUNTIME_CANDIDATE_COOLDOWN_SECONDS,
    quarantine: bool = True,
) -> dict[str, object]:
    path_text = _runtime_candidate_path_text(python_exe)
    abi_text = _normalize_python_runtime_abi(abi)
    candidate_key = _runtime_candidate_state_key(path_text, abi_text)
    with _bootstrap_state_lock(timeout_seconds=10.0):
        payload = _load_bootstrap_runtime_state()
        quarantine_payload = payload.setdefault(RUNTIME_CANDIDATE_QUARANTINE_STATE_KEY, {})
        if not isinstance(quarantine_payload, dict):
            quarantine_payload = {}
            payload[RUNTIME_CANDIDATE_QUARANTINE_STATE_KEY] = quarantine_payload
        previous_entry = quarantine_payload.get(candidate_key, {})
        previous_count = int(previous_entry.get("failure_count", 0) or 0) if isinstance(previous_entry, dict) else 0
        failure_count = previous_count + 1
        requested_cooldown = max(0.0, float(cooldown_seconds))
        effective_cooldown = min(
            MAX_RUNTIME_CANDIDATE_COOLDOWN_SECONDS,
            requested_cooldown * (2 ** min(failure_count - 1, 6)),
        )
        now_epoch = time.time()
        entry = {
            "python_exe": path_text,
            "abi": abi_text,
            "status": "quarantined" if quarantine else "cooldown",
            "failure_kind": str(failure_kind or "native-crash"),
            "detail": str(detail or "")[-8000:],
            "failure_count": failure_count,
            "last_failure_epoch": now_epoch,
            "cooldown_seconds": effective_cooldown,
            "cooldown_until_epoch": now_epoch + effective_cooldown,
        }
        if isinstance(previous_entry, dict) and previous_entry.get("first_failure_epoch") is not None:
            entry["first_failure_epoch"] = previous_entry["first_failure_epoch"]
        else:
            entry["first_failure_epoch"] = now_epoch
        quarantine_payload[candidate_key] = entry
        if _save_bootstrap_runtime_state(payload) is not True:
            raise OSError(f"Unable to persist runtime candidate failure state: {BOOTSTRAP_RUNTIME_STATE_PATH}")
    return get_python_runtime_candidate_health(path_text, abi=abi_text)


def _record_python_runtime_rehabilitated_unlocked(
    python_exe: str | Path,
    *,
    abi: object | None = None,
    detail: str = "",
) -> dict[str, object]:
    path_text = _runtime_candidate_path_text(python_exe)
    abi_text = _normalize_python_runtime_abi(abi)
    candidate_key = _runtime_candidate_state_key(path_text, abi_text)
    payload = _load_bootstrap_runtime_state()
    quarantine_payload = payload.get(RUNTIME_CANDIDATE_QUARANTINE_STATE_KEY, {})
    if not isinstance(quarantine_payload, dict):
        quarantine_payload = {}
    cleared_entry = quarantine_payload.pop(candidate_key, None)
    payload[RUNTIME_CANDIDATE_QUARANTINE_STATE_KEY] = quarantine_payload
    rehabilitation_payload = payload.setdefault(RUNTIME_CANDIDATE_REHABILITATION_STATE_KEY, {})
    if not isinstance(rehabilitation_payload, dict):
        rehabilitation_payload = {}
        payload[RUNTIME_CANDIDATE_REHABILITATION_STATE_KEY] = rehabilitation_payload
    rehabilitation_payload[candidate_key] = {
        "python_exe": path_text,
        "abi": abi_text,
        "rehabilitated_epoch": time.time(),
        "detail": str(detail or "")[-8000:],
        "cleared_failure_count": (
            int(cleared_entry.get("failure_count", 0) or 0)
            if isinstance(cleared_entry, dict)
            else 0
        ),
    }
    if _save_bootstrap_runtime_state(payload) is not True:
        raise OSError(f"Unable to persist runtime candidate rehabilitation: {BOOTSTRAP_RUNTIME_STATE_PATH}")
    return get_python_runtime_candidate_health(path_text, abi=abi_text)


def record_python_runtime_rehabilitated(
    python_exe: str | Path,
    *,
    abi: object | None = None,
    detail: str = "",
) -> dict[str, object]:
    with _bootstrap_state_lock(timeout_seconds=10.0):
        return _record_python_runtime_rehabilitated_unlocked(
            python_exe,
            abi=abi,
            detail=detail,
        )


def _get_cached_supported_python_path() -> Path | None:
    payload = _load_bootstrap_runtime_state()
    active_path = _normalize_existing_python_path(_runtime_ab_active_entry(payload).get("python_exe"))
    if active_path is not None:
        return active_path
    return _normalize_existing_python_path(payload.get("preferred_python_exe"))


def _remember_supported_python_path(path_value: object | None, report: dict[str, object] | None = None) -> None:
    python_path = _normalize_existing_python_path(path_value)
    if python_path is None:
        return
    if isinstance(report, dict) and report.get("candidate_session") is True:
        return
    with _bootstrap_state_lock(timeout_seconds=10.0):
        payload = _load_bootstrap_runtime_state()
        payload["preferred_python_exe"] = str(python_path)
        if isinstance(report, dict):
            payload["preferred_python_version"] = str(report.get("current_python", "") or "")
        if _save_bootstrap_runtime_state(payload) is not True:
            raise OSError(f"Unable to persist preferred Python runtime: {BOOTSTRAP_RUNTIME_STATE_PATH}")


def _read_python_path_from_command(command: list[str]) -> Path | None:
    try:
        completed = _run_hidden_subprocess(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=_isolated_python_child_environment(),
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    combined_output = "\n".join(
        line.strip()
        for line in (
            *(completed.stdout or "").splitlines(),
            *(completed.stderr or "").splitlines(),
        )
        if str(line).strip() != ""
    )
    for output_line in reversed(combined_output.splitlines()):
        python_path = _normalize_existing_python_path(output_line)
        if python_path is not None:
            return python_path
    return None


def _ordered_supported_python_targets() -> tuple[tuple[int, int], ...]:
    ordered: list[tuple[int, int]] = []
    for version_info in (RECOMMENDED_PYTHON, *sorted(SUPPORTED_PYTHON_MINORS, reverse=True)):
        if version_info not in ordered:
            ordered.append(version_info)
    return tuple(ordered)


def _blocked_python_runtime_abis(
    state_payload: dict[str, object] | None = None,
) -> set[str]:
    payload = state_payload if isinstance(state_payload, dict) else _load_bootstrap_runtime_state()
    quarantine_payload = payload.get(RUNTIME_CANDIDATE_QUARANTINE_STATE_KEY, {})
    if not isinstance(quarantine_payload, dict):
        return set()
    return {
        str(entry.get("abi", "") or "").strip().casefold()
        for entry in quarantine_payload.values()
        if isinstance(entry, dict) and _runtime_candidate_entry_is_blocked(entry)
    }


def _discover_python_paths_via_launcher_targets(
    state_payload: dict[str, object] | None = None,
) -> tuple[Path, ...]:
    candidates: list[Path] = []
    for major, minor in _ordered_supported_python_targets():
        python_path = _read_python_path_from_command(
            [
                "py",
                f"-{major}.{minor}",
                "-c",
                "import sys; print(sys.executable)",
            ]
        )
        if (
            python_path is not None
            and _python_runtime_candidate_is_blocked(python_path, state_payload=state_payload) is not True
            and python_path not in candidates
        ):
            candidates.append(python_path)
    return tuple(candidates)


def _discover_python_paths_via_launcher_list() -> tuple[Path, ...]:
    try:
        completed = _run_hidden_subprocess(
            ["py", "-0p"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=_isolated_python_child_environment(),
        )
    except Exception:
        return tuple()
    if completed.returncode != 0:
        return tuple()
    candidates: list[Path] = []
    output_lines = [
        *(completed.stdout or "").splitlines(),
        *(completed.stderr or "").splitlines(),
    ]
    for output_line in output_lines:
        match = re.search(r"([A-Za-z]:\\.*python(?:w)?\.exe)\s*$", str(output_line or ""), re.IGNORECASE)
        if match is None:
            continue
        python_path = _normalize_existing_python_path(match.group(1))
        if python_path is not None and python_path not in candidates:
            candidates.append(python_path)
    return tuple(candidates)


def _discover_python_paths_via_registry() -> tuple[Path, ...]:
    """Read every registered Windows PythonCore install, including custom dirs."""
    if os.name != "nt":
        return tuple()
    try:
        import winreg
    except Exception:
        return tuple()
    candidates: list[Path] = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, r"Software\Python\PythonCore") as root_key:
                version_count = int(winreg.QueryInfoKey(root_key)[0])
                version_names = [
                    str(winreg.EnumKey(root_key, index))
                    for index in range(version_count)
                ]
        except Exception:
            continue
        for version_name in version_names:
            try:
                with winreg.OpenKey(
                    hive,
                    rf"Software\Python\PythonCore\{version_name}\InstallPath",
                ) as install_key:
                    values: list[str] = []
                    for value_name in ("ExecutablePath", ""):
                        try:
                            value, _value_type = winreg.QueryValueEx(install_key, value_name)
                        except Exception:
                            continue
                        if str(value).strip():
                            values.append(str(value))
                    for value in values:
                        candidate = Path(value)
                        if candidate.is_dir():
                            candidate = candidate / "python.exe"
                        normalized = _normalize_existing_python_path(candidate)
                        if normalized is not None and normalized not in candidates:
                            candidates.append(normalized)
            except Exception:
                continue
    return tuple(candidates)


def _discover_python_paths_from_common_locations() -> tuple[Path, ...]:
    candidates: list[Path] = []

    def add_candidate(path_value: object | None) -> None:
        candidate = _normalize_existing_python_path(path_value)
        if candidate is not None and candidate not in candidates:
            candidates.append(candidate)

    # Permit a user who intentionally keeps a custom Python install outside
    # the normal registry/launcher locations to point the Launcher at it
    # without allowing the managed installer to overwrite that directory.
    for environment_name in ("PC_REHD_CODE_X_PYTHON", "CODEX_PRIMARY_PYTHON"):
        add_candidate(os.environ.get(environment_name, ""))

    local_python_root = BASE_DIR / "Python"
    if local_python_root.exists() is True:
        for pattern in (
            "pythoncore-3.*-64/python.exe",
            "Python3*/python.exe",
        ):
            for candidate_path in sorted(local_python_root.glob(pattern)):
                add_candidate(candidate_path)

    local_app_data = Path(str(os.environ.get("LOCALAPPDATA", "") or "").strip())
    if str(local_app_data).strip() != "":
        for pattern in (
            "Python/pythoncore-3.*-64/python.exe",
            "Programs/Python/Python3*/python.exe",
        ):
            for candidate_path in sorted(local_app_data.glob(pattern)):
                add_candidate(candidate_path)

    for env_name in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        root_text = str(os.environ.get(env_name, "") or "").strip()
        if root_text == "":
            continue
        root_path = Path(root_text)
        for pattern in (
            "Python3*/python.exe",
            "Python/Python3*/python.exe",
        ):
            for candidate_path in sorted(root_path.glob(pattern)):
                add_candidate(candidate_path)

    return tuple(candidates)


def _get_tried_python_paths() -> set[str]:
    tried_paths: set[str] = set()
    raw_text = str(os.environ.get(BOOTSTRAP_TRIED_PYTHONS_ENV, "") or "")
    for item in raw_text.split(os.pathsep):
        normalized = _normalize_existing_python_path(item)
        if normalized is not None:
            tried_paths.add(str(normalized).lower())
    current_python = _normalize_existing_python_path(sys.executable)
    if current_python is not None:
        tried_paths.add(str(current_python).lower())
    return tried_paths


def _iter_supported_python_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    tried_paths = _get_tried_python_paths()
    candidate_health_state = _load_bootstrap_runtime_state()

    def add_candidate(path_value: object | None) -> None:
        candidate = _normalize_existing_python_path(path_value)
        if candidate is None:
            return
        normalized_text = str(candidate).lower()
        if normalized_text in tried_paths:
            return
        if _python_runtime_candidate_is_blocked(
            candidate,
            state_payload=candidate_health_state,
        ):
            return
        if candidate not in candidates:
            candidates.append(candidate)

    add_candidate(_get_cached_supported_python_path())
    add_candidate(os.environ.get(BOOTSTRAP_PYTHON_HINT_ENV, ""))
    for candidate_path in _discover_python_paths_via_launcher_list():
        add_candidate(candidate_path)
    for candidate_path in _discover_python_paths_from_common_locations():
        add_candidate(candidate_path)
    for candidate_path in _discover_python_paths_via_launcher_targets(candidate_health_state):
        add_candidate(candidate_path)
    for candidate_path in _discover_python_paths_via_registry():
        add_candidate(candidate_path)
    return tuple(candidates)


def _probe_python_runtime_candidate(path_value: Path) -> dict[str, object] | None:
    candidate_path = _normalize_existing_python_path(path_value)
    if candidate_path is None:
        return None
    try:
        completed = _run_hidden_subprocess(
            [
                str(candidate_path),
                "-c",
                "import json,platform,struct,sys,sysconfig; print(json.dumps({'major': sys.version_info[0], 'minor': sys.version_info[1], 'micro': sys.version_info[2], 'releaselevel': sys.version_info.releaselevel, 'executable': sys.executable, 'pointer_bits':struct.calcsize('P')*8, 'machine':platform.machine(), 'platform_tag':sysconfig.get_platform()}))",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=_isolated_python_child_environment(),
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    payload_text = ""
    for output_line in reversed((completed.stdout or "").splitlines()):
        if str(output_line).strip() != "":
            payload_text = str(output_line).strip()
            break
    if payload_text == "":
        return None
    try:
        payload = json.loads(payload_text)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        version_info = (
            int(payload.get("major")),
            int(payload.get("minor")),
            int(payload.get("micro", 0) or 0),
        )
    except Exception:
        return None
    runtime_report = get_runtime_support_report(version_info)
    candidate_architecture = _runtime_architecture_report(
        pointer_bits=payload.get("pointer_bits"),
        machine=payload.get("machine"),
        platform_tag=payload.get("platform_tag"),
    )
    runtime_report["architecture"] = candidate_architecture
    runtime_report["native_abi_supported"] = (
        runtime_report.get("exact_bundle_supported") is True
        and candidate_architecture.get("supported") is True
    )
    runtime_report["supported"] = runtime_report["native_abi_supported"]
    resolved_python = _normalize_existing_python_path(payload.get("executable")) or candidate_path
    if _runtime_ab_path_is_approved(resolved_python, version_info):
        runtime_report["ab_approved"] = True
        runtime_report["exact_bundle_supported"] = True
        runtime_report["native_abi_supported"] = candidate_architecture.get("supported") is True
        runtime_report["supported"] = runtime_report["native_abi_supported"]
    return {
        "path": str(resolved_python),
        "version_tuple": version_info,
        "python_version": _python_runtime_version_text(version_info),
        "releaselevel": str(payload.get("releaselevel", "") or ""),
        "runtime_report": runtime_report,
    }


def _find_supported_python_runtime_candidate() -> dict[str, object] | None:
    supported_candidates: list[dict[str, object]] = []
    for candidate_path in _iter_supported_python_candidates():
        probe = _probe_python_runtime_candidate(candidate_path)
        if not isinstance(probe, dict):
            continue
        runtime_report = probe.get("runtime_report")
        if isinstance(runtime_report, dict) and runtime_report.get("supported") is True:
            supported_candidates.append(probe)
    if not supported_candidates:
        return None

    # Prefer the highest ABI with a formally bundled dependency lane. A newer
    # forward-compat Python remains a fallback until its cpXXX bundle is added.
    return max(
        supported_candidates,
        key=lambda probe: (
            bool(dict(probe.get("runtime_report", {})).get("exact_bundle_supported")),
            _coerce_version_info(probe.get("version_tuple")),
        ),
    )


def _find_exact_python_runtime_candidate(required_version: tuple[int, int]) -> dict[str, object] | None:
    target_version = _coerce_version_info(required_version)
    for candidate_path in _iter_supported_python_candidates():
        probe = _probe_python_runtime_candidate(candidate_path)
        if not isinstance(probe, dict):
            continue
        version_tuple = _coerce_version_info(probe.get("version_tuple"))
        if version_tuple == target_version:
            return probe
    return None


def _find_exact_python_runtime_patch_candidate(
    required_version: tuple[int, int, int],
) -> dict[str, object] | None:
    """Find an installed final interpreter matching every version component."""
    target_version = _python_runtime_version_tuple(required_version)
    for candidate_path in _iter_supported_python_candidates():
        probe = _probe_python_runtime_candidate(candidate_path)
        if not isinstance(probe, dict):
            continue
        if _python_runtime_version_tuple(probe.get("version_tuple")) != target_version:
            continue
        if str(probe.get("releaselevel", "") or "").casefold() != "final":
            continue
        runtime_report = probe.get("runtime_report")
        if not isinstance(runtime_report, dict) or runtime_report.get("supported") is not True:
            continue
        return probe
    return None


def _required_python_installer_hint() -> str:
    version_text = _python_runtime_version_text(REQUIRED_PYTHON_RUNTIME)
    return str(BASE_DIR / f"python-{version_text}-amd64.exe")


def _build_runtime_restart_error_message(report: dict[str, object]) -> str:
    installer_hint = _required_python_installer_hint()
    if runtime_ui_is_chinese():
        lines = [
            "当前 Python 运行时不受支持：" + str(report.get("current_python", "未知")) + "。",
            "最低支持版本：" + str(report.get("minimum_supported_python", "未知")) + "。",
            "推荐版本：" + str(report.get("recommended_python", "未知")) + "。",
            "V4 本地桥接支持版本：" + _format_supported_python_minors() + "。",
            "Python 已尝试使用受支持的解释器重新启动，但没有找到可用版本。",
            "请确认 py launcher 能找到受支持的 Python 3.14；其他版本必须先通过 Bootstrap A/B 合同。",
            "如果本机没有可用的 Python，请从发布目录安装：" + installer_hint,
        ]
    else:
        lines = [
            "Unsupported Python runtime " + str(report.get("current_python", "unknown")) + ".",
            "Minimum supported: " + str(report.get("minimum_supported_python", "unknown")) + ".",
            "Recommended: " + str(report.get("recommended_python", "unknown")) + ".",
            "Supported local V4 bridge runtimes: " + _format_supported_python_minors() + ".",
            "Python tried to relaunch itself under a supported interpreter, but none was found.",
            "Check that the py launcher can see supported Python 3.14; other versions must pass the Bootstrap A/B contract first.",
            "If this machine has no usable Python, install it from the release directory: " + installer_hint,
        ]
    return " ".join(lines)


def _build_exact_runtime_restart_error_message(
    required_version: tuple[int, int],
    *,
    context_label: str,
) -> str:
    installer_hint = _required_python_installer_hint()
    required_text = f"{required_version[0]}.{required_version[1]}"
    current_text = f"{sys.version_info.major}.{sys.version_info.minor}"
    if runtime_ui_is_chinese():
        lines = [
            context_label + " 必须使用 Python " + required_text + " 运行。",
            "当前 Python：" + current_text + "。",
            "V4 本地桥接支持版本：" + _format_supported_python_minors() + "。",
            "Python 已尝试使用要求的解释器重新启动，但没有找到该版本。",
            "请先从发布目录安装本地运行时：" + installer_hint,
        ]
    else:
        lines = [
            context_label + " must run under Python " + required_text + ".",
            "Current Python: " + current_text + ".",
            "Supported local V4 bridge runtimes: " + _format_supported_python_minors() + ".",
            "Python tried to relaunch itself under the required interpreter, but none was found.",
            "Install the local runtime from the release directory: " + installer_hint,
        ]
    return " ".join(lines)


def _show_blocking_runtime_error_gui(message_text: str) -> bool:
    return _default_runtime_advisory_popup(
        runtime_ui_text("Codex Python 运行时错误", "Codex Python Runtime Error"),
        str(message_text),
    )


def _classify_runtime_lock_owner_for_termination(
    owner: dict[str, object] | None,
    process: dict[str, object] | None,
) -> dict[str, object]:
    """Allow termination only for a verified Bootstrap/Launcher Python PID."""
    metadata = dict(owner or {})
    observed = dict(process or {})
    try:
        pid = int(metadata.get("pid", 0) or 0)
    except (TypeError, ValueError):
        pid = 0
    expected_executable = str(metadata.get("python_executable", "") or "").strip()
    process_name = str(observed.get("process_name", "") or "").strip()
    actual_executable = str(observed.get("executable_path", "") or "").strip()
    command_line = str(observed.get("command_line", "") or "").strip()
    executable_name = Path(actual_executable).name.casefold()
    expected_markers = (
        str(Path(__file__).resolve()).casefold(),
        str((BASE_DIR / "PC-REHD Code X Launcher.py").resolve()).casefold(),
    )
    command_line_folded = command_line.casefold()
    reasons: list[str] = []
    if pid <= 0 or pid == os.getpid():
        reasons.append("invalid-or-self-pid")
    if observed.get("alive") is not True:
        reasons.append("owner-not-alive")
    if executable_name not in {"python.exe", "pythonw.exe"}:
        reasons.append("not-python-executable")
    if not expected_executable or actual_executable.casefold() != expected_executable.casefold():
        reasons.append("executable-path-mismatch")
    if not command_line or not any(marker in command_line_folded for marker in expected_markers):
        reasons.append("bootstrap-launcher-command-not-confirmed")
    return {
        "eligible": not reasons,
        "pid": pid,
        "process_name": process_name,
        "expected_executable": expected_executable,
        "actual_executable": actual_executable,
        "command_line": command_line,
        "reasons": reasons,
    }


def _query_runtime_lock_owner_process(pid: int) -> dict[str, object]:
    """Read one exact Windows PID; failure means it cannot be terminated by Bootstrap."""
    if os.name != "nt" or int(pid) <= 0:
        return {"alive": False, "error": "Windows process inspection is unavailable."}
    query = (
        "$p=Get-CimInstance Win32_Process -Filter 'ProcessId={0}' -ErrorAction Stop; "
        "[pscustomobject]@{{alive=$true; process_name=$p.Name; executable_path=$p.ExecutablePath; "
        "command_line=$p.CommandLine}} | ConvertTo-Json -Compress"
    ).format(int(pid))
    try:
        completed = _run_hidden_subprocess(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                query,
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3.0,
        )
        if completed.returncode != 0:
            return {"alive": False, "error": completed.stderr[-1000:]}
        payload = json.loads(completed.stdout.strip() or "{}")
        return dict(payload) if isinstance(payload, dict) else {"alive": False}
    except Exception as exc:
        return {"alive": False, "error": f"{type(exc).__name__}: {exc}"}


def _terminate_process_exact(pid: int) -> bool:
    """Terminate exactly one already-verified PID and wait for its handle to close."""
    if os.name != "nt" or int(pid) <= 0:
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    process_terminate = 0x0001
    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(process_terminate | synchronize, False, int(pid))
    if not handle:
        return False
    try:
        if not kernel32.TerminateProcess(handle, 1):
            return False
        wait_result = int(kernel32.WaitForSingleObject(handle, 5000))
        return wait_result in {wait_object_0, wait_timeout}
    finally:
        kernel32.CloseHandle(handle)


def _terminate_verified_runtime_lock_owner(
    verification: dict[str, object],
    *,
    terminator: Callable[[int], bool] | None = None,
) -> bool:
    if verification.get("eligible") is not True:
        return False
    try:
        pid = int(verification.get("pid", 0) or 0)
    except (TypeError, ValueError):
        return False
    terminate = terminator or _terminate_process_exact
    return bool(terminate(pid))


def _ask_runtime_lock_owner_termination(owner: dict[str, object]) -> bool:
    """Show only the explicit close-or-cancel choice after owner verification."""
    pid = int(owner.get("pid", 0) or 0)
    process = _query_runtime_lock_owner_process(pid)
    verification = _classify_runtime_lock_owner_for_termination(owner, process)
    if verification.get("eligible") is not True:
        return False
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.title(runtime_ui_text("关闭占用进程", "Close blocking runtime process"))
        root.configure(background="#111820")
        root.resizable(False, False)
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        result = {"close": False}
        frame = tk.Frame(root, background="#111820", padx=24, pady=20)
        frame.pack(fill="both", expand=True)
        tk.Label(
            frame,
            text=runtime_ui_text("检测到 Python 修复进程占用运行时锁。", "A Python repair process is holding the runtime lock."),
            background="#111820",
            foreground="#F4F7FA",
            font=("Microsoft YaHei", 12, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 10))
        detail = runtime_ui_text(
            f"PID：{pid}\n进程：{verification.get('process_name', '')}\n程序路径：{verification.get('actual_executable', '')}\n\n是否关闭该进程并重试？",
            f"PID: {pid}\nProcess: {verification.get('process_name', '')}\nExecutable: {verification.get('actual_executable', '')}\n\nClose this process and retry?",
        )
        owner_label = tk.Label(
            frame,
            text=runtime_ui_text(
                f"占用者 PID {pid}    {verification.get('process_name', '')}",
                f"Blocking PID {pid}    {verification.get('process_name', '')}",
            ),
            background="#1B2530",
            foreground="#F59E0B",
            font=("Microsoft YaHei", 15, "bold"),
            anchor="w",
            padx=14,
            pady=10,
        )
        owner_label.pack(fill="x", pady=(0, 8))
        tk.Label(
            frame,
            text=detail,
            background="#1B2530",
            foreground="#C8D1DB",
            justify="left",
            anchor="w",
            wraplength=600,
            padx=14,
            pady=12,
        ).pack(fill="x")
        buttons = tk.Frame(frame, background="#111820")
        buttons.pack(fill="x", pady=(16, 0))
        tk.Button(
            buttons,
            text=runtime_ui_text("关闭占用进程并重试", "Close process and retry"),
            command=lambda: (result.update(close=True), root.destroy()),
            background="#F59E0B",
            foreground="#111820",
            relief="flat",
            padx=14,
            pady=8,
        ).pack(side="left")
        cancel = tk.Button(
            buttons,
            text=runtime_ui_text("取消", "Cancel"),
            command=root.destroy,
            background="#273646",
            foreground="#F4F7FA",
            relief="flat",
            padx=24,
            pady=8,
        )
        cancel.pack(side="right")
        root.bind("<Escape>", lambda _event: root.destroy())
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        root.update_idletasks()
        width = max(650, int(root.winfo_reqwidth()))
        height = max(220, int(root.winfo_reqheight()))
        root.geometry(f"{width}x{height}+{max(0, (root.winfo_screenwidth() - width) // 2)}+{max(0, (root.winfo_screenheight() - height) // 2)}")
        root.deiconify()
        root.lift()
        root.focus_force()
        cancel.focus_set()
        root.mainloop()
        if result.get("close") is not True:
            return False
        return _terminate_verified_runtime_lock_owner(verification)
    except Exception:
        return False


class _RequiredRuntimeInstallProgress:
    """Small non-modal status window for first-run exact-runtime installation."""

    def __init__(self, required_text: str, installer_path: Path | None = None) -> None:
        self.required_text = str(required_text)
        self.installer_path = installer_path
        self._finished = threading.Event()
        self._ui_ready = threading.Event()
        self._cancel_requested = threading.Event()
        self._automatic_started = threading.Event()
        self._manual_installer_started = threading.Event()
        self._success = False
        self._thread = threading.Thread(
            target=self._run,
            name="pc-rehd-python-runtime-progress",
            daemon=True,
        )
        self._thread.start()

    def wait_for_auto_start(self, timeout_seconds: float = 8.0) -> bool:
        if self._ui_ready.wait(timeout=1.0) is not True:
            return False
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            if self._cancel_requested.is_set():
                return True
            time.sleep(0.05)
        return self._cancel_requested.is_set()

    def begin_automatic_install(self) -> None:
        self._automatic_started.set()

    def wait_for_manual_runtime(
        self,
        finder: Callable[[], dict[str, object] | None],
        *,
        timeout_seconds: float = 1800.0,
    ) -> dict[str, object] | None:
        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            candidate = finder()
            if isinstance(candidate, dict):
                return candidate
            time.sleep(1.0)
        return None

    def finish(self, *, success: bool) -> None:
        self._success = bool(success)
        self._finished.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.5)

    def _launch_manual_installer(self) -> None:
        installer = self.installer_path
        if installer is None or installer.is_file() is not True:
            return
        try:
            subprocess.Popen([str(installer)], cwd=str(BASE_DIR), close_fds=True)
        except Exception:
            return
        self._manual_installer_started.set()

    def _request_manual_install(self, body: Any, button: Any) -> None:
        if self._cancel_requested.is_set():
            return
        self._cancel_requested.set()
        button.configure(state="disabled")
        body.configure(
            text=runtime_ui_text(
                "自动安装已停止。请在打开的 Python 安装程序中选择自定义安装并指定其他目录；安装完成后脚本会自动启动。",
                "Automatic installation stopped. Choose Customize Installation and another folder in the opened Python installer; the Launcher will start automatically after installation.",
            ),
            foreground="#FFD27A",
        )
        self._launch_manual_installer()

    def _run(self) -> None:
        try:
            import tkinter as tk

            root = tk.Tk()
            root.title("PC-REHD Code X")
            root.configure(background="#111820")
            root.resizable(False, False)
            try:
                root.attributes("-topmost", True)
            except tk.TclError:
                pass
            frame = tk.Frame(
                root,
                background="#111820",
                highlightbackground="#2D91FF",
                highlightcolor="#2D91FF",
                highlightthickness=1,
                padx=24,
                pady=20,
            )
            frame.grid(row=0, column=0, sticky="nsew")
            title = tk.Label(
                frame,
                text=runtime_ui_text(
                    "Python 安装中，请稍候！",
                    "Python runtime installation",
                ),
                background="#111820",
                foreground="#F4F7FA",
                font=("Microsoft YaHei", 13, "bold"),
                anchor="w",
            )
            title.grid(row=0, column=0, sticky="ew", pady=(0, 10))
            body = tk.Label(
                frame,
                text=runtime_ui_text(
                    f"Python {self.required_text} 正在自动安装，安装成功后脚本会重启。",
                    f"Python {self.required_text} is being installed automatically. The Launcher will restart after installation succeeds.",
                ),
                background="#111820",
                foreground="#C8D1DB",
                font=("Microsoft YaHei", 10),
                justify="left",
                anchor="w",
                wraplength=510,
            )
            body.grid(row=1, column=0, sticky="ew")
            body.configure(
                text=runtime_ui_text(
                    f"即将替换本工具管理的旧版 Python，安装新版 Python {self.required_text}。如果仍需保留旧版，请点击下方按钮停止自动安装。",
                    f"The tool-managed old Python will be replaced by Python {self.required_text}. If you need to keep the old version, click the button below to stop automatic installation.",
                )
            )
            stop_button = tk.Button(
                frame,
                text=runtime_ui_text(
                    "停止自动安装，手动安装到其他目录",
                    "Stop automatic install and choose another folder",
                ),
                command=lambda: self._request_manual_install(body, stop_button),
                background="#273646",
                foreground="#F4F7FA",
                activebackground="#38526D",
                activeforeground="#FFFFFF",
                relief="flat",
                padx=12,
                pady=8,
            )
            stop_button.grid(row=2, column=0, sticky="ew", pady=(16, 0))
            frame.columnconfigure(0, weight=1)
            root.update_idletasks()
            width = max(560, int(root.winfo_reqwidth()))
            height = max(150, int(root.winfo_reqheight()))
            x = max(0, (int(root.winfo_screenwidth()) - width) // 2)
            y = max(0, (int(root.winfo_screenheight()) - height) // 2)
            root.geometry(f"{width}x{height}+{x}+{y}")
            self._ui_ready.set()

            def poll() -> None:
                if not self._finished.is_set():
                    if self._automatic_started.is_set():
                        stop_button.configure(state="disabled")
                        body.configure(
                            text=runtime_ui_text(
                                f"Python {self.required_text} 正在自动安装，安装成功后脚本会重启。",
                                f"Python {self.required_text} is being installed automatically. The Launcher will restart after installation succeeds.",
                            )
                        )
                    root.after(100, poll)
                    return
                body.configure(
                    text=runtime_ui_text(
                        f"Python {self.required_text} 安装成功，脚本即将重启。"
                        if self._success
                        else f"Python {self.required_text} 安装失败。",
                        f"Python {self.required_text} installed successfully. The Launcher is restarting."
                        if self._success
                        else f"Python {self.required_text} installation failed.",
                    ),
                    foreground="#8BE28B" if self._success else "#FF9B9B",
                )
                stop_button.configure(state="disabled")
                root.after(850, root.destroy)

            root.protocol("WM_DELETE_WINDOW", lambda: None)
            root.deiconify()
            root.lift()
            root.after(100, poll)
            root.mainloop()
        except Exception:
            # A headless session can still complete the silent installer.
            self._ui_ready.clear()
            return


def _relaunch_under_supported_python(report: dict[str, object]) -> dict[str, object]:
    candidate = _find_supported_python_runtime_candidate()
    if not isinstance(candidate, dict):
        message_text = _build_runtime_restart_error_message(report)
        _show_blocking_runtime_error_gui(message_text)
        raise RuntimeError(message_text)
    target_path = _normalize_existing_python_path(candidate.get("path"))
    if target_path is None:
        message_text = _build_runtime_restart_error_message(report)
        _show_blocking_runtime_error_gui(message_text)
        raise RuntimeError(message_text)
    runtime_report = candidate.get("runtime_report")
    if isinstance(runtime_report, dict):
        _remember_supported_python_path(target_path, runtime_report)
    env = _isolated_python_child_environment()
    current_python = _normalize_existing_python_path(sys.executable)
    tried_paths = list(_get_tried_python_paths())
    if target_path is not None:
        target_text = str(target_path).lower()
        if target_text not in tried_paths:
            tried_paths.append(target_text)
    env[BOOTSTRAP_REEXEC_PATH_ENV] = str(target_path)
    env[BOOTSTRAP_REEXEC_DEPTH_ENV] = str(int(str(env.get(BOOTSTRAP_REEXEC_DEPTH_ENV, "0") or "0")) + 1)
    env[BOOTSTRAP_TRIED_PYTHONS_ENV] = os.pathsep.join(tried_paths)
    if current_python is not None:
        env[BOOTSTRAP_PYTHON_HINT_ENV] = str(target_path)
    completed = _run_hidden_subprocess(
        [
            str(target_path),
            *sys.argv,
        ],
        check=False,
        env=env,
    )
    raise SystemExit(completed.returncode)


def _relaunch_under_exact_python(
    required_version: tuple[int, int],
    *,
    context_label: str,
) -> dict[str, object]:
    candidate = _find_exact_python_runtime_candidate(required_version)
    if not isinstance(candidate, dict):
        message_text = _build_exact_runtime_restart_error_message(required_version, context_label=context_label)
        _show_blocking_runtime_error_gui(message_text)
        raise RuntimeError(message_text)
    target_path = _normalize_existing_python_path(candidate.get("path"))
    if target_path is None:
        message_text = _build_exact_runtime_restart_error_message(required_version, context_label=context_label)
        _show_blocking_runtime_error_gui(message_text)
        raise RuntimeError(message_text)
    runtime_report = candidate.get("runtime_report")
    if isinstance(runtime_report, dict):
        _remember_supported_python_path(target_path, runtime_report)
    env = _isolated_python_child_environment()
    current_python = _normalize_existing_python_path(sys.executable)
    tried_paths = list(_get_tried_python_paths())
    target_text = str(target_path).lower()
    if target_text not in tried_paths:
        tried_paths.append(target_text)
    env[BOOTSTRAP_REEXEC_PATH_ENV] = str(target_path)
    env[BOOTSTRAP_REEXEC_DEPTH_ENV] = str(int(str(env.get(BOOTSTRAP_REEXEC_DEPTH_ENV, "0") or "0")) + 1)
    env[BOOTSTRAP_TRIED_PYTHONS_ENV] = os.pathsep.join(tried_paths)
    if current_python is not None:
        env[BOOTSTRAP_PYTHON_HINT_ENV] = str(target_path)
    completed = _run_hidden_subprocess(
        [
            str(target_path),
            *sys.argv,
        ],
        check=False,
        env=env,
    )
    raise SystemExit(completed.returncode)


def _relaunch_under_python_path(
    target_path: str | Path,
    *,
    context_label: str,
    required_version: tuple[int, int, int],
) -> dict[str, object]:
    """Re-exec the current command under a verified interpreter path.

    This path deliberately has no fallback: the caller has requested an exact
    runtime and continuing under the old interpreter would violate that
    contract.
    """
    normalized_target = _normalize_existing_python_path(target_path)
    required_text = _python_runtime_version_text(required_version)
    if normalized_target is None:
        raise RuntimeError(
            f"{context_label} cannot start required Python {required_text}: {target_path}"
        )
    probe = _probe_python_runtime_candidate(normalized_target)
    if not isinstance(probe, dict) or not _runtime_matches_required_python(
        probe.get("version_tuple"),
        releaselevel=probe.get("releaselevel"),
    ):
        raise RuntimeError(
            f"{context_label} rejected non-matching Python runtime {normalized_target}; "
            f"required {required_text}."
        )
    runtime_report = probe.get("runtime_report")
    if isinstance(runtime_report, dict):
        _remember_supported_python_path(normalized_target, runtime_report)
    env = _isolated_python_child_environment()
    tried_paths = list(_get_tried_python_paths())
    target_text = str(normalized_target).lower()
    if target_text not in tried_paths:
        tried_paths.append(target_text)
    env[BOOTSTRAP_REEXEC_PATH_ENV] = str(normalized_target)
    env[BOOTSTRAP_REEXEC_DEPTH_ENV] = str(
        int(str(env.get(BOOTSTRAP_REEXEC_DEPTH_ENV, "0") or "0")) + 1
    )
    env[BOOTSTRAP_TRIED_PYTHONS_ENV] = os.pathsep.join(tried_paths)
    env[BOOTSTRAP_PYTHON_HINT_ENV] = str(normalized_target)
    try:
        completed = _run_hidden_subprocess(
            [str(normalized_target), *sys.argv],
            check=False,
            env=env,
        )
    except OSError as exc:
        raise RuntimeError(
            f"{context_label} could not start required Python {required_text}: {exc}"
        ) from exc
    raise SystemExit(completed.returncode)


def validate_python_runtime(version_info: object | None = None) -> dict[str, object]:
    report = get_runtime_support_report(version_info)
    if report["supported"] is True or report["override_enabled"] is True:
        if version_info is None and report.get("candidate_session") is not True:
            _remember_supported_python_path(sys.executable, report)
        return report
    if version_info is None:
        return _relaunch_under_supported_python(report)
    raise RuntimeError(_build_runtime_restart_error_message(report))


def ensure_preferred_python_runtime(
    *,
    preferred_version: tuple[int, int] = RECOMMENDED_PYTHON,
    context_label: str = "Codex Python runtime",
) -> dict[str, object]:
    target_version = _coerce_version_info(preferred_version)
    runtime_report = get_runtime_support_report()
    if runtime_report["supported"] is not True and runtime_report["override_enabled"] is not True:
        return _relaunch_under_supported_python(runtime_report)
    current_version = (sys.version_info.major, sys.version_info.minor)
    if current_version == target_version:
        _remember_supported_python_path(sys.executable, runtime_report)
        return runtime_report
    return _relaunch_under_exact_python(target_version, context_label=context_label)


def ensure_required_python_runtime(
    *,
    context_label: str = "PC-REHD Code X Launcher",
) -> dict[str, object]:
    """Ensure Launcher activation runs on exactly Python 3.14.7.

    An older or newer interpreter is only a staging process.  It first looks
    for an already-installed exact runtime, then installs the signed official
    target through the existing A/B installer, and finally re-execs.  There is
    intentionally no old-runtime fallback.
    """
    target_version = REQUIRED_PYTHON_RUNTIME
    target_text = _python_runtime_version_text(target_version)
    current_probe = _probe_python_runtime_candidate(Path(sys.executable))
    if isinstance(current_probe, dict) and _runtime_matches_required_python(
        current_probe.get("version_tuple"),
        releaselevel=current_probe.get("releaselevel"),
    ):
        runtime_report = current_probe.get("runtime_report")
        if isinstance(runtime_report, dict) and runtime_report.get("supported") is True:
            _remember_supported_python_path(sys.executable, runtime_report)
            return {
                **runtime_report,
                "required_python": target_text,
                "exact_required": True,
            }

    installed_candidate = _find_exact_python_runtime_patch_candidate(target_version)
    if isinstance(installed_candidate, dict):
        return _relaunch_under_python_path(
            str(installed_candidate.get("path", "")),
            context_label=context_label,
            required_version=target_version,
        )

    installer_path = _find_required_python_installer_path()
    progress = _RequiredRuntimeInstallProgress(target_text, installer_path)
    if progress.wait_for_auto_start():
        manual_candidate = progress.wait_for_manual_runtime(
            lambda: _find_exact_python_runtime_patch_candidate(target_version)
        )
        if isinstance(manual_candidate, dict):
            progress.finish(success=True)
            return _relaunch_under_python_path(
                str(manual_candidate.get("path", "")),
                context_label=context_label,
                required_version=target_version,
            )
        progress.finish(success=False)
        message = (
            f"{context_label} is waiting for a separate Python {target_text} installation, "
            "but no matching interpreter was found."
        )
        _show_blocking_runtime_error_gui(message)
        raise RuntimeError(message)
    progress.begin_automatic_install()
    try:
        try:
            upgrade = run_python_runtime_upgrade(
                target_python=target_text,
                force=True,
            )
        except RuntimeInstallLockTimeout as exc:
            lock_path = Path(str(exc.report.get("lock_path", "") or ""))
            if (
                lock_path.name.casefold() != RUNTIME_INTERPRETER_UPDATE_LOCK_PATH.name.casefold()
                or not _ask_runtime_lock_owner_termination(dict(exc.report.get("owner", {}) or {}))
            ):
                raise
            upgrade = run_python_runtime_upgrade(
                target_python=target_text,
                force=True,
            )
    except Exception as exc:
        progress.finish(success=False)
        message = (
            f"{context_label} requires Python {target_text}. Automatic installation failed: "
            f"{type(exc).__name__}: {exc}"
        )
        _show_blocking_runtime_error_gui(message)
        raise RuntimeError(message) from exc
    if upgrade.get("ready") is not True or str(upgrade.get("status", "")).casefold() != "promoted":
        progress.finish(success=False)
        message = (
            f"{context_label} requires Python {target_text}. Automatic installation did not complete: "
            + json.dumps(upgrade, ensure_ascii=False)[-12000:]
        )
        _show_blocking_runtime_error_gui(message)
        raise RuntimeError(message)
    active = upgrade.get("active")
    active_path = (
        dict(active).get("python_exe")
        if isinstance(active, dict)
        else ""
    )
    progress.finish(success=True)
    return _relaunch_under_python_path(
        str(active_path or ""),
        context_label=context_label,
        required_version=target_version,
    )


def _extract_first_version_text(text_value: str) -> str | None:
    match = re.search(r"(\d+(?:\.\d+)+)", text_value)
    if match is None:
        return None
    return match.group(1)


def _extract_version_from_text(text_value: str) -> str | None:
    patterns = (
        r"__version__\s*=\s*[\"']([^\"']+)[\"']",
        r"\bVERSION\s*=\s*[\"']([^\"']+)[\"']",
        r"\bversion\s*=\s*[\"']([^\"']+)[\"']",
        r"\bversion\s*:\s*[\"']([^\"']+)[\"']",
    )
    for pattern in patterns:
        match = re.search(pattern, text_value)
        if match is not None:
            return str(match.group(1)).strip()
    return None


def _read_candidate_metadata_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""
    except Exception:
        return ""


def _version_key(version_text: str | None) -> tuple[tuple[int, object], ...]:
    if version_text is None:
        return tuple()
    text_value = str(version_text).strip()
    if text_value == "":
        return tuple()
    key_parts: list[tuple[int, object]] = []
    for raw_part in re.split(r"[^0-9A-Za-z]+", text_value):
        if raw_part == "":
            continue
        for token in re.findall(r"\d+|[A-Za-z]+", raw_part):
            if token.isdigit():
                key_parts.append((0, int(token)))
            else:
                key_parts.append((1, token.lower()))
    return tuple(key_parts)


def _compare_version_text(left_version: str | None, right_version: str | None) -> int:
    left_key = _version_key(left_version)
    right_key = _version_key(right_version)
    max_count = max(len(left_key), len(right_key))
    for index in range(max_count):
        left_part = left_key[index] if index < len(left_key) else (0, 0)
        right_part = right_key[index] if index < len(right_key) else (0, 0)
        if left_part == right_part:
            continue
        return -1 if left_part < right_part else 1
    return 0


def _extract_candidate_version(package_name: str) -> str | None:
    package_path = Path(package_name)
    candidate_version = _extract_first_version_text(package_path.name)
    if candidate_version is not None:
        return candidate_version
    if package_path.exists() is not True or package_path.is_dir() is not True:
        return None
    metadata_paths = (
        package_path / "__init__.py",
        package_path / "setup.py",
        package_path / "setup.cfg",
        package_path / "pyproject.toml",
        package_path / package_path.name / "__init__.py",
    )
    for metadata_path in metadata_paths:
        if metadata_path.exists() is not True:
            continue
        candidate_version = _extract_version_from_text(_read_candidate_metadata_text(metadata_path))
        if candidate_version not in (None, ""):
            return candidate_version
    return None


def _candidate_sort_key(package_name: str) -> tuple[tuple[tuple[int, object], ...], int, str]:
    candidate_path = Path(package_name)
    is_prebuilt = 1
    if candidate_path.exists() and candidate_path.is_dir():
        is_prebuilt = 1 if (candidate_path / "__init__.py").exists() else 0
    return (_version_key(_extract_candidate_version(package_name)), is_prebuilt, str(package_name).lower())


def _patched_ufbx_source_fingerprint(source_dir: Path | None = None) -> str:
    root = PATCHED_UFBX_SOURCE_DIR if source_dir is None else Path(source_dir)
    if root.is_dir() is not True:
        return "missing:" + hashlib.sha256(str(root).encode("utf-8", errors="replace")).hexdigest()
    digest = hashlib.sha256()
    included_count = 0
    for file_path in sorted(root.rglob("*"), key=lambda path: path.as_posix().casefold()):
        if file_path.is_file() is not True:
            continue
        relative_path = file_path.relative_to(root)
        relative_parts = relative_path.parts
        if any(
            part in {"build", "__pycache__", ".git"} or part.endswith(".egg-info")
            for part in relative_parts
        ):
            continue
        if file_path.suffix.casefold() in {".pyd", ".pyc", ".pyo"}:
            continue
        relative_text = relative_path.as_posix()
        digest.update(relative_text.encode("utf-8", errors="replace"))
        digest.update(b"\0")
        digest.update(_sha256_file(file_path, use_cache=False).encode("ascii"))
        digest.update(b"\0")
        included_count += 1
    return f"tree-sha256:{digest.hexdigest()}:{included_count}"


def _candidate_artifact_fingerprint(package_name: str, *, import_name: str | None = None) -> str:
    candidate_path = Path(package_name)
    if import_name == "ufbx":
        prebuilt_dir = _prebuilt_ufbx_package_dir()
        if prebuilt_dir is not None:
            candidate_path = prebuilt_dir
    if candidate_path.is_file():
        # Artifact identity is a safety contract, so it must not trust metadata
        # caches that can survive same-size rewrites with restored timestamps.
        return "sha256:" + _sha256_file(candidate_path, use_cache=False)
    if candidate_path.is_dir():
        digest = hashlib.sha256()
        included_count = 0
        for file_path in sorted(candidate_path.rglob("*"), key=lambda path: path.as_posix().casefold()):
            if file_path.is_file() is not True:
                continue
            relative_parts = file_path.relative_to(candidate_path).parts
            if any(part == "__pycache__" or part.endswith(".egg-info") for part in relative_parts):
                continue
            if file_path.suffix.casefold() not in {".py", ".pyx", ".pxd", ".c", ".h", ".pyd", ".whl", ".toml", ".cfg"}:
                continue
            relative_text = file_path.relative_to(candidate_path).as_posix()
            digest.update(relative_text.encode("utf-8", errors="replace"))
            digest.update(b"\0")
            digest.update(_sha256_file(file_path, use_cache=False).encode("ascii"))
            digest.update(b"\0")
            included_count += 1
        return f"tree-sha256:{digest.hexdigest()}:{included_count}"
    return "missing:" + hashlib.sha256(str(candidate_path).encode("utf-8", errors="replace")).hexdigest()


def _candidate_source_label(package_name: str) -> str:
    candidate_path = Path(package_name)
    if _path_is_within(candidate_path, UPGRADE_VENDOR_ROOT_DIR):
        return "local-upgrade"
    if _path_is_within(candidate_path, FIXED_VENDOR_ROOT_DIR):
        return "local-fixed"
    if _path_is_within(candidate_path, PACKAGED_VENDOR_PY_ROOT_DIR):
        return "packaged-abi"
    return "local-candidate"


def _artifact_block_key(
    import_name: str,
    *,
    version: str,
    source: str,
    fingerprint: str,
) -> str:
    identity = {
        "import_name": str(import_name),
        "version": str(version or "unknown"),
        "source": str(source or "unknown"),
        "fingerprint": str(fingerprint or "unknown"),
    }
    return hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def get_candidate_artifact_report(import_name: str, package_name: str) -> dict[str, object]:
    version = str(_extract_candidate_version(package_name) or "unknown")
    source = _candidate_source_label(package_name)
    fingerprint = _candidate_artifact_fingerprint(package_name, import_name=import_name)
    block_key = _artifact_block_key(
        import_name,
        version=version,
        source=source,
        fingerprint=fingerprint,
    )
    payload = _load_runtime_state()
    blocked_artifacts = payload.get("blocked_artifacts", {})
    entry = blocked_artifacts.get(block_key) if isinstance(blocked_artifacts, dict) else None
    return {
        "import_name": import_name,
        "candidate": str(package_name),
        "version": version,
        "source": source,
        "fingerprint": fingerprint,
        "block_key": block_key,
        "blocked": isinstance(entry, dict),
        "blocked_entry": dict(entry) if isinstance(entry, dict) else None,
    }


def _artifact_report_has_stable_identity(report: dict[str, object]) -> bool:
    fingerprint = str(report.get("fingerprint", "") or "")
    return fingerprint.startswith("sha256:") or fingerprint.startswith("tree-sha256:")


def _record_failed_candidate_artifact(
    import_name: str,
    package_name: str,
    reason_text: str,
) -> dict[str, object]:
    report = get_candidate_artifact_report(import_name, package_name)
    def update(payload: dict[str, object]) -> None:
        blocked_artifacts = payload.setdefault("blocked_artifacts", {})
        if not isinstance(blocked_artifacts, dict):
            blocked_artifacts = {}
            payload["blocked_artifacts"] = blocked_artifacts
        block_key = str(report["block_key"])
        previous = blocked_artifacts.get(block_key, {})
        failure_count = int(previous.get("failure_count", 0) or 0) + 1 if isinstance(previous, dict) else 1
        blocked_artifacts[block_key] = {
            **{key: report[key] for key in ("import_name", "candidate", "version", "source", "fingerprint")},
            "reason": str(reason_text or "")[-8000:],
            "failure_count": failure_count,
            "last_failure_epoch": time.time(),
        }

    _update_runtime_state(update)
    return get_candidate_artifact_report(import_name, package_name)


def _record_failed_online_artifact(
    import_name: str,
    *,
    version: str,
    source_url: str,
    sha256: str,
    reason_text: str,
) -> dict[str, object] | None:
    if not sha256:
        return None
    distribution_name = str(IMPORT_POLICY.get(import_name, {}).get("distribution_name", import_name) or import_name)
    source = "online:" + OFFICIAL_PYPI_INDEX_URL + "#" + distribution_name.casefold()
    fingerprint = "sha256:" + str(sha256).casefold()
    block_key = _artifact_block_key(
        import_name,
        version=version,
        source=source,
        fingerprint=fingerprint,
    )
    def update(payload: dict[str, object]) -> dict[str, object]:
        blocked_artifacts = payload.setdefault("blocked_artifacts", {})
        if not isinstance(blocked_artifacts, dict):
            blocked_artifacts = {}
            payload["blocked_artifacts"] = blocked_artifacts
        previous = blocked_artifacts.get(block_key, {})
        blocked_artifacts[block_key] = {
            "import_name": import_name,
            "candidate": source_url,
            "version": str(version or "unknown"),
            "source": source,
            "fingerprint": fingerprint,
            "reason": str(reason_text or "")[-8000:],
            "failure_count": int(previous.get("failure_count", 0) or 0) + 1 if isinstance(previous, dict) else 1,
            "last_failure_epoch": time.time(),
        }
        return dict(blocked_artifacts[block_key])

    return _update_runtime_state(update)


def _online_artifact_is_blocked(import_name: str, download: dict[str, object]) -> bool:
    sha256 = str(download.get("sha256", "") or "").casefold()
    if not sha256:
        return False
    version = str(download.get("version", "") or "unknown")
    distribution_name = str(IMPORT_POLICY.get(import_name, {}).get("distribution_name", import_name) or import_name)
    block_key = _artifact_block_key(
        import_name,
        version=version,
        source="online:" + OFFICIAL_PYPI_INDEX_URL + "#" + distribution_name.casefold(),
        fingerprint="sha256:" + sha256,
    )
    blocked_artifacts = _load_runtime_state().get("blocked_artifacts", {})
    return isinstance(blocked_artifacts, dict) and isinstance(blocked_artifacts.get(block_key), dict)


def _get_best_candidate_version(candidates: tuple[str, ...]) -> str | None:
    best_version: str | None = None
    for package_name in candidates:
        candidate_version = _extract_candidate_version(package_name)
        if candidate_version is None:
            continue
        if best_version is None or _compare_version_text(candidate_version, best_version) > 0:
            best_version = candidate_version
    return best_version


def _get_install_candidates(
    import_name: str,
    *,
    fixed_only: bool = False,
    upgrade_only: bool = False,
) -> tuple[str, ...]:
    if fixed_only is True and upgrade_only is True:
        raise ValueError("fixed_only and upgrade_only cannot both be true")
    if fixed_only is True:
        candidates = list(_get_fixed_install_candidates(import_name))
    elif upgrade_only is True:
        candidates = list(_get_upgrade_install_candidates(import_name))
    else:
        refresh_package_candidates()
        candidates = list(PACKAGE_BY_IMPORT_NAME.get(import_name, ()))
    allow_upgrade = IMPORT_POLICY.get(import_name, {}).get("allow_upgrade") is True
    upgrade_candidates = set(_get_upgrade_install_candidates(import_name)) if allow_upgrade else set()
    if allow_upgrade:
        candidates.sort(key=_candidate_sort_key, reverse=True)
    accepted_candidates: list[str] = []
    for candidate in candidates:
        artifact_report = get_candidate_artifact_report(import_name, candidate)
        if artifact_report.get("blocked") is True:
            continue
        # blocked_versions predates artifact fingerprints. Keep it only for a
        # candidate whose exact bytes cannot be identified; otherwise a new
        # build with the same version must be allowed to rehabilitate itself.
        if (
            candidate in upgrade_candidates
            and _artifact_report_has_stable_identity(artifact_report) is not True
            and _is_import_version_blocked(import_name, _extract_candidate_version(candidate)) is True
        ):
            continue
        accepted_candidates.append(candidate)
    return tuple(accepted_candidates)


def _get_import_local_approved_version(import_name: str) -> str | None:
    return _get_best_candidate_version(_get_install_candidates(import_name))


def _get_import_local_floor_version(import_name: str) -> str | None:
    return _get_best_candidate_version(_get_install_candidates(import_name, fixed_only=True))


def _read_integer_assignment(path: Path, assignment_name: str) -> int | None:
    if path.exists() is not True:
        return None
    pattern = re.compile(r"^\s*" + re.escape(assignment_name) + r"\s*=\s*(\d+)\s*$", re.MULTILINE)
    match = pattern.search(_read_candidate_metadata_text(path))
    return int(match.group(1)) if match is not None else None


def _run_clean_export_bridge_contract_probe(
    bridge_path: Path,
    bootstrap_path: Path,
) -> dict[str, object]:
    """Import copied export/import sources in a fresh interpreter with no pycache."""
    probe_source_path = BASE_DIR / "codex_fbx_probe.py"
    import_source_path = BASE_DIR / "codex_re6_mod_import_fbx.py"
    compatibility_source_path = BASE_DIR / "codex_re6_scene_compatibility.py"
    launcher_source_path = BASE_DIR / "PC-REHD Code X Launcher.py"
    real_pl0600_fixture_candidates = (
        BASE_DIR / "_codex_final_audit" / "pl0600.mod",
        BASE_DIR.parent / "MRL研究" / "AI MRL Editor - Incomplete - 真神" / "pl0600.mod",
    )
    probe_code = "\n".join(
        (
            "import importlib.util",
            "import json",
            "from pathlib import Path",
            "import sys",
            "import traceback",
            "",
            "root = Path(sys.argv[1]).resolve()",
            "writer_path = root / 'codex_python_export_bridge.py'",
            "import_path = root / 'codex_re6_mod_import_fbx.py'",
            "compatibility_path = root / 'codex_re6_scene_compatibility.py'",
            "bootstrap_path = root / 'codex_python_runtime_bootstrap.py'",
            f"required_status_names = {EXPORT_BRIDGE_REQUIRED_REGRESSION_STATUSES!r}",
            "payload = {'ready': False, 'root': str(root)}",
            "",
            "def load_exact(name, path):",
            "    spec = importlib.util.spec_from_file_location(name, path)",
            "    if spec is None or spec.loader is None:",
            "        raise ImportError('Unable to create import spec for ' + str(path))",
            "    module = importlib.util.module_from_spec(spec)",
            "    sys.modules[name] = module",
            "    spec.loader.exec_module(module)",
            "    return module",
            "",
            "try:",
            "    sys.path.insert(0, str(root))",
            "    bootstrap = load_exact('codex_python_runtime_bootstrap', bootstrap_path)",
            "    bridge = load_exact('codex_python_export_bridge', writer_path)",
            "    import_module = load_exact('codex_re6_mod_import_fbx', import_path)",
            "    import codex_re6_scene_compatibility as compatibility",
            "    writer_suite = bridge.run_writer_maintenance_regression_suite()",
            "    import_suite = import_module.run_import_maintenance_regression_suite()",
            "    statuses = {name: getattr(bridge, name, None) for name in required_status_names}",
            f"    optional_fixture_statuses = {EXPORT_BRIDGE_OPTIONAL_FIXTURE_REGRESSION_STATUSES!r}",
            "    status_warnings = [name + ':' + str(value.get('status')) for name, value in statuses.items() if isinstance(value, dict) and value.get('status') == 'SKIP' and name in optional_fixture_statuses]",
            "    statuses_ready = all(isinstance(value, dict) and (value.get('status') == 'PASS' or (value.get('status') == 'SKIP' and name in optional_fixture_statuses)) for name, value in statuses.items())",
            "    revision = int(getattr(bridge, 'DELETE_SELECTED_STABLE_SLOT_CONTRACT_REVISION', 0) or 0)",
            "    writer_entry_ready = callable(getattr(bridge, 'run_memory_export', None))",
            "    maintenance_error = getattr(bridge, 'WRITER_MAINTENANCE_GATE_ERROR', None)",
            "    import_revision = int(getattr(import_module, 'IMPORT_MODULE_CONTRACT_REVISION', 0) or 0)",
            "    import_status = import_suite",
            "    import_route_ready = callable(getattr(import_module, 'build_normal_route_table', None)) and callable(getattr(import_module, '_build_fbx_roots', None))",
            "    compatibility_ready = Path(str(getattr(compatibility, '__file__', '') or '')).resolve() == compatibility_path and callable(getattr(compatibility, 'describe_import_skin_compatibility', None)) and callable(getattr(compatibility, 'apply_export_compatibility_contract', None))",
            f"    import_ready = import_revision == {REQUIRED_RE6_MOD_IMPORT_FBX_CONTRACT_REVISION!r} and isinstance(import_status, dict) and import_status.get('status') == 'PASS' and import_route_ready",
            "    writer_origin = Path(str(getattr(bridge, '__file__', '') or '')).resolve()",
            "    import_origin = Path(str(getattr(import_module, '__file__', '') or '')).resolve()",
            "    bootstrap_origin = Path(str(getattr(bootstrap, '__file__', '') or '')).resolve()",
            "    writer_ready = isinstance(writer_suite, dict) and writer_suite.get('status') == 'PASS' and statuses_ready and writer_entry_ready and maintenance_error is None and revision >= 2 and writer_origin == writer_path",
            "    ready = writer_ready and import_ready and compatibility_ready and import_origin == import_path and bootstrap_origin == bootstrap_path",
            f"    payload.update({{'ready': ready, 'revision': revision, 'writer_entry_ready': writer_entry_ready, 'writer_ready': writer_ready, 'writer_maintenance_suite': writer_suite, 'regression_statuses': statuses, 'maintenance_warnings': status_warnings, 'maintenance_error': '' if maintenance_error is None else repr(maintenance_error), 'writer_origin': str(writer_origin), 'import_revision': import_revision, 'required_import_revision': {REQUIRED_RE6_MOD_IMPORT_FBX_CONTRACT_REVISION!r}, 'import_maintenance_suite': import_suite, 'import_regression_status': import_status, 'import_origin': str(import_origin), 'import_route_ready': import_route_ready, 'import_ready': import_ready, 'bootstrap_origin': str(bootstrap_origin), 'error_type': '' if ready else 'PythonWriterImporterContractRejected', 'error': '' if ready else 'Python writer/importer executable contract rejected; writer_ready=' + str(writer_ready) + '; import_ready=' + str(import_ready), 'traceback': ''}})",
            "except BaseException as exc:",
            "    payload.update({'error_type': type(exc).__name__, 'error': str(exc), 'traceback': traceback.format_exc()})",
            "print(json.dumps(payload, ensure_ascii=False))",
            "raise SystemExit(0 if payload.get('ready') is True else 42)",
        )
    )
    with tempfile.TemporaryDirectory(prefix="codex-v4-writer-clean-contract-") as temp_text:
        temp_root = Path(temp_text)
        staged_bridge = temp_root / bridge_path.name
        staged_bootstrap = temp_root / bootstrap_path.name
        shutil.copy2(bridge_path, staged_bridge)
        shutil.copy2(bootstrap_path, staged_bootstrap)
        if import_source_path.is_file():
            shutil.copy2(import_source_path, temp_root / import_source_path.name)
        if compatibility_source_path.is_file():
            shutil.copy2(
                compatibility_source_path,
                temp_root / compatibility_source_path.name,
            )
        if probe_source_path.is_file():
            shutil.copy2(probe_source_path, temp_root / probe_source_path.name)
        if launcher_source_path.is_file():
            shutil.copy2(launcher_source_path, temp_root / launcher_source_path.name)
        real_pl0600_fixture = next(
            (candidate for candidate in real_pl0600_fixture_candidates if candidate.is_file()),
            None,
        )
        if real_pl0600_fixture is not None:
            staged_fixture = temp_root / "_codex_final_audit" / "pl0600.mod"
            staged_fixture.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(real_pl0600_fixture, staged_fixture)

        child_env = _isolated_python_child_environment()
        child_env.update(
            {
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPYCACHEPREFIX": str(temp_root / "pycache"),
                PACKAGED_VENDOR_ROOT_OVERRIDE_ENV: str(PACKAGED_VENDOR_PY_ROOT_DIR),
                RUNTIME_ROOT_OVERRIDE_ENV: str(RUNTIME_ROOT_DIR),
            }
        )
        try:
            completed = _run_hidden_subprocess(
                [sys.executable, "-I", "-B", "-c", probe_code, str(temp_root)],
                cwd=str(temp_root),
                env=child_env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            return {
                "ready": False,
                "returncode": None,
                "error_type": "TimeoutExpired",
                "error": "Clean copied writer/importer source import timed out after 120 seconds.",
                "traceback": traceback.format_exc(),
                "stdout": stdout,
                "stderr": stderr,
                "stdout_tail": stdout[-3000:],
                "stderr_tail": stderr[-5000:],
                "python": str(sys.executable),
                "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
            }
        payload = _decode_json_subprocess_output(completed.stdout)
        result = dict(payload) if isinstance(payload, dict) else {}
        result.update(
            {
                "ready": completed.returncode == 0 and result.get("ready") is True,
                "returncode": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
                "stdout_tail": completed.stdout[-3000:],
                "stderr_tail": completed.stderr[-5000:],
                "python": str(sys.executable),
                "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
            }
        )
        if not result.get("error") and completed.returncode != 0:
            result["error_type"] = str(result.get("error_type") or "CleanSourceImportError")
            result["error"] = "Clean copied writer import exited with code " + str(completed.returncode)
        return result


def get_export_bridge_contract_report() -> dict[str, object]:
    """Verify the exact writer/importer in source and clean copied lanes."""
    bridge_path = BASE_DIR / "codex_python_export_bridge.py"
    import_path = BASE_DIR / "codex_re6_mod_import_fbx.py"
    compatibility_path = BASE_DIR / "codex_re6_scene_compatibility.py"
    bootstrap_path = Path(__file__).resolve()
    report: dict[str, object] = {
        "ready": False,
        "bridge_path": str(bridge_path),
        "import_path": str(import_path),
        "compatibility_path": str(compatibility_path),
        "bootstrap_path": str(bootstrap_path),
        "delete_selected_stable_slot_contract_revision": None,
        "minimum_delete_selected_stable_slot_contract_revision": (
            MIN_DELETE_SELECTED_STABLE_SLOT_CONTRACT_REVISION
        ),
        "required_re6_mod_import_fbx_contract_revision": (
            REQUIRED_RE6_MOD_IMPORT_FBX_CONTRACT_REVISION
        ),
        "check_mode": "real-source-import+clean-child-import",
    }
    if (
        bridge_path.is_file() is not True
        or import_path.is_file() is not True
        or compatibility_path.is_file() is not True
    ):
        missing_path = next(
            path
            for path in (bridge_path, import_path, compatibility_path)
            if path.is_file() is not True
        )
        report.update(
            {
                "error_type": "FileNotFoundError",
                "error": "Required bridge source is missing: " + str(missing_path),
                "traceback": "",
            }
        )
        return report

    try:
        bridge_hash = _sha256_file(bridge_path, use_cache=False)
        import_hash = _sha256_file(import_path, use_cache=False)
        compatibility_hash = _sha256_file(compatibility_path, use_cache=False)
        bootstrap_hash = _sha256_file(bootstrap_path, use_cache=False)
    except Exception as exc:
        report.update(
            {
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        return report

    report["bridge_sha256"] = bridge_hash
    report["import_sha256"] = import_hash
    report["compatibility_sha256"] = compatibility_hash
    report["bootstrap_sha256"] = bootstrap_hash
    cache_key = (
        str(bridge_path.resolve()).casefold(),
        bridge_hash.casefold(),
        str(import_path.resolve()).casefold(),
        import_hash.casefold(),
        str(compatibility_path.resolve()).casefold(),
        compatibility_hash.casefold(),
        bootstrap_hash.casefold(),
    )
    cached = _EXPORT_BRIDGE_CONTRACT_CACHE.get(cache_key)
    if isinstance(cached, dict):
        return dict(cached)

    module_name = "_codex_v4_export_bridge_contract_" + bridge_hash[:16]
    import_module_name = "_codex_re6_import_fbx_contract_" + import_hash[:16]
    inserted_base_dir = False
    try:
        base_dir_text = str(BASE_DIR)
        if base_dir_text not in sys.path:
            sys.path.insert(0, base_dir_text)
            inserted_base_dir = True
        spec = importlib.util.spec_from_file_location(module_name, bridge_path)
        if spec is None or spec.loader is None:
            raise ImportError("Unable to create an import spec for the export bridge")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        import_spec = importlib.util.spec_from_file_location(import_module_name, import_path)
        if import_spec is None or import_spec.loader is None:
            raise ImportError("Unable to create an import spec for the RE6 MOD import FBX module")
        import_module = importlib.util.module_from_spec(import_spec)
        sys.modules[import_module_name] = import_module
        import_spec.loader.exec_module(import_module)
        compatibility_module = sys.modules.get("codex_re6_scene_compatibility")

        writer_suite_runner = getattr(module, "run_writer_maintenance_regression_suite", None)
        if not callable(writer_suite_runner):
            raise RuntimeError("Writer strict maintenance regression entry is missing")
        writer_maintenance_suite = writer_suite_runner()
        import_suite_runner = getattr(import_module, "run_import_maintenance_regression_suite", None)
        if not callable(import_suite_runner):
            raise RuntimeError("Importer strict maintenance regression entry is missing")
        import_maintenance_suite = import_suite_runner()

        actual_revision = int(
            getattr(module, "DELETE_SELECTED_STABLE_SLOT_CONTRACT_REVISION", 0) or 0
        )
        status_report: dict[str, object] = {}
        maintenance_warnings: list[str] = []
        statuses_ready = True
        for status_name in EXPORT_BRIDGE_REQUIRED_REGRESSION_STATUSES:
            status_value = getattr(module, status_name, None)
            status_report[status_name] = status_value
            status_text = (
                str(status_value.get("status", "") or "").upper()
                if isinstance(status_value, dict)
                else ""
            )
            if (
                status_text == "SKIP"
                and status_name in EXPORT_BRIDGE_OPTIONAL_FIXTURE_REGRESSION_STATUSES
            ):
                maintenance_warnings.append(f"{status_name}:SKIP")
            elif status_text != "PASS":
                statuses_ready = False

        writer_entry_ready = callable(getattr(module, "run_memory_export", None))
        maintenance_error = getattr(module, "WRITER_MAINTENANCE_GATE_ERROR", None)
        imported_origin = Path(str(getattr(module, "__file__", "") or "")).resolve()
        origin_ready = imported_origin == bridge_path.resolve()
        import_revision = int(getattr(import_module, "IMPORT_MODULE_CONTRACT_REVISION", 0) or 0)
        import_regression_status = import_maintenance_suite
        import_origin = Path(str(getattr(import_module, "__file__", "") or "")).resolve()
        compatibility_origin = Path(
            str(getattr(compatibility_module, "__file__", "") or "")
        ).resolve()
        compatibility_ready = (
            compatibility_origin == compatibility_path.resolve()
            and callable(
                getattr(compatibility_module, "describe_import_skin_compatibility", None)
            )
            and callable(
                getattr(compatibility_module, "apply_export_compatibility_contract", None)
            )
        )
        import_route_ready = callable(getattr(import_module, "build_normal_route_table", None)) and callable(
            getattr(import_module, "_build_fbx_roots", None)
        )
        import_ready = (
            import_revision == REQUIRED_RE6_MOD_IMPORT_FBX_CONTRACT_REVISION
            and isinstance(import_regression_status, dict)
            and import_regression_status.get("status") == "PASS"
            and import_origin == import_path.resolve()
            and import_route_ready
        )
        writer_ready = (
            actual_revision >= MIN_DELETE_SELECTED_STABLE_SLOT_CONTRACT_REVISION
            and writer_entry_ready
            and isinstance(writer_maintenance_suite, dict)
            and writer_maintenance_suite.get("status") == "PASS"
            and statuses_ready
            and maintenance_error is None
            and origin_ready
        )
        contract_ready = writer_ready and import_ready and compatibility_ready
        report.update(
            {
                "ready": contract_ready,
                "delete_selected_stable_slot_contract_revision": actual_revision,
                "writer_entry_ready": writer_entry_ready,
                "writer_ready": writer_ready,
                "writer_maintenance_suite": writer_maintenance_suite,
                "regression_statuses": status_report,
                "maintenance_warnings": maintenance_warnings,
                "maintenance_error": (
                    "" if maintenance_error is None else repr(maintenance_error)
                ),
                "imported_origin": str(imported_origin),
                "origin_ready": origin_ready,
                "re6_mod_import_fbx_contract_revision": import_revision,
                "required_re6_mod_import_fbx_contract_revision": (
                    REQUIRED_RE6_MOD_IMPORT_FBX_CONTRACT_REVISION
                ),
                "minimum_re6_mod_import_fbx_contract_revision": (
                    REQUIRED_RE6_MOD_IMPORT_FBX_CONTRACT_REVISION
                ),
                "re6_mod_import_fbx_regression_status": import_regression_status,
                "import_maintenance_suite": import_maintenance_suite,
                "re6_mod_import_fbx_origin": str(import_origin),
                "re6_mod_import_fbx_route_ready": import_route_ready,
                "re6_mod_import_fbx_ready": import_ready,
                "compatibility_origin": str(compatibility_origin),
                "compatibility_ready": compatibility_ready,
                "error_type": "" if contract_ready else "PythonWriterImporterContractRejected",
                "error": "" if contract_ready else (
                    "Python writer/importer executable contract rejected; writer_ready="
                    + str(writer_ready)
                    + "; import_ready="
                    + str(import_ready)
                    + "; import_revision="
                    + str(import_revision)
                    + "; import_route_ready="
                    + str(import_route_ready)
                    + "; compatibility_ready="
                    + str(compatibility_ready)
                ),
                "traceback": "",
            }
        )
    except Exception as exc:
        report.update(
            {
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
    finally:
        sys.modules.pop(module_name, None)
        sys.modules.pop(import_module_name, None)
        if inserted_base_dir:
            try:
                sys.path.remove(str(BASE_DIR))
            except ValueError:
                pass

    if report.get("ready") is True:
        try:
            clean_source_report = _run_clean_export_bridge_contract_probe(
                bridge_path,
                bootstrap_path,
            )
        except Exception as exc:
            clean_source_report = {
                "ready": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
        report["clean_source_import"] = clean_source_report
        if clean_source_report.get("ready") is not True:
            report["ready"] = False
            report["error_type"] = str(
                clean_source_report.get("error_type") or "CleanSourceImportError"
            )
            report["error"] = str(
                clean_source_report.get("error")
                or "Clean copied writer source failed its executable contract."
            )
            report["traceback"] = str(clean_source_report.get("traceback") or "")
            report["stdout"] = str(clean_source_report.get("stdout") or "")
            report["stderr"] = str(clean_source_report.get("stderr") or "")
    else:
        report["clean_source_import"] = {
            "ready": False,
            "skipped": "source import contract failed",
        }

    _EXPORT_BRIDGE_CONTRACT_CACHE[cache_key] = dict(report)
    return dict(report)


def _iter_present_release_dependency_roots() -> list[Path]:
    roots: list[Path] = []
    for release_dir_name in RELEASE_DEPENDENCY_DIR_NAMES:
        release_dir = BASE_DIR / release_dir_name
        for wrapper_dir_name in DEPENDENCY_WRAPPER_DIR_NAMES:
            candidate = release_dir / wrapper_dir_name
            if _dependency_dir_contains_vendors(candidate) and candidate.resolve() != DEPENDENCY_BASE_DIR.resolve():
                roots.append(candidate)
    return roots


def _iter_managed_accelerator_files(root: Path) -> list[Path]:
    managed_suffixes = {".py", ".c", ".h", ".pyd", ".whl", ".toml", ".cfg"}
    ignored_dir_names = {"build", "__pycache__"}
    files: list[Path] = []
    if root.exists() is not True:
        return files
    for candidate in root.rglob("*"):
        if candidate.is_file() is not True:
            continue
        relative_parts = candidate.relative_to(root).parts
        if any(part in ignored_dir_names or part.endswith(".egg-info") for part in relative_parts):
            continue
        if candidate.suffix.lower() in managed_suffixes:
            files.append(candidate)
    return files


def get_accelerator_dependency_sync_report(
    *,
    refresh: bool = False,
    include_release_copies: bool = True,
) -> dict[str, object]:
    cache_key = bool(include_release_copies)
    if refresh is not True and cache_key in ACCELERATOR_SYNC_REPORT_CACHE:
        return dict(ACCELERATOR_SYNC_REPORT_CACHE[cache_key])

    errors: list[str] = []
    warnings: list[str] = []
    checked_files: list[str] = []
    if RECOMMENDED_PYTHON not in SUPPORTED_PYTHON_MINORS:
        errors.append("recommended Python is not registered as a formally supported ABI")
    roots = [FIXED_VENDOR_ROOT_DIR]
    if include_release_copies is True:
        roots.extend(root / "_vendor_fixed" for root in _iter_present_release_dependency_roots())
    for fixed_root in roots:
        for relative_path, expected_sha256 in ACCELERATOR_FIXED_SOURCE_SHA256.items():
            candidate = fixed_root / relative_path
            checked_files.append(str(candidate))
            if candidate.exists() is not True:
                errors.append("missing synchronized dependency file: " + str(candidate))
                continue
            actual_sha256 = _sha256_file(candidate, use_cache=False)
            if actual_sha256.lower() != expected_sha256.lower():
                errors.append(
                    "dependency/bootstrap SHA-256 mismatch: "
                    + str(candidate)
                    + " | expected="
                    + expected_sha256
                    + " | actual="
                    + actual_sha256
                )
        managed_fixed_files = _iter_managed_accelerator_files(fixed_root / "codex_accel_re6_v4")
        managed_fixed_files.extend(_iter_managed_accelerator_files(fixed_root / "python_build_tools_re6_v4"))
        for candidate in managed_fixed_files:
            relative_path = candidate.relative_to(fixed_root).as_posix()
            if relative_path not in ACCELERATOR_FIXED_SOURCE_SHA256:
                errors.append("unregistered fixed dependency file; bootstrap update required: " + str(candidate))

    dependency_roots = [DEPENDENCY_BASE_DIR]
    if include_release_copies is True:
        dependency_roots.extend(_iter_present_release_dependency_roots())
    for dependency_root in dependency_roots:
        fixed_root = dependency_root / "_vendor_fixed"
        for major, minor in SUPPORTED_PYTHON_MINORS:
            cp_tag = f"cp{major}{minor}"
            for import_name in LOCAL_ACCELERATOR_IMPORTS:
                package_dir = fixed_root / "codex_accel_re6_v4" / cp_tag / import_name
                init_path = package_dir / "__init__.py"
                checked_files.append(str(init_path))
                if init_path.exists() is not True:
                    warnings.append("partial ABI lane is missing fixed accelerator wrapper: " + str(init_path))
                extension_paths = list(package_dir.glob(f"*.{cp_tag}-win_amd64.pyd"))
                if not extension_paths:
                    warnings.append("partial ABI lane is missing fixed accelerator binary: " + str(package_dir))

            wheel_requirements = (
                (fixed_root / "orjson_re6_v4", "orjson"),
                (fixed_root / "pillow_re6_v4", "Pillow"),
            )
            for wheel_dir, distribution_name in wheel_requirements:
                wheel_paths = list(wheel_dir.glob(f"{distribution_name}-*-{cp_tag}-{cp_tag}-win_amd64.whl"))
                if not wheel_paths:
                    warnings.append(
                        "partial ABI lane is missing fixed wheel: "
                        + distribution_name
                        + " "
                        + cp_tag
                        + " under "
                        + str(wheel_dir)
                    )

            ufbx_build_root = fixed_root / "pyufbx_re6_v4" / "pyufbx-0.0.7" / "build"
            ufbx_package_dir = ufbx_build_root / f"lib.win-amd64-cpython-{major}{minor}" / "ufbx"
            if (ufbx_package_dir / "__init__.py").exists() is not True or not list(
                ufbx_package_dir.glob(f"_ufbx.{cp_tag}-win_amd64.pyd")
            ):
                warnings.append("partial ABI lane is missing patched ufbx prebuilt package: " + str(ufbx_package_dir))

        upgrade_root = dependency_root / "_vendor_upgrade"
        present_upgrade_paths: set[str] = set()
        for candidate in _iter_managed_accelerator_files(upgrade_root / "codex_accel_re6_v4"):
            relative_path = candidate.relative_to(upgrade_root).as_posix()
            present_upgrade_paths.add(relative_path)
            expected_sha256 = ACCELERATOR_APPROVED_UPGRADE_SHA256.get(relative_path)
            if expected_sha256 is None:
                errors.append("unapproved accelerator upgrade; bootstrap registration required: " + str(candidate))
                continue
            actual_sha256 = _sha256_file(candidate, use_cache=False)
            if actual_sha256.lower() != expected_sha256.lower():
                errors.append(
                    "accelerator upgrade SHA-256 mismatch: "
                    + str(candidate)
                    + " | expected="
                    + expected_sha256
                    + " | actual="
                    + actual_sha256
                )
        for relative_path in ACCELERATOR_APPROVED_UPGRADE_SHA256:
            if relative_path not in present_upgrade_paths:
                errors.append(
                    "approved accelerator upgrade missing from synchronized dependency root: "
                    + str(upgrade_root / relative_path)
                )

    consumer_contracts = (
        (BASE_DIR / "codex_fbx_probe.py", "REQUIRED_FBX_ACCELERATOR_CONTRACT_REVISION"),
        (BASE_DIR / "codex_python_export_bridge.py", "REQUIRED_UV_ACCELERATOR_CONTRACT_REVISION"),
    )
    expected_revisions = {
        int(requirements.get("contract_revision", 0) or 0)
        for requirements in LOCAL_ACCELERATOR_HEALTH_REQUIREMENTS.values()
    }
    if len(expected_revisions) != 1:
        errors.append("bootstrap accelerator contract revisions are not unified")
        expected_revision = 0
    else:
        expected_revision = next(iter(expected_revisions))
    for consumer_path, assignment_name in consumer_contracts:
        actual_revision = _read_integer_assignment(consumer_path, assignment_name)
        if actual_revision != expected_revision:
            errors.append(
                "consumer/bootstrap contract revision mismatch: "
                + str(consumer_path)
                + " | "
                + assignment_name
                + "="
                + str(actual_revision)
                + " | expected="
                + str(expected_revision)
            )

    report: dict[str, object] = {
        "ready": len(errors) == 0,
        "contract_revision": expected_revision,
        "include_release_copies": include_release_copies,
        "checked_file_count": len(checked_files),
        "checked_files": checked_files,
        "warnings": warnings,
        "errors": errors,
    }
    ACCELERATOR_SYNC_REPORT_CACHE[cache_key] = dict(report)
    return report


def _get_import_installed_version(import_name: str, checker: ImportChecker | None = None) -> str | None:
    effective_checker = checker or _default_import_checker
    if effective_checker(import_name) is not True:
        return None
    configure_vendor_paths()
    module = _get_healthy_loaded_import(import_name)
    if module is None:
        try:
            module = importlib.import_module(import_name)
        except Exception:
            return None
    version_value = getattr(module, "__version__", None)
    if version_value in (None, ""):
        version_value = getattr(module, "VERSION", None)
    if version_value not in (None, ""):
        return str(version_value)
    return None


def _read_dist_info_identity(dist_info_dir: Path) -> tuple[str, str]:
    metadata_path = dist_info_dir / "METADATA"
    name_text = ""
    version_text = ""
    if metadata_path.is_file():
        try:
            for line in metadata_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("Name:") and not name_text:
                    name_text = line.partition(":")[2].strip()
                elif line.startswith("Version:") and not version_text:
                    version_text = line.partition(":")[2].strip()
                if name_text and version_text:
                    break
        except OSError:
            pass
    if not version_text:
        match = re.match(r".+?-(\d[^/]*)\.dist-info$", dist_info_dir.name, re.IGNORECASE)
        if match is not None:
            version_text = match.group(1)
    return name_text, version_text


def get_vendor_metadata_contract_report(
    import_name: str,
    *,
    version_info: object | None = None,
) -> dict[str, object]:
    distribution_name = str(IMPORT_POLICY.get(import_name, {}).get("distribution_name", import_name) or import_name)
    normalized_distribution = re.sub(r"[-_.]+", "-", distribution_name).casefold()
    lane_reports: list[dict[str, object]] = []
    all_metadata_versions: set[str] = set()
    module_versions: set[str] = set()
    python_tag = _python_tag(version_info)
    for lane_dir in (
        VENDOR_PY_ROOT_DIR / python_tag,
        PACKAGED_VENDOR_PY_ROOT_DIR / python_tag,
    ):
        metadata_entries: list[dict[str, str]] = []
        if lane_dir.is_dir():
            for candidate in lane_dir.glob("*.dist-info"):
                metadata_name, metadata_version = _read_dist_info_identity(candidate)
                normalized_name = re.sub(r"[-_.]+", "-", metadata_name or candidate.name.split("-", 1)[0]).casefold()
                if normalized_name != normalized_distribution:
                    continue
                metadata_entries.append(
                    {
                        "path": str(candidate),
                        "name": metadata_name or distribution_name,
                        "version": metadata_version,
                    }
                )
                if metadata_version:
                    all_metadata_versions.add(metadata_version)
        module_init = lane_dir / import_name / "__init__.py"
        module_version = _extract_version_from_text(_read_candidate_metadata_text(module_init)) if module_init.is_file() else None
        if module_version:
            module_versions.add(module_version)
        lane_reports.append(
            {
                "lane": str(lane_dir),
                "module_init": str(module_init),
                "module_version": module_version,
                "dist_info": metadata_entries,
            }
        )
    multiple_metadata_versions = len(all_metadata_versions) > 1
    module_metadata_mismatch = bool(
        module_versions
        and all_metadata_versions
        and any(module_version not in all_metadata_versions for module_version in module_versions)
    )
    conflict = multiple_metadata_versions or module_metadata_mismatch
    authority = "healthy-module-contract" if import_name in LOCAL_ACCELERATOR_IMPORTS else "healthy-import-then-metadata"
    return {
        "import_name": import_name,
        "distribution_name": distribution_name,
        "python_tag": python_tag,
        "authority": authority,
        "module_versions": sorted(module_versions, key=_version_key),
        "dist_info_versions": sorted(all_metadata_versions, key=_version_key),
        "multiple_dist_info_versions": multiple_metadata_versions,
        "module_metadata_mismatch": module_metadata_mismatch,
        "metadata_conflict": conflict,
        "lanes": lane_reports,
        "warning": (
            "Conflicting dist-info is diagnostic only; a healthy module contract remains authoritative."
            if conflict
            else ""
        ),
    }


def _get_import_distribution_version(import_name: str) -> str | None:
    configure_vendor_paths()
    distribution_name = str(IMPORT_POLICY.get(import_name, {}).get("distribution_name", import_name) or import_name)
    metadata_contract = get_vendor_metadata_contract_report(import_name)
    if metadata_contract.get("metadata_conflict") is True:
        return None
    try:
        return str(importlib.metadata.version(distribution_name))
    except Exception:
        return None


def _should_upgrade_import(
    import_name: str,
    installed_version: str | None,
    local_version: str | None,
    *,
    blocked_current_version: bool = False,
    prefer_local_floor: bool = False,
) -> tuple[bool, str]:
    policy = IMPORT_POLICY.get(import_name, {})
    if policy.get("allow_upgrade") is not True:
        return (False, "locked-patched")
    if blocked_current_version is True:
        if local_version in (None, ""):
            return (False, "blocked-no-local-floor")
        return (True, "repaired-blocked-version")
    if local_version in (None, ""):
        return (False, "no-local-version")
    if installed_version in (None, ""):
        return (True, "upgrade-to-local-floor")
    if prefer_local_floor is True and _compare_version_text(installed_version, local_version) != 0:
        return (True, "forced-local-floor")
    comparison = _compare_version_text(installed_version, local_version)
    if comparison < 0:
        return (True, "upgraded")
    if comparison > 0:
        return (False, "kept-newer-installed")
    return (False, "already-approved")


def _active_vendor_lane() -> Path:
    if _VENDOR_LANE_CONTEXT is not None:
        return _VENDOR_LANE_CONTEXT
    return VENDOR_PY_DIR


def _include_packaged_vendor() -> bool:
    if _VENDOR_INCLUDE_PACKAGED_CONTEXT is not None:
        return _VENDOR_INCLUDE_PACKAGED_CONTEXT
    return True


def _vendor_path_candidates(
    writable_lane: Path | None = None,
    *,
    include_packaged: bool | None = None,
) -> tuple[Path, ...]:
    lane = Path(writable_lane) if writable_lane is not None else _active_vendor_lane()
    packaged_enabled = _include_packaged_vendor() if include_packaged is None else bool(include_packaged)
    candidates: list[Path] = [lane]
    if packaged_enabled:
        candidates.append(PACKAGED_VENDOR_PY_DIR)
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = os.path.normcase(os.path.abspath(str(candidate)))
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(candidate)
    return tuple(unique)


def configure_vendor_paths(
    writable_lane: str | Path | None = None,
    *,
    include_packaged: bool | None = None,
) -> tuple[Path, ...]:
    """Register existing vendor lanes without creating or modifying anything."""
    global _CONFIGURED_VENDOR_PATHS
    candidates = _vendor_path_candidates(
        Path(writable_lane) if writable_lane is not None else None,
        include_packaged=include_packaged,
    )
    remove_keys = {
        os.path.normcase(os.path.abspath(path_text))
        for path_text in _CONFIGURED_VENDOR_PATHS
    }
    remove_keys.update(
        os.path.normcase(os.path.abspath(str(path_value)))
        for path_value in (
            VENDOR_PY_DIR,
            VENDOR_PY_ROOT_DIR,
            PACKAGED_VENDOR_PY_DIR,
            PACKAGED_VENDOR_PY_ROOT_DIR,
        )
    )
    sys.path[:] = [
        path_text
        for path_text in sys.path
        if os.path.normcase(os.path.abspath(str(path_text or os.curdir))) not in remove_keys
    ]
    existing_paths = [path_value for path_value in candidates if path_value.is_dir()]
    insert_index = 1 if len(sys.path) >= 1 else 0
    for path_value in reversed(existing_paths):
        sys.path.insert(insert_index, str(path_value))
    _CONFIGURED_VENDOR_PATHS = [str(path_value) for path_value in existing_paths]
    return tuple(existing_paths)


@contextmanager
def _vendor_path_context(writable_lane: Path, *, include_packaged: bool):
    global _VENDOR_LANE_CONTEXT, _VENDOR_INCLUDE_PACKAGED_CONTEXT
    previous_lane = _VENDOR_LANE_CONTEXT
    previous_include_packaged = _VENDOR_INCLUDE_PACKAGED_CONTEXT
    _VENDOR_LANE_CONTEXT = Path(writable_lane)
    _VENDOR_INCLUDE_PACKAGED_CONTEXT = bool(include_packaged)
    configure_vendor_paths()
    try:
        yield
    finally:
        _VENDOR_LANE_CONTEXT = previous_lane
        _VENDOR_INCLUDE_PACKAGED_CONTEXT = previous_include_packaged
        configure_vendor_paths()


def ensure_vendor_path() -> Path:
    """Create the active writable ABI lane; never call this during import."""
    vendor_dir = _active_vendor_lane()
    vendor_dir.mkdir(parents=True, exist_ok=True)
    configure_vendor_paths()
    return vendor_dir


def _load_runtime_state() -> dict[str, object]:
    configure_vendor_paths()
    if not RUNTIME_STATE_PATH.exists():
        return {"blocked_versions": {}, "blocked_artifacts": {}, "upgrade_checks": {}}
    try:
        payload = json.loads(RUNTIME_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"blocked_versions": {}, "blocked_artifacts": {}, "upgrade_checks": {}}
    if not isinstance(payload, dict):
        return {"blocked_versions": {}, "blocked_artifacts": {}, "upgrade_checks": {}}
    blocked_versions = payload.get("blocked_versions")
    if not isinstance(blocked_versions, dict):
        payload["blocked_versions"] = {}
    if not isinstance(payload.get("blocked_artifacts"), dict):
        payload["blocked_artifacts"] = {}
    if not isinstance(payload.get("upgrade_checks"), dict):
        payload["upgrade_checks"] = {}
    return payload


def _save_runtime_state(payload: dict[str, object]) -> None:
    safe_payload = dict(payload)
    blocked_versions = safe_payload.get("blocked_versions")
    if not isinstance(blocked_versions, dict):
        safe_payload["blocked_versions"] = {}
    if not isinstance(safe_payload.get("blocked_artifacts"), dict):
        safe_payload["blocked_artifacts"] = {}
    if not isinstance(safe_payload.get("upgrade_checks"), dict):
        safe_payload["upgrade_checks"] = {}
    _atomic_write_runtime_bytes(
        RUNTIME_STATE_PATH,
        json.dumps(safe_payload, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def _update_runtime_state(
    update: Callable[[dict[str, object]], Any],
    *,
    lock_timeout_seconds: float = 10.0,
) -> Any:
    with _abi_state_lock(timeout_seconds=lock_timeout_seconds):
        payload = _load_runtime_state()
        result = update(payload)
        _save_runtime_state(payload)
        return result


def _get_upgrade_check_state(import_name: str) -> dict[str, object]:
    upgrade_checks = _load_runtime_state().get("upgrade_checks", {})
    if not isinstance(upgrade_checks, dict):
        return {}
    entry = upgrade_checks.get(import_name, {})
    return dict(entry) if isinstance(entry, dict) else {}


def _upgrade_check_is_due(import_name: str, *, now_epoch: float | None = None) -> bool:
    entry = _get_upgrade_check_state(import_name)
    if not entry:
        return True
    now_value = time.time() if now_epoch is None else float(now_epoch)
    try:
        next_check_epoch = float(entry.get("next_check_epoch", 0.0) or 0.0)
    except (TypeError, ValueError):
        next_check_epoch = 0.0
    return now_value >= next_check_epoch


def _record_upgrade_check_state(
    import_name: str,
    *,
    status: str,
    report: dict[str, object] | None = None,
    error: str = "",
    ttl_seconds: float = DEFAULT_UPGRADE_CHECK_TTL_SECONDS,
) -> dict[str, object]:
    def update(payload: dict[str, object]) -> dict[str, object]:
        upgrade_checks = payload.setdefault("upgrade_checks", {})
        if not isinstance(upgrade_checks, dict):
            upgrade_checks = {}
            payload["upgrade_checks"] = upgrade_checks
        now_epoch = time.time()
        previous = upgrade_checks.get(import_name, {})
        entry = {
            "import_name": import_name,
            "status": str(status),
            "last_check_epoch": now_epoch,
            "next_check_epoch": now_epoch + max(60.0, float(ttl_seconds)),
            "python_tag": _python_tag(),
            "report": dict(report or {}),
            "error": str(error or "")[-8000:],
        }
        if isinstance(previous, dict):
            for field_name in (
                "scheduled_token",
                "scheduled_epoch",
                "lease_expires_epoch",
                "worker_pid",
                "worker_started_epoch",
                "worker_completed_epoch",
                "result_path",
                "log_path",
                "current_version",
            ):
                if field_name in previous:
                    entry[field_name] = previous[field_name]
        upgrade_checks[import_name] = entry
        return dict(entry)

    return _update_runtime_state(update)


def _cleanup_background_upgrade_artifacts(
    *,
    keep_paths: tuple[Path, ...] = (),
    root_dir: Path | None = None,
    now_epoch: float | None = None,
) -> dict[str, object]:
    root = Path(root_dir) if root_dir is not None else RUNTIME_BACKGROUND_UPGRADE_DIR
    if root.is_dir() is not True:
        return {"root": str(root), "removed": [], "errors": [], "remaining": 0}
    now_value = time.time() if now_epoch is None else float(now_epoch)
    keep_keys = {
        os.path.normcase(os.path.abspath(str(path_value)))
        for path_value in keep_paths
    }
    artifacts: list[tuple[Path, float]] = []
    errors: list[str] = []
    for candidate in root.iterdir():
        if (
            candidate.is_file() is not True
            or candidate.is_symlink()
            or not candidate.name.endswith((".result.json", ".log"))
        ):
            continue
        try:
            artifacts.append((candidate, candidate.stat().st_mtime))
        except OSError as exc:
            errors.append(str(candidate) + ": " + str(exc))

    removed: list[str] = []

    def remove_candidate(candidate: Path) -> bool:
        candidate_key = os.path.normcase(os.path.abspath(str(candidate)))
        if candidate_key in keep_keys:
            return False
        try:
            candidate.unlink(missing_ok=True)
            removed.append(str(candidate))
            return True
        except OSError as exc:
            errors.append(str(candidate) + ": " + str(exc))
            return False

    survivors: list[tuple[Path, float]] = []
    for candidate, modified_epoch in artifacts:
        if now_value - modified_epoch > BACKGROUND_UPGRADE_ARTIFACT_RETENTION_SECONDS:
            if remove_candidate(candidate):
                continue
        survivors.append((candidate, modified_epoch))

    survivors.sort(key=lambda item: item[1], reverse=True)
    for candidate, _modified_epoch in survivors[BACKGROUND_UPGRADE_ARTIFACT_MAX_FILES:]:
        remove_candidate(candidate)

    remaining = 0
    try:
        remaining = sum(
            1
            for candidate in root.iterdir()
            if candidate.is_file()
            and candidate.name.endswith((".result.json", ".log"))
        )
    except OSError as exc:
        errors.append(str(exc))
    return {
        "root": str(root),
        "removed": removed,
        "errors": errors,
        "remaining": remaining,
    }


def _background_upgrade_paths(import_name: str, token: str) -> tuple[Path, Path]:
    safe_import_name = re.sub(r"[^a-z0-9_.-]+", "-", import_name.casefold()).strip("-") or "dependency"
    stem = safe_import_name + "-" + _python_tag() + "-" + token
    return (
        RUNTIME_BACKGROUND_UPGRADE_DIR / (stem + ".result.json"),
        RUNTIME_BACKGROUND_UPGRADE_DIR / (stem + ".log"),
    )


def _reserve_background_upgrade(
    import_name: str,
    *,
    current_version: str | None,
) -> dict[str, object]:
    _cleanup_background_upgrade_artifacts()
    token = uuid.uuid4().hex
    result_path, log_path = _background_upgrade_paths(import_name, token)

    def update(payload: dict[str, object]) -> dict[str, object]:
        upgrade_checks = payload.setdefault("upgrade_checks", {})
        if not isinstance(upgrade_checks, dict):
            upgrade_checks = {}
            payload["upgrade_checks"] = upgrade_checks
        now_epoch = time.time()
        existing = upgrade_checks.get(import_name, {})
        if isinstance(existing, dict):
            try:
                next_check_epoch = float(existing.get("next_check_epoch", 0.0) or 0.0)
            except (TypeError, ValueError):
                next_check_epoch = 0.0
            try:
                lease_expires_epoch = float(existing.get("lease_expires_epoch", 0.0) or 0.0)
            except (TypeError, ValueError):
                lease_expires_epoch = 0.0
            existing_status = str(existing.get("status", "") or "")
            if existing_status in {"scheduled", "running"} and lease_expires_epoch > now_epoch:
                return {"reserved": False, "status": "already-scheduled", "state": dict(existing)}
            if next_check_epoch > now_epoch:
                return {"reserved": False, "status": "ttl-cache", "state": dict(existing)}
        entry = {
            "import_name": import_name,
            "status": "scheduled",
            "last_check_epoch": now_epoch,
            "next_check_epoch": now_epoch + BACKGROUND_UPGRADE_LEASE_SECONDS,
            "python_tag": _python_tag(),
            "report": {"current_version": current_version},
            "error": "",
            "scheduled_token": token,
            "scheduled_epoch": now_epoch,
            "lease_expires_epoch": now_epoch + BACKGROUND_UPGRADE_LEASE_SECONDS,
            "worker_pid": None,
            "result_path": str(result_path),
            "log_path": str(log_path),
            "current_version": current_version,
        }
        upgrade_checks[import_name] = entry
        return {"reserved": True, "status": "scheduled", "state": dict(entry)}

    return _update_runtime_state(
        update,
        lock_timeout_seconds=BACKGROUND_UPGRADE_STATE_LOCK_TIMEOUT_SECONDS,
    )


def _update_background_upgrade_worker_pid(import_name: str, token: str, pid: int) -> None:
    def update(payload: dict[str, object]) -> None:
        upgrade_checks = payload.get("upgrade_checks", {})
        entry = upgrade_checks.get(import_name, {}) if isinstance(upgrade_checks, dict) else {}
        if (
            isinstance(entry, dict)
            and entry.get("scheduled_token") == token
            and entry.get("status") == "scheduled"
        ):
            entry["worker_pid"] = int(pid)

    try:
        _update_runtime_state(
            update,
            lock_timeout_seconds=BACKGROUND_UPGRADE_STATE_LOCK_TIMEOUT_SECONDS,
        )
    except RuntimeInstallLockTimeout:
        pass


def _mark_background_upgrade_launch_failed(import_name: str, token: str, error_text: str) -> dict[str, object]:
    def update(payload: dict[str, object]) -> dict[str, object]:
        upgrade_checks = payload.setdefault("upgrade_checks", {})
        entry = upgrade_checks.get(import_name, {}) if isinstance(upgrade_checks, dict) else {}
        if not isinstance(entry, dict) or entry.get("scheduled_token") != token:
            return dict(entry) if isinstance(entry, dict) else {}
        now_epoch = time.time()
        entry.update(
            {
                "status": "schedule-failed",
                "last_check_epoch": now_epoch,
                "next_check_epoch": now_epoch + FAILED_UPGRADE_CHECK_TTL_SECONDS,
                "lease_expires_epoch": now_epoch,
                "error": str(error_text or "")[-8000:],
            }
        )
        return dict(entry)

    try:
        return _update_runtime_state(
            update,
            lock_timeout_seconds=BACKGROUND_UPGRADE_STATE_LOCK_TIMEOUT_SECONDS,
        )
    except RuntimeInstallLockTimeout as exc:
        return {
            "status": "schedule-failed-state-lock",
            "error": str(exc),
            "lock": dict(exc.report),
        }


def _claim_background_upgrade_worker(import_name: str, token: str) -> dict[str, object]:
    def update(payload: dict[str, object]) -> dict[str, object]:
        upgrade_checks = payload.get("upgrade_checks", {})
        entry = upgrade_checks.get(import_name, {}) if isinstance(upgrade_checks, dict) else {}
        now_epoch = time.time()
        if not isinstance(entry, dict) or entry.get("scheduled_token") != token:
            return {"claimed": False, "status": "token-mismatch", "state": dict(entry) if isinstance(entry, dict) else {}}
        if str(entry.get("status", "") or "") != "scheduled":
            return {"claimed": False, "status": "not-scheduled", "state": dict(entry)}
        try:
            lease_expires_epoch = float(entry.get("lease_expires_epoch", 0.0) or 0.0)
        except (TypeError, ValueError):
            lease_expires_epoch = 0.0
        if lease_expires_epoch < now_epoch:
            entry["status"] = "schedule-stale"
            entry["error"] = "Background upgrade worker started after its scheduling lease expired."
            return {"claimed": False, "status": "schedule-stale", "state": dict(entry)}
        entry.update(
            {
                "status": "running",
                "worker_pid": os.getpid(),
                "worker_started_epoch": now_epoch,
                "lease_expires_epoch": now_epoch + BACKGROUND_UPGRADE_LEASE_SECONDS,
                "next_check_epoch": now_epoch + BACKGROUND_UPGRADE_LEASE_SECONDS,
            }
        )
        return {"claimed": True, "status": "running", "state": dict(entry)}

    return _update_runtime_state(update)


def _complete_background_upgrade_worker(
    import_name: str,
    token: str,
    worker_result: dict[str, object],
) -> dict[str, object]:
    def update(payload: dict[str, object]) -> dict[str, object]:
        upgrade_checks = payload.setdefault("upgrade_checks", {})
        entry = upgrade_checks.get(import_name, {}) if isinstance(upgrade_checks, dict) else {}
        if not isinstance(entry, dict) or entry.get("scheduled_token") != token:
            return dict(entry) if isinstance(entry, dict) else {}
        now_epoch = time.time()
        if str(entry.get("status", "") or "") in {"scheduled", "running"}:
            entry.update(
                {
                    "status": str(worker_result.get("status", "worker-complete") or "worker-complete"),
                    "last_check_epoch": now_epoch,
                    "next_check_epoch": now_epoch + DEFAULT_UPGRADE_CHECK_TTL_SECONDS,
                    "report": dict(worker_result),
                    "error": str(worker_result.get("error", "") or "")[-8000:],
                }
            )
        entry["worker_status"] = "completed"
        entry["worker_completed_epoch"] = now_epoch
        entry["lease_expires_epoch"] = now_epoch
        return dict(entry)

    return _update_runtime_state(update)


def mark_import_version_blocked(import_name: str, version_text: str | None, reason_text: str = "") -> bool:
    normalized_version = str(version_text or "").strip()
    if normalized_version == "":
        return False
    def update(payload: dict[str, object]) -> None:
        blocked_versions = payload.setdefault("blocked_versions", {})
        if not isinstance(blocked_versions, dict):
            blocked_versions = {}
            payload["blocked_versions"] = blocked_versions
        import_blocked = blocked_versions.setdefault(import_name, {})
        if not isinstance(import_blocked, dict):
            import_blocked = {}
            blocked_versions[import_name] = import_blocked
        import_blocked[normalized_version] = {
            "reason": str(reason_text or "").strip(),
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        }

    _update_runtime_state(update)
    return True


def _is_import_version_blocked(import_name: str, version_text: str | None) -> bool:
    normalized_version = str(version_text or "").strip()
    if normalized_version == "":
        return False
    payload = _load_runtime_state()
    blocked_versions = payload.get("blocked_versions", {})
    if not isinstance(blocked_versions, dict):
        return False
    import_blocked = blocked_versions.get(import_name, {})
    if not isinstance(import_blocked, dict):
        return False
    return normalized_version in import_blocked


def _set_import_error_text(import_name: str, message_text: str = "") -> None:
    normalized = str(message_text or "").strip()
    if normalized == "":
        IMPORT_HEALTH_ERRORS.pop(import_name, None)
        return
    IMPORT_HEALTH_ERRORS[import_name] = normalized


def get_last_import_error_text(import_name: str) -> str:
    return str(IMPORT_HEALTH_ERRORS.get(import_name, "") or "")


def clear_runtime_advisories() -> None:
    RUNTIME_ADVISORIES.clear()


def _build_optional_runtime_solution_lines(import_name: str, report: dict[str, object]) -> list[str]:
    runtime_report = report.get("python_runtime", {})
    current_python = "unknown"
    if isinstance(runtime_report, dict):
        current_python = str(runtime_report.get("current_python", "unknown") or "unknown")
    solutions: list[str] = []
    chinese_ui = runtime_ui_is_chinese()
    if import_name in LOCAL_ACCELERATOR_IMPORTS:
        if chinese_ui:
            solutions.append(
                "请把新版本地加速器放入 "
                + str(UPGRADE_ACCELERATOR_BUNDLE_DIR / _python_tag())
                + "，并在 "
                + str(LOCAL_ACCELERATOR_BUNDLE_DIR / _python_tag())
                + " 保留已验证的回滚版本。"
            )
            solutions.append("请安装 Visual Studio Build Tools，并启用“使用 C++ 的桌面开发”。")
            solutions.append(
                "如果本机正在使用较新的 Python "
                + current_python
                + "，请添加匹配的本地加速器，或者改回当前批准的 Python 3.14。"
            )
        else:
            solutions.append(
                "Place newer local accelerator builds under "
                + str(UPGRADE_ACCELERATOR_BUNDLE_DIR / _python_tag())
                + ", and keep the known-good rollback floor under "
                + str(LOCAL_ACCELERATOR_BUNDLE_DIR / _python_tag())
                + "."
            )
            solutions.append("Install Visual Studio Build Tools and enable Desktop development with C++.")
            solutions.append(
                "If this machine now uses a newer Python "
                + current_python
                + ", either add a matching local accelerator build or switch back to the approved Python 3.14 baseline."
            )
    elif import_name == "orjson":
        if chinese_ui:
            solutions.append(
                "请把新版 orjson wheel 放入 "
                + str(UPGRADE_ORJSON_BUNDLE_DIR)
                + "，并在 "
                + str(LOCAL_ORJSON_BUNDLE_DIR)
                + " 保留回滚版本。"
            )
            solutions.append(
                "如果 Python "
                + current_python
                + " 高于本地已批准 wheel 的支持范围，可以继续使用无 orjson 的导出路径，或者改回当前批准的 Python 3.14。"
            )
        else:
            solutions.append(
                "Place newer local orjson wheels under "
                + str(UPGRADE_ORJSON_BUNDLE_DIR)
                + ", and keep the rollback floor under "
                + str(LOCAL_ORJSON_BUNDLE_DIR)
                + "."
            )
            solutions.append(
                "If Python "
                + current_python
                + " is newer than the approved local wheels, keep exporting without orjson or switch back to the approved Python 3.14 baseline."
            )
    elif import_name == "PIL":
        if chinese_ui:
            solutions.append(
                "请把新版 Pillow wheel 放入 "
                + str(UPGRADE_PILLOW_BUNDLE_DIR)
                + "，并在 "
                + str(LOCAL_PILLOW_BUNDLE_DIR)
                + " 保留回滚版本。"
            )
        else:
            solutions.append(
                "Place newer local Pillow wheels under "
                + str(UPGRADE_PILLOW_BUNDLE_DIR)
                + ", and keep the rollback floor under "
                + str(LOCAL_PILLOW_BUNDLE_DIR)
                + "."
            )
    return solutions


def _build_optional_runtime_advisory(import_name: str, report: dict[str, object], *, error_text: str = "") -> dict[str, object]:
    runtime_report = report.get("python_runtime", {})
    current_python = "unknown"
    if isinstance(runtime_report, dict):
        current_python = str(runtime_report.get("current_python", "unknown") or "unknown")
    status_text = str(report.get("status", "") or report.get("upgrade_action", "") or "warning")
    summary_lines: list[str] = []
    chinese_ui = runtime_ui_is_chinese()
    if import_name in LOCAL_ACCELERATOR_IMPORTS:
        if status_text == "no-local-bundle":
            summary_lines.append(
                import_name + " 没有适用于 Python " + current_python + " 的本地包；导出已改用较慢的内置 Python 路径。"
                if chinese_ui
                else import_name + " has no local bundle for Python " + current_python + "; export already continued on the built-in slower Python path."
            )
        elif status_text == "install-failed":
            summary_lines.append(
                import_name + " 本地编译失败；导出已改用较慢的内置 Python 路径。"
                if chinese_ui
                else import_name + " local build failed; export already continued on the built-in slower Python path."
            )
        elif status_text == "repaired-blocked-version":
            summary_lines.append(
                import_name + " 的较新运行时副本已被阻止，并恢复到已批准的本地版本。"
                if chinese_ui
                else import_name + " restored its approved local build after a newer runtime copy was blocked."
            )
        else:
            summary_lines.append(
                import_name + " 不可用；导出已改用较慢的内置 Python 路径。"
                if chinese_ui
                else import_name + " is unavailable; export continued on the built-in slower Python path."
            )
    elif import_name == "orjson":
        if status_text == "repaired-blocked-version":
            summary_lines.append(
                "较新的 orjson 运行时副本已被阻止，并回滚到已批准的本地版本。"
                if chinese_ui
                else "orjson rolled back to the approved local version after a newer runtime copy was blocked."
            )
        else:
            summary_lines.append(
                "Python " + current_python + " 无法使用 orjson；JSON 快速路径加速已禁用。"
                if chinese_ui
                else "orjson is unavailable for Python " + current_python + "; JSON fast-path acceleration is disabled."
            )
    elif import_name == "PIL":
        summary_lines.append(
            "Python " + current_python + " 无法使用 Pillow；RTEX 占位图保存将使用回退路径。"
            if chinese_ui
            else "Pillow is unavailable for Python " + current_python + "; RTEX placeholder save will use the fallback path."
        )
    if error_text != "":
        summary_lines.append(("原因：" if chinese_ui else "Reason: ") + error_text)
    summary_lines.extend(_build_optional_runtime_solution_lines(import_name, report))
    return {
        "title": runtime_ui_text("Codex Python 运行时警告", "Codex Python Runtime Warning"),
        "import_name": import_name,
        "status": status_text,
        "summary_lines": summary_lines,
    }


def _append_runtime_advisory(advisory: dict[str, object] | None) -> None:
    if not isinstance(advisory, dict):
        return
    summary_lines = advisory.get("summary_lines")
    if not isinstance(summary_lines, list) or len(summary_lines) <= 0:
        return
    import_name = str(advisory.get("import_name", "") or "")
    status_text = str(advisory.get("status", "") or "")
    signature = import_name + "|" + status_text + "|" + " | ".join(str(line or "") for line in summary_lines)
    for existing in RUNTIME_ADVISORIES:
        existing_lines = existing.get("summary_lines")
        existing_signature = (
            str(existing.get("import_name", "") or "")
            + "|"
            + str(existing.get("status", "") or "")
            + "|"
            + (" | ".join(str(line or "") for line in existing_lines) if isinstance(existing_lines, list) else "")
        )
        if existing_signature == signature:
            return
    RUNTIME_ADVISORIES.append(advisory)


def get_runtime_advisories(*, clear: bool = False) -> list[dict[str, object]]:
    advisories = [dict(item) for item in RUNTIME_ADVISORIES if isinstance(item, dict)]
    if clear is True:
        RUNTIME_ADVISORIES.clear()
    return advisories


def _build_runtime_advisory_popup_payload(advisories: list[dict[str, object]]) -> tuple[str, str, str] | None:
    title_text = runtime_ui_text("Codex Python 运行时警告", "Codex Python Runtime Warning")
    summary_lines: list[str] = []
    seen_lines: set[str] = set()
    for advisory in advisories:
        if not isinstance(advisory, dict):
            continue
        title_text = str(advisory.get("title", title_text) or title_text)
        entry_lines = advisory.get("summary_lines")
        if not isinstance(entry_lines, list):
            continue
        for line in entry_lines:
            normalized_line = str(line or "").strip()
            if normalized_line == "" or normalized_line in seen_lines:
                continue
            seen_lines.add(normalized_line)
            summary_lines.append(normalized_line)
    if len(summary_lines) <= 0:
        return None
    intro_text = runtime_ui_text(
        "Python 已处理运行时依赖回退、版本回滚或本地编译问题。\r\n\r\n请查看以下详情和建议解决方案：",
        "Python handled a runtime dependency fallback, rollback, or local build problem."
        + "\r\n\r\n"
        + "Review the details and suggested fixes below:",
    )
    final_note = runtime_ui_text(
        "修复本地依赖包或编译环境后，请重新执行同一次导出。",
        "After fixing the local dependency bundle or build environment, run the same export again.",
    )
    popup_text = intro_text + "\r\n\r\n" + "\r\n".join(summary_lines) + "\r\n\r\n" + final_note
    signature = title_text + "|" + " | ".join(summary_lines)
    return (title_text, popup_text, signature)


def _default_runtime_advisory_popup(title_text: str, popup_text: str) -> bool:
    try:
        import tkinter as tk

        background = "#111820"
        panel = "#1B2530"
        border = "#2B3947"
        accent = "#F59E0B"
        foreground = "#F4F7FA"
        muted = "#C8D1DB"
        root = tk.Tk()
        root.withdraw()
        root.title(str(title_text))
        root.configure(background=background)
        root.resizable(False, False)
        root.overrideredirect(True)
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass

        outer = tk.Frame(
            root,
            background=background,
            highlightbackground=border,
            highlightcolor=border,
            highlightthickness=1,
            padx=22,
            pady=18,
        )
        outer.pack(fill="both", expand=True)
        tk.Frame(outer, background=accent, height=4).pack(fill="x", side="top")

        header = tk.Frame(outer, background=background)
        header.pack(fill="x", pady=(14, 10))
        tk.Label(
            header,
            text=str(title_text),
            background=background,
            foreground=foreground,
            font=("Microsoft YaHei", 13, "bold"),
            anchor="w",
        ).pack(fill="x")
        tk.Label(
            header,
            text=runtime_ui_text(
                "Bootstrap 需要先解决运行时问题。",
                "Bootstrap needs this runtime issue resolved before it can continue.",
            ),
            background=background,
            foreground=accent,
            font=("Microsoft YaHei", 10),
            anchor="w",
        ).pack(fill="x", pady=(4, 0))

        content = tk.Frame(outer, background=panel)
        content.pack(fill="both", expand=True)
        scrollbar = tk.Scrollbar(
            content,
            orient="vertical",
            background=border,
            troughcolor=panel,
            activebackground=accent,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
        )
        scrollbar.pack(side="right", fill="y")
        details = tk.Text(
            content,
            width=76,
            height=14,
            wrap="word",
            background=panel,
            foreground=muted,
            insertbackground=foreground,
            selectbackground="#3B4D60",
            selectforeground=foreground,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            padx=14,
            pady=12,
            font=("Microsoft YaHei", 10),
            yscrollcommand=scrollbar.set,
        )
        details.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=details.yview)
        details.insert("1.0", str(popup_text))
        details.configure(state="disabled")

        footer = tk.Frame(outer, background=background)
        footer.pack(fill="x", pady=(14, 0))
        tk.Label(
            footer,
            text=runtime_ui_text("请处理后重新启动工具。", "Fix the issue, then restart the tool."),
            background=background,
            foreground=muted,
            font=("Microsoft YaHei", 9),
            anchor="w",
        ).pack(side="left")
        close_button = tk.Button(
            footer,
            text=runtime_ui_text("确定", "OK"),
            command=root.destroy,
            background=accent,
            activebackground="#FFB84D",
            foreground="#111820",
            activeforeground="#111820",
            relief="flat",
            borderwidth=0,
            padx=24,
            pady=7,
            cursor="hand2",
            font=("Microsoft YaHei", 10, "bold"),
        )
        close_button.pack(side="right")
        root.bind("<Return>", lambda _event: root.destroy())
        root.bind("<Escape>", lambda _event: root.destroy())
        root.update_idletasks()
        width = max(640, int(root.winfo_reqwidth()))
        height = max(330, int(root.winfo_reqheight()))
        x = max(0, (int(root.winfo_screenwidth()) - width) // 2)
        y = max(0, (int(root.winfo_screenheight()) - height) // 2)
        root.geometry(f"{width}x{height}+{x}+{y}")
        root.deiconify()
        root.lift()
        root.focus_force()
        close_button.focus_set()
        root.mainloop()
        return True
    except Exception:
        try:
            sys.stderr.write(str(title_text) + "\n" + str(popup_text) + "\n")
        except Exception:
            pass
        return False


def show_runtime_advisories_gui(
    advisories: list[dict[str, object]] | None = None,
    *,
    clear: bool = False,
    popup: RuntimePopup | None = None,
) -> bool:
    if advisories is None:
        advisories = get_runtime_advisories(clear=clear)
    payload = _build_runtime_advisory_popup_payload(advisories)
    if payload is None:
        return False
    title_text, popup_text, signature = payload
    if signature in RUNTIME_ADVISORY_POPUP_SIGNATURES:
        return False
    popup_func = popup or _default_runtime_advisory_popup
    popup_func(title_text, popup_text)
    RUNTIME_ADVISORY_POPUP_SIGNATURES.add(signature)
    return True


def _prebuilt_ufbx_package_dir(version_info: object | None = None) -> Path | None:
    major, minor = _coerce_version_info(version_info)
    py_tag = f"lib.win-amd64-cpython-{major}{minor}"
    package_dir = PATCHED_UFBX_SOURCE_DIR / "build" / py_tag / "ufbx"
    expected_binary_tag = f"cp{major}{minor}"
    if (
        (package_dir / "__init__.py").is_file()
        and any(package_dir.glob(f"_ufbx.{expected_binary_tag}-win_amd64.pyd"))
    ):
        return package_dir
    return None


def _install_prebuilt_ufbx_bundle(*, target_dir: Path | None = None) -> bool:
    package_dir = _prebuilt_ufbx_package_dir()
    if package_dir is None:
        return False
    return _install_prebuilt_package_dir("ufbx", package_dir, target_dir=target_dir)


def _install_prebuilt_package_dir(
    import_name: str,
    package_dir: Path,
    *,
    target_dir: Path | None = None,
) -> bool:
    if _is_local_package_dir(package_dir, allow_prebuilt_only=True) is not True:
        return False
    vendor_dir = ensure_vendor_path() if target_dir is None else Path(target_dir)
    vendor_dir.mkdir(parents=True, exist_ok=True)
    target_package_dir = vendor_dir / import_name
    if target_package_dir.exists():
        shutil.rmtree(target_package_dir, ignore_errors=True)
    shutil.copytree(package_dir, target_package_dir)
    return True


def _find_import_name_for_candidate(package_name: str) -> str | None:
    refresh_package_candidates()
    normalized = str(package_name)
    for import_name, candidates in PACKAGE_BY_IMPORT_NAME.items():
        if normalized in candidates:
            return import_name
    return None


def _clear_import_tree(import_name: str) -> None:
    prefix = import_name + "."
    remove_names = [name for name in list(sys.modules.keys()) if name == import_name or name.startswith(prefix)]
    for name in remove_names:
        sys.modules.pop(name, None)


def _get_healthy_loaded_import(import_name: str) -> object | None:
    """Reuse native modules instead of trying to unload and initialize them again."""
    module = sys.modules.get(import_name)
    if module is None:
        return None
    if _is_import_healthy(import_name, module) is not True:
        return None
    _set_import_error_text(import_name, "")
    return module


def _is_expected_ufbx_build(module: object) -> bool:
    try:
        skin_cluster = getattr(module, "SkinCluster", None)
        mesh_type = getattr(module, "Mesh", None)
        return (
            getattr(module, "__codex_patch__", "") == PATCHED_UFBX_MARKER
            and getattr(module, "__version__", "") == PATCHED_UFBX_VERSION
            and callable(getattr(module, "load_file", None))
            and callable(getattr(module, "load_memory", None))
            and skin_cluster is not None
            and mesh_type is not None
            and hasattr(skin_cluster, "bone_name")
            and hasattr(skin_cluster, "vertices")
            and hasattr(skin_cluster, "weights")
            and hasattr(mesh_type, "num_uv_sets")
            and hasattr(mesh_type, "uv_set_names")
            and callable(getattr(mesh_type, "get_vertex_uvs_for_set", None))
            and callable(getattr(mesh_type, "get_vertex_uv_indices_for_set", None))
        )
    except Exception:
        return False


def _patched_ufbx_contract_differences(
    expected: Any,
    actual: Any,
    *,
    path: str = "$",
    limit: int = 24,
) -> list[dict[str, object]]:
    differences: list[dict[str, object]] = []

    def compare(left: Any, right: Any, location: str) -> None:
        if len(differences) >= limit:
            return
        if type(left) is not type(right):
            differences.append(
                {
                    "path": location,
                    "expected_type": type(left).__name__,
                    "actual_type": type(right).__name__,
                    "expected": repr(left)[:500],
                    "actual": repr(right)[:500],
                }
            )
            return
        if isinstance(left, dict):
            left_keys = set(left)
            right_keys = set(right)
            for key in sorted(left_keys - right_keys, key=str):
                differences.append({"path": f"{location}.{key}", "error": "missing-from-candidate"})
                if len(differences) >= limit:
                    return
            for key in sorted(right_keys - left_keys, key=str):
                differences.append({"path": f"{location}.{key}", "error": "unexpected-in-candidate"})
                if len(differences) >= limit:
                    return
            for key in sorted(left_keys & right_keys, key=str):
                compare(left[key], right[key], f"{location}.{key}")
                if len(differences) >= limit:
                    return
            return
        if isinstance(left, list):
            if len(left) != len(right):
                differences.append(
                    {
                        "path": location,
                        "error": "length-mismatch",
                        "expected": len(left),
                        "actual": len(right),
                    }
                )
                return
            for index, (left_item, right_item) in enumerate(zip(left, right)):
                compare(left_item, right_item, f"{location}[{index}]")
                if len(differences) >= limit:
                    return
            return
        if left != right:
            differences.append(
                {
                    "path": location,
                    "expected": repr(left)[:500],
                    "actual": repr(right)[:500],
                }
            )

    compare(expected, actual, path)
    return differences


def _load_fbx_probe_for_ufbx_contract() -> object:
    loaded = sys.modules.get("codex_fbx_probe")
    if loaded is not None:
        return loaded
    source_path = BASE_DIR / "codex_fbx_probe.py"
    if source_path.is_file() is not True:
        raise ModuleNotFoundError(f"FBX Probe source is missing: {source_path}")
    bootstrap_alias_added = False
    if "codex_python_runtime_bootstrap" not in sys.modules:
        sys.modules["codex_python_runtime_bootstrap"] = sys.modules[__name__]
        bootstrap_alias_added = True
    spec = importlib.util.spec_from_file_location("codex_fbx_probe", source_path)
    if spec is None or spec.loader is None:
        if bootstrap_alias_added:
            sys.modules.pop("codex_python_runtime_bootstrap", None)
        raise ModuleNotFoundError(f"Unable to create an import spec for FBX Probe: {source_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["codex_fbx_probe"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop("codex_fbx_probe", None)
        if bootstrap_alias_added:
            sys.modules.pop("codex_python_runtime_bootstrap", None)
        raise
    return module


def get_patched_ufbx_behavior_contract_report() -> dict[str, object]:
    source_fingerprint = _patched_ufbx_source_fingerprint()
    report: dict[str, object] = {
        "schema": "pc-rehd-code-x-patched-ufbx-gate-v1",
        "ready": False,
        "source_dir": str(PATCHED_UFBX_SOURCE_DIR),
        "source_fingerprint": source_fingerprint,
        "approved_source_fingerprint": PATCHED_UFBX_APPROVED_SOURCE_FINGERPRINT,
        "fixture": str(PATCHED_UFBX_CONTRACT_FBX_PATH),
        "baseline": str(PATCHED_UFBX_CONTRACT_BASELINE_PATH),
    }
    if source_fingerprint != PATCHED_UFBX_APPROVED_SOURCE_FINGERPRINT:
        report["error"] = "Patched ufbx source fingerprint differs from the approved source tree."
        return report
    if PATCHED_UFBX_CONTRACT_FBX_PATH.is_file() is not True:
        report["error"] = "Patched ufbx FBX behavior fixture is missing."
        return report
    if PATCHED_UFBX_CONTRACT_BASELINE_PATH.is_file() is not True:
        report["error"] = "Patched ufbx behavior baseline is missing."
        return report
    fixture_sha256 = _sha256_file(PATCHED_UFBX_CONTRACT_FBX_PATH, use_cache=False)
    baseline_sha256 = _sha256_file(PATCHED_UFBX_CONTRACT_BASELINE_PATH, use_cache=False)
    report["fixture_sha256"] = fixture_sha256
    report["baseline_sha256"] = baseline_sha256
    if fixture_sha256.casefold() != PATCHED_UFBX_CONTRACT_FBX_SHA256.casefold():
        report["error"] = "Patched ufbx FBX behavior fixture hash is not approved."
        return report
    if baseline_sha256.casefold() != PATCHED_UFBX_CONTRACT_BASELINE_SHA256.casefold():
        report["error"] = "Patched ufbx behavior baseline hash is not approved."
        return report
    try:
        expected = runtime_read_json_file(PATCHED_UFBX_CONTRACT_BASELINE_PATH)
        if not isinstance(expected, dict):
            raise TypeError("Patched ufbx behavior baseline must be a JSON object.")
        probe_module = _load_fbx_probe_for_ufbx_contract()
        builder = getattr(probe_module, "build_ufbx_behavior_contract", None)
        if not callable(builder):
            raise AttributeError("codex_fbx_probe.build_ufbx_behavior_contract is missing.")
        actual = builder(PATCHED_UFBX_CONTRACT_FBX_PATH)
        if not isinstance(actual, dict):
            raise TypeError("Patched ufbx behavior probe did not return a contract object.")
        differences = _patched_ufbx_contract_differences(expected, actual)
        report.update(
            {
                "ready": not differences,
                "expected_contract_sha256": expected.get("contract_sha256"),
                "actual_contract_sha256": actual.get("contract_sha256"),
                "stats": actual.get("stats", {}),
                "differences": differences,
                "ufbx": actual.get("ufbx", {}),
            }
        )
        if differences:
            report["error"] = "Candidate patched ufbx changed the approved FBX parse contract."
        return report
    except Exception as exc:
        report.update(
            {
                "error_type": type(exc).__name__,
                "error": str(exc),
                "traceback": traceback.format_exc()[-8000:],
            }
        )
        return report


def _is_import_healthy(import_name: str, module: object) -> bool:
    if import_name == "numpy":
        try:
            probe = module.asarray([[1.0, 2.0], [3.0, 4.0]], dtype=module.float32)
            return (
                tuple(probe.shape) == (2, 2)
                and float(module.sum(probe)) == 10.0
                and str(getattr(module, "__version__", "") or "") != ""
            )
        except Exception:
            return False
    if import_name == "ufbx":
        return _is_expected_ufbx_build(module)
    if import_name == "orjson":
        try:
            indent_option = getattr(module, "OPT_INDENT_2")
            payload = {"codex_runtime_probe": [1, "re6", True]}
            encoded = module.dumps(payload, option=indent_option)
            return module.loads(encoded) == payload
        except Exception:
            return False
    accelerator_requirements = LOCAL_ACCELERATOR_HEALTH_REQUIREMENTS.get(import_name)
    if accelerator_requirements is not None:
        if get_accelerator_dependency_sync_report(include_release_copies=False).get("ready") is not True:
            return False
        installed_version = str(getattr(module, "__version__", "") or "")
        minimum_version = str(accelerator_requirements.get("minimum_version", "") or "")
        if installed_version == "" or _compare_version_text(installed_version, minimum_version) < 0:
            return False
        for capability in accelerator_requirements.get("capabilities", ()):
            if getattr(module, str(capability), False) is not True:
                return False
        expected_contract_revision = int(accelerator_requirements.get("contract_revision", 0) or 0)
        module_contract_revision = int(getattr(module, "BOOTSTRAP_ACCELERATOR_CONTRACT_REVISION", 0) or 0)
        if expected_contract_revision <= 0 or module_contract_revision != expected_contract_revision:
            return False
    return True


def _ufbx_binary_tag_matches_current_runtime(spec: importlib.machinery.ModuleSpec | None) -> bool:
    if spec is None or not spec.submodule_search_locations:
        return True
    try:
        package_dir = Path(next(iter(spec.submodule_search_locations)))
    except Exception:
        return True
    if not package_dir.exists():
        return True
    binary_files = list(package_dir.glob("_ufbx.cp*-*.pyd"))
    if not binary_files:
        return True
    expected_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    return any(expected_tag in binary_path.name.lower() for binary_path in binary_files)


def _find_vendor_package_dir(import_name: str) -> Path | None:
    configure_vendor_paths()
    for vendor_root in _vendor_path_candidates():
        package_dir = vendor_root / import_name
        if (package_dir / "__init__.py").is_file():
            return package_dir
    return None


def _import_vendor_ufbx_module(vendor_package_dir: Path | None = None) -> object:
    if vendor_package_dir is None:
        vendor_package_dir = _find_vendor_package_dir("ufbx")
    if vendor_package_dir is None:
        raise ModuleNotFoundError("Missing vendor ufbx package in writable and packaged ABI lanes")
    init_path = vendor_package_dir / "__init__.py"
    if not init_path.exists():
        raise ModuleNotFoundError(f"Missing vendor ufbx package: {init_path}")
    spec = importlib.util.spec_from_file_location(
        "ufbx",
        init_path,
        submodule_search_locations=[str(vendor_package_dir)],
    )
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(f"Unable to create import spec for vendor ufbx: {init_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["ufbx"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        _clear_import_tree("ufbx")
        raise
    return module


def _default_import_checker(import_name: str) -> bool:
    configure_vendor_paths()
    # CPython does not guarantee that an extension module can survive being
    # removed from sys.modules and imported again in the same process. ufbx and
    # both Codex accelerators contain .pyd modules; repeated health checks used
    # to initialize them several times and produced intermittent 0xC0000005
    # failures under concurrent Max exports, especially on Python 3.14.
    if _get_healthy_loaded_import(import_name) is not None:
        return True
    _clear_import_tree(import_name)
    importlib.invalidate_caches()
    if import_name == "ufbx":
        vendor_package_dir = _find_vendor_package_dir("ufbx")
        if vendor_package_dir is None:
            _set_import_error_text(import_name, "Missing vendor ufbx package in writable and packaged ABI lanes.")
            return False
        vendor_init = vendor_package_dir / "__init__.py"
        vendor_spec = importlib.util.spec_from_file_location(
            "ufbx",
            vendor_init,
            submodule_search_locations=[str(vendor_init.parent)],
        )
        if _ufbx_binary_tag_matches_current_runtime(vendor_spec) is not True:
            _set_import_error_text(import_name, "Vendor ufbx binary tag does not match the active Python runtime.")
            return False
        try:
            module = _import_vendor_ufbx_module(vendor_package_dir)
            if _is_import_healthy(import_name, module) is not True:
                _set_import_error_text(import_name, "Vendor ufbx import is present but did not match the expected Codex-patched build.")
                return False
            _set_import_error_text(import_name, "")
            return True
        except Exception as exc:
            _clear_import_tree(import_name)
            _set_import_error_text(import_name, str(exc))
            return False
    spec = importlib.util.find_spec(import_name)
    if spec is None:
        _set_import_error_text(import_name, "Import spec not found.")
        return False
    try:
        module = importlib.import_module(import_name)
        if _is_import_healthy(import_name, module) is not True:
            _clear_import_tree(import_name)
            _set_import_error_text(import_name, "Import loaded but failed the Codex runtime health check.")
            return False
        _set_import_error_text(import_name, "")
        return True
    except Exception as exc:
        _clear_import_tree(import_name)
        _set_import_error_text(import_name, str(exc))
        return False


def _ensure_pip_available(*, bootstrap_if_missing: bool = False) -> None:
    try:
        import pip  # noqa: F401
        return
    except Exception as initial_error:
        if bootstrap_if_missing:
            completed = _run_hidden_subprocess(
                [sys.executable, "-I", "-B", "-m", "ensurepip", "--upgrade", "--default-pip"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180.0,
                env=_isolated_python_child_environment(),
            )
            probe = _run_hidden_subprocess(
                [sys.executable, "-I", "-B", "-c", "import pip; print(pip.__version__)"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30.0,
                env=_isolated_python_child_environment(),
            )
            if completed.returncode == 0 and probe.returncode == 0:
                return
            raise RuntimeError(
                "Managed Python could not bootstrap pip with ensurepip: "
                + (probe.stderr or completed.stderr or completed.stdout or "pip probe failed")[-4000:]
            )
        raise RuntimeError(
            "pip is unavailable in this Python runtime. Offline fixed-wheel repair remains available, "
            "but official-index repair and upgrade discovery are disabled."
        ) from initial_error


def _install_local_wheel_without_pip(
    wheel_path: Path,
    target_dir: Path,
    *,
    require_registered: bool = True,
) -> dict[str, object]:
    wheel_path = wheel_path.resolve()
    if wheel_path.is_file() is not True or wheel_path.suffix.casefold() != ".whl":
        raise RuntimeError(f"Local wheel payload is missing or invalid: {wheel_path}")
    if require_registered:
        registered_candidates = {
            os.path.normcase(os.path.abspath(str(Path(candidate).resolve())))
            for candidates in _build_package_candidate_map().values()
            for candidate in candidates
            if Path(candidate).suffix.casefold() == ".whl"
        }
        if os.path.normcase(os.path.abspath(str(wheel_path))) not in registered_candidates:
            raise RuntimeError(f"Unregistered local wheel payload was rejected: {wheel_path}")
    if zipfile.is_zipfile(wheel_path) is not True:
        raise RuntimeError(f"Local wheel is not a valid ZIP archive: {wheel_path}")
    target_dir.mkdir(parents=True, exist_ok=True)
    installed_files: list[str] = []
    skipped_members: list[str] = []
    with zipfile.ZipFile(wheel_path) as wheel_zip:
        for member in wheel_zip.infolist():
            member_name = member.filename.replace("\\", "/")
            member_path = PurePosixPath(member_name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RuntimeError(f"Unsafe wheel member path rejected: {member.filename}")
            if member.is_dir():
                continue
            relative_parts = list(member_path.parts)
            if relative_parts and relative_parts[0].casefold().endswith(".data"):
                if len(relative_parts) < 3:
                    skipped_members.append(member.filename)
                    continue
                scheme = relative_parts[1].casefold()
                relative_parts = relative_parts[2:]
                if scheme in {"purelib", "platlib", "data"}:
                    pass
                elif scheme == "scripts":
                    relative_parts.insert(0, "Scripts")
                elif scheme == "headers":
                    relative_parts.insert(0, "Include")
                else:
                    skipped_members.append(member.filename)
                    continue
            if not relative_parts:
                skipped_members.append(member.filename)
                continue
            destination = target_dir.joinpath(*relative_parts)
            if _path_is_within(destination, target_dir) is not True:
                raise RuntimeError(f"Wheel member escaped target directory: {member.filename}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with wheel_zip.open(member, "r") as source_stream, destination.open("wb") as target_stream:
                shutil.copyfileobj(source_stream, target_stream)
            installed_files.append(str(destination))
    return {
        "source": "offline-local-wheel",
        "wheel": str(wheel_path),
        "wheel_sha256": _sha256_file(wheel_path, use_cache=False),
        "installed_file_count": len(installed_files),
        "installed_files": installed_files,
        "skipped_members": skipped_members,
        "installer": "stdlib-wheel-zip",
    }


def _ensure_local_accelerator_build_backend(*, target_dir: Path | None = None) -> None:
    # AI MAINTENANCE GATE: accelerator source builds are local-only. Keep this
    # backend floor synchronized with python_build_tools_re6_v4 and the charter.
    wheel_candidates = sorted(
        LOCAL_PYTHON_BUILD_TOOLS_DIR.glob("setuptools-*-py3-none-any.whl"),
        key=lambda path: _version_key(_extract_candidate_version(path.name)),
        reverse=True,
    )
    if not wheel_candidates:
        raise RuntimeError(
            "Local accelerator source build requires a setuptools wheel under "
            + str(LOCAL_PYTHON_BUILD_TOOLS_DIR)
        )

    target_dir = ensure_vendor_path() if target_dir is None else Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    _install_local_wheel_without_pip(
        wheel_candidates[0],
        target_dir,
        require_registered=False,
    )
    _clear_import_tree("setuptools")
    importlib.invalidate_caches()


def _build_local_accelerator_source_without_pip(
    import_name: str,
    project_dir: Path,
    target_dir: Path,
) -> dict[str, object]:
    _ensure_local_accelerator_build_backend(target_dir=target_dir)
    with tempfile.TemporaryDirectory(prefix=f"codex-{import_name}-build-") as build_text:
        build_root = Path(build_text)
        project_copy = build_root / "source"
        output_root = build_root / "output"
        shutil.copytree(project_dir, project_copy)
        child_env = _isolated_python_child_environment()
        child_env["PYTHONPATH"] = str(target_dir)
        completed = _run_hidden_subprocess(
            [
                sys.executable,
                str(project_copy / "setup.py"),
                "build",
                "--build-base",
                str(output_root),
            ],
            cwd=project_copy,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            env=child_env,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "Local accelerator source build failed for "
                + import_name
                + f" with exit code {completed.returncode}\nSTDOUT:\n{completed.stdout[-4000:]}\nSTDERR:\n{completed.stderr[-4000:]}"
            )
        built_packages = sorted(output_root.glob(f"lib*/{import_name}"))
        if not built_packages:
            built_packages = sorted(output_root.glob(f"lib*/**/{import_name}"))
        if not built_packages:
            raise RuntimeError(f"Local accelerator build produced no {import_name} package under {output_root}")
        target_package = target_dir / import_name
        if target_package.exists():
            shutil.rmtree(target_package, ignore_errors=True)
        shutil.copytree(built_packages[0], target_package)
        return {
            "source": "offline-local-source-build",
            "project_dir": str(project_dir),
            "source_fingerprint": _candidate_artifact_fingerprint(str(project_dir)),
            "built_package": str(target_package),
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }


def _default_installer(
    packages: tuple[str, ...],
    *,
    target_dir: Path | None = None,
) -> None:
    if not packages:
        return
    import_name: str | None = None
    package_path: Path | None = None
    use_no_deps = False
    use_no_build_isolation = False
    if len(packages) == 1:
        import_name = _find_import_name_for_candidate(packages[0])
        package_path = Path(packages[0])
        if (
            import_name == "ufbx"
            and packages[0] in PACKAGE_BY_IMPORT_NAME.get("ufbx", ())
            and _install_prebuilt_ufbx_bundle(target_dir=target_dir)
        ):
            return
        if import_name == "ufbx":
            raise DependencyBundleBrokenError(
                f"Patched ufbx prebuilt payload is missing for {_python_tag()}; source or PyPI substitution is forbidden.",
                {
                    "import_name": "ufbx",
                    "python_tag": _python_tag(),
                    "candidate": str(package_path),
                    "prebuilt_path": str(
                        PATCHED_UFBX_SOURCE_DIR
                        / "build"
                        / f"lib.win-amd64-cpython-{sys.version_info.major}{sys.version_info.minor}"
                        / "ufbx"
                    ),
                },
            )
        if import_name is not None and package_path.is_file() and package_path.suffix.casefold() == ".whl":
            _install_local_wheel_without_pip(package_path, Path(target_dir) if target_dir is not None else ensure_vendor_path())
            return
        if (
            import_name in LOCAL_ACCELERATOR_IMPORTS
            and _install_prebuilt_package_dir(import_name, package_path, target_dir=target_dir)
        ):
            return
        if import_name in LOCAL_ACCELERATOR_IMPORTS and _is_local_source_package_dir(package_path):
            _build_local_accelerator_source_without_pip(
                import_name,
                package_path,
                Path(target_dir) if target_dir is not None else ensure_vendor_path(),
            )
            return
    non_local = [package_name for package_name in packages if Path(package_name).exists() is not True]
    if non_local:
        raise RuntimeError(
            "Unclassified network dependency spec(s) were blocked. Online repair must pass through IMPORT_POLICY: "
            + ", ".join(non_local),
        )
    target_dir = ensure_vendor_path() if target_dir is None else Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    _ensure_pip_available()
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--upgrade",
        "--target",
        str(target_dir),
        *packages,
    ]
    insert_at = 5
    if use_no_build_isolation is True:
        cmd.insert(insert_at, "--no-build-isolation")
        insert_at += 1
    if use_no_deps is True:
        cmd.insert(insert_at, "--no-deps")
    child_env = _isolated_python_child_environment()
    child_env["PYTHONPATH"] = str(target_dir)
    completed = _run_hidden_subprocess(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        env=child_env,
    )
    if completed.returncode != 0:  # pragma: no cover - network / installer dependent
        raise RuntimeError(
            "Dependency install failed for "
            + ", ".join(packages)
            + f" with exit code {completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}",
        )


def _online_repair_allowed(import_name: str) -> bool:
    policy = IMPORT_POLICY.get(import_name, {})
    return (
        NETWORK_REPAIR_ENABLED
        and str(policy.get("repair_mode", "") or "") == "online-repair"
        and str(policy.get("online_spec", "") or "").strip() != ""
    )


def _online_install_report_entries(report_path: Path) -> list[dict[str, object]]:
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    install_entries = payload.get("install", []) if isinstance(payload, dict) else []
    results: list[dict[str, object]] = []
    for entry in install_entries if isinstance(install_entries, list) else []:
        if not isinstance(entry, dict):
            continue
        metadata = entry.get("metadata", {})
        download_info = entry.get("download_info", {})
        archive_info = download_info.get("archive_info", {}) if isinstance(download_info, dict) else {}
        hashes = archive_info.get("hashes", {}) if isinstance(archive_info, dict) else {}
        results.append(
            {
                "name": str(metadata.get("name", "") or "") if isinstance(metadata, dict) else "",
                "version": str(metadata.get("version", "") or "") if isinstance(metadata, dict) else "",
                "url": str(download_info.get("url", "") or "") if isinstance(download_info, dict) else "",
                "sha256": str(hashes.get("sha256", "") or "") if isinstance(hashes, dict) else "",
            }
        )
    return results


def _install_online_import_to_stage(
    import_name: str,
    target_dir: Path,
    *,
    network_timeout_seconds: int = 20,
    retries: int = 2,
    process_timeout_seconds: float = 180.0,
) -> dict[str, object]:
    policy = dict(IMPORT_POLICY.get(import_name, {}))
    if _online_repair_allowed(import_name) is not True:
        raise DependencyBundleBrokenError(
            f"Dependency {import_name} is {policy.get('repair_mode', 'pinned-local-only')} and cannot be replaced from PyPI."
        )
    online_spec = str(policy["online_spec"])
    target_dir.mkdir(parents=True, exist_ok=True)
    report_path = target_dir.parent / f".{target_dir.name}-{import_name}-{uuid.uuid4().hex}.pip-report.json"
    try:
        _ensure_pip_available()
    except Exception as exc:
        raise OnlineDependencyRepairError(
            "Online repair requires pip or ensurepip, but this Python runtime provides neither.",
            {
                "import_name": import_name,
                "source": "online-repair",
                "policy": policy,
                "index_url": OFFICIAL_PYPI_INDEX_URL,
                "requested_spec": online_spec,
                "error_type": "PIP_UNAVAILABLE",
                "error": str(exc),
            },
        ) from exc
    command = [
        sys.executable,
        "-m",
        "pip",
        "--isolated",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--no-cache-dir",
        "--only-binary=:all:",
        "--prefer-binary",
        "--no-deps",
        "--upgrade",
        "--timeout",
        str(max(1, int(network_timeout_seconds))),
        "--retries",
        str(max(0, int(retries))),
        "--index-url",
        OFFICIAL_PYPI_INDEX_URL,
        "--target",
        str(target_dir),
        "--report",
        str(report_path),
        online_spec,
    ]
    child_env = _isolated_python_child_environment()
    for env_name in ("PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS", "PIP_NO_INDEX", "PYTHONPATH"):
        child_env.pop(env_name, None)
    completed = _run_hidden_subprocess(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(10.0, float(process_timeout_seconds)),
        env=child_env,
    )
    report_entries = _online_install_report_entries(report_path)
    try:
        report_path.unlink(missing_ok=True)
    except OSError:
        pass
    result = {
        "import_name": import_name,
        "source": "online-repair",
        "policy": policy,
        "index_url": OFFICIAL_PYPI_INDEX_URL,
        "requested_spec": online_spec,
        "returncode": completed.returncode,
        "downloads": report_entries,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }
    if completed.returncode != 0:
        raise OnlineDependencyRepairError(
            f"Official-index repair failed for {import_name} with exit code {completed.returncode}.",
            result,
        )
    if not report_entries:
        raise OnlineDependencyRepairError(
            f"Official-index repair for {import_name} did not produce provenance metadata.",
            result,
        )
    return result


def _record_failed_upgrade_candidate(import_name: str, package_name: str, reason_text: str) -> None:
    if package_name not in set(_get_upgrade_install_candidates(import_name)):
        return
    candidate_version = _extract_candidate_version(package_name)
    artifact_report = _record_failed_candidate_artifact(import_name, package_name, reason_text)
    version_label = str(candidate_version or "unknown")
    _append_runtime_advisory(
        {
            "title": "Codex Python Runtime Warning",
            "import_name": import_name,
            "status": "upgrade-rolled-back",
            "summary_lines": [
                import_name
                + " upgrade "
                + version_label
                + " failed under Python "
                + f"{sys.version_info.major}.{sys.version_info.minor}"
                + " and was rejected. Bootstrap will use the fixed rollback package or the pure-Python fallback.",
                "Reason: " + str(reason_text or "unknown failure"),
                "The rejected artifact fingerprint is skipped until its version or content hash changes. State: "
                + str(RUNTIME_STATE_PATH)
                + " | fingerprint="
                + str(artifact_report.get("fingerprint", "unknown")),
            ],
        }
    )


def _install_import_from_candidates(
    import_name: str,
    checker: ImportChecker,
    install_packages: Installer,
    *,
    fixed_only: bool = False,
) -> list[str]:
    if install_packages is _default_installer:
        transaction_report = _transactional_repair_import(import_name, fixed_only=fixed_only)
        LAST_INSTALL_PROVENANCE[import_name] = dict(transaction_report)
        return [str(value) for value in transaction_report.get("installed_packages", [])]
    candidates = _get_install_candidates(import_name, fixed_only=fixed_only)
    if not candidates:
        raise ModuleNotFoundError(f"No local package candidates registered for import {import_name}")
    installed_packages: list[str] = []
    install_error: Exception | None = None
    for package_name in candidates:
        try:
            install_packages((package_name,))
        except Exception as exc:
            install_error = exc
            _record_failed_upgrade_candidate(import_name, package_name, "Install/build failed: " + str(exc))
            continue
        if checker(import_name) is True:
            installed_packages.append(package_name)
            return installed_packages
        _record_failed_upgrade_candidate(
            import_name,
            package_name,
            "Installed candidate failed import or runtime health checks: " + get_last_import_error_text(import_name),
        )
    if install_error is not None:
        raise ModuleNotFoundError(f"Runtime dependency install did not provide import {import_name}") from install_error
    raise ModuleNotFoundError(f"Runtime dependency install did not provide import {import_name}")


def _try_relaunch_after_required_dependency_failure(import_name: str) -> bool:
    if import_name != "ufbx":
        return False
    fallback_candidate = _find_supported_python_runtime_candidate()
    if not isinstance(fallback_candidate, dict):
        return False
    # ufbx is required to read FBX. If this ABI cannot copy or compile it, retry
    # the complete bridge under the next available formally supported runtime.
    _relaunch_under_supported_python(get_runtime_support_report())
    return True


def ensure_import_runtime(
    import_name: str,
    *,
    import_checker: ImportChecker | None = None,
    installer: Installer | None = None,
    prefer_local_floor: bool = False,
) -> dict[str, object]:
    runtime_report = validate_python_runtime()
    checker = import_checker or _default_import_checker
    install_packages = installer or _default_installer

    missing_before_install: list[str] = []
    installed_packages: list[str] = []
    upgrade_action = "already-approved"
    import_ready = checker(import_name) is True
    installed_version = (
        _get_import_installed_version(import_name, checker=checker)
        if import_ready is True
        else _get_import_distribution_version(import_name)
    )
    local_version = _get_import_local_approved_version(import_name)
    floor_version = _get_import_local_floor_version(import_name)
    effective_target_version = floor_version if prefer_local_floor is True and floor_version not in (None, "") else local_version
    import_error_text = "" if import_ready is True else get_last_import_error_text(import_name)
    blocked_current_version = (
        installed_version not in (None, "")
        and _is_import_version_blocked(import_name, installed_version) is True
    )
    if (
        import_ready is not True
        and IMPORT_POLICY.get(import_name, {}).get("allow_upgrade") is True
        and installed_version not in (None, "")
        and (floor_version not in (None, "") or local_version not in (None, ""))
        and _compare_version_text(installed_version, floor_version or local_version) > 0
        and blocked_current_version is not True
    ):
        reason_text = "Runtime import failed"
        if import_error_text != "":
            reason_text += ": " + import_error_text
        reason_text += " | local approved floor=" + str(floor_version or local_version)
        if local_version not in (None, ""):
            reason_text += " | preferred local=" + str(local_version)
        mark_import_version_blocked(import_name, installed_version, reason_text)
        blocked_current_version = True
    if import_ready is not True:
        missing_before_install.append(import_name)
        try:
            installed_packages.extend(
                _install_import_from_candidates(
                    import_name,
                    checker,
                    install_packages,
                    fixed_only=True,
                )
            )
        except Exception as exc:
            if isinstance(exc, (DependencyBundleBrokenError, OnlineDependencyRepairError)):
                raise
            if _try_relaunch_after_required_dependency_failure(import_name) is not True:
                if import_name == "ufbx":
                    message_text = runtime_ui_text(
                        "当前 Python "
                        + f"{sys.version_info.major}.{sys.version_info.minor} "
                        + "无法修复导出所必需的定制 ufbx FBX 读取器。Python 已尝试匹配的预编译包、本地源码编译以及其他可用的受支持 Python ABI。"
                        + "请安装或修复 Python 3.14，或者为当前 A/B 候选运行时添加可用的定制 ufbx 包。没有 FBX 读取器时无法继续导出。",
                        "The required patched ufbx FBX reader could not be repaired for Python "
                        + f"{sys.version_info.major}.{sys.version_info.minor}. "
                        + "Python tried the matching prebuilt package, local source build, and every other available "
                        + "supported Python ABI. Install or repair Python 3.14, or add a working patched ufbx "
                        + "package for the active cpXXX runtime. Export cannot continue without an FBX reader.",
                    )
                    _show_blocking_runtime_error_gui(message_text)
                    raise RuntimeError(message_text) from exc
                raise
        if blocked_current_version is True:
            upgrade_action = "repaired-blocked-version"
        else:
            upgrade_action = "installed-missing"
    else:
        should_upgrade, upgrade_action = _should_upgrade_import(
            import_name,
            installed_version,
            effective_target_version,
            blocked_current_version=blocked_current_version,
            prefer_local_floor=prefer_local_floor,
        )
        if should_upgrade is True:
            installed_packages.extend(
                _install_import_from_candidates(
                    import_name,
                    checker,
                    install_packages,
                    fixed_only=blocked_current_version or prefer_local_floor,
                )
            )

    ready = checker(import_name) is True
    if ready is not True:
        raise ModuleNotFoundError(f"Runtime dependency unresolved after install: {import_name}")

    effective_installed_version = _get_import_installed_version(import_name, checker=checker)
    online_upgrade_report: dict[str, object] = {"status": "not-applicable"}
    if (
        checker is _default_import_checker
        and install_packages is _default_installer
        and prefer_local_floor is not True
        and _online_repair_allowed(import_name)
        and IMPORT_POLICY.get(import_name, {}).get("allow_upgrade") is True
    ):
        if missing_before_install or installed_packages:
            deferred_state = _record_upgrade_check_state(
                import_name,
                status="deferred-after-local-repair",
                report={
                    "installed_packages": installed_packages,
                    "effective_installed_version": effective_installed_version,
                },
                ttl_seconds=FAILED_UPGRADE_CHECK_TTL_SECONDS,
            )
            online_upgrade_report = {
                "status": "deferred-after-local-repair",
                "state": deferred_state,
            }
        else:
            try:
                online_upgrade_report = _schedule_online_upgrade(
                    import_name,
                    current_version=effective_installed_version or installed_version,
                )
            except Exception as exc:
                # Upgrade discovery is advisory; a healthy active lane must keep exporting.
                online_upgrade_report = {
                    "status": "deferred-error",
                    "error_type": getattr(exc, "error_type", type(exc).__name__),
                    "error": str(exc),
                    "error_report": dict(getattr(exc, "report", {})),
                }

    return {
        "import_name": import_name,
        "python_runtime": runtime_report,
        "missing_before_install": missing_before_install,
        "installed_packages": installed_packages,
        "installed_version": installed_version,
        "effective_installed_version": effective_installed_version,
        "local_version": local_version,
        "preferred_version": local_version,
        "floor_version": floor_version,
        "blocked_current_version": blocked_current_version,
        "upgrade_action": upgrade_action,
        "error_text": import_error_text,
        "install_provenance": dict(LAST_INSTALL_PROVENANCE.get(import_name, {})),
        "online_upgrade": online_upgrade_report,
        "ready": True,
    }


def ensure_optional_import_runtime(
    import_name: str,
    *,
    import_checker: ImportChecker | None = None,
    installer: Installer | None = None,
    prefer_local_floor: bool = False,
) -> dict[str, object]:
    checker = import_checker or _default_import_checker
    if checker(import_name) is True:
        report = ensure_import_runtime(
            import_name,
            import_checker=checker,
            installer=installer,
            prefer_local_floor=prefer_local_floor,
        )
        report["status"] = "ready"
        return report

    candidates = _get_install_candidates(import_name)
    import_error_text = get_last_import_error_text(import_name)
    can_use_default_online_repair = (
        _online_repair_allowed(import_name)
        and checker is _default_import_checker
        and (installer is None or installer is _default_installer)
    )
    if not candidates and can_use_default_online_repair is not True:
        policy = dict(IMPORT_POLICY.get(import_name, {}))
        pinned_local_only = str(policy.get("repair_mode", "") or "") == "pinned-local-only"
        return {
            "import_name": import_name,
            "python_runtime": validate_python_runtime(),
            "missing_before_install": [import_name],
            "installed_packages": [],
            "local_version": _get_import_local_approved_version(import_name),
            "preferred_version": _get_import_local_approved_version(import_name),
            "floor_version": _get_import_local_floor_version(import_name),
            "upgrade_action": "no-local-bundle",
            "ready": False,
            "status": "no-local-bundle",
            "error_text": import_error_text,
            "error_type": "DEPENDENCY_BUNDLE_BROKEN" if pinned_local_only else "NO_LOCAL_BUNDLE",
            "error": (
                f"Pinned dependency bundle has no healthy local payload for {import_name} and public-index repair is forbidden."
                if pinned_local_only
                else f"No local payload is registered for {import_name}."
            ),
            "policy": policy,
        }

    try:
        report = ensure_import_runtime(
            import_name,
            import_checker=checker,
            installer=installer,
            prefer_local_floor=prefer_local_floor,
        )
        report["status"] = "ready"
        return report
    except Exception as exc:
        return {
            "import_name": import_name,
            "python_runtime": validate_python_runtime(),
            "missing_before_install": [import_name],
            "installed_packages": [],
            "local_version": _get_import_local_approved_version(import_name),
            "preferred_version": _get_import_local_approved_version(import_name),
            "floor_version": _get_import_local_floor_version(import_name),
            "upgrade_action": "install-failed",
            "ready": checker(import_name) is True,
            "status": "install-failed",
            "error_type": getattr(exc, "error_type", type(exc).__name__),
            "error": str(exc),
            "error_report": dict(getattr(exc, "report", {})),
            "error_text": get_last_import_error_text(import_name),
        }


def try_import_optional_runtime_module(
    import_name: str,
    *,
    import_checker: ImportChecker | None = None,
    installer: Installer | None = None,
    prefer_local_floor: bool = False,
    refresh: bool = False,
    repair: bool = False,
) -> Any | None:
    if refresh is True:
        OPTIONAL_RUNTIME_MODULE_CACHE.pop(import_name, None)
    elif import_name in OPTIONAL_RUNTIME_MODULE_CACHE:
        cached_module = OPTIONAL_RUNTIME_MODULE_CACHE[import_name]
        if cached_module is not None or repair is not True:
            return cached_module

    if repair is not True:
        configure_vendor_paths()
        loaded_module = _get_healthy_loaded_import(import_name)
        if loaded_module is not None:
            OPTIONAL_RUNTIME_MODULE_CACHE[import_name] = loaded_module
            return loaded_module
        _clear_import_tree(import_name)
        importlib.invalidate_caches()
        try:
            loaded_module = importlib.import_module(import_name)
            if _is_import_healthy(import_name, loaded_module) is not True:
                raise ImportError(
                    f"Optional runtime import failed its health contract: {import_name}"
                )
        except Exception as exc:
            _clear_import_tree(import_name)
            _set_import_error_text(import_name, str(exc))
            OPTIONAL_RUNTIME_MODULE_CACHE[import_name] = None
            return None
        _set_import_error_text(import_name, "")
        OPTIONAL_RUNTIME_MODULE_CACHE[import_name] = loaded_module
        return loaded_module

    checker = import_checker or _default_import_checker
    report = ensure_optional_import_runtime(
        import_name,
        import_checker=checker,
        installer=installer,
        prefer_local_floor=prefer_local_floor,
    )
    if report.get("ready") is not True:
        _append_runtime_advisory(
            _build_optional_runtime_advisory(
                import_name,
                report,
                error_text=str(report.get("error_text", "") or report.get("error", "") or ""),
            )
        )
        OPTIONAL_RUNTIME_MODULE_CACHE[import_name] = None
        return None
    if str(report.get("upgrade_action", "") or "") == "repaired-blocked-version":
        _append_runtime_advisory(
            _build_optional_runtime_advisory(
                import_name,
                {
                    **report,
                    "status": "repaired-blocked-version",
                },
                error_text=str(report.get("error_text", "") or ""),
            )
        )
    configure_vendor_paths()
    loaded_module = _get_healthy_loaded_import(import_name)
    if loaded_module is not None:
        OPTIONAL_RUNTIME_MODULE_CACHE[import_name] = loaded_module
        return loaded_module
    _clear_import_tree(import_name)
    importlib.invalidate_caches()
    try:
        module = importlib.import_module(import_name)
        _set_import_error_text(import_name, "")
        OPTIONAL_RUNTIME_MODULE_CACHE[import_name] = module
        return module
    except Exception as exc:
        _clear_import_tree(import_name)
        _set_import_error_text(import_name, str(exc))
        _append_runtime_advisory(
            _build_optional_runtime_advisory(
                import_name,
                {
                    **report,
                    "status": "import-failed",
                },
                error_text=str(exc),
            )
        )
        OPTIONAL_RUNTIME_MODULE_CACHE[import_name] = None
        return None


def _run_loaded_native_import_reuse_regression_guard() -> dict[str, object]:
    import_name = "codex_bootstrap_loaded_native_guard"
    marker = object()
    sys.modules[import_name] = marker
    OPTIONAL_RUNTIME_MODULE_CACHE.pop(import_name, None)
    try:
        if _default_import_checker(import_name) is not True:
            raise RuntimeError("Loaded-import regression: a healthy loaded module failed validation")
        if sys.modules.get(import_name) is not marker:
            raise RuntimeError("Loaded-import regression: health validation reloaded an existing module")
        imported = try_import_optional_runtime_module(import_name)
        if imported is not marker or sys.modules.get(import_name) is not marker:
            raise RuntimeError("Loaded-import regression: optional import reinitialized an existing module")
    finally:
        OPTIONAL_RUNTIME_MODULE_CACHE.pop(import_name, None)
        sys.modules.pop(import_name, None)

    missing_name = "codex_bootstrap_optional_negative_cache_guard"
    original_import_module = importlib.import_module
    import_attempts = 0

    def counted_import_module(name: str, package: str | None = None) -> Any:
        nonlocal import_attempts
        if name == missing_name:
            import_attempts += 1
            raise ModuleNotFoundError(name)
        return original_import_module(name, package)

    OPTIONAL_RUNTIME_MODULE_CACHE.pop(missing_name, None)
    try:
        importlib.import_module = counted_import_module
        first = try_import_optional_runtime_module(missing_name)
        second = try_import_optional_runtime_module(missing_name)
        if first is not None or second is not None or import_attempts != 1:
            raise RuntimeError("Missing optional import was not negatively cached")
        refreshed = try_import_optional_runtime_module(missing_name, refresh=True)
        if refreshed is not None or import_attempts != 2:
            raise RuntimeError("Explicit optional-import refresh did not invalidate the negative cache")
    finally:
        importlib.import_module = original_import_module
        OPTIONAL_RUNTIME_MODULE_CACHE.pop(missing_name, None)
        IMPORT_HEALTH_ERRORS.pop(missing_name, None)
        sys.modules.pop(missing_name, None)
    return {
        "status": "PASS",
        "loaded_module_reused": True,
        "missing_optional_negative_cache": True,
        "explicit_refresh_reprobes": True,
    }


def runtime_json_loads(payload: str | bytes | bytearray) -> Any:
    orjson_module = try_import_optional_runtime_module("orjson", repair=False)
    if orjson_module is not None:
        if isinstance(payload, str):
            return orjson_module.loads(payload.encode("utf-8"))
        return orjson_module.loads(bytes(payload))
    if isinstance(payload, (bytes, bytearray)):
        return json.loads(bytes(payload).decode("utf-8"))
    return json.loads(payload)


def runtime_json_dumps_bytes(payload: Any, *, pretty: bool = False) -> bytes:
    orjson_module = try_import_optional_runtime_module("orjson", repair=False)
    if orjson_module is not None:
        options = 0
        if pretty is True:
            options |= getattr(orjson_module, "OPT_INDENT_2", 0)
        return orjson_module.dumps(payload, option=options)
    return json.dumps(payload, ensure_ascii=False, indent=2 if pretty else None).encode("utf-8")


def runtime_json_dumps_text(payload: Any, *, pretty: bool = False) -> str:
    return runtime_json_dumps_bytes(payload, pretty=pretty).decode("utf-8")


def runtime_read_json_file(path: str | Path) -> Any:
    return runtime_json_loads(Path(path).read_bytes())


def runtime_write_json_file(path: str | Path, payload: Any, *, pretty: bool = True) -> None:
    target = Path(path)
    _atomic_write_runtime_bytes(target, runtime_json_dumps_bytes(payload, pretty=pretty))


def ensure_module_runtime(
    module_name: str,
    *,
    import_checker: ImportChecker | None = None,
    installer: Installer | None = None,
) -> dict[str, object]:
    runtime_report = validate_python_runtime()
    requirements = _expand_policy_import_dependencies(MODULE_REQUIREMENTS[module_name])
    checker = import_checker or _default_import_checker
    install_packages = installer or _default_installer

    missing_before_install: list[str] = []
    installed_packages: list[str] = []
    for import_name in requirements:
        report = ensure_import_runtime(
            import_name,
            import_checker=checker,
            installer=install_packages,
        )
        missing_before_install.extend(report["missing_before_install"])
        installed_packages.extend(report["installed_packages"])

    ready_dependencies = [import_name for import_name in requirements if checker(import_name) is True]
    unresolved_dependencies = [import_name for import_name in requirements if import_name not in ready_dependencies]
    if unresolved_dependencies:
        raise ModuleNotFoundError(
            f"{module_name} runtime dependencies unresolved: {', '.join(unresolved_dependencies)}",
        )
    bridge_contract = (
        get_export_bridge_contract_report()
        if module_name == "codex_python_export_bridge"
        else None
    )

    return {
        "module": module_name,
        "python_runtime": runtime_report,
        "missing_before_install": missing_before_install,
        "installed_packages": installed_packages,
        "ready_dependencies": ready_dependencies,
        "bridge_contract": bridge_contract,
    }


def ensure_named_modules_runtime(
    module_names: tuple[str, ...] | list[str],
    *,
    import_checker: ImportChecker | None = None,
    installer: Installer | None = None,
) -> dict[str, object]:
    runtime_report = validate_python_runtime()
    checker = import_checker or _default_import_checker
    install_packages = installer or _default_installer

    unique_missing: list[str] = []
    installed_packages: list[str] = []
    import_reports: list[dict[str, object]] = []
    seen_imports: list[str] = []
    for module_name in module_names:
        for import_name in _expand_policy_import_dependencies(MODULE_REQUIREMENTS[module_name]):
            if import_name in seen_imports:
                continue
            seen_imports.append(import_name)
            report = ensure_import_runtime(
                import_name,
                import_checker=checker,
                installer=install_packages,
            )
            if report["missing_before_install"]:
                unique_missing.append(import_name)
            installed_packages.extend(report["installed_packages"])
            import_reports.append(report)

    module_reports = [
        ensure_module_runtime(
            module_name,
            import_checker=checker,
            installer=install_packages,
        )
        for module_name in module_names
    ]
    return {
        "modules": [str(name) for name in module_names],
        "python_runtime": runtime_report,
        "missing_dependencies": unique_missing,
        "installed_packages": installed_packages,
        "import_reports": import_reports,
        "module_reports": module_reports,
    }


def ensure_named_imports_runtime(
    import_names: tuple[str, ...] | list[str],
    *,
    import_checker: ImportChecker | None = None,
    installer: Installer | None = None,
    prefer_local_floor: bool = False,
) -> dict[str, object]:
    runtime_report = validate_python_runtime()
    checker = import_checker or _default_import_checker
    install_packages = installer or _default_installer
    import_reports: list[dict[str, object]] = []
    missing_dependencies: list[str] = []
    installed_packages: list[str] = []
    for import_name in import_names:
        report = ensure_import_runtime(
            import_name,
            import_checker=checker,
            installer=install_packages,
            prefer_local_floor=prefer_local_floor,
        )
        if report["missing_before_install"]:
            missing_dependencies.append(import_name)
        installed_packages.extend(report["installed_packages"])
        import_reports.append(report)
    return {
        "imports": [str(name) for name in import_names],
        "python_runtime": runtime_report,
        "missing_dependencies": missing_dependencies,
        "installed_packages": installed_packages,
        "import_reports": import_reports,
    }


def _normalize_operation_runtime_name(operation: object) -> str:
    normalized = str(operation or "").strip().casefold().replace("-", "_")
    normalized = OPERATION_RUNTIME_ALIASES.get(normalized, normalized)
    if normalized not in OPERATION_RUNTIME_DOMAINS:
        raise ValueError(f"Unknown runtime operation domain: {operation!r}")
    return normalized


def get_operation_runtime_domain_contract(operation: object) -> dict[str, object]:
    normalized = _normalize_operation_runtime_name(operation)
    contract = dict(OPERATION_RUNTIME_DOMAINS[normalized])
    return {
        "schema": OPERATION_RUNTIME_DOMAIN_SCHEMA,
        "operation": normalized,
        "failure_domain": str(contract.get("failure_domain", normalized)),
        "modules": [str(value) for value in contract.get("modules", ())],
        "required_imports": [str(value) for value in contract.get("required_imports", ())],
        "optional_imports": [str(value) for value in contract.get("optional_imports", ())],
    }


def get_operation_runtime_domain_report(
    operation: object,
    *,
    repair: bool = False,
    include_optional: bool = False,
    correlation_id: str = "",
) -> dict[str, object]:
    """Check one operation lane without importing unrelated business modules."""
    started = time.perf_counter()
    contract = get_operation_runtime_domain_contract(operation)
    required_reports: list[dict[str, object]] = []
    optional_reports: list[dict[str, object]] = []

    for import_name in contract["required_imports"]:
        try:
            if repair:
                dependency_report = ensure_import_runtime(str(import_name))
                ready = dependency_report.get("ready") is True
            else:
                loaded = try_import_optional_runtime_module(
                    str(import_name),
                    refresh=True,
                    repair=False,
                )
                ready = loaded is not None
                dependency_report = {
                    "import_name": str(import_name),
                    "ready": ready,
                    "repair_attempted": False,
                    "error": "" if ready else get_last_import_error_text(str(import_name)),
                }
        except Exception as exc:
            ready = False
            dependency_report = {
                "import_name": str(import_name),
                "ready": False,
                "repair_attempted": bool(repair),
                "error_type": getattr(exc, "error_type", type(exc).__name__),
                "error": str(exc),
                "error_report": dict(getattr(exc, "report", {})),
            }
        required_reports.append(dict(dependency_report))

    if include_optional:
        for import_name in contract["optional_imports"]:
            try:
                if repair:
                    dependency_report = ensure_optional_import_runtime(str(import_name))
                    ready = dependency_report.get("ready") is True
                else:
                    loaded = try_import_optional_runtime_module(
                        str(import_name),
                        refresh=True,
                        repair=False,
                    )
                    ready = loaded is not None
                    dependency_report = {
                        "import_name": str(import_name),
                        "ready": ready,
                        "repair_attempted": False,
                        "error": "" if ready else get_last_import_error_text(str(import_name)),
                    }
            except Exception as exc:
                ready = False
                dependency_report = {
                    "import_name": str(import_name),
                    "ready": False,
                    "repair_attempted": bool(repair),
                    "error_type": getattr(exc, "error_type", type(exc).__name__),
                    "error": str(exc),
                    "error_report": dict(getattr(exc, "report", {})),
                }
            optional_reports.append(dict(dependency_report))

    required_failures = [row for row in required_reports if row.get("ready") is not True]
    optional_failures = [row for row in optional_reports if row.get("ready") is not True]
    ready = not required_failures
    degraded = ready and bool(optional_failures)
    status = "PASS" if ready and not degraded else ("DEGRADED" if ready else "FAIL")
    status_code = (
        "RUNTIME_DOMAIN_OK"
        if status == "PASS"
        else ("RUNTIME_OPTIONAL_UNAVAILABLE" if status == "DEGRADED" else "RUNTIME_REQUIRED_UNAVAILABLE")
    )
    return {
        "schema": OPERATION_RUNTIME_DOMAIN_SCHEMA,
        "ok": ready,
        "status": status,
        "contract": contract,
        "required_imports": required_reports,
        "optional_imports": optional_reports,
        "optional_checked": bool(include_optional),
        "receipt": {
            "schema": OPERATION_RUNTIME_RECEIPT_SCHEMA,
            "module": "codex_python_runtime_bootstrap",
            "operation": contract["operation"],
            "failure_domain": contract["failure_domain"],
            "isolation_scope": "current_operation_only",
            "status": status,
            "status_code": status_code,
            "retryable": bool(required_failures),
            "degraded": degraded,
            "recovery_action": "run_explicit_runtime_health"
            if required_failures
            else ("optional_acceleration_disabled" if optional_failures else "none"),
            "correlation_id": str(correlation_id or ""),
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
            "detail": ""
            if ready and not degraded
            else (
                "Required runtime dependency is unavailable in this operation domain."
                if required_failures
                else "Optional acceleration is unavailable; the operation may use its fallback."
            ),
        },
    }


def _path_is_within(path_value: str | Path, root_value: str | Path) -> bool:
    try:
        path_text = os.path.normcase(os.path.abspath(str(path_value)))
        root_text = os.path.normcase(os.path.abspath(str(root_value)))
        return os.path.commonpath((path_text, root_text)) == root_text
    except (OSError, ValueError):
        return False


def _module_origin_paths(import_name: str) -> list[Path]:
    module = sys.modules.get(import_name)
    if module is None:
        return []
    origin_paths: list[Path] = []
    module_file = str(getattr(module, "__file__", "") or "").strip()
    if module_file:
        origin_paths.append(Path(module_file))
    module_path = getattr(module, "__path__", None)
    if module_path is not None:
        try:
            origin_paths.extend(Path(path_text) for path_text in module_path)
        except Exception:
            pass
    unique: list[Path] = []
    seen: set[str] = set()
    for origin_path in origin_paths:
        normalized = os.path.normcase(os.path.abspath(str(origin_path)))
        if normalized not in seen:
            seen.add(normalized)
            unique.append(origin_path)
    return unique


def _lane_contains_import_payload(lane_dir: Path, import_name: str) -> bool:
    if lane_dir.is_dir() is not True:
        return False
    direct_candidates = (
        lane_dir / import_name,
        lane_dir / f"{import_name}.py",
        lane_dir / f"{import_name}.pyc",
    )
    if any(candidate.exists() for candidate in direct_candidates):
        return True
    normalized_name = import_name.casefold().replace("_", "-")
    distribution_name = str(IMPORT_POLICY.get(import_name, {}).get("distribution_name", import_name) or import_name)
    normalized_distribution = distribution_name.casefold().replace("_", "-")
    try:
        for candidate in lane_dir.iterdir():
            candidate_name = candidate.name.casefold().replace("_", "-")
            if candidate_name.startswith(normalized_name + "-") or candidate_name.startswith(normalized_distribution + "-"):
                return True
    except OSError:
        return False
    return False


def _scan_lane_abi_tags(lane_dir: Path, *, version_info: object | None = None) -> dict[str, object]:
    expected_tag = _python_tag(version_info)
    present_tags: set[str] = set()
    mismatched_files: list[str] = []
    if lane_dir.is_dir():
        try:
            candidates = lane_dir.rglob("*")
            for candidate in candidates:
                if candidate.is_file() is not True or candidate.suffix.lower() not in {".pyd", ".so", ".whl"}:
                    continue
                file_tags = {
                    "cp" + match.group(1)
                    for match in re.finditer(r"(?:^|[._-])cp(\d{2,3})(?:[._-]|$)", candidate.name.casefold())
                }
                present_tags.update(file_tags)
                if file_tags and expected_tag not in file_tags:
                    mismatched_files.append(str(candidate))
        except OSError:
            pass
    return {
        "expected_tag": expected_tag,
        "present_tags": sorted(present_tags),
        "mismatched_files": mismatched_files,
        "ready": len(mismatched_files) == 0,
    }


def _probe_runtime_lane_in_process(
    lane_dir: str | Path,
    *,
    include_packaged: bool,
    import_names: tuple[str, ...] | list[str] = REPAIR_HEALTH_IMPORTS,
) -> dict[str, object]:
    lane_path = Path(lane_dir)
    requested_imports = tuple(dict.fromkeys(str(name) for name in import_names))
    allowed_roots = [lane_path]
    if include_packaged:
        allowed_roots.append(PACKAGED_VENDOR_PY_DIR)
    abi_report = _scan_lane_abi_tags(lane_path)
    import_reports: list[dict[str, object]] = []
    with _vendor_path_context(lane_path, include_packaged=include_packaged):
        for import_name in requested_imports:
            _clear_import_tree(import_name)
            importlib.invalidate_caches()
            checker_ready = _default_import_checker(import_name) is True
            origin_paths = _module_origin_paths(import_name) if checker_ready else []
            origin_allowed = bool(origin_paths) and all(
                any(_path_is_within(origin_path, root_path) for root_path in allowed_roots)
                for origin_path in origin_paths
            )
            ready = checker_ready and origin_allowed
            error_text = get_last_import_error_text(import_name)
            if checker_ready and origin_allowed is not True:
                error_text = "Import resolved outside the probed vendor lane(s)."
            import_reports.append(
                {
                    "import_name": import_name,
                    "ready": ready,
                    "checker_ready": checker_ready,
                    "origin_allowed": origin_allowed,
                    "origins": [str(path_value) for path_value in origin_paths],
                    "present_in_lane": _lane_contains_import_payload(lane_path, import_name),
                    "error": error_text,
                }
            )

    path_exists = lane_path.exists()
    path_is_dir = lane_path.is_dir()
    if path_exists and path_is_dir is not True:
        lane_state = "not-directory"
    elif path_is_dir and any(lane_path.iterdir()):
        lane_state = "present"
    elif path_is_dir:
        lane_state = "empty"
    else:
        lane_state = "absent"
    requested_ready = all(report["ready"] is True for report in import_reports)
    required_reports = [
        report
        for report in import_reports
        if report["import_name"] in REPAIR_REQUIRED_IMPORTS
    ]
    export_ready = (
        all(report["ready"] is True for report in required_reports)
        if required_reports
        else requested_ready
    )
    broken_present = any(
        report["present_in_lane"] is True and report["ready"] is not True
        for report in import_reports
    )
    if lane_state == "not-directory":
        classification = "corrupt"
    elif abi_report["ready"] is not True:
        classification = "abi-mismatch"
    elif broken_present:
        classification = "import-error"
    elif requested_ready:
        classification = "healthy" if lane_state != "absent" else "healthy-packaged"
    elif lane_state in {"absent", "empty"}:
        classification = lane_state
    elif any(report["ready"] is True for report in import_reports):
        classification = "partial"
    else:
        classification = "missing"
    return {
        "classification": classification,
        "lane_state": lane_state,
        "lane_dir": str(lane_path),
        "include_packaged": include_packaged,
        "python_tag": _python_tag(),
        "python_executable": sys.executable,
        "requested_imports": list(requested_imports),
        "ready": requested_ready,
        "export_ready": export_ready,
        "abi": abi_report,
        "imports": import_reports,
        "bridge_contract": get_export_bridge_contract_report(),
    }


def _decode_json_subprocess_output(stdout_text: str) -> dict[str, object] | None:
    normalized = str(stdout_text or "").strip()
    if normalized == "":
        return None
    try:
        payload = json.loads(normalized)
        return payload if isinstance(payload, dict) else None
    except Exception:
        pass
    decoder = json.JSONDecoder()
    for start_index in reversed([index for index, char in enumerate(normalized) if char == "{"]):
        try:
            payload, end_index = decoder.raw_decode(normalized[start_index:])
        except Exception:
            continue
        if isinstance(payload, dict) and normalized[start_index + end_index :].strip() == "":
            return payload
    return None


def _write_json_stdout(payload: dict[str, object]) -> None:
    payload_bytes = (
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8", errors="backslashreplace")
        + b"\n"
    )
    stdout_buffer = getattr(sys.stdout, "buffer", None)
    if stdout_buffer is not None:
        stdout_buffer.write(payload_bytes)
        stdout_buffer.flush()
        return
    print(payload_bytes.decode("utf-8"))


def _run_fresh_lane_probe(
    lane_dir: str | Path,
    *,
    include_packaged: bool,
    import_names: tuple[str, ...] | list[str] = REPAIR_HEALTH_IMPORTS,
    timeout_seconds: float = 90.0,
) -> dict[str, object]:
    command = [
        sys.executable,
        "-I",
        "-B",
        str(Path(__file__).resolve()),
        "--probe-runtime-lane",
        str(Path(lane_dir)),
    ]
    for import_name in tuple(dict.fromkeys(str(name) for name in import_names)):
        command.extend(("--probe-import", import_name))
    if include_packaged:
        command.append("--probe-include-packaged")
    command.append("--json")
    child_env = _isolated_python_child_environment()
    try:
        completed = _run_hidden_subprocess(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(5.0, float(timeout_seconds)),
            env=child_env,
        )
    except Exception as exc:
        return {
            "classification": "probe-failed",
            "ready": False,
            "export_ready": False,
            "python_tag": _python_tag(),
            "lane_dir": str(lane_dir),
            "error": str(exc),
        }
    payload = _decode_json_subprocess_output(completed.stdout)
    if (
        completed.returncode != 0
        or not isinstance(payload, dict)
        or payload.get("python_tag") != _python_tag()
    ):
        return {
            "classification": "probe-failed",
            "ready": False,
            "export_ready": False,
            "python_tag": _python_tag(),
            "lane_dir": str(lane_dir),
            "child_returncode": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
            "error": "Fresh child runtime probe failed or returned the wrong Python ABI.",
        }
    payload["child_returncode"] = completed.returncode
    payload["fresh_child_same_abi"] = True
    return payload


def _remove_staged_import_artifacts(stage_dir: Path, import_name: str) -> None:
    distribution_name = str(IMPORT_POLICY.get(import_name, {}).get("distribution_name", import_name) or import_name)
    normalized_prefixes = {
        import_name.casefold().replace("_", "-"),
        distribution_name.casefold().replace("_", "-"),
    }
    if stage_dir.is_dir() is not True:
        return
    artifact_patterns = PACKAGED_IMPORT_ARTIFACT_PATTERNS.get(import_name, ())
    explicit_matches = {
        path_value.name.casefold()
        for artifact_pattern in artifact_patterns
        for path_value in stage_dir.glob(artifact_pattern)
    }
    for candidate in list(stage_dir.iterdir()):
        normalized_name = candidate.name.casefold().replace("_", "-")
        matches = candidate.name.casefold() in {
            import_name.casefold(),
            f"{import_name.casefold()}.py",
            f"{import_name.casefold()}.pyc",
        } or candidate.name.casefold() in explicit_matches or any(
            normalized_name.startswith(prefix + "-")
            for prefix in normalized_prefixes
        )
        if not matches:
            continue
        if candidate.is_dir() and candidate.is_symlink() is not True:
            shutil.rmtree(candidate, ignore_errors=True)
        else:
            candidate.unlink(missing_ok=True)


def _iter_repair_dependency_closure(import_name: str) -> tuple[str, ...]:
    ordered: list[str] = []

    def add_dependencies(parent_name: str) -> None:
        for dependency_name in REPAIR_IMPORT_DEPENDENCY_CLOSURE.get(parent_name, ()):
            if dependency_name in ordered:
                continue
            add_dependencies(dependency_name)
            ordered.append(dependency_name)

    add_dependencies(import_name)
    return tuple(ordered)


def _find_packaged_import_source_root(
    import_name: str,
    *,
    version_info: object | None = None,
) -> Path | None:
    python_tag = _python_tag(version_info)
    exact_abi_root = PACKAGED_VENDOR_PY_ROOT_DIR / python_tag
    for vendor_root in (exact_abi_root,):
        package_dir = vendor_root / import_name
        if package_dir.is_dir() is not True:
            continue
        if _scan_lane_abi_tags(package_dir, version_info=version_info).get("ready") is True:
            return vendor_root
    return None


def _copy_packaged_import_artifacts(import_name: str, target_dir: Path) -> list[str]:
    source_root = _find_packaged_import_source_root(import_name)
    if source_root is None:
        raise RuntimeError(
            f"No packaged {_python_tag()} payload is available for repair dependency {import_name}."
        )
    artifact_patterns = PACKAGED_IMPORT_ARTIFACT_PATTERNS.get(
        import_name,
        (import_name, f"{import_name}-*.dist-info"),
    )
    source_paths: list[Path] = []
    for artifact_pattern in artifact_patterns:
        source_paths.extend(sorted(source_root.glob(artifact_pattern)))
    unique_sources: list[Path] = []
    seen: set[str] = set()
    for source_path in source_paths:
        normalized = os.path.normcase(os.path.abspath(str(source_path)))
        if normalized in seen:
            continue
        seen.add(normalized)
        unique_sources.append(source_path)
    if not unique_sources:
        raise RuntimeError(
            f"Packaged repair dependency {import_name} has no copyable artifacts under {source_root}."
        )
    target_dir.mkdir(parents=True, exist_ok=True)
    copied_paths: list[str] = []
    for source_path in unique_sources:
        target_path = target_dir / source_path.name
        if target_path.exists():
            if target_path.is_dir() and target_path.is_symlink() is not True:
                shutil.rmtree(target_path, ignore_errors=True)
            else:
                target_path.unlink(missing_ok=True)
        if source_path.is_dir():
            shutil.copytree(source_path, target_path)
        else:
            shutil.copy2(source_path, target_path)
        copied_paths.append(str(target_path))
    return copied_paths


def _local_candidate_provenance(candidate: str) -> dict[str, object]:
    candidate_path = Path(candidate)
    hashes: list[dict[str, str]] = []
    if candidate_path.is_file():
        hashes.append({"path": str(candidate_path), "sha256": _sha256_file(candidate_path, use_cache=False)})
    elif candidate_path.is_dir():
        for artifact_path in sorted(candidate_path.rglob("*")):
            if artifact_path.is_file() and artifact_path.suffix.lower() in {".pyd", ".whl"}:
                hashes.append({"path": str(artifact_path), "sha256": _sha256_file(artifact_path, use_cache=False)})
    artifact_report = get_candidate_artifact_report(
        _find_import_name_for_candidate(candidate) or "unknown",
        candidate,
    )
    return {
        "source": "offline-local",
        "candidate": str(candidate_path),
        "artifact_hashes": hashes,
        "candidate_version": artifact_report.get("version"),
        "candidate_source": artifact_report.get("source"),
        "candidate_fingerprint": artifact_report.get("fingerprint"),
        "candidate_block_key": artifact_report.get("block_key"),
    }


def _stage_import_from_policy(
    import_name: str,
    stage_dir: Path,
    *,
    fixed_only: bool,
) -> dict[str, object]:
    policy = dict(IMPORT_POLICY.get(import_name, {}))
    attempts: list[dict[str, object]] = []
    candidates = _get_install_candidates(import_name, fixed_only=fixed_only)
    for candidate in candidates:
        _remove_staged_import_artifacts(stage_dir, import_name)
        provenance = _local_candidate_provenance(candidate)
        try:
            _default_installer((candidate,), target_dir=stage_dir)
        except Exception as exc:
            blocked_artifact = _record_failed_candidate_artifact(
                import_name,
                candidate,
                "Install/build failed: " + str(exc),
            )
            attempts.append(
                {
                    **provenance,
                    "status": "install-error",
                    "error": str(exc),
                    "blocked_artifact": blocked_artifact,
                }
            )
            continue
        probe = _run_fresh_lane_probe(
            stage_dir,
            include_packaged=False,
            import_names=(import_name,),
        )
        attempt = {
            **provenance,
            "status": "ready" if probe.get("ready") is True else "health-error",
            "probe": probe,
        }
        attempts.append(attempt)
        if probe.get("ready") is True:
            return {
                "import_name": import_name,
                "policy": policy,
                "source": "offline-local",
                "installed_packages": [candidate],
                "copied_artifacts": [],
                "provenance": provenance,
                "attempts": attempts,
            }
        attempt["blocked_artifact"] = _record_failed_candidate_artifact(
            import_name,
            candidate,
            "Fresh-child health check failed: " + json.dumps(probe, ensure_ascii=False),
        )

    packaged_root = _find_packaged_import_source_root(import_name)
    if packaged_root is not None:
        _remove_staged_import_artifacts(stage_dir, import_name)
        try:
            copied_artifacts = _copy_packaged_import_artifacts(import_name, stage_dir)
            probe = _run_fresh_lane_probe(
                stage_dir,
                include_packaged=False,
                import_names=(import_name,),
            )
            packaged_attempt = {
                "source": "offline-local",
                "packaged_abi_root": str(packaged_root),
                "copied_artifacts": copied_artifacts,
                "status": "ready" if probe.get("ready") is True else "health-error",
                "probe": probe,
            }
            attempts.append(packaged_attempt)
            if probe.get("ready") is True:
                return {
                    "import_name": import_name,
                    "policy": policy,
                    "source": "offline-local",
                    "installed_packages": [],
                    "copied_artifacts": copied_artifacts,
                    "provenance": packaged_attempt,
                    "attempts": attempts,
                }
        except Exception as exc:
            attempts.append(
                {
                    "source": "offline-local",
                    "packaged_abi_root": str(packaged_root),
                    "status": "copy-error",
                    "error": str(exc),
                }
            )

    cached_upgrade_state = _get_upgrade_check_state(import_name)
    online_retry_blocked = (
        str(cached_upgrade_state.get("status", "") or "")
        in {"blocked-artifact", "rejected-health", "rejected-commit"}
        and _upgrade_check_is_due(import_name) is not True
    )
    if online_retry_blocked:
        attempts.append(
            {
                "source": "online-repair",
                "status": "ttl-blocked-artifact",
                "cached_upgrade_state": cached_upgrade_state,
            }
        )
    if _online_repair_allowed(import_name) and online_retry_blocked is not True:
        _remove_staged_import_artifacts(stage_dir, import_name)
        try:
            online_report = _install_online_import_to_stage(import_name, stage_dir)
            downloads = [value for value in online_report.get("downloads", []) if isinstance(value, dict)]
            if downloads and _online_artifact_is_blocked(import_name, downloads[0]):
                raise OnlineDependencyRepairError(
                    f"Online candidate for {import_name} is blocked by exact artifact fingerprint.",
                    {
                        **online_report,
                        "status": "blocked-artifact",
                        "error": "Exact online wheel fingerprint was rejected by an earlier health check.",
                    },
                )
            probe = _run_fresh_lane_probe(
                stage_dir,
                include_packaged=False,
                import_names=(import_name,),
            )
            online_attempt = {
                **online_report,
                "status": "ready" if probe.get("ready") is True else "health-error",
                "probe": probe,
            }
            attempts.append(online_attempt)
            if probe.get("ready") is True:
                return {
                    "import_name": import_name,
                    "policy": policy,
                    "source": "online-repair",
                    "installed_packages": [str(policy.get("online_spec", import_name))],
                    "copied_artifacts": [],
                    "provenance": online_report,
                    "attempts": attempts,
                }
            if downloads:
                download = downloads[0]
                online_attempt["blocked_artifact"] = _record_failed_online_artifact(
                    import_name,
                    version=str(download.get("version", "") or "unknown"),
                    source_url=str(download.get("url", "") or ""),
                    sha256=str(download.get("sha256", "") or ""),
                    reason_text="Fresh-child health check failed: " + json.dumps(probe, ensure_ascii=False),
                )
        except OnlineDependencyRepairError as exc:
            attempts.append({**exc.report, "status": "install-error", "error": str(exc)})

    detail = json.dumps(attempts, ensure_ascii=False)
    if str(policy.get("repair_mode", "") or "") == "pinned-local-only":
        raise DependencyBundleBrokenError(
            f"Pinned dependency bundle cannot provide healthy {import_name} for {_python_tag()}: {detail}",
            {
                "import_name": import_name,
                "python_tag": _python_tag(),
                "policy": policy,
                "attempts": attempts,
                "dependency_contract": get_dependency_bundle_contract_report(include_runtime_health=False),
            },
        )
    raise OnlineDependencyRepairError(
        f"Local and online repair could not provide healthy {import_name} for {_python_tag()}.",
        {
            "import_name": import_name,
            "python_tag": _python_tag(),
            "policy": policy,
            "attempts": attempts,
        },
    )


def _rebuild_export_runtime_stage(stage_dir: Path) -> dict[str, object]:
    stage_dir.mkdir(parents=True, exist_ok=True)
    installed_packages: list[str] = []
    copied_dependency_artifacts: list[str] = []
    attempts: list[dict[str, object]] = []
    provenance: list[dict[str, object]] = []
    for import_name in REPAIR_REQUIRED_IMPORTS:
        import_report = _stage_import_from_policy(import_name, stage_dir, fixed_only=True)
        installed_packages.extend(str(value) for value in import_report.get("installed_packages", []))
        copied_dependency_artifacts.extend(str(value) for value in import_report.get("copied_artifacts", []))
        attempts.extend(dict(value) for value in import_report.get("attempts", []) if isinstance(value, dict))
        provenance.append(dict(import_report.get("provenance", {})))

    optional_reports: list[dict[str, object]] = []
    for import_name in REPAIR_HEALTH_IMPORTS:
        if import_name in REPAIR_REQUIRED_IMPORTS:
            continue
        try:
            optional_report = _stage_import_from_policy(import_name, stage_dir, fixed_only=False)
        except Exception as exc:
            optional_reports.append(
                {
                    "import_name": import_name,
                    "ready": False,
                    "error_type": getattr(exc, "error_type", type(exc).__name__),
                    "error": str(exc),
                    "report": dict(getattr(exc, "report", {})),
                }
            )
            continue
        optional_reports.append({"import_name": import_name, "ready": True, **optional_report})
    final_probe = _run_fresh_lane_probe(
        stage_dir,
        include_packaged=False,
        import_names=REPAIR_HEALTH_IMPORTS,
    )
    if final_probe.get("export_ready") is not True:
        raise RuntimeError(
            "Fresh same-ABI child rejected the rebuilt export runtime staging lane: "
            + json.dumps(final_probe, ensure_ascii=False)
        )
    return {
        "installed_packages": installed_packages,
        "copied_dependency_artifacts": copied_dependency_artifacts,
        "attempts": attempts,
        "provenance": provenance,
        "optional_reports": optional_reports,
        "fresh_child_probe": final_probe,
    }


def _local_lane_has_bad_payload(
    local_probe: dict[str, object],
    *,
    import_names: tuple[str, ...] | list[str] = REPAIR_REQUIRED_IMPORTS,
) -> bool:
    if local_probe.get("classification") in {"corrupt", "probe-failed"}:
        return True
    import_reports = local_probe.get("imports", [])
    if not isinstance(import_reports, list):
        return True
    requested_names = set(str(value) for value in import_names)
    return any(
        isinstance(report, dict)
        and report.get("import_name") in requested_names
        and report.get("present_in_lane") is True
        and report.get("ready") is not True
        for report in import_reports
    )


def _commit_runtime_lane(
    stage_dir: Path,
    target_lane: Path,
    *,
    quarantine_root: Path,
) -> Path | None:
    target_lane.parent.mkdir(parents=True, exist_ok=True)
    quarantine_path: Path | None = None
    if target_lane.exists():
        quarantine_root.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        quarantine_path = quarantine_root / (
            f"{_python_tag()}-{timestamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        os.replace(target_lane, quarantine_path)
    try:
        os.replace(stage_dir, target_lane)
    except Exception:
        if quarantine_path is not None and quarantine_path.exists() and target_lane.exists() is not True:
            os.replace(quarantine_path, target_lane)
        raise
    return quarantine_path


def _remove_runtime_lane_path(path_value: Path) -> None:
    if path_value.is_dir():
        shutil.rmtree(path_value, ignore_errors=True)
    elif path_value.exists():
        path_value.unlink(missing_ok=True)


def _copy_runtime_lane_atomic(
    source_lane: Path,
    target_lane: Path,
    *,
    previous_active_quarantine: Path | None = None,
) -> dict[str, object]:
    if source_lane.is_dir() is not True:
        raise RuntimeError(f"Runtime recovery source is not a directory: {source_lane}")
    target_lane.parent.mkdir(parents=True, exist_ok=True)
    copy_stage = Path(
        tempfile.mkdtemp(
            prefix=f".{target_lane.name}-restore-{os.getpid()}-",
            dir=target_lane.parent,
        )
    )
    managed_previous = previous_active_quarantine
    committed = False
    rejected_target: Path | None = None
    if target_lane.exists():
        if managed_previous is not None:
            raise RuntimeError("Runtime recovery received both an active target and an explicit previous quarantine.")
        managed_previous = _quarantine_runtime_lane(target_lane, reason_label="pre-recovery-active")
    try:
        shutil.copytree(source_lane, copy_stage, dirs_exist_ok=True)
        source_probe = _run_fresh_lane_probe(
            copy_stage,
            include_packaged=False,
            import_names=REPAIR_REQUIRED_IMPORTS,
        )
        if source_probe.get("export_ready") is not True:
            raise RuntimeError(
                "Runtime recovery copy failed its fresh-child probe: "
                + json.dumps(source_probe, ensure_ascii=False)
            )
        os.replace(copy_stage, target_lane)
        committed = True
        committed_probe = _run_fresh_lane_probe(
            target_lane,
            include_packaged=False,
            import_names=REPAIR_REQUIRED_IMPORTS,
        )
        if committed_probe.get("export_ready") is not True:
            raise RuntimeError(
                "Committed runtime recovery copy failed its fresh-child probe: "
                + json.dumps(committed_probe, ensure_ascii=False)
            )
        return {
            "source": str(source_lane),
            "target": str(target_lane),
            "source_probe": source_probe,
            "committed_probe": committed_probe,
            "previous_active_quarantine": str(managed_previous) if managed_previous is not None else None,
        }
    except Exception:
        if committed and target_lane.exists():
            try:
                rejected_target = _quarantine_runtime_lane(
                    target_lane,
                    reason_label="rejected-runtime-recovery",
                )
            except Exception:
                _remove_runtime_lane_path(target_lane)
        if target_lane.exists():
            _remove_runtime_lane_path(target_lane)
        if managed_previous is not None and managed_previous.exists():
            try:
                os.replace(managed_previous, target_lane)
                configure_vendor_paths()
            except Exception as restore_exc:
                raise RuntimeError(
                    "Runtime recovery failed and its previous active lane could not be restored atomically: "
                    + str(restore_exc)
                    + " | rejected_target="
                    + str(rejected_target)
                ) from restore_exc
        if managed_previous is None and target_lane.exists():
            _remove_runtime_lane_path(target_lane)
        raise
    finally:
        if copy_stage.exists():
            shutil.rmtree(copy_stage, ignore_errors=True)


def _refresh_last_known_good(source_lane: Path) -> dict[str, object]:
    if source_lane.is_dir() is not True:
        raise RuntimeError(f"Cannot snapshot missing runtime lane: {source_lane}")
    RUNTIME_LAST_KNOWN_GOOD_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    snapshot_stage = Path(
        tempfile.mkdtemp(
            prefix=f".{_python_tag()}-lkg-{os.getpid()}-",
            dir=RUNTIME_LAST_KNOWN_GOOD_ROOT_DIR,
        )
    )
    previous_snapshot: Path | None = None
    try:
        shutil.copytree(source_lane, snapshot_stage, dirs_exist_ok=True)
        stage_probe = _run_fresh_lane_probe(
            snapshot_stage,
            include_packaged=False,
            import_names=REPAIR_REQUIRED_IMPORTS,
        )
        if stage_probe.get("export_ready") is not True:
            raise RuntimeError(
                "Last-known-good candidate failed its fresh-child probe: "
                + json.dumps(stage_probe, ensure_ascii=False)
            )
        if RUNTIME_LAST_KNOWN_GOOD_DIR.exists():
            previous_snapshot = RUNTIME_LAST_KNOWN_GOOD_ROOT_DIR / (
                f".{_python_tag()}-previous-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            )
            os.replace(RUNTIME_LAST_KNOWN_GOOD_DIR, previous_snapshot)
        try:
            os.replace(snapshot_stage, RUNTIME_LAST_KNOWN_GOOD_DIR)
        except Exception:
            if previous_snapshot is not None and previous_snapshot.exists():
                os.replace(previous_snapshot, RUNTIME_LAST_KNOWN_GOOD_DIR)
            raise
        committed_probe = _run_fresh_lane_probe(
            RUNTIME_LAST_KNOWN_GOOD_DIR,
            include_packaged=False,
            import_names=REPAIR_REQUIRED_IMPORTS,
        )
        if committed_probe.get("export_ready") is not True:
            rejected_snapshot = RUNTIME_LAST_KNOWN_GOOD_ROOT_DIR / (
                f".{_python_tag()}-rejected-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            )
            os.replace(RUNTIME_LAST_KNOWN_GOOD_DIR, rejected_snapshot)
            if previous_snapshot is not None and previous_snapshot.exists():
                os.replace(previous_snapshot, RUNTIME_LAST_KNOWN_GOOD_DIR)
            shutil.rmtree(rejected_snapshot, ignore_errors=True)
            raise RuntimeError(
                "Committed last-known-good snapshot failed its fresh-child probe: "
                + json.dumps(committed_probe, ensure_ascii=False)
            )
        if previous_snapshot is not None and previous_snapshot.exists():
            shutil.rmtree(previous_snapshot, ignore_errors=True)
        return {
            "path": str(RUNTIME_LAST_KNOWN_GOOD_DIR),
            "fresh_child_probe": committed_probe,
            "status": "refreshed",
        }
    finally:
        if snapshot_stage.exists():
            shutil.rmtree(snapshot_stage, ignore_errors=True)


def _restore_previous_runtime_lane(target_lane: Path, previous_lane: Path) -> dict[str, object] | None:
    if previous_lane.is_dir() is not True:
        return None
    rejected_lane = _quarantine_runtime_lane(target_lane, reason_label="rejected-commit") if target_lane.exists() else None
    os.replace(previous_lane, target_lane)
    configure_vendor_paths()
    restored_probe = _run_fresh_lane_probe(
        target_lane,
        include_packaged=True,
        import_names=REPAIR_REQUIRED_IMPORTS,
    )
    if restored_probe.get("export_ready") is not True:
        _quarantine_runtime_lane(target_lane, reason_label="rejected-previous-lane")
        return None
    return {
        "action": "restored-previous-lane",
        "source": str(previous_lane),
        "rejected_lane": str(rejected_lane) if rejected_lane is not None else None,
        "fresh_child_probe": restored_probe,
    }


def _transactional_repair_import(import_name: str, *, fixed_only: bool) -> dict[str, object]:
    runtime_report = validate_python_runtime()
    if runtime_report.get("native_abi_supported") is not True:
        raise RuntimeError(f"Transactional dependency repair requires a supported ABI: {_python_tag()}")
    with _runtime_install_lock() as lock_report:
        RUNTIME_STAGING_ROOT_DIR.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(
            tempfile.mkdtemp(
                prefix=f"{_python_tag()}-{import_name}-{os.getpid()}-",
                dir=RUNTIME_STAGING_ROOT_DIR,
            )
        )
        previous_lane: Path | None = None
        committed = False
        try:
            if VENDOR_PY_DIR.is_dir():
                shutil.copytree(VENDOR_PY_DIR, stage_dir, dirs_exist_ok=True)
            import_reports: list[dict[str, object]] = []
            installed_packages: list[str] = []
            requested_imports = list(_expand_policy_import_dependencies((import_name,)))
            if import_name == "numpy" and "ufbx" not in requested_imports:
                requested_imports.append("ufbx")
            for dependency_name in requested_imports:
                dependency_report = _stage_import_from_policy(
                    dependency_name,
                    stage_dir,
                    fixed_only=True if dependency_name == "ufbx" else fixed_only,
                )
                import_reports.append(dependency_report)
                installed_packages.extend(str(value) for value in dependency_report.get("installed_packages", []))
            stage_probe = _run_fresh_lane_probe(
                stage_dir,
                include_packaged=False,
                import_names=requested_imports,
            )
            if stage_probe.get("ready") is not True:
                raise RuntimeError(
                    "Transactional dependency staging probe failed: "
                    + json.dumps(stage_probe, ensure_ascii=False)
                )
            previous_lane = _commit_runtime_lane(
                stage_dir,
                VENDOR_PY_DIR,
                quarantine_root=RUNTIME_QUARANTINE_ROOT_DIR,
            )
            committed = True
            configure_vendor_paths()
            committed_probe = _run_fresh_lane_probe(
                VENDOR_PY_DIR,
                include_packaged=False,
                import_names=requested_imports,
            )
            if committed_probe.get("ready") is not True:
                restored = (
                    _restore_previous_runtime_lane(VENDOR_PY_DIR, previous_lane)
                    if previous_lane is not None
                    else None
                )
                if restored is None and VENDOR_PY_DIR.exists():
                    _quarantine_runtime_lane(VENDOR_PY_DIR, reason_label="rejected-transaction")
                raise RuntimeError(
                    "Committed transactional dependency repair failed; previous lane restoration="
                    + json.dumps(restored, ensure_ascii=False)
                    + " probe="
                    + json.dumps(committed_probe, ensure_ascii=False)
                )
            lkg_report: dict[str, object] | None = None
            lkg_warning = ""
            full_export_probe = _run_fresh_lane_probe(
                VENDOR_PY_DIR,
                include_packaged=False,
                import_names=REPAIR_REQUIRED_IMPORTS,
            )
            if full_export_probe.get("export_ready") is True:
                try:
                    lkg_report = _refresh_last_known_good(VENDOR_PY_DIR)
                except Exception as exc:
                    lkg_warning = str(exc)
            for dependency_name in requested_imports:
                _clear_import_tree(dependency_name)
            return {
                "action": "transactional-repair",
                "import_name": import_name,
                "python_runtime": runtime_report,
                "lock": lock_report,
                "installed_packages": installed_packages,
                "import_reports": import_reports,
                "stage_probe": stage_probe,
                "committed_probe": committed_probe,
                "quarantine_path": str(previous_lane) if previous_lane is not None else None,
                "last_known_good": lkg_report,
                "last_known_good_warning": lkg_warning,
            }
        finally:
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)


def _spawn_background_upgrade_worker(
    import_name: str,
    *,
    token: str,
    current_version: str | None,
    log_path: Path,
) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-I",
        "-B",
        str(Path(__file__).resolve()),
        "--background-upgrade-import",
        import_name,
        "--background-upgrade-token",
        token,
        "--background-upgrade-current-version",
        str(current_version or ""),
        "--json",
    ]
    child_env = _isolated_python_child_environment()
    popen_kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
        "cwd": str(BASE_DIR),
        "env": child_env,
        "close_fds": True,
    }
    if os.name == "nt":
        creationflags = (
            int(getattr(subprocess, "DETACHED_PROCESS", 0))
            | int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            | int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        )
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0))
        startupinfo.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
        popen_kwargs["creationflags"] = creationflags
        popen_kwargs["startupinfo"] = startupinfo
    else:
        popen_kwargs["start_new_session"] = True
    with log_path.open("ab", buffering=0) as log_stream:
        process = subprocess.Popen(command, stdout=log_stream, **popen_kwargs)
    return int(process.pid)


def _schedule_online_upgrade(
    import_name: str,
    *,
    current_version: str | None,
) -> dict[str, object]:
    policy = dict(IMPORT_POLICY.get(import_name, {}))
    if policy.get("allow_upgrade") is not True or _online_repair_allowed(import_name) is not True:
        return {"status": "disabled", "import_name": import_name, "policy": policy}
    started = time.perf_counter()
    try:
        reservation = _reserve_background_upgrade(import_name, current_version=current_version)
    except RuntimeInstallLockTimeout as exc:
        return {
            "status": "schedule-deferred-lock",
            "import_name": import_name,
            "error": str(exc),
            "lock": dict(exc.report),
            "elapsed_seconds": round(time.perf_counter() - started, 6),
        }
    if reservation.get("reserved") is not True:
        return {
            "status": str(reservation.get("status", "ttl-cache") or "ttl-cache"),
            "import_name": import_name,
            "state": dict(reservation.get("state", {})),
            "elapsed_seconds": round(time.perf_counter() - started, 6),
        }
    state = dict(reservation.get("state", {}))
    token = str(state.get("scheduled_token", "") or "")
    log_path = Path(str(state.get("log_path", "") or ""))
    try:
        worker_pid = _spawn_background_upgrade_worker(
            import_name,
            token=token,
            current_version=current_version,
            log_path=log_path,
        )
    except Exception as exc:
        failed_state = _mark_background_upgrade_launch_failed(import_name, token, str(exc))
        return {
            "status": "schedule-failed",
            "import_name": import_name,
            "error": str(exc),
            "state": failed_state,
            "elapsed_seconds": round(time.perf_counter() - started, 6),
        }
    _update_background_upgrade_worker_pid(import_name, token, worker_pid)
    return {
        "status": "scheduled",
        "import_name": import_name,
        "worker_pid": worker_pid,
        "scheduled_token": token,
        "result_path": state.get("result_path"),
        "log_path": state.get("log_path"),
        "elapsed_seconds": round(time.perf_counter() - started, 6),
    }


def _run_background_upgrade_worker(
    import_name: str,
    *,
    token: str,
    current_version: str | None,
) -> dict[str, object]:
    claim = _claim_background_upgrade_worker(import_name, token)
    if claim.get("claimed") is not True:
        return {
            "status": "worker-not-claimed",
            "import_name": import_name,
            "claim": claim,
        }
    worker_report: dict[str, object]
    try:
        test_delay_text = str(os.environ.get(BACKGROUND_UPGRADE_TEST_DELAY_ENV, "") or "").strip()
        test_status = str(os.environ.get(BACKGROUND_UPGRADE_TEST_STATUS_ENV, "") or "").strip()
        if test_delay_text or test_status:
            if test_delay_text:
                time.sleep(max(0.0, float(test_delay_text)))
            worker_report = {
                "status": test_status or "test-worker-complete",
                "import_name": import_name,
                "test_worker": True,
            }
            _record_upgrade_check_state(
                import_name,
                status=str(worker_report["status"]),
                report=worker_report,
            )
        else:
            worker_report = _maybe_upgrade_online_import(
                import_name,
                current_version=current_version,
                bypass_ttl=True,
            )
    except Exception as exc:
        worker_report = {
            "status": "worker-error",
            "import_name": import_name,
            "error_type": getattr(exc, "error_type", type(exc).__name__),
            "error": str(exc),
            "error_report": dict(getattr(exc, "report", {})),
        }
        _record_upgrade_check_state(
            import_name,
            status="worker-error",
            report=worker_report,
            error=str(exc),
            ttl_seconds=FAILED_UPGRADE_CHECK_TTL_SECONDS,
        )
    completed_state = _complete_background_upgrade_worker(import_name, token, worker_report)
    result_payload = {
        "status": str(worker_report.get("status", "worker-complete") or "worker-complete"),
        "import_name": import_name,
        "python_tag": _python_tag(),
        "worker_pid": os.getpid(),
        "scheduled_token": token,
        "worker_report": worker_report,
        "state": completed_state,
    }
    result_path_text = str(completed_state.get("result_path", "") or "")
    keep_paths = tuple(
        Path(path_text)
        for path_text in (
            result_path_text,
            str(completed_state.get("log_path", "") or ""),
        )
        if path_text
    )
    if result_path_text:
        _atomic_write_runtime_bytes(
            Path(result_path_text),
            json.dumps(result_payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    result_payload["artifact_cleanup"] = _cleanup_background_upgrade_artifacts(
        keep_paths=keep_paths,
    )
    if result_path_text:
        _atomic_write_runtime_bytes(
            Path(result_path_text),
            json.dumps(result_payload, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    return result_payload


def _maybe_upgrade_online_import(
    import_name: str,
    *,
    current_version: str | None,
    bypass_ttl: bool = False,
) -> dict[str, object]:
    policy = dict(IMPORT_POLICY.get(import_name, {}))
    if policy.get("allow_upgrade") is not True or _online_repair_allowed(import_name) is not True:
        return {"status": "disabled", "import_name": import_name, "policy": policy}
    if bypass_ttl is not True and _upgrade_check_is_due(import_name) is not True:
        return {
            "status": "ttl-cache",
            "import_name": import_name,
            "state": _get_upgrade_check_state(import_name),
        }

    with _runtime_install_lock(timeout_seconds=15.0) as lock_report:
        if bypass_ttl is not True and _upgrade_check_is_due(import_name) is not True:
            return {
                "status": "ttl-cache",
                "import_name": import_name,
                "state": _get_upgrade_check_state(import_name),
                "lock": lock_report,
            }
        RUNTIME_STAGING_ROOT_DIR.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(
            tempfile.mkdtemp(
                prefix=f"{_python_tag()}-{import_name}-upgrade-{os.getpid()}-",
                dir=RUNTIME_STAGING_ROOT_DIR,
            )
        )
        previous_lane: Path | None = None
        try:
            if VENDOR_PY_DIR.is_dir():
                shutil.copytree(VENDOR_PY_DIR, stage_dir, dirs_exist_ok=True)
            _remove_staged_import_artifacts(stage_dir, import_name)
            try:
                online_report = _install_online_import_to_stage(
                    import_name,
                    stage_dir,
                    network_timeout_seconds=5,
                    retries=0,
                    process_timeout_seconds=35.0,
                )
            except Exception as exc:
                error_report = dict(getattr(exc, "report", {}))
                state = _record_upgrade_check_state(
                    import_name,
                    status="network-unavailable",
                    report=error_report,
                    error=str(exc),
                    ttl_seconds=FAILED_UPGRADE_CHECK_TTL_SECONDS,
                )
                return {
                    "status": "network-unavailable",
                    "import_name": import_name,
                    "error": str(exc),
                    "error_report": error_report,
                    "state": state,
                    "lock": lock_report,
                }

            downloads = [value for value in online_report.get("downloads", []) if isinstance(value, dict)]
            if not downloads:
                state = _record_upgrade_check_state(
                    import_name,
                    status="invalid-online-report",
                    report=online_report,
                    error="Online install returned no wheel provenance.",
                    ttl_seconds=FAILED_UPGRADE_CHECK_TTL_SECONDS,
                )
                return {"status": "invalid-online-report", "import_name": import_name, "state": state, "lock": lock_report}
            candidate = downloads[0]
            candidate_version = str(candidate.get("version", "") or "")
            if _online_artifact_is_blocked(import_name, candidate):
                state = _record_upgrade_check_state(
                    import_name,
                    status="blocked-artifact",
                    report={"candidate": candidate, "online": online_report},
                    error="Exact online wheel fingerprint failed an earlier health check.",
                    ttl_seconds=ONLINE_BLOCKED_ARTIFACT_RECHECK_SECONDS,
                )
                return {
                    "status": "blocked-artifact",
                    "import_name": import_name,
                    "candidate": candidate,
                    "state": state,
                    "lock": lock_report,
                }
            if current_version not in (None, "") and _compare_version_text(candidate_version, current_version) <= 0:
                state = _record_upgrade_check_state(
                    import_name,
                    status="up-to-date",
                    report={"current_version": current_version, "candidate": candidate, "online": online_report},
                )
                return {
                    "status": "up-to-date",
                    "import_name": import_name,
                    "current_version": current_version,
                    "candidate": candidate,
                    "state": state,
                    "lock": lock_report,
                }

            requested_imports: tuple[str, ...]
            dependency_report: dict[str, object] | None = None
            if import_name == "numpy":
                dependency_report = _stage_import_from_policy("ufbx", stage_dir, fixed_only=True)
                requested_imports = ("numpy", "ufbx")
            else:
                requested_imports = (import_name,)
            stage_probe = _run_fresh_lane_probe(
                stage_dir,
                include_packaged=False,
                import_names=requested_imports,
            )
            if stage_probe.get("ready") is not True:
                blocked_artifact = _record_failed_online_artifact(
                    import_name,
                    version=candidate_version or "unknown",
                    source_url=str(candidate.get("url", "") or ""),
                    sha256=str(candidate.get("sha256", "") or ""),
                    reason_text="Upgrade staging health check failed: " + json.dumps(stage_probe, ensure_ascii=False),
                )
                state = _record_upgrade_check_state(
                    import_name,
                    status="rejected-health",
                    report={
                        "candidate": candidate,
                        "stage_probe": stage_probe,
                        "dependency_report": dependency_report,
                        "blocked_artifact": blocked_artifact,
                    },
                    error="Fresh-child health check rejected online upgrade.",
                    ttl_seconds=ONLINE_BLOCKED_ARTIFACT_RECHECK_SECONDS,
                )
                return {
                    "status": "rejected-health",
                    "import_name": import_name,
                    "candidate": candidate,
                    "stage_probe": stage_probe,
                    "blocked_artifact": blocked_artifact,
                    "state": state,
                    "lock": lock_report,
                }

            previous_lane = _commit_runtime_lane(
                stage_dir,
                VENDOR_PY_DIR,
                quarantine_root=RUNTIME_QUARANTINE_ROOT_DIR,
            )
            configure_vendor_paths()
            committed_probe = _run_fresh_lane_probe(
                VENDOR_PY_DIR,
                include_packaged=False,
                import_names=requested_imports,
            )
            if committed_probe.get("ready") is not True:
                restored = (
                    _restore_previous_runtime_lane(VENDOR_PY_DIR, previous_lane)
                    if previous_lane is not None
                    else None
                )
                if restored is None and VENDOR_PY_DIR.exists():
                    _quarantine_runtime_lane(VENDOR_PY_DIR, reason_label="rejected-online-upgrade")
                blocked_artifact = _record_failed_online_artifact(
                    import_name,
                    version=candidate_version or "unknown",
                    source_url=str(candidate.get("url", "") or ""),
                    sha256=str(candidate.get("sha256", "") or ""),
                    reason_text="Committed upgrade health check failed: " + json.dumps(committed_probe, ensure_ascii=False),
                )
                state = _record_upgrade_check_state(
                    import_name,
                    status="rejected-commit",
                    report={"candidate": candidate, "committed_probe": committed_probe, "restored": restored},
                    error="Committed online upgrade was rejected and rolled back.",
                    ttl_seconds=ONLINE_BLOCKED_ARTIFACT_RECHECK_SECONDS,
                )
                return {
                    "status": "rejected-commit",
                    "import_name": import_name,
                    "candidate": candidate,
                    "committed_probe": committed_probe,
                    "restored": restored,
                    "blocked_artifact": blocked_artifact,
                    "state": state,
                    "lock": lock_report,
                }

            lkg_report: dict[str, object] | None = None
            lkg_warning = ""
            export_probe = _run_fresh_lane_probe(
                VENDOR_PY_DIR,
                include_packaged=False,
                import_names=REPAIR_REQUIRED_IMPORTS,
            )
            if export_probe.get("export_ready") is True:
                try:
                    lkg_report = _refresh_last_known_good(VENDOR_PY_DIR)
                except Exception as exc:
                    lkg_warning = str(exc)
            state = _record_upgrade_check_state(
                import_name,
                status="promoted",
                report={
                    "previous_version": current_version,
                    "candidate": candidate,
                    "online": online_report,
                    "committed_probe": committed_probe,
                },
            )
            return {
                "status": "promoted",
                "import_name": import_name,
                "previous_version": current_version,
                "candidate": candidate,
                "online": online_report,
                "dependency_report": dependency_report,
                "stage_probe": stage_probe,
                "committed_probe": committed_probe,
                "quarantine_path": str(previous_lane) if previous_lane is not None else None,
                "last_known_good": lkg_report,
                "last_known_good_warning": lkg_warning,
                "state": state,
                "lock": lock_report,
            }
        finally:
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)


def _quarantine_runtime_lane(target_lane: Path, *, reason_label: str) -> Path | None:
    if target_lane.exists() is not True:
        return None
    RUNTIME_QUARANTINE_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    safe_reason = re.sub(r"[^a-z0-9_-]+", "-", reason_label.casefold()).strip("-") or "runtime"
    quarantine_path = RUNTIME_QUARANTINE_ROOT_DIR / (
        f"{_python_tag()}-{safe_reason}-{timestamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    os.replace(target_lane, quarantine_path)
    configure_vendor_paths()
    return quarantine_path


def _recover_packaged_or_last_known_good(
    target_lane: Path,
    *,
    excluded_quarantine_paths: tuple[Path, ...] = (),
) -> dict[str, object] | None:
    if target_lane.exists():
        raise RuntimeError("Fallback recovery requires the active writable lane to be absent.")
    packaged_probe = _run_fresh_lane_probe(
        target_lane,
        include_packaged=True,
        import_names=REPAIR_REQUIRED_IMPORTS,
    )
    if packaged_probe.get("export_ready") is True:
        configure_vendor_paths()
        return {
            "action": "recovered-packaged",
            "source": str(PACKAGED_VENDOR_PY_DIR),
            "fresh_child_probe": packaged_probe,
        }

    lkg_probe: dict[str, object] | None = None
    if RUNTIME_LAST_KNOWN_GOOD_DIR.is_dir():
        lkg_probe = _run_fresh_lane_probe(
            RUNTIME_LAST_KNOWN_GOOD_DIR,
            include_packaged=False,
            import_names=REPAIR_REQUIRED_IMPORTS,
        )
        if lkg_probe.get("export_ready") is True:
            copy_report = _copy_runtime_lane_atomic(RUNTIME_LAST_KNOWN_GOOD_DIR, target_lane)
            configure_vendor_paths()
            return {
                "action": "recovered-last-known-good",
                "source": str(RUNTIME_LAST_KNOWN_GOOD_DIR),
                "fresh_child_probe": copy_report["committed_probe"],
                "copy_report": copy_report,
                "packaged_probe": packaged_probe,
            }

    excluded_keys = {
        os.path.normcase(os.path.abspath(str(path_value)))
        for path_value in excluded_quarantine_paths
    }
    candidates: list[Path] = []
    if RUNTIME_QUARANTINE_ROOT_DIR.is_dir():
        candidates = sorted(
            (
                candidate
                for candidate in RUNTIME_QUARANTINE_ROOT_DIR.glob(f"{_python_tag()}-*")
                if candidate.is_dir()
                and os.path.normcase(os.path.abspath(str(candidate))) not in excluded_keys
            ),
            key=lambda candidate: candidate.stat().st_mtime_ns,
            reverse=True,
        )
    candidate_reports: list[dict[str, object]] = []
    for candidate in candidates:
        candidate_probe = _run_fresh_lane_probe(
            candidate,
            include_packaged=False,
            import_names=REPAIR_REQUIRED_IMPORTS,
        )
        candidate_reports.append(
            {
                "path": str(candidate),
                "probe": candidate_probe,
            }
        )
        if candidate_probe.get("export_ready") is not True:
            continue
        try:
            copy_report = _copy_runtime_lane_atomic(candidate, target_lane)
        except Exception as exc:
            candidate_reports[-1]["copy_error"] = str(exc)
            if target_lane.exists():
                _quarantine_runtime_lane(target_lane, reason_label="rejected-last-known-good")
            continue
        configure_vendor_paths()
        if copy_report["committed_probe"].get("export_ready") is True:
            return {
                "action": "recovered-last-known-good",
                "source": str(candidate),
                "fresh_child_probe": copy_report["committed_probe"],
                "copy_report": copy_report,
                "candidate_reports": candidate_reports,
                "packaged_probe": packaged_probe,
                "explicit_last_known_good_probe": lkg_probe,
            }
        _quarantine_runtime_lane(target_lane, reason_label="rejected-last-known-good")
    return None


def _record_export_runtime_repair_state(result: dict[str, object]) -> str | None:
    try:
        def update(state_payload: dict[str, object]) -> None:
            state_payload["last_export_runtime_repair"] = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
                "action": str(result.get("action", "unknown")),
                "python_tag": _python_tag(),
                "quarantine_path": result.get("quarantine_path"),
                "installed_packages": list(result.get("installed_packages", [])),
            }

        _update_runtime_state(update)
    except Exception as exc:
        return str(exc)
    return None


def repair_export_runtime() -> dict[str, object]:
    runtime_report = get_runtime_support_report()
    if runtime_report.get("native_abi_supported") is not True:
        raise RuntimeError(
            "Export runtime repair requires a bundled ABI; current="
            + str(runtime_report.get("current_python"))
            + " supported="
            + _format_supported_python_minors()
        )
    bridge_contract = get_export_bridge_contract_report()
    dependency_contract_before = get_dependency_bundle_contract_report(include_runtime_health=True)
    maintenance_warnings: list[str] = []
    if bridge_contract.get("ready") is not True:
        maintenance_warnings.append(
            "Export bridge maintenance contract is below the release-smoke minimum: "
            + json.dumps(bridge_contract, ensure_ascii=False)
        )

    with _runtime_install_lock() as lock_report:
        local_probe = _run_fresh_lane_probe(
            VENDOR_PY_DIR,
            include_packaged=False,
            import_names=REPAIR_HEALTH_IMPORTS,
        )
        effective_probe = _run_fresh_lane_probe(
            VENDOR_PY_DIR,
            include_packaged=True,
            import_names=REPAIR_REQUIRED_IMPORTS,
        )
        local_bad = _local_lane_has_bad_payload(local_probe)
        if effective_probe.get("export_ready") is True and local_bad is not True:
            last_known_good: dict[str, object] | None = None
            last_known_good_warning = ""
            if (
                local_probe.get("export_ready") is True
                and VENDOR_PY_DIR.is_dir()
                and RUNTIME_LAST_KNOWN_GOOD_DIR.is_dir() is not True
            ):
                try:
                    last_known_good = _refresh_last_known_good(VENDOR_PY_DIR)
                except Exception as exc:
                    last_known_good_warning = str(exc)
            result = {
                "action": "already-healthy",
                "python_runtime": runtime_report,
                "vendor_dir": str(VENDOR_PY_DIR),
                "packaged_vendor_dir": str(PACKAGED_VENDOR_PY_DIR),
                "installed_packages": [],
                "local_probe": local_probe,
                "effective_probe": effective_probe,
                "bridge_contract": bridge_contract,
                "dependency_contract_before": dependency_contract_before,
                "dependency_contract_after": dependency_contract_before,
                "lock": lock_report,
                "maintenance_warnings": maintenance_warnings,
                "quarantine_path": None,
                "last_known_good": last_known_good,
                "last_known_good_warning": last_known_good_warning,
            }
            try:
                result["runtime_candidate_rehabilitation"] = record_python_runtime_rehabilitated(
                    sys.executable,
                    abi=_python_tag(),
                    detail="Fresh-child export runtime probe passed without repair.",
                )
            except Exception as exc:
                result["candidate_state_warning"] = str(exc)
            return result

        RUNTIME_STAGING_ROOT_DIR.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(
            tempfile.mkdtemp(
                prefix=f"{_python_tag()}-{os.getpid()}-",
                dir=RUNTIME_STAGING_ROOT_DIR,
            )
        )
        quarantine_path: Path | None = None
        committed_stage = False
        recovery_quarantines: list[Path] = []
        try:
            rebuild_report = _rebuild_export_runtime_stage(stage_dir)
            quarantine_path = _commit_runtime_lane(
                stage_dir,
                VENDOR_PY_DIR,
                quarantine_root=RUNTIME_QUARANTINE_ROOT_DIR,
            )
            committed_stage = True
            configure_vendor_paths()
            committed_probe = _run_fresh_lane_probe(
                VENDOR_PY_DIR,
                include_packaged=False,
                import_names=REPAIR_REQUIRED_IMPORTS,
            )
            if committed_probe.get("export_ready") is not True:
                raise RuntimeError(
                    "Committed runtime lane failed its fresh-child probe: "
                    + json.dumps(committed_probe, ensure_ascii=False)
                )
            last_known_good: dict[str, object] | None = None
            last_known_good_warning = ""
            try:
                last_known_good = _refresh_last_known_good(VENDOR_PY_DIR)
            except Exception as exc:
                last_known_good_warning = str(exc)
            result = {
                "action": "repaired",
                "python_runtime": runtime_report,
                "vendor_dir": str(VENDOR_PY_DIR),
                "packaged_vendor_dir": str(PACKAGED_VENDOR_PY_DIR),
                "installed_packages": list(rebuild_report.get("installed_packages", [])),
                "local_probe_before": local_probe,
                "effective_probe_before": effective_probe,
                "staging_rebuild": rebuild_report,
                "committed_probe": committed_probe,
                "last_known_good": last_known_good,
                "last_known_good_warning": last_known_good_warning,
                "bridge_contract": bridge_contract,
                "dependency_contract_before": dependency_contract_before,
                "dependency_contract_after": get_dependency_bundle_contract_report(include_runtime_health=False),
                "lock": lock_report,
                "maintenance_warnings": maintenance_warnings,
                "quarantine_path": str(quarantine_path) if quarantine_path is not None else None,
            }
            try:
                result["runtime_candidate_rehabilitation"] = record_python_runtime_rehabilitated(
                    sys.executable,
                    abi=_python_tag(),
                    detail="Transactional repair and committed fresh-child probe passed.",
                )
            except Exception as exc:
                result["candidate_state_warning"] = str(exc)
            state_warning = _record_export_runtime_repair_state(result)
            if state_warning:
                result["state_warning"] = state_warning
            return result
        except Exception as repair_exc:
            restored_previous: dict[str, object] | None = None
            if committed_stage is True:
                if quarantine_path is not None and effective_probe.get("export_ready") is True:
                    restored_previous = _restore_previous_runtime_lane(VENDOR_PY_DIR, quarantine_path)
                elif VENDOR_PY_DIR.exists():
                    recovery_quarantine = _quarantine_runtime_lane(
                        VENDOR_PY_DIR,
                        reason_label="repair-failed-after-commit",
                    )
                    if recovery_quarantine is not None:
                        recovery_quarantines.append(recovery_quarantine)
            elif effective_probe.get("export_ready") is not True and VENDOR_PY_DIR.exists():
                recovery_quarantine = _quarantine_runtime_lane(
                    VENDOR_PY_DIR,
                    reason_label="repair-failed-before-commit",
                )
                if recovery_quarantine is not None:
                    recovery_quarantines.append(recovery_quarantine)
            if restored_previous is not None:
                result = {
                    "action": "restored-previous-lane",
                    "python_runtime": runtime_report,
                    "vendor_dir": str(VENDOR_PY_DIR),
                    "packaged_vendor_dir": str(PACKAGED_VENDOR_PY_DIR),
                    "installed_packages": [],
                    "local_probe_before": local_probe,
                    "effective_probe_before": effective_probe,
                    "fallback": restored_previous,
                    "repair_error_recovered": str(repair_exc),
                    "repair_error_type": getattr(repair_exc, "error_type", type(repair_exc).__name__),
                    "repair_error_report": dict(getattr(repair_exc, "report", {})),
                    "bridge_contract": bridge_contract,
                    "dependency_contract_before": dependency_contract_before,
                    "dependency_contract_after": get_dependency_bundle_contract_report(include_runtime_health=False),
                    "lock": lock_report,
                    "maintenance_warnings": maintenance_warnings,
                    "quarantine_path": restored_previous.get("rejected_lane"),
                }
                state_warning = _record_export_runtime_repair_state(result)
                if state_warning:
                    result["state_warning"] = state_warning
                return result
            excluded_paths = tuple(
                path_value
                for path_value in recovery_quarantines
                if path_value is not None
            )
            if VENDOR_PY_DIR.exists() is not True:
                fallback_report = _recover_packaged_or_last_known_good(
                    VENDOR_PY_DIR,
                    excluded_quarantine_paths=excluded_paths,
                )
                if fallback_report is not None:
                    result = {
                        "action": fallback_report["action"],
                        "python_runtime": runtime_report,
                        "vendor_dir": str(VENDOR_PY_DIR),
                        "packaged_vendor_dir": str(PACKAGED_VENDOR_PY_DIR),
                        "installed_packages": [],
                        "local_probe_before": local_probe,
                        "effective_probe_before": effective_probe,
                        "fallback": fallback_report,
                        "bridge_contract": bridge_contract,
                        "maintenance_warnings": maintenance_warnings,
                        "repair_error_recovered": str(repair_exc),
                        "repair_error_type": getattr(repair_exc, "error_type", type(repair_exc).__name__),
                        "repair_error_report": dict(getattr(repair_exc, "report", {})),
                        "dependency_contract_before": dependency_contract_before,
                        "dependency_contract_after": get_dependency_bundle_contract_report(include_runtime_health=False),
                        "lock": lock_report,
                        "quarantine_path": (
                            str(recovery_quarantines[-1])
                            if recovery_quarantines
                            else str(quarantine_path) if quarantine_path is not None else None
                        ),
                    }
                    try:
                        result["runtime_candidate_rehabilitation"] = record_python_runtime_rehabilitated(
                            sys.executable,
                            abi=_python_tag(),
                            detail="Packaged or last-known-good recovery passed a fresh-child probe.",
                        )
                    except Exception as state_exc:
                        result["candidate_state_warning"] = str(state_exc)
                    state_warning = _record_export_runtime_repair_state(result)
                    if state_warning:
                        result["state_warning"] = state_warning
                    return result
            if effective_probe.get("export_ready") is True and VENDOR_PY_DIR.exists():
                return {
                    "action": "repair-failed-active-lane-preserved",
                    "python_runtime": runtime_report,
                    "vendor_dir": str(VENDOR_PY_DIR),
                    "packaged_vendor_dir": str(PACKAGED_VENDOR_PY_DIR),
                    "installed_packages": [],
                    "local_probe_before": local_probe,
                    "effective_probe_before": effective_probe,
                    "repair_error_recovered": str(repair_exc),
                    "repair_error_type": getattr(repair_exc, "error_type", type(repair_exc).__name__),
                    "repair_error_report": dict(getattr(repair_exc, "report", {})),
                    "dependency_contract_before": dependency_contract_before,
                    "dependency_contract_after": get_dependency_bundle_contract_report(include_runtime_health=False),
                    "bridge_contract": bridge_contract,
                    "lock": lock_report,
                    "maintenance_warnings": maintenance_warnings,
                    "quarantine_path": None,
                }
            raise
        finally:
            if stage_dir.exists():
                shutil.rmtree(stage_dir, ignore_errors=True)


def run_bootstrap_self_tests() -> dict[str, object]:
    test_results: list[dict[str, object]] = []
    operation_domain_guard = _run_operation_domain_isolation_regression_guard()
    test_results.append(
        {
            "name": "operation-domain-isolation",
            "ready": operation_domain_guard.get("status") == "PASS",
            "report": operation_domain_guard,
        }
    )
    source_path = Path(__file__).resolve()
    with tempfile.TemporaryDirectory(prefix="codex-v4-bootstrap-selftest-") as temp_text:
        temp_root = Path(temp_text)

        lock_metadata_regression = run_runtime_lock_metadata_regression()
        lock_metadata_ready = lock_metadata_regression.get("status") == "PASS"
        test_results.append(
            {
                "name": "runtime-lock-metadata-rewrite",
                "ready": lock_metadata_ready,
                "report": lock_metadata_regression,
            }
        )
        if lock_metadata_ready is not True:
            raise AssertionError(
                "Runtime lock metadata rewrite regression failed: "
                + repr(lock_metadata_regression)
            )

        import_runtime_root = temp_root / "import-must-not-create"
        import_code = (
            "import importlib.util,json;"
            f"p={str(source_path)!r};"
            "s=importlib.util.spec_from_file_location('codex_bootstrap_import_smoke',p);"
            "m=importlib.util.module_from_spec(s);"
            "s.loader.exec_module(m);"
            "print(json.dumps({'runtime_root':str(m.RUNTIME_ROOT_DIR),'loaded':True}))"
        )
        import_env = os.environ.copy()
        import_env[RUNTIME_ROOT_OVERRIDE_ENV] = str(import_runtime_root)
        import_env["PYTHONUTF8"] = "1"
        import_env["PYTHONDONTWRITEBYTECODE"] = "1"
        import_completed = _run_hidden_subprocess(
            [sys.executable, "-I", "-B", "-c", import_code],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=import_env,
        )
        import_payload = _decode_json_subprocess_output(import_completed.stdout)
        import_ready = (
            import_completed.returncode == 0
            and isinstance(import_payload, dict)
            and import_payload.get("loaded") is True
            and import_runtime_root.exists() is not True
        )
        test_results.append(
            {
                "name": "import-is-read-only",
                "ready": import_ready,
                "returncode": import_completed.returncode,
                "runtime_root_created": import_runtime_root.exists(),
                "stderr": import_completed.stderr[-2000:],
            }
        )
        if import_ready is not True:
            raise AssertionError("Import created runtime state or failed: " + import_completed.stderr[-2000:])

        poisoned_environment = {
            "Path": str(os.environ.get("Path", "")),
            "CODEX_V4_TEST_SENTINEL": "preserved",
            "PYTHONHOME": "poisoned-home",
            "pythonpath": "poisoned-path",
            "PYTHONUSERBASE": "poisoned-user-base",
            "PYTHONWARNINGS": "error",
            "PYTHONOPTIMIZE": "2",
        }
        isolated_environment = _isolated_python_child_environment(poisoned_environment)
        isolated_python_keys = {
            str(name).upper(): str(value)
            for name, value in isolated_environment.items()
            if str(name).upper().startswith("PYTHON")
        }
        isolated_environment_ready = (
            isolated_python_keys == ISOLATED_PYTHON_ENVIRONMENT
            and isolated_environment.get("CODEX_V4_TEST_SENTINEL") == "preserved"
            and isolated_environment.get("Path") == poisoned_environment["Path"]
        )
        test_results.append(
            {
                "name": "python-child-environment-isolation",
                "ready": isolated_environment_ready,
                "python_environment": isolated_python_keys,
            }
        )
        if isolated_environment_ready is not True:
            raise AssertionError("Managed Python child environment retained external Python configuration.")

        runtime_matrix = {
            f"{major}.{minor}": bool(get_runtime_support_report((major, minor)).get("supported"))
            for major, minor in ((3, 11), (3, 12), (3, 13), (3, 14), (3, 15), (4, 0))
        }
        unsupported_32_bit = _runtime_architecture_report(
            pointer_bits=32,
            machine="AMD64",
            platform_tag="win-amd64",
        )
        runtime_matrix_ready = runtime_matrix == {
            "3.11": False,
            "3.12": False,
            "3.13": False,
            "3.14": True,
            "3.15": False,
            "4.0": False,
        } and unsupported_32_bit.get("supported") is False
        test_results.append(
            {
                "name": "exact-python-abi-and-future-major-rejection",
                "ready": runtime_matrix_ready,
                "runtime_matrix": runtime_matrix,
                "unsupported_32_bit": unsupported_32_bit,
            }
        )
        if runtime_matrix_ready is not True:
            raise AssertionError("Unsupported Python ABI or architecture escaped the exact runtime gate.")

        priority_lane = temp_root / "priority" / _python_tag()
        priority_lane.mkdir(parents=True)
        with _vendor_path_context(priority_lane, include_packaged=True):
            configured_paths = configure_vendor_paths()
            priority_ready = bool(configured_paths) and configured_paths[0] == priority_lane
            if PACKAGED_VENDOR_PY_DIR.is_dir():
                priority_ready = (
                    priority_ready
                    and PACKAGED_VENDOR_PY_DIR in configured_paths
                    and configured_paths.index(priority_lane) < configured_paths.index(PACKAGED_VENDOR_PY_DIR)
                )
        test_results.append(
            {
                "name": "writable-before-packaged-priority",
                "ready": priority_ready,
                "configured_paths": [str(path_value) for path_value in configured_paths],
            }
        )
        if priority_ready is not True:
            raise AssertionError("Writable and packaged vendor path priority is incorrect.")

        bridge_contract = get_export_bridge_contract_report()
        test_results.append(
            {
                "name": "delete-selected-bridge-contract",
                "ready": bridge_contract.get("ready") is True,
                "report": bridge_contract,
            }
        )
        if bridge_contract.get("ready") is not True:
            raise AssertionError("Bridge stable-slot contract revision is below the bootstrap minimum.")

        abi_contracts = get_supported_abi_bundle_contracts()
        fixed_abi_ready = (
            {str(report.get("python_tag")) for report in abi_contracts} == {"cp314"}
            and all(dict(report.get("fixed_numpy", {})).get("hash_ready") is True for report in abi_contracts)
            and all(dict(report.get("patched_ufbx", {})).get("present") is True for report in abi_contracts)
            and all(report.get("pinned_contract_ready") is True for report in abi_contracts)
        )
        test_results.append(
            {
                "name": "fixed-cp314-hash-and-pinned-contract",
                "ready": fixed_abi_ready,
                "reports": abi_contracts,
            }
        )
        if fixed_abi_ready is not True:
            raise AssertionError("Fixed cp314 NumPy, ufbx, or accelerator contract is incomplete.")

        wheel_root = temp_root / "stdlib-wheel-installer"
        wheel_path = wheel_root / "synthetic-1.0-py3-none-any.whl"
        wheel_target = wheel_root / "target"
        wheel_root.mkdir(parents=True)
        with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as wheel_zip:
            wheel_zip.writestr("root_pkg/__init__.py", "VALUE = 1\n")
            wheel_zip.writestr("synthetic-1.0.data/purelib/pure_module.py", "PURE = True\n")
            wheel_zip.writestr("synthetic-1.0.data/platlib/plat_module.py", "PLAT = True\n")
            wheel_zip.writestr("synthetic-1.0.data/scripts/tool.py", "print('tool')\n")
            wheel_zip.writestr("synthetic-1.0.data/headers/synthetic/header.h", "#define OK 1\n")
        wheel_report = _install_local_wheel_without_pip(
            wheel_path,
            wheel_target,
            require_registered=False,
        )
        wheel_ready = all(
            path_value.is_file()
            for path_value in (
                wheel_target / "root_pkg" / "__init__.py",
                wheel_target / "pure_module.py",
                wheel_target / "plat_module.py",
                wheel_target / "Scripts" / "tool.py",
                wheel_target / "Include" / "synthetic" / "header.h",
            )
        )
        unsafe_wheel = wheel_root / "unsafe-1.0-py3-none-any.whl"
        with zipfile.ZipFile(unsafe_wheel, "w") as wheel_zip:
            wheel_zip.writestr("../escape.py", "raise RuntimeError\n")
        unsafe_rejected = False
        try:
            _install_local_wheel_without_pip(
                unsafe_wheel,
                wheel_root / "unsafe-target",
                require_registered=False,
            )
        except RuntimeError:
            unsafe_rejected = True
        wheel_ready = wheel_ready and unsafe_rejected and (wheel_root / "escape.py").exists() is not True
        test_results.append(
            {
                "name": "stdlib-wheel-installer-no-pip-and-safe-data-layout",
                "ready": wheel_ready,
                "report": wheel_report,
                "unsafe_rejected": unsafe_rejected,
            }
        )
        if wheel_ready is not True:
            raise AssertionError("Stdlib wheel installer layout or path-safety regression failed.")

        sha_cache_path = temp_root / "sha-cache-same-size.bin"
        sha_cache_path.write_bytes(b"artifact-A")
        original_stat = sha_cache_path.stat()
        first_default_hash = _sha256_file(sha_cache_path)
        first_fingerprint = _candidate_artifact_fingerprint(str(sha_cache_path))
        sha_cache_path.write_bytes(b"artifact-B")
        os.utime(
            sha_cache_path,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        second_default_hash = _sha256_file(sha_cache_path)
        second_fingerprint = _candidate_artifact_fingerprint(str(sha_cache_path))
        sha_cache_ready = (
            first_default_hash != second_default_hash
            and first_fingerprint != second_fingerprint
        )
        test_results.append(
            {
                "name": "artifact-sha-rehashes-same-size-restored-mtime",
                "ready": sha_cache_ready,
                "first_default_hash": first_default_hash,
                "second_default_hash": second_default_hash,
                "first": first_fingerprint,
                "second": second_fingerprint,
            }
        )
        if sha_cache_ready is not True:
            raise AssertionError("Artifact SHA cache reused stale content identity.")

        artifact_fixture_root = temp_root / "artifact-transaction-fixtures"
        artifact_fixed_root = artifact_fixture_root / "fixed"
        artifact_upgrade_root = artifact_fixture_root / "upgrade"
        artifact_template_root = artifact_fixture_root / "templates"
        artifact_fixed_root.mkdir(parents=True)
        artifact_upgrade_root.mkdir(parents=True)
        artifact_template_root.mkdir(parents=True)
        artifact_tag = _python_tag()
        artifact_fixed_wheel = artifact_fixed_root / f"orjson-1.0.0-{artifact_tag}-{artifact_tag}-win_amd64.whl"
        artifact_upgrade_wheel = artifact_upgrade_root / f"orjson-9.9.0-{artifact_tag}-{artifact_tag}-win_amd64.whl"
        artifact_rebuilt_template = artifact_template_root / "same-version-rebuilt.whl"
        artifact_next_template = artifact_template_root / "next-version.whl"

        def write_synthetic_orjson_wheel(
            wheel_path: Path,
            *,
            version: str,
            marker: str,
            healthy: bool,
        ) -> None:
            if healthy:
                module_body = (
                    "import json\n"
                    + f"__version__ = {version!r}\n"
                    + f"BUILD_MARKER = {marker!r}\n"
                    + "OPT_INDENT_2 = 1\n"
                    + "def dumps(value, option=0):\n    return json.dumps(value, sort_keys=True).encode('utf-8')\n"
                    + "def loads(value):\n    return json.loads(value.decode('utf-8') if isinstance(value, bytes) else value)\n"
                )
            else:
                module_body = (
                    f"__version__ = {version!r}\n"
                    + f"BUILD_MARKER = {marker!r}\n"
                    + "OPT_INDENT_2 = 1\n"
                    + "def dumps(value, option=0):\n    raise RuntimeError('intentional bad artifact')\n"
                    + "def loads(value):\n    return None\n"
                )
            wheel_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as wheel_zip:
                wheel_zip.writestr("orjson/__init__.py", module_body)
                wheel_zip.writestr(
                    f"orjson-{version}.dist-info/METADATA",
                    "Name: orjson\nVersion: " + version + "\n",
                )
                wheel_zip.writestr(
                    f"orjson-{version}.dist-info/WHEEL",
                    "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
                )

        write_synthetic_orjson_wheel(
            artifact_fixed_wheel,
            version="1.0.0",
            marker="fixed-floor",
            healthy=True,
        )
        write_synthetic_orjson_wheel(
            artifact_upgrade_wheel,
            version="9.9.0",
            marker="bad-hash-a",
            healthy=False,
        )
        write_synthetic_orjson_wheel(
            artifact_rebuilt_template,
            version="9.9.0",
            marker="good-hash-b",
            healthy=True,
        )
        write_synthetic_orjson_wheel(
            artifact_next_template,
            version="9.9.1",
            marker="good-next-version",
            healthy=True,
        )

        artifact_state_root = temp_root / "artifact-transaction-state"
        artifact_state_code = "\n".join(
            [
                "import importlib.util,shutil",
                f"p={str(source_path)!r}",
                "s=importlib.util.spec_from_file_location('codex_bootstrap_artifact_transaction',p)",
                "m=importlib.util.module_from_spec(s)",
                "s.loader.exec_module(m)",
                f"m.LOCAL_ORJSON_BUNDLE_DIR=m.Path({str(artifact_fixed_root)!r})",
                f"m.UPGRADE_ORJSON_BUNDLE_DIR=m.Path({str(artifact_upgrade_root)!r})",
                "m.NETWORK_REPAIR_ENABLED=False",
                "m.refresh_package_candidates()",
                f"upgrade=m.Path({str(artifact_upgrade_wheel)!r})",
                "try:\n    first=m._transactional_repair_import('orjson',fixed_only=False)\nexcept Exception as exc:\n    m._write_json_stdout({'phase':'first-transaction','error':str(exc),'error_type':getattr(exc,'error_type',type(exc).__name__),'error_report':getattr(exc,'report',{})})\n    raise",
                "first_text=(m.VENDOR_PY_DIR/'orjson'/'__init__.py').read_text(encoding='utf-8')",
                "blocked_a=m.get_candidate_artifact_report('orjson',str(upgrade))",
                "m.mark_import_version_blocked('orjson','9.9.0','legacy state compatibility self-test')",
                "second=m._transactional_repair_import('orjson',fixed_only=False)",
                "second_attempts=second['import_reports'][0]['attempts']",
                "blocked_a_after=m.get_candidate_artifact_report('orjson',str(upgrade))",
                f"shutil.copyfile({str(artifact_rebuilt_template)!r},upgrade)",
                "m.refresh_package_candidates()",
                "rebuilt_report=m.get_candidate_artifact_report('orjson',str(upgrade))",
                "rebuilt_candidates=m._get_install_candidates('orjson')",
                "third=m._transactional_repair_import('orjson',fixed_only=False)",
                "third_text=(m.VENDOR_PY_DIR/'orjson'/'__init__.py').read_text(encoding='utf-8')",
                f"next_path=m.Path({str(artifact_upgrade_root)!r})/('orjson-9.9.1-'+m._python_tag()+'-'+m._python_tag()+'-win_amd64.whl')",
                f"shutil.copyfile({str(artifact_next_template)!r},next_path)",
                "m.refresh_package_candidates()",
                "next_report=m.get_candidate_artifact_report('orjson',str(next_path))",
                "fourth=m._transactional_repair_import('orjson',fixed_only=False)",
                "fourth_text=(m.VENDOR_PY_DIR/'orjson'/'__init__.py').read_text(encoding='utf-8')",
                "first_attempts=first['import_reports'][0]['attempts']",
                "m._write_json_stdout({'first_ready':first['committed_probe']['ready'],'first_fixed':'fixed-floor' in first_text,'first_attempts':first_attempts,'blocked_a':blocked_a,'second_ready':second['committed_probe']['ready'],'second_attempts':second_attempts,'blocked_a_after':blocked_a_after,'rebuilt_report':rebuilt_report,'rebuilt_candidate_present':str(upgrade) in rebuilt_candidates,'third_ready':third['committed_probe']['ready'],'third_promoted':'good-hash-b' in third_text,'next_report':next_report,'fourth_ready':fourth['committed_probe']['ready'],'next_promoted':'good-next-version' in fourth_text})",
            ]
        )
        artifact_state_env = os.environ.copy()
        artifact_state_env[RUNTIME_ROOT_OVERRIDE_ENV] = str(artifact_state_root)
        artifact_state_env[PACKAGED_VENDOR_ROOT_OVERRIDE_ENV] = str(artifact_state_root / "missing-packaged")
        artifact_state_completed = _run_hidden_subprocess(
            [sys.executable, "-I", "-B", "-c", artifact_state_code],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=420,
            env=artifact_state_env,
        )
        artifact_state_payload = _decode_json_subprocess_output(artifact_state_completed.stdout)
        artifact_state_ready = (
            artifact_state_completed.returncode == 0
            and isinstance(artifact_state_payload, dict)
            and artifact_state_payload.get("first_ready") is True
            and artifact_state_payload.get("first_fixed") is True
            and dict(artifact_state_payload.get("blocked_a", {})).get("blocked") is True
            and artifact_state_payload.get("second_ready") is True
            and not any(
                str(attempt.get("candidate", "")) == str(artifact_upgrade_wheel)
                for attempt in artifact_state_payload.get("second_attempts", [])
                if isinstance(attempt, dict)
            )
            and dict(artifact_state_payload.get("blocked_a_after", {})).get("blocked") is True
            and dict(artifact_state_payload.get("rebuilt_report", {})).get("blocked") is False
            and artifact_state_payload.get("rebuilt_candidate_present") is True
            and artifact_state_payload.get("third_ready") is True
            and artifact_state_payload.get("third_promoted") is True
            and dict(artifact_state_payload.get("next_report", {})).get("blocked") is False
            and artifact_state_payload.get("fourth_ready") is True
            and artifact_state_payload.get("next_promoted") is True
        )
        test_results.append(
            {
                "name": "artifact-transaction-block-skip-rebuild-and-next-version",
                "ready": artifact_state_ready,
                "report": artifact_state_payload,
                "stderr": artifact_state_completed.stderr[-4000:],
            }
        )
        if artifact_state_ready is not True:
            raise AssertionError(
                "Artifact transaction block/retry regression failed: stdout="
                + artifact_state_completed.stdout[-4000:]
                + " stderr="
                + artifact_state_completed.stderr[-4000:]
            )

        optional_online_root = temp_root / "optional-online-repair"
        optional_online_empty = optional_online_root / "empty"
        optional_online_empty.mkdir(parents=True)
        optional_online_code = "\n".join(
            [
                "import importlib.util",
                f"p={str(source_path)!r}",
                "s=importlib.util.spec_from_file_location('codex_bootstrap_optional_online',p)",
                "m=importlib.util.module_from_spec(s)",
                "s.loader.exec_module(m)",
                f"m.LOCAL_ORJSON_BUNDLE_DIR=m.Path({str(optional_online_empty)!r})",
                f"m.UPGRADE_ORJSON_BUNDLE_DIR=m.Path({str(optional_online_empty)!r})",
                f"m.LOCAL_ACCELERATOR_BUNDLE_DIR=m.Path({str(optional_online_empty)!r})",
                f"m.UPGRADE_ACCELERATOR_BUNDLE_DIR=m.Path({str(optional_online_empty)!r})",
                "broken=m.VENDOR_PY_DIR/'orjson'",
                "broken.mkdir(parents=True,exist_ok=True)",
                "(broken/'__init__.py').write_text(\"raise RuntimeError('broken-before-online-repair')\\n\",encoding='ascii')",
                "calls=[]",
                f"wheel=m.Path({str(artifact_rebuilt_template)!r})",
                "def fake_online(name,target_dir,**kwargs):\n    calls.append(name)\n    m._install_local_wheel_without_pip(wheel,target_dir,require_registered=False)\n    return {'import_name':name,'source':'online-repair','downloads':[{'name':'orjson','version':'9.9.0','url':'self-test://orjson','sha256':m._sha256_file(wheel,use_cache=False)}]}",
                "m._install_online_import_to_stage=fake_online",
                "m.refresh_package_candidates()",
                "online=m.ensure_optional_import_runtime('orjson')",
                "pinned=m.ensure_optional_import_runtime('codex_fbx_probe_accel')",
                "active=(m.VENDOR_PY_DIR/'orjson'/'__init__.py').read_text(encoding='utf-8')",
                "m._write_json_stdout({'online_ready':online.get('ready'),'online_status':online.get('status'),'online_calls':calls,'online_source':online.get('install_provenance',{}).get('import_reports',[{}])[0].get('source'),'active_promoted':'good-hash-b' in active,'pinned_ready':pinned.get('ready'),'pinned_status':pinned.get('status'),'pinned_error_type':pinned.get('error_type')})",
            ]
        )
        optional_online_env = os.environ.copy()
        optional_online_env[RUNTIME_ROOT_OVERRIDE_ENV] = str(optional_online_root / "runtime")
        optional_online_env[PACKAGED_VENDOR_ROOT_OVERRIDE_ENV] = str(optional_online_root / "missing-packaged")
        optional_online_completed = _run_hidden_subprocess(
            [sys.executable, "-I", "-B", "-c", optional_online_code],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=optional_online_env,
        )
        optional_online_payload = _decode_json_subprocess_output(optional_online_completed.stdout)
        optional_online_ready = (
            optional_online_completed.returncode == 0
            and isinstance(optional_online_payload, dict)
            and optional_online_payload.get("online_ready") is True
            and optional_online_payload.get("online_status") == "ready"
            and optional_online_payload.get("online_calls") == ["orjson"]
            and optional_online_payload.get("online_source") == "online-repair"
            and optional_online_payload.get("active_promoted") is True
            and optional_online_payload.get("pinned_ready") is False
            and optional_online_payload.get("pinned_status") == "no-local-bundle"
            and optional_online_payload.get("pinned_error_type") == "DEPENDENCY_BUNDLE_BROKEN"
        )
        test_results.append(
            {
                "name": "optional-online-repair-without-local-candidate",
                "ready": optional_online_ready,
                "report": optional_online_payload,
                "stderr": optional_online_completed.stderr[-4000:],
            }
        )
        if optional_online_ready is not True:
            raise AssertionError(
                "Optional empty-candidate online repair regression failed: stdout="
                + optional_online_completed.stdout[-4000:]
                + " stderr="
                + optional_online_completed.stderr[-4000:]
            )

        background_root = temp_root / "background-upgrade"
        background_code = "\n".join(
            [
                "import importlib.util,json,time",
                f"p={str(source_path)!r}",
                "s=importlib.util.spec_from_file_location('codex_bootstrap_background',p)",
                "m=importlib.util.module_from_spec(s)",
                "s.loader.exec_module(m)",
                "started=time.perf_counter()",
                "first=m._schedule_online_upgrade('orjson',current_version='1.0.0')",
                "elapsed=time.perf_counter()-started",
                "second=m._schedule_online_upgrade('orjson',current_version='1.0.0')",
                "result_path=m.Path(str(first.get('result_path','')))",
                "deadline=time.monotonic()+15",
                "while not result_path.is_file() and time.monotonic()<deadline: time.sleep(0.05)",
                "result=json.loads(result_path.read_text(encoding='utf-8')) if result_path.is_file() else {}",
                "state=m._get_upgrade_check_state('orjson')",
                "log_path=m.Path(str(first.get('log_path','')))",
                "m._write_json_stdout({'first':first,'second':second,'elapsed':elapsed,'result':result,'state':state,'result_exists':result_path.is_file(),'log_exists':log_path.is_file()})",
            ]
        )
        background_env = os.environ.copy()
        background_env[RUNTIME_ROOT_OVERRIDE_ENV] = str(background_root)
        background_env[BACKGROUND_UPGRADE_TEST_DELAY_ENV] = "1.0"
        background_env[BACKGROUND_UPGRADE_TEST_STATUS_ENV] = "test-worker-promoted"
        background_completed = _run_hidden_subprocess(
            [sys.executable, "-I", "-B", "-c", background_code],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=background_env,
        )
        background_payload = _decode_json_subprocess_output(background_completed.stdout)
        background_ready = (
            background_completed.returncode == 0
            and isinstance(background_payload, dict)
            and dict(background_payload.get("first", {})).get("status") == "scheduled"
            and float(background_payload.get("elapsed", 99.0) or 99.0) < 0.25
            and dict(background_payload.get("second", {})).get("status") == "already-scheduled"
            and background_payload.get("result_exists") is True
            and background_payload.get("log_exists") is True
            and dict(background_payload.get("result", {})).get("status") == "test-worker-promoted"
            and dict(background_payload.get("state", {})).get("worker_status") == "completed"
        )
        test_results.append(
            {
                "name": "healthy-upgrade-schedules-detached-deduplicated-worker",
                "ready": background_ready,
                "report": background_payload,
                "stderr": background_completed.stderr[-4000:],
            }
        )
        if background_ready is not True:
            raise AssertionError(
                "Background upgrade scheduling regression failed: stdout="
                + background_completed.stdout[-4000:]
                + " stderr="
                + background_completed.stderr[-4000:]
            )

        def clear_worker_failure_state(payload: dict[str, object]) -> None:
            upgrade_checks = payload.get("upgrade_checks", {})
            if isinstance(upgrade_checks, dict):
                upgrade_checks.pop("PIL", None)

        _update_runtime_state(clear_worker_failure_state)
        worker_failure_reservation = _reserve_background_upgrade(
            "PIL",
            current_version="1.0.0",
        )
        worker_failure_state = dict(worker_failure_reservation.get("state", {}))
        worker_failure_env = os.environ.copy()
        worker_failure_env[RUNTIME_ROOT_OVERRIDE_ENV] = str(RUNTIME_ROOT_DIR)
        worker_failure_env[BACKGROUND_UPGRADE_TEST_STATUS_ENV] = "worker-error"
        worker_failure_completed = _run_hidden_subprocess(
            [
                sys.executable,
                "-I",
                "-B",
                str(source_path),
                "--background-upgrade-import",
                "PIL",
                "--background-upgrade-token",
                str(worker_failure_state.get("scheduled_token", "") or ""),
                "--background-upgrade-current-version",
                "1.0.0",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=worker_failure_env,
        )
        worker_failure_payload = _decode_json_subprocess_output(worker_failure_completed.stdout)
        worker_failure_ready = (
            worker_failure_reservation.get("reserved") is True
            and worker_failure_completed.returncode != 0
            and isinstance(worker_failure_payload, dict)
            and worker_failure_payload.get("status") == "worker-error"
            and worker_failure_payload.get("worker_ok") is False
        )
        test_results.append(
            {
                "name": "background-worker-failure-preserves-status-and-nonzero-exit",
                "ready": worker_failure_ready,
                "returncode": worker_failure_completed.returncode,
                "report": worker_failure_payload,
                "stderr": worker_failure_completed.stderr[-4000:],
            }
        )
        if worker_failure_ready is not True:
            raise AssertionError(
                "Background worker exit-status regression failed: stdout="
                + worker_failure_completed.stdout[-4000:]
                + " stderr="
                + worker_failure_completed.stderr[-4000:]
            )

        retention_root = temp_root / "background-artifact-retention"
        retention_root.mkdir(parents=True)
        retention_now = time.time()
        retention_files: list[Path] = []
        for artifact_index in range(BACKGROUND_UPGRADE_ARTIFACT_MAX_FILES + 12):
            suffix = ".result.json" if artifact_index % 2 == 0 else ".log"
            artifact_path = retention_root / (f"artifact-{artifact_index:03d}" + suffix)
            artifact_path.write_text("fixture", encoding="utf-8")
            modified_epoch = retention_now - float(artifact_index)
            os.utime(artifact_path, (modified_epoch, modified_epoch))
            retention_files.append(artifact_path)
        retention_keep = retention_files[0]
        retention_stale = retention_root / "stale.result.json"
        retention_stale.write_text("stale", encoding="utf-8")
        stale_epoch = retention_now - BACKGROUND_UPGRADE_ARTIFACT_RETENTION_SECONDS - 60.0
        os.utime(retention_stale, (stale_epoch, stale_epoch))
        retention_report = _cleanup_background_upgrade_artifacts(
            keep_paths=(retention_keep,),
            root_dir=retention_root,
            now_epoch=retention_now,
        )
        retention_remaining = [
            candidate
            for candidate in retention_root.iterdir()
            if candidate.is_file()
            and candidate.name.endswith((".result.json", ".log"))
        ]
        retention_ready = (
            retention_keep.is_file()
            and retention_stale.exists() is not True
            and len(retention_remaining) <= BACKGROUND_UPGRADE_ARTIFACT_MAX_FILES
            and not retention_report.get("errors")
        )
        test_results.append(
            {
                "name": "background-upgrade-artifacts-expire-and-rotate",
                "ready": retention_ready,
                "report": retention_report,
                "remaining": len(retention_remaining),
            }
        )
        if retention_ready is not True:
            raise AssertionError(
                "Background upgrade artifact retention regression failed: "
                + json.dumps(retention_report, ensure_ascii=False)
            )

        copy_rollback_root = temp_root / "copy-rollback"
        copy_rollback_code = "\n".join(
            [
                "import importlib.util",
                f"p={str(source_path)!r}",
                "s=importlib.util.spec_from_file_location('codex_bootstrap_copy_rollback',p)",
                "m=importlib.util.module_from_spec(s)",
                "s.loader.exec_module(m)",
                "root=m.RUNTIME_ROOT_DIR/'fixture'",
                "source=root/'source'",
                "previous=root/'previous-active'",
                "target=root/'target'",
                "source.mkdir(parents=True)",
                "previous.mkdir(parents=True)",
                "(source/'marker.txt').write_text('candidate',encoding='ascii')",
                "(previous/'marker.txt').write_text('original-active',encoding='ascii')",
                "calls={'count':0}",
                "def injected_probe(*args,**kwargs):\n    calls['count']+=1\n    return {'ready':calls['count']!=2,'export_ready':calls['count']!=2,'call':calls['count']}",
                "m._run_fresh_lane_probe=injected_probe",
                "failed_with_previous=False",
                "try: m._copy_runtime_lane_atomic(source,target,previous_active_quarantine=previous)\nexcept RuntimeError: failed_with_previous=True",
                "restored=failed_with_previous and (target/'marker.txt').read_text(encoding='ascii')=='original-active' and not previous.exists()",
                "source2=root/'source2'",
                "target2=root/'target2'",
                "source2.mkdir(parents=True)",
                "(source2/'marker.txt').write_text('candidate2',encoding='ascii')",
                "calls['count']=0",
                "failed_without_previous=False",
                "try: m._copy_runtime_lane_atomic(source2,target2)\nexcept RuntimeError: failed_without_previous=True",
                "absent=failed_without_previous and not target2.exists()",
                "m._write_json_stdout({'restored':restored,'absent':absent,'quarantines':[str(v) for v in m.RUNTIME_QUARANTINE_ROOT_DIR.glob('*')]})",
            ]
        )
        copy_rollback_env = os.environ.copy()
        copy_rollback_env[RUNTIME_ROOT_OVERRIDE_ENV] = str(copy_rollback_root)
        copy_rollback_completed = _run_hidden_subprocess(
            [sys.executable, "-I", "-B", "-c", copy_rollback_code],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=copy_rollback_env,
        )
        copy_rollback_payload = _decode_json_subprocess_output(copy_rollback_completed.stdout)
        copy_rollback_ready = (
            copy_rollback_completed.returncode == 0
            and isinstance(copy_rollback_payload, dict)
            and copy_rollback_payload.get("restored") is True
            and copy_rollback_payload.get("absent") is True
        )
        test_results.append(
            {
                "name": "post-commit-probe-failure-restores-or-removes-active",
                "ready": copy_rollback_ready,
                "report": copy_rollback_payload,
                "stderr": copy_rollback_completed.stderr[-4000:],
            }
        )
        if copy_rollback_ready is not True:
            raise AssertionError(
                "Runtime copy rollback regression failed: stdout="
                + copy_rollback_completed.stdout[-4000:]
                + " stderr="
                + copy_rollback_completed.stderr[-4000:]
            )

        metadata_root = temp_root / "metadata-conflict"
        metadata_vendor_root = metadata_root / "vendor"
        metadata_packaged_root = metadata_root / "packaged"
        metadata_lane = metadata_packaged_root / _python_tag()
        metadata_package = metadata_lane / "codex_fbx_probe_accel"
        metadata_package.mkdir(parents=True)
        (metadata_package / "__init__.py").write_text("__version__ = '0.3.0'\n", encoding="ascii")
        for version_text in ("0.1.0", "0.2.0"):
            dist_info = metadata_lane / f"codex_fbx_probe_accel-{version_text}.dist-info"
            dist_info.mkdir()
            (dist_info / "METADATA").write_text(
                "Name: codex_fbx_probe_accel\nVersion: " + version_text + "\n",
                encoding="ascii",
            )
        original_vendor_root = VENDOR_PY_ROOT_DIR
        original_packaged_root = PACKAGED_VENDOR_PY_ROOT_DIR
        try:
            globals()["VENDOR_PY_ROOT_DIR"] = metadata_vendor_root
            globals()["PACKAGED_VENDOR_PY_ROOT_DIR"] = metadata_packaged_root
            metadata_report = get_vendor_metadata_contract_report("codex_fbx_probe_accel")
        finally:
            globals()["VENDOR_PY_ROOT_DIR"] = original_vendor_root
            globals()["PACKAGED_VENDOR_PY_ROOT_DIR"] = original_packaged_root
        metadata_ready = (
            metadata_report.get("metadata_conflict") is True
            and metadata_report.get("multiple_dist_info_versions") is True
            and metadata_report.get("module_metadata_mismatch") is True
            and metadata_report.get("authority") == "healthy-module-contract"
        )
        test_results.append(
            {
                "name": "accelerator-module-version-authority-over-conflicting-dist-info",
                "ready": metadata_ready,
                "report": metadata_report,
            }
        )
        if metadata_ready is not True:
            raise AssertionError("Accelerator metadata conflict detection regression failed.")

        stale_lock_root = temp_root / "stale-lock"
        stale_lock_code = "\n".join(
            [
                "import importlib.util,json",
                f"p={str(source_path)!r}",
                "s=importlib.util.spec_from_file_location('codex_bootstrap_stale_lock',p)",
                "m=importlib.util.module_from_spec(s)",
                "s.loader.exec_module(m)",
                "m.RUNTIME_LOCK_ROOT_DIR.mkdir(parents=True,exist_ok=True)",
                "m.RUNTIME_INSTALL_LOCK_PATH.write_bytes(b'\\0'+json.dumps({'pid':99999999,'python_tag':m._python_tag(),'acquired_epoch':1}).encode('utf-8'))",
                "before=m.get_runtime_lock_report()",
                "with m._runtime_install_lock(timeout_seconds=2) as acquired: pass",
                "after=m.get_runtime_lock_report()",
                "m._write_json_stdout({'before':before,'acquired':acquired,'after':after})",
            ]
        )
        stale_lock_env = os.environ.copy()
        stale_lock_env[RUNTIME_ROOT_OVERRIDE_ENV] = str(stale_lock_root)
        stale_lock_completed = _run_hidden_subprocess(
            [sys.executable, "-I", "-B", "-c", stale_lock_code],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=stale_lock_env,
        )
        stale_lock_payload = _decode_json_subprocess_output(stale_lock_completed.stdout)
        stale_lock_ready = (
            stale_lock_completed.returncode == 0
            and isinstance(stale_lock_payload, dict)
            and dict(stale_lock_payload.get("before", {})).get("state") == "stale-metadata"
            and dict(stale_lock_payload.get("acquired", {})).get("previous_owner_state") == "stale"
            and dict(stale_lock_payload.get("after", {})).get("state") == "unlocked"
        )
        test_results.append(
            {
                "name": "stale-lock-classification-and-recovery",
                "ready": stale_lock_ready,
                "report": stale_lock_payload,
                "stderr": stale_lock_completed.stderr[-4000:],
            }
        )
        if stale_lock_ready is not True:
            raise AssertionError("Stale runtime lock classification regression failed.")

        fixed_only_root = temp_root / "fixed-only-no-packaged"
        fixed_only_code = "\n".join(
            [
                "import importlib.util",
                f"p={str(source_path)!r}",
                "s=importlib.util.spec_from_file_location('codex_bootstrap_fixed_only',p)",
                "m=importlib.util.module_from_spec(s)",
                "s.loader.exec_module(m)",
                "m._ensure_pip_available=lambda:(_ for _ in ()).throw(AssertionError('pip used during fixed-only repair'))",
                "r=m.repair_export_runtime()",
                "provenance=r.get('staging_rebuild',{}).get('provenance',[])",
                "m._write_json_stdout({'action':r.get('action'),'ready':r.get('committed_probe',{}).get('export_ready'),'lkg':r.get('last_known_good',{}).get('status'),'provenance':provenance,'packaged_root':str(m.PACKAGED_VENDOR_PY_ROOT_DIR)})",
            ]
        )
        fixed_only_env = os.environ.copy()
        fixed_only_env[RUNTIME_ROOT_OVERRIDE_ENV] = str(fixed_only_root)
        fixed_only_env[PACKAGED_VENDOR_ROOT_OVERRIDE_ENV] = str(fixed_only_root / "intentionally-missing-vendor-py")
        fixed_only_completed = _run_hidden_subprocess(
            [sys.executable, "-I", "-B", "-c", fixed_only_code],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
            env=fixed_only_env,
        )
        fixed_only_payload = _decode_json_subprocess_output(fixed_only_completed.stdout)
        fixed_only_provenance = (
            fixed_only_payload.get("provenance", [])
            if isinstance(fixed_only_payload, dict)
            else []
        )
        fixed_only_ready = (
            fixed_only_completed.returncode == 0
            and isinstance(fixed_only_payload, dict)
            and fixed_only_payload.get("action") == "repaired"
            and fixed_only_payload.get("ready") is True
            and fixed_only_payload.get("lkg") == "refreshed"
            and [str(value.get("candidate_source", "")) for value in fixed_only_provenance] == ["local-fixed", "local-fixed"]
        )
        test_results.append(
            {
                "name": "fixed-only-repair-without-packaged-vendor-or-pip",
                "ready": fixed_only_ready,
                "report": fixed_only_payload,
                "stderr": fixed_only_completed.stderr[-4000:],
            }
        )
        if fixed_only_ready is not True:
            raise AssertionError(
                "Fixed-only no-pip repair regression failed: stdout="
                + fixed_only_completed.stdout[-4000:]
                + " stderr="
                + fixed_only_completed.stderr[-4000:]
            )

        candidate_state_root = temp_root / "candidate-health-state"
        candidate_state_code = "\n".join(
            [
                "import importlib.util,sys",
                f"p={str(source_path)!r}",
                "s=importlib.util.spec_from_file_location('codex_bootstrap_candidate_state',p)",
                "m=importlib.util.module_from_spec(s)",
                "s.loader.exec_module(m)",
                "target=m.Path(sys.executable)",
                "m._get_tried_python_paths=lambda:set()",
                "m._get_cached_supported_python_path=lambda:target",
                "m._discover_python_paths_via_launcher_list=lambda:tuple()",
                "m._discover_python_paths_from_common_locations=lambda:tuple()",
                "m._ordered_supported_python_targets=lambda:((sys.version_info.major,sys.version_info.minor),)",
                "m._read_python_path_from_command=lambda command:target",
                "failed=m.record_python_runtime_launch_failure(target,abi=m._python_tag(),failure_kind='native-crash',detail='self-test')",
                "blocked_target_discovery=[str(v) for v in m._discover_python_paths_via_launcher_targets()]",
                "blocked_candidates=[str(v) for v in m._iter_supported_python_candidates()]",
                "rehabilitated=m.record_python_runtime_rehabilitated(target,abi=m._python_tag(),detail='self-test fresh-child pass')",
                "m._discover_python_paths_via_launcher_targets=lambda *args,**kwargs:tuple()",
                "restored_candidates=[str(v) for v in m._iter_supported_python_candidates()]",
                "m._write_json_stdout({'failed':failed,'blocked_target_discovery':blocked_target_discovery,'blocked_candidates':blocked_candidates,'rehabilitated':rehabilitated,'restored_candidates':restored_candidates,'state_exists':m.BOOTSTRAP_RUNTIME_STATE_PATH.is_file()})",
            ]
        )
        candidate_state_env = os.environ.copy()
        candidate_state_env[RUNTIME_ROOT_OVERRIDE_ENV] = str(candidate_state_root)
        candidate_state_completed = _run_hidden_subprocess(
            [sys.executable, "-I", "-B", "-c", candidate_state_code],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            env=candidate_state_env,
        )
        candidate_state_payload = _decode_json_subprocess_output(candidate_state_completed.stdout)
        candidate_state_ready = (
            candidate_state_completed.returncode == 0
            and isinstance(candidate_state_payload, dict)
            and dict(candidate_state_payload.get("failed", {})).get("blocked") is True
            and candidate_state_payload.get("blocked_target_discovery") == []
            and candidate_state_payload.get("blocked_candidates") == []
            and dict(candidate_state_payload.get("rehabilitated", {})).get("blocked") is False
            and len(candidate_state_payload.get("restored_candidates", [])) == 1
            and candidate_state_payload.get("state_exists") is True
        )
        test_results.append(
            {
                "name": "runtime-candidate-quarantine-and-rehabilitation",
                "ready": candidate_state_ready,
                "returncode": candidate_state_completed.returncode,
                "report": candidate_state_payload,
                "stderr": candidate_state_completed.stderr[-4000:],
            }
        )
        if candidate_state_ready is not True:
            raise AssertionError(
                "Runtime candidate quarantine regression failed: stdout="
                + candidate_state_completed.stdout[-4000:]
                + " stderr="
                + candidate_state_completed.stderr[-4000:]
            )

        commit_root = temp_root / "atomic-commit"
        old_lane = commit_root / "vendor" / _python_tag()
        stage_lane = commit_root / "staging" / "candidate"
        old_lane.mkdir(parents=True)
        stage_lane.mkdir(parents=True)
        (old_lane / "marker.txt").write_text("old", encoding="ascii")
        (stage_lane / "marker.txt").write_text("new", encoding="ascii")
        quarantine_path = _commit_runtime_lane(
            stage_lane,
            old_lane,
            quarantine_root=commit_root / "quarantine",
        )
        commit_ready = (
            (old_lane / "marker.txt").read_text(encoding="ascii") == "new"
            and quarantine_path is not None
            and (quarantine_path / "marker.txt").read_text(encoding="ascii") == "old"
        )
        test_results.append(
            {
                "name": "atomic-commit-and-quarantine",
                "ready": commit_ready,
                "quarantine_path": str(quarantine_path),
            }
        )
        if commit_ready is not True:
            raise AssertionError("Atomic runtime lane commit regression failed.")

        repair_root = temp_root / "repair-runtime"
        broken_lane = repair_root / "vendor" / _python_tag()
        broken_package = broken_lane / "ufbx"
        broken_package.mkdir(parents=True)
        (broken_package / "__init__.py").write_text(
            "raise RuntimeError('intentional bootstrap repair self-test failure')\n",
            encoding="ascii",
        )
        repair_env = os.environ.copy()
        repair_env[RUNTIME_ROOT_OVERRIDE_ENV] = str(repair_root)
        repair_env["PYTHONUTF8"] = "1"
        repair_env["PYTHONDONTWRITEBYTECODE"] = "1"
        repair_completed = _run_hidden_subprocess(
            [
                sys.executable,
                "-I",
                "-B",
                str(source_path),
                "--repair-export-runtime",
                "--json",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env=repair_env,
        )
        repair_payload = _decode_json_subprocess_output(repair_completed.stdout)
        repaired_probe = _run_fresh_lane_probe(
            broken_lane,
            include_packaged=True,
            import_names=REPAIR_REQUIRED_IMPORTS,
        )
        repair_ready = (
            repair_completed.returncode == 0
            and isinstance(repair_payload, dict)
            and repair_payload.get("action") == "repaired"
            and repaired_probe.get("export_ready") is True
            and bool(repair_payload.get("quarantine_path"))
        )
        test_results.append(
            {
                "name": "broken-lane-transactional-repair",
                "ready": repair_ready,
                "returncode": repair_completed.returncode,
                "action": repair_payload.get("action") if isinstance(repair_payload, dict) else None,
                "quarantine_path": repair_payload.get("quarantine_path") if isinstance(repair_payload, dict) else None,
                "fresh_probe": repaired_probe,
                "stderr": repair_completed.stderr[-4000:],
            }
        )
        if repair_ready is not True:
            raise AssertionError(
                "Transactional repair regression failed: stdout="
                + repair_completed.stdout[-4000:]
                + " stderr="
                + repair_completed.stderr[-4000:]
            )

        def run_forced_recovery_case(runtime_root: Path, *, fail_packaged: bool) -> tuple[subprocess.CompletedProcess[str], dict[str, object] | None]:
            active_lane = runtime_root / "vendor" / _python_tag()
            active_package = active_lane / "ufbx"
            active_package.mkdir(parents=True)
            (active_package / "__init__.py").write_text(
                "raise RuntimeError('intentional forced recovery failure')\n",
                encoding="ascii",
            )
            recovery_lines = [
                "import importlib.util",
                f"p={str(source_path)!r}",
                "s=importlib.util.spec_from_file_location('codex_bootstrap_forced_recovery',p)",
                "m=importlib.util.module_from_spec(s)",
                "s.loader.exec_module(m)",
                "def fail_stage(stage_dir):",
                "    raise RuntimeError('intentional staging failure for recovery regression')",
                "m._rebuild_export_runtime_stage=fail_stage",
            ]
            if fail_packaged:
                recovery_lines.extend(
                    [
                        "original_probe=m._run_fresh_lane_probe",
                        "def force_packaged_failure(lane_dir, **kwargs):",
                        "    if kwargs.get('include_packaged') and not m.Path(lane_dir).exists():",
                        "        return {'classification':'probe-failed','ready':False,'export_ready':False,'error':'intentional packaged failure'}",
                        "    return original_probe(lane_dir, **kwargs)",
                        "m._run_fresh_lane_probe=force_packaged_failure",
                    ]
                )
            recovery_lines.append("m._write_json_stdout(m.repair_export_runtime())")
            recovery_env = os.environ.copy()
            recovery_env[RUNTIME_ROOT_OVERRIDE_ENV] = str(runtime_root)
            recovery_env["PYTHONDONTWRITEBYTECODE"] = "1"
            completed = _run_hidden_subprocess(
                [sys.executable, "-I", "-B", "-c", "\n".join(recovery_lines)],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                env=recovery_env,
            )
            return completed, _decode_json_subprocess_output(completed.stdout)

        packaged_recovery_root = temp_root / "packaged-recovery"
        packaged_completed, packaged_payload = run_forced_recovery_case(
            packaged_recovery_root,
            fail_packaged=False,
        )
        packaged_lane = packaged_recovery_root / "vendor" / _python_tag()
        packaged_probe = _run_fresh_lane_probe(
            packaged_lane,
            include_packaged=True,
            import_names=REPAIR_REQUIRED_IMPORTS,
        )
        packaged_recovery_ready = (
            packaged_completed.returncode == 0
            and isinstance(packaged_payload, dict)
            and packaged_payload.get("action") == "recovered-packaged"
            and packaged_lane.exists() is not True
            and packaged_probe.get("export_ready") is True
        )
        test_results.append(
            {
                "name": "staging-failure-recovers-packaged",
                "ready": packaged_recovery_ready,
                "returncode": packaged_completed.returncode,
                "action": packaged_payload.get("action") if isinstance(packaged_payload, dict) else None,
                "fresh_probe": packaged_probe,
                "stderr": packaged_completed.stderr[-4000:],
            }
        )
        if packaged_recovery_ready is not True:
            raise AssertionError(
                "Packaged fallback recovery regression failed: stdout="
                + packaged_completed.stdout[-4000:]
                + " stderr="
                + packaged_completed.stderr[-4000:]
            )

        lkg_recovery_root = temp_root / "last-known-good-recovery"
        lkg_seed = lkg_recovery_root / "quarantine" / f"{_python_tag()}-known-good-seed"
        packaged_ufbx_dir = PACKAGED_VENDOR_PY_DIR / "ufbx"
        if packaged_ufbx_dir.is_dir() is not True:
            raise AssertionError("Packaged ufbx ABI lane is missing; cannot seed last-known-good regression.")
        lkg_seed.mkdir(parents=True)
        for dependency_name in _iter_repair_dependency_closure("ufbx"):
            _copy_packaged_import_artifacts(dependency_name, lkg_seed)
        shutil.copytree(packaged_ufbx_dir, lkg_seed / "ufbx")
        lkg_completed, lkg_payload = run_forced_recovery_case(
            lkg_recovery_root,
            fail_packaged=True,
        )
        lkg_lane = lkg_recovery_root / "vendor" / _python_tag()
        lkg_probe = _run_fresh_lane_probe(
            lkg_lane,
            include_packaged=False,
            import_names=REPAIR_REQUIRED_IMPORTS,
        )
        lkg_recovery_ready = (
            lkg_completed.returncode == 0
            and isinstance(lkg_payload, dict)
            and lkg_payload.get("action") == "recovered-last-known-good"
            and lkg_lane.is_dir()
            and lkg_probe.get("export_ready") is True
        )
        test_results.append(
            {
                "name": "packaged-failure-recovers-last-known-good",
                "ready": lkg_recovery_ready,
                "returncode": lkg_completed.returncode,
                "action": lkg_payload.get("action") if isinstance(lkg_payload, dict) else None,
                "fresh_probe": lkg_probe,
                "stderr": lkg_completed.stderr[-4000:],
            }
        )
        if lkg_recovery_ready is not True:
            raise AssertionError(
                "Last-known-good recovery regression failed: stdout="
                + lkg_completed.stdout[-4000:]
                + " stderr="
                + lkg_completed.stderr[-4000:]
            )

    loaded_guard = _run_loaded_native_import_reuse_regression_guard()
    test_results.append(
        {
            "name": "loaded-native-module-reuse",
            "ready": loaded_guard.get("status") == "PASS",
            "report": loaded_guard,
        }
    )
    return {
        "tests": test_results,
        "test_count": len(test_results),
        "passed": all(test.get("ready") is True for test in test_results),
        "python_runtime": get_runtime_support_report(),
        "installed_packages": [],
    }


def ensure_export_runtime(
    *,
    import_checker: ImportChecker | None = None,
    installer: Installer | None = None,
) -> dict[str, object]:
    payload = ensure_named_modules_runtime(
        EXPORT_RUNTIME_MODULES,
        import_checker=import_checker,
        installer=installer,
    )
    payload["dependency_contract"] = get_dependency_bundle_contract_report(include_runtime_health=False)
    return payload


# =============================================================================
# Runtime health supervisor
#
# Bootstrap used to run only when an import/export asked for dependencies.  The
# Launcher now starts this small resident supervisor as well.  It deliberately
# owns diagnosis and repair decisions in one process: individual bridge modules
# only report their state and never race each other by repairing a live runtime.
# =============================================================================

HEALTH_SUPERVISOR_SCHEMA = "pc-rehd-code-x-health-supervisor-v1"
HEALTH_SUPERVISOR_DEFAULT_INTERVAL_SECONDS = 20.0
HEALTH_SUPERVISOR_DEFAULT_DEEP_INTERVAL_SECONDS = 300.0
HEALTH_SUPERVISOR_OPERATION_STALE_SECONDS = 30.0 * 60.0
HEALTH_SUPERVISOR_REPAIR_COOLDOWN_SECONDS = 5.0 * 60.0
HEALTH_SUPERVISOR_WATCHED_SOURCES = {
    "bootstrap": "codex_python_runtime_bootstrap.py",
    "launcher": "PC-REHD Code X Launcher.py",
    "writer": "codex_python_export_bridge.py",
    "importer": "codex_re6_mod_import_fbx.py",
    "fbx_probe": "codex_fbx_probe.py",
    "tex_decode": "codex_re6_tex_decode.py",
}
HEALTH_SUPERVISOR_LIGHTWEIGHT_IMPORTS = (
    "codex_fbx_probe",
    "codex_re6_tex_decode",
)

# Source syntax checks run on every supervisor heartbeat. Keep the compiled
# result keyed by file identity so an unchanged multi-megabyte Launcher does
# not get read, parsed, and hashed again every 20 seconds.
_HEALTH_SUPERVISOR_SOURCE_REPORT_CACHE: dict[
    tuple[str, bool, int, int], dict[str, object]
] = {}


def _health_supervisor_timestamp() -> dict[str, object]:
    now = time.time()
    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
        "timestamp_epoch": now,
    }


def _health_supervisor_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=str,
    ).encode("utf-8")


def _health_supervisor_source_report() -> dict[str, object]:
    files: dict[str, dict[str, object]] = {}
    errors: list[str] = []
    for label, name in HEALTH_SUPERVISOR_WATCHED_SOURCES.items():
        path = BASE_DIR / name
        path_text = str(path)
        try:
            stat_result = path.stat()
            exists = path.is_file()
            file_size = int(stat_result.st_size) if exists else 0
            file_mtime_ns = int(
                getattr(
                    stat_result,
                    "st_mtime_ns",
                    round(float(stat_result.st_mtime) * 1_000_000_000),
                )
            ) if exists else 0
        except OSError:
            exists = False
            file_size = 0
            file_mtime_ns = 0

        cache_key = (path_text, exists, file_size, file_mtime_ns)
        cached_item = _HEALTH_SUPERVISOR_SOURCE_REPORT_CACHE.get(cache_key)
        if cached_item is not None:
            item = copy.deepcopy(cached_item)
        else:
            item = {"path": path_text, "exists": exists}
            if not exists:
                item["status"] = "FAIL"
                item["error"] = "source file is missing"
            else:
                try:
                    source = path.read_bytes()
                    compile(source, path_text, "exec")
                    item.update(
                        {
                            "status": "PASS",
                            "bytes": len(source),
                            "sha256": hashlib.sha256(source).hexdigest().upper(),
                        }
                    )
                except Exception as exc:
                    item.update(
                        {
                            "status": "FAIL",
                            "error": f"{type(exc).__name__}: {exc}",
                            "traceback": traceback.format_exc(),
                        }
                    )
            # Keep only the current identity for each path while retaining the
            # full identity in the key for precise invalidation semantics.
            stale_keys = [
                key
                for key in _HEALTH_SUPERVISOR_SOURCE_REPORT_CACHE
                if key[0] == path_text and key != cache_key
            ]
            for stale_key in stale_keys:
                _HEALTH_SUPERVISOR_SOURCE_REPORT_CACHE.pop(stale_key, None)
            _HEALTH_SUPERVISOR_SOURCE_REPORT_CACHE[cache_key] = copy.deepcopy(item)
        files[label] = item
        if item.get("status") == "FAIL":
            errors.append(f"{label}: {item.get('error', 'source check failed')}")
    return {"status": "PASS" if not errors else "FAIL", "files": files, "errors": errors}


def _health_supervisor_probe_import(module_name: str) -> dict[str, object]:
    code = (
        "import importlib, json, sys; "
        "sys.path.insert(0, sys.argv[1]); "
        "importlib.import_module(sys.argv[2]); "
        "print(json.dumps({'status':'PASS','module':sys.argv[2]}))"
    )
    try:
        completed = _run_hidden_subprocess(
            [str(sys.executable), "-c", code, str(BASE_DIR), str(module_name)],
            cwd=str(BASE_DIR),
            check=False,
            capture_output=True,
            text=True,
            timeout=90.0,
            env=_isolated_python_child_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "module": module_name,
            "status": "FAIL",
            "error": "import probe timed out after 90 seconds",
            "stdout": str(exc.stdout or "")[-4000:],
            "stderr": str(exc.stderr or "")[-4000:],
        }
    except Exception as exc:
        return {"module": module_name, "status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
    result = {
        "module": module_name,
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "returncode": int(completed.returncode),
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }
    if completed.returncode != 0:
        result["error"] = "isolated import returned a non-zero exit code"
    return result


def _health_supervisor_deep_report() -> dict[str, object]:
    """Run expensive contracts in this isolated supervisor, never in a Max scene process."""
    report: dict[str, object] = {"status": "PASS", "errors": []}
    try:
        dependency_contract = get_dependency_bundle_contract_report(include_runtime_health=False)
        report["dependency_contract"] = dependency_contract
        if dependency_contract.get("ready") is not True:
            report["errors"].append("dependency contract is not ready")
    except Exception as exc:
        report["dependency_contract"] = {
            "ready": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
        report["errors"].append("dependency contract check raised an exception")

    try:
        bridge_contract = get_export_bridge_contract_report()
        report["bridge_contract"] = bridge_contract
        if bridge_contract.get("ready") is not True:
            report["errors"].append("writer/importer contract is not ready")
    except Exception as exc:
        report["bridge_contract"] = {
            "ready": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
        report["errors"].append("writer/importer contract check raised an exception")

    import_reports = [_health_supervisor_probe_import(name) for name in HEALTH_SUPERVISOR_LIGHTWEIGHT_IMPORTS]
    report["module_imports"] = import_reports
    for item in import_reports:
        if item.get("status") != "PASS":
            report["errors"].append(f"{item.get('module', 'unknown')} isolated import failed")
    if report["errors"]:
        report["status"] = "FAIL"
    return report


def _health_supervisor_repair_target(
    deep_report: dict[str, object],
) -> tuple[str, str]:
    """Return the only operation domain whose dependencies can be repaired."""
    dependency = deep_report.get("dependency_contract")
    if isinstance(dependency, dict) and dependency.get("ready") is not True:
        return "export_mod", "export dependency contract is not ready"
    repair_tokens = (
        "modulenotfounderror",
        "importerror",
        "no module named",
        "dll load",
        "winerror 126",
        "winerror 193",
        "abi",
        "native module",
    )
    for row in deep_report.get("module_imports", []):
        if not isinstance(row, dict) or row.get("status") == "PASS":
            continue
        if str(row.get("module", "") or "") != "codex_fbx_probe":
            continue
        detail = " ".join(str(row.get(key, "")) for key in ("error", "stderr", "stdout")).casefold()
        if any(token in detail for token in repair_tokens):
            return "export_mod", "repairable runtime import failure in codex_fbx_probe"
    return "", ""


def _health_supervisor_is_repairable(deep_report: dict[str, object]) -> tuple[bool, str]:
    """Only repair dependency/runtime damage; never overwrite user-edited source code."""
    repair_operation, reason = _health_supervisor_repair_target(deep_report)
    return bool(repair_operation), reason


def _health_supervisor_failure_operation(receipt: dict[str, object]) -> str:
    action = str(receipt.get("action", "") or "").strip()
    if not action:
        return ""
    try:
        return _normalize_operation_runtime_name(action)
    except ValueError:
        if action.casefold() in {"health", "inspect_scene"}:
            return "max_agent"
        return ""


def _run_operation_domain_isolation_regression_guard() -> dict[str, object]:
    forbidden_repair_calls = {
        "ensure_import_runtime",
        "ensure_module_runtime",
        "ensure_named_imports_runtime",
        "ensure_named_modules_runtime",
        "mark_import_version_blocked",
        "repair_export_runtime",
    }
    business_modules = (
        "codex_re6_mod_import_fbx.py",
        "codex_python_export_bridge.py",
        "codex_fbx_probe.py",
        "codex_re6_tex_decode.py",
    )
    violations: list[str] = []
    for file_name in business_modules:
        source_path = BASE_DIR / file_name
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                called_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                called_name = node.func.attr
            else:
                continue
            if called_name in forbidden_repair_calls:
                violations.append(f"{file_name}:{node.lineno}:{called_name}")
    if violations:
        raise RuntimeError(
            "Business modules entered Bootstrap repair APIs: " + ", ".join(violations)
        )

    texture_failure = {
        "status": "FAIL",
        "dependency_contract": {"ready": True},
        "module_imports": [
            {
                "module": "codex_re6_tex_decode",
                "status": "FAIL",
                "error": "ModuleNotFoundError",
            }
        ],
    }
    export_failure = {
        "status": "FAIL",
        "dependency_contract": {"ready": True},
        "module_imports": [
            {
                "module": "codex_fbx_probe",
                "status": "FAIL",
                "error": "ModuleNotFoundError: ufbx",
            }
        ],
    }
    if _health_supervisor_repair_target(texture_failure) != ("", ""):
        raise RuntimeError("Texture failure was incorrectly routed into runtime repair")
    if _health_supervisor_repair_target(export_failure)[0] != "export_mod":
        raise RuntimeError("Export dependency failure lost its explicit repair domain")
    if _health_supervisor_failure_operation({"action": "texture"}) != "texture":
        raise RuntimeError("Texture failure receipt lost its operation identity")
    if _health_supervisor_failure_operation({"action": "export_mod"}) != "export_mod":
        raise RuntimeError("Export failure receipt lost its operation identity")
    return {
        "status": "PASS",
        "business_modules": list(business_modules),
        "forbidden_repair_calls": sorted(forbidden_repair_calls),
        "cross_domain_repair_blocked": True,
        "export_repair_preserved": True,
    }


# One resident supervisor per Launcher owns diagnosis and writes one classified
# error-only TXT. Normal operation receipts stay in named Windows memory.
# The Bootstrap supervisor has one public human-readable health artifact.
# Keep this in sync with the Launcher TXT contract; the Launcher still
# migrates the pre-existing legacy name on startup.
HEALTH_SUPERVISOR_LOG_FILE_NAME = "Bootstrap RE6 Script Health check 脚本健康度日志.txt"
HEALTH_SUPERVISOR_LOG_RETENTION_SECONDS = 10.0 * 24.0 * 60.0 * 60.0
_HEALTH_SUPERVISOR_EVENT_HANDLES: dict[tuple[int, str], int] = {}
HEALTH_SUPERVISOR_FAILURE_RECEIPT_BYTES = 64 * 1024
HEALTH_SUPERVISOR_OPERATION_RECEIPT_SCHEMA = "pc-rehd-code-x-health-operation-state-v2"
HEALTH_SUPERVISOR_MAX_ACTIVE_RECEIPTS = 12
HEALTH_SUPERVISOR_MAX_FAILURE_RECEIPTS = 8
HEALTH_SUPERVISOR_USER_OPERATION_PATTERNS = (
    "scenechangederror",
    "max scene changed before",
    "max scene names changed repeatedly",
    "mesh rename stale node",
    "mesh filter node handles are missing",
    "duplicate mesh rename target",
    "select one or more mesh",
    "no mesh selected",
    "no source mesh selected",
    "请先在 max 中选择",
    "请先在 max 场景中选择",
    "未选择 mesh",
    "is outside 0..255",
)
_HEALTH_SUPERVISOR_FAILURE_RECEIPTS: dict[int, mmap.mmap] = {}
_HEALTH_SUPERVISOR_FAILURE_FALLBACKS: dict[int, dict[str, object]] = {}


def _health_supervisor_log_root(log_dir: str | Path | None = None) -> Path:
    """Use the Launcher log directory itself; Bootstrap never creates a log tree."""
    requested = str(log_dir or "").strip()
    candidates: list[Path] = []

    def add(path_value: str | Path | None) -> None:
        if path_value in {None, ""}:
            return
        candidate = Path(os.path.expandvars(str(path_value))).expanduser()
        key = os.path.normcase(os.path.abspath(os.fspath(candidate)))
        if all(os.path.normcase(os.path.abspath(os.fspath(existing))) != key for existing in candidates):
            candidates.append(candidate)

    add(requested)
    add(Path(tempfile.gettempdir()) / "PC_REHD_Code_X")
    add(Path(os.environ["LOCALAPPDATA"]) / "PC_REHD_Code_X" if os.environ.get("LOCALAPPDATA") else None)
    add(Path.home() / "PC_REHD_Code_X")
    last_error: Exception | None = None
    for root in candidates:
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe_path = root / f".codex_health_write_probe_{os.getpid()}_{uuid.uuid4().hex}.tmp"
            _atomic_write_runtime_bytes(probe_path, b"ok")
            probe_path.unlink(missing_ok=True)
            return root
        except Exception as exc:
            last_error = exc
    raise RuntimeError(
        "Bootstrap health supervisor cannot use the Launcher log directory: "
        + str(last_error or "unknown filesystem error")
    )


def _health_supervisor_log_path(root: Path) -> Path:
    return root / HEALTH_SUPERVISOR_LOG_FILE_NAME


@contextmanager
def _health_supervisor_named_mutex(scope: str, identity: str, *, timeout_ms: int = 5000):
    """Serialize one text log without persisting a second lock file on disk."""
    if os.name != "nt":
        yield True
        return
    import ctypes
    from ctypes import wintypes

    digest = hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest().upper()
    name = f"Local\\PC_REHD_Code_X_Bootstrap_{scope}_{digest}"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise OSError(ctypes.get_last_error(), f"CreateMutexW failed for {name}")
    acquired = False
    try:
        result = int(kernel32.WaitForSingleObject(handle, max(0, int(timeout_ms))))
        acquired = result in {0x00000000, 0x00000080}
        yield acquired
    finally:
        if acquired:
            kernel32.ReleaseMutex(handle)
        kernel32.CloseHandle(handle)


def _health_supervisor_tail(text_value: object, limit: int = 1800) -> str:
    value = str(text_value or "").strip()
    return value[-max(0, int(limit)):] if value else ""


def _health_supervisor_relevant_details(snapshot: dict[str, object]) -> dict[str, object]:
    """Keep the one user-facing log focused on the failure rather than success noise."""
    details: dict[str, object] = {
        "issues": [str(item) for item in snapshot.get("issues", []) if str(item).strip()],
    }
    source = snapshot.get("source")
    if isinstance(source, dict):
        failed_sources = {
            name: {
                "path": row.get("path"),
                "error": row.get("error"),
                "traceback": _health_supervisor_tail(row.get("traceback")),
            }
            for name, row in source.get("files", {}).items()
            if isinstance(row, dict) and row.get("status") != "PASS"
        }
        if failed_sources:
            details["source_failures"] = failed_sources
    runtime = snapshot.get("runtime")
    if isinstance(runtime, dict) and runtime.get("supported") is not True and runtime.get("override_enabled") is not True:
        details["runtime"] = {
            key: runtime.get(key)
            for key in ("current_python", "supported", "override_enabled", "error", "traceback")
            if runtime.get(key) not in {None, ""}
        }
    deep = snapshot.get("deep")
    if isinstance(deep, dict) and deep.get("status") == "FAIL":
        reduced_deep: dict[str, object] = {"errors": list(deep.get("errors", []))}
        dependency = deep.get("dependency_contract")
        if isinstance(dependency, dict) and dependency.get("ready") is not True:
            reduced_deep["dependency_contract"] = {
                key: dependency.get(key)
                for key in ("ready", "error", "errors", "traceback")
                if dependency.get(key) not in {None, "", []}
            }
        bridge = deep.get("bridge_contract")
        if isinstance(bridge, dict) and bridge.get("ready") is not True:
            reduced_deep["writer_importer_contract"] = {
                key: bridge.get(key)
                for key in ("ready", "error_type", "error", "traceback", "maintenance_error")
                if bridge.get(key) not in {None, ""}
            }
            if "traceback" in reduced_deep["writer_importer_contract"]:
                reduced_deep["writer_importer_contract"]["traceback"] = _health_supervisor_tail(
                    reduced_deep["writer_importer_contract"]["traceback"]
                )
        failed_imports = []
        for row in deep.get("module_imports", []):
            if isinstance(row, dict) and row.get("status") != "PASS":
                failed_imports.append(
                    {
                        "module": row.get("module"),
                        "error": row.get("error"),
                        "returncode": row.get("returncode"),
                        "stderr": _health_supervisor_tail(row.get("stderr")),
                    }
                )
        if failed_imports:
            reduced_deep["failed_module_imports"] = failed_imports
        details["deep_check"] = reduced_deep
    repair = snapshot.get("repair")
    if isinstance(repair, dict) and repair.get("attempted") is True:
        result = repair.get("result")
        details["auto_repair"] = {
            "reason": repair.get("reason"),
            "status": repair.get("status"),
            "error": repair.get("error"),
            "traceback": _health_supervisor_tail(repair.get("traceback")),
            "action": result.get("action") if isinstance(result, dict) else None,
            "maintenance_warnings": result.get("maintenance_warnings") if isinstance(result, dict) else None,
        }
    return details


def _health_supervisor_event_name(parent_pid: int, kind: str) -> str:
    return f"Local\\PC_REHD_Code_X_Health_{str(kind).upper()}_{max(0, int(parent_pid))}"


def _health_supervisor_set_named_event(parent_pid: int, kind: str, *, active: bool, manual_reset: bool) -> None:
    if os.name != "nt" or int(parent_pid or 0) <= 0:
        return
    import ctypes
    from ctypes import wintypes

    key = (int(parent_pid), str(kind).upper())
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
    kernel32.ResetEvent.restype = wintypes.BOOL
    handle = _HEALTH_SUPERVISOR_EVENT_HANDLES.get(key)
    if not handle:
        handle = kernel32.CreateEventW(None, bool(manual_reset), False, _health_supervisor_event_name(*key))
        if not handle:
            raise OSError(ctypes.get_last_error(), f"CreateEventW failed for Bootstrap health {kind}")
        _HEALTH_SUPERVISOR_EVENT_HANDLES[key] = int(handle)
    action = kernel32.SetEvent if active else kernel32.ResetEvent
    if not action(handle):
        raise OSError(ctypes.get_last_error(), f"Unable to update Bootstrap health {kind} event")


def _health_supervisor_named_event_is_set(parent_pid: int, kind: str) -> bool:
    if os.name != "nt" or int(parent_pid or 0) <= 0:
        return False
    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenEventW(synchronize, False, _health_supervisor_event_name(parent_pid, kind))
    if not handle:
        return False
    try:
        return int(kernel32.WaitForSingleObject(handle, 0)) == 0x00000000
    finally:
        kernel32.CloseHandle(handle)


def _health_supervisor_operation_receipt_name(launcher_pid: int) -> str:
    # Preserve the existing tag name so a supervisor can finish a Launcher that
    # was already running when this Bootstrap source was updated.
    return f"Local\\PC_REHD_Code_X_HealthFailure_{max(0, int(launcher_pid or 0))}"


def _health_supervisor_open_operation_receipts(
    launcher_pid: int,
    *,
    retain: bool,
) -> mmap.mmap | None:
    """Open one per-Launcher queue; retained supervisor handles survive Launcher crashes."""
    pid = max(0, int(launcher_pid or 0))
    if pid <= 0 or os.name != "nt":
        return None
    if retain:
        existing = _HEALTH_SUPERVISOR_FAILURE_RECEIPTS.get(pid)
        if existing is not None:
            return existing
    try:
        mapping = mmap.mmap(
            -1,
            HEALTH_SUPERVISOR_FAILURE_RECEIPT_BYTES,
            tagname=_health_supervisor_operation_receipt_name(pid),
            access=mmap.ACCESS_WRITE,
        )
    except (OSError, ValueError):
        return None
    if retain:
        _HEALTH_SUPERVISOR_FAILURE_RECEIPTS[pid] = mapping
    return mapping


def _health_supervisor_empty_operation_receipts(launcher_pid: int) -> dict[str, object]:
    return {
        "schema": HEALTH_SUPERVISOR_OPERATION_RECEIPT_SCHEMA,
        "launcher_pid": max(0, int(launcher_pid or 0)),
        "active": [],
        "failures": [],
    }


def _health_supervisor_compact_operation_receipt(payload: dict[str, object]) -> dict[str, object]:
    """Keep enough evidence for diagnosis while bounding the shared-memory queue."""
    extra = payload.get("extra")
    compact_extra: object = {}
    if isinstance(extra, dict) and extra:
        encoded_extra = json.dumps(extra, ensure_ascii=False, sort_keys=True, default=str)
        compact_extra = (
            dict(extra)
            if len(encoded_extra) <= 1800
            else {"truncated_excerpt": _health_supervisor_tail(encoded_extra, 1800)}
        )
    request_id = str(payload.get("request_id", "") or "")[:256]
    receipt_id = str(payload.get("receipt_id", "") or "").strip()
    if not receipt_id:
        identity = {
            "action": str(payload.get("action", "unknown") or "unknown"),
            "launcher_pid": max(0, int(payload.get("launcher_pid", 0) or 0)),
            "max_process_id": max(0, int(payload.get("max_process_id", 0) or 0)),
            "request_id": request_id,
            "phase": str(payload.get("phase", "") or ""),
            "reported": payload.get("reported"),
        }
        receipt_id = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:32]
    reported = payload.get("reported")
    return {
        "receipt_id": receipt_id,
        "action": str(payload.get("action", "unknown") or "unknown")[:160],
        "phase": str(payload.get("phase", "unknown") or "unknown")[:32],
        "launcher_pid": max(0, int(payload.get("launcher_pid", 0) or 0)),
        "max_process_id": max(0, int(payload.get("max_process_id", 0) or 0)),
        "request_id": request_id,
        "detail": _health_supervisor_tail(payload.get("detail"), 2400),
        "extra": compact_extra,
        "reported": dict(reported) if isinstance(reported, dict) else _health_supervisor_timestamp(),
    }


def _health_supervisor_normalize_operation_receipts(
    payload: object,
    launcher_pid: int,
) -> dict[str, object]:
    state = _health_supervisor_empty_operation_receipts(launcher_pid)
    if not isinstance(payload, dict):
        return state
    if payload.get("schema") == HEALTH_SUPERVISOR_OPERATION_RECEIPT_SCHEMA:
        active = payload.get("active")
        failures = payload.get("failures")
        state["active"] = [
            _health_supervisor_compact_operation_receipt(row)
            for row in active
            if isinstance(row, dict)
        ] if isinstance(active, list) else []
        state["failures"] = [
            _health_supervisor_compact_operation_receipt(row)
            for row in failures
            if isinstance(row, dict)
        ] if isinstance(failures, list) else []
        return state

    # Read the retired one-receipt payload long enough to diagnose Launchers
    # that were already running during this source upgrade.
    phase = str(payload.get("phase", "") or "").casefold()
    receipt = _health_supervisor_compact_operation_receipt(payload)
    if phase == "started":
        state["active"] = [receipt]
    elif phase in {"failed", "stalled", "launcher_crashed"}:
        state["failures"] = [receipt]
    return state


def _health_supervisor_read_operation_receipts_unlocked(launcher_pid: int) -> dict[str, object]:
    pid = max(0, int(launcher_pid or 0))
    mapping = _health_supervisor_open_operation_receipts(pid, retain=False)
    if mapping is None:
        return _health_supervisor_normalize_operation_receipts(
            _HEALTH_SUPERVISOR_FAILURE_FALLBACKS.get(pid),
            pid,
        )
    try:
        mapping.seek(0)
        size_bytes = mapping.read(4)
        if len(size_bytes) != 4:
            return _health_supervisor_empty_operation_receipts(pid)
        size = struct.unpack("<I", size_bytes)[0]
        if size <= 0 or size > HEALTH_SUPERVISOR_FAILURE_RECEIPT_BYTES - 4:
            return _health_supervisor_empty_operation_receipts(pid)
        return _health_supervisor_normalize_operation_receipts(
            json.loads(mapping.read(size).decode("utf-8")),
            pid,
        )
    except (BufferError, OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return _health_supervisor_empty_operation_receipts(pid)
    finally:
        mapping.close()


def _health_supervisor_read_operation_receipts(launcher_pid: int) -> dict[str, object]:
    pid = max(0, int(launcher_pid or 0))
    with _health_supervisor_named_mutex("OperationReceipt", str(pid)) as acquired:
        if acquired is not True:
            return _health_supervisor_empty_operation_receipts(pid)
        return _health_supervisor_read_operation_receipts_unlocked(pid)


def _health_supervisor_write_operation_receipts_unlocked(
    launcher_pid: int,
    state: dict[str, object],
) -> str:
    pid = max(0, int(launcher_pid or 0))
    normalized = _health_supervisor_normalize_operation_receipts(state, pid)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    maximum = HEALTH_SUPERVISOR_FAILURE_RECEIPT_BYTES - 4
    if len(encoded) > maximum:
        for collection_name in ("active", "failures"):
            collection = normalized.get(collection_name)
            if not isinstance(collection, list):
                continue
            for receipt in collection:
                if isinstance(receipt, dict):
                    receipt["detail"] = _health_supervisor_tail(receipt.get("detail"), 600)
                    receipt["extra"] = {}
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    if len(encoded) > maximum:
        return "operation receipt queue exceeds the fixed in-memory handoff capacity"

    _HEALTH_SUPERVISOR_FAILURE_FALLBACKS[pid] = dict(normalized)
    mapping = _health_supervisor_open_operation_receipts(pid, retain=True)
    if mapping is None:
        return "named operation receipt queue is unavailable"
    try:
        mapping.seek(0)
        mapping.write(struct.pack("<I", 0))
        mapping.write(encoded)
        mapping.seek(0)
        mapping.write(struct.pack("<I", len(encoded)))
        mapping.flush()
    except (BufferError, OSError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return ""


def _health_supervisor_find_active_receipt(
    active: list[dict[str, object]],
    payload: dict[str, object],
) -> int:
    request_id = str(payload.get("request_id", "") or "")
    if request_id:
        for index, receipt in enumerate(active):
            if str(receipt.get("request_id", "") or "") == request_id:
                return index
        # During the request-token migration an already-running Launcher may
        # finish with its inner Max request ID while the active receipt still
        # contains the old token. Only recover when the action/PID identifies
        # exactly one active receipt; never guess across concurrent operations.
        action = str(payload.get("action", "unknown") or "unknown").casefold()
        max_process_id = max(0, int(payload.get("max_process_id", 0) or 0))
        candidates = [
            index
            for index, receipt in enumerate(active)
            if (
                str(receipt.get("action", "unknown") or "unknown").casefold() == action
                and max(0, int(receipt.get("max_process_id", 0) or 0)) == max_process_id
            )
        ]
        if len(candidates) == 1:
            return candidates[0]
        return -1
    action = str(payload.get("action", "unknown") or "unknown").casefold()
    max_process_id = max(0, int(payload.get("max_process_id", 0) or 0))
    for index, receipt in enumerate(active):
        if (
            str(receipt.get("action", "unknown") or "unknown").casefold() == action
            and max(0, int(receipt.get("max_process_id", 0) or 0)) == max_process_id
        ):
            return index
    return -1


def _health_supervisor_update_operation_receipts(
    launcher_pid: int,
    payload: dict[str, object],
    *,
    retain_failure: bool,
) -> tuple[dict[str, object], str]:
    pid = max(0, int(launcher_pid or 0))
    phase = str(payload.get("phase", "") or "").casefold()
    with _health_supervisor_named_mutex("OperationReceipt", str(pid)) as acquired:
        if acquired is not True:
            return _health_supervisor_empty_operation_receipts(pid), "timed out updating operation receipts"
        state = _health_supervisor_read_operation_receipts_unlocked(pid)
        active = [dict(row) for row in state.get("active", []) if isinstance(row, dict)]
        failures = [dict(row) for row in state.get("failures", []) if isinstance(row, dict)]
        active_index = _health_supervisor_find_active_receipt(active, payload)
        inherited_receipt_id = ""
        if phase == "started":
            started_payload = dict(payload)
            started_payload["receipt_id"] = str(payload.get("receipt_id", "") or uuid.uuid4().hex)
            receipt = _health_supervisor_compact_operation_receipt(started_payload)
            if active_index >= 0 and str(payload.get("request_id", "") or ""):
                active[active_index] = receipt
            else:
                active.append(receipt)
            if len(active) > HEALTH_SUPERVISOR_MAX_ACTIVE_RECEIPTS:
                return state, "too many concurrent Launcher operations for the health receipt queue"
        elif phase in {"completed", "failed", "cancelled"}:
            if active_index >= 0:
                inherited_receipt_id = str(active.pop(active_index).get("receipt_id", "") or "")
            if phase == "failed" and retain_failure:
                failed_payload = dict(payload)
                failed_payload["receipt_id"] = inherited_receipt_id or uuid.uuid4().hex
                failures.append(_health_supervisor_compact_operation_receipt(failed_payload))
                if len(failures) > HEALTH_SUPERVISOR_MAX_FAILURE_RECEIPTS:
                    return state, "too many pending Launcher failures for the health receipt queue"
        state["active"] = active
        state["failures"] = failures
        return state, _health_supervisor_write_operation_receipts_unlocked(pid, state)


def _health_supervisor_consume_operation_receipts(
    launcher_pid: int,
    *,
    active_receipt_ids: set[str] | None = None,
    failure_receipt_ids: set[str] | None = None,
) -> str:
    pid = max(0, int(launcher_pid or 0))
    active_ids = {str(value) for value in (active_receipt_ids or set()) if str(value)}
    failure_ids = {str(value) for value in (failure_receipt_ids or set()) if str(value)}
    with _health_supervisor_named_mutex("OperationReceipt", str(pid)) as acquired:
        if acquired is not True:
            return "timed out consuming operation receipts"
        state = _health_supervisor_read_operation_receipts_unlocked(pid)
        if active_ids:
            state["active"] = [
                row
                for row in state.get("active", [])
                if isinstance(row, dict) and str(row.get("receipt_id", "") or "") not in active_ids
            ]
        if failure_ids:
            state["failures"] = [
                row
                for row in state.get("failures", [])
                if isinstance(row, dict) and str(row.get("receipt_id", "") or "") not in failure_ids
            ]
        return _health_supervisor_write_operation_receipts_unlocked(pid, state)


def _health_supervisor_is_user_operation_error(receipt: dict[str, object]) -> bool:
    """Exclude expected user/scene validation failures without hiding Python defects."""
    phase = str(receipt.get("phase", "") or "").casefold()
    if phase == "cancelled":
        return True
    evidence = json.dumps(receipt, ensure_ascii=False, sort_keys=True, default=str).casefold()
    if any(
        code in evidence
        for code in ("operation_invalid_request", "aux_invalid_request")
    ):
        return True
    return any(pattern in evidence for pattern in HEALTH_SUPERVISOR_USER_OPERATION_PATTERNS)


def _health_supervisor_operation_state_has_work(state: dict[str, object]) -> bool:
    return bool(state.get("active") or state.get("failures"))


def _health_supervisor_operation_failure_issue(
    snapshot: dict[str, object],
    receipt: dict[str, object],
) -> tuple[str, str, dict[str, object]]:
    """Attribute a failed Launcher operation without claiming to repair scene data."""
    details = _health_supervisor_relevant_details(snapshot)
    action = str(receipt.get("action", "unknown") or "unknown").strip() or "unknown"
    action_key = re.sub(r"[^A-Z0-9_]+", "_", action.upper()).strip("_") or "UNKNOWN"
    launcher_pid = max(0, int(receipt.get("launcher_pid", 0) or 0))
    task_identity = str(
        receipt.get("request_id", "") or receipt.get("receipt_id", "") or "UNTRACKED"
    )
    task_key = re.sub(r"[^A-Z0-9_]+", "_", task_identity.upper()).strip("_")[:32] or "UNTRACKED"
    raw_detail = str(receipt.get("detail", "") or "")
    probe_text = raw_detail.casefold()
    component = "MAX_AGENT_OR_SCENE"
    reason = "Python runtime checks passed, so Bootstrap cannot safely attribute the live operation beyond Max Agent, scene data, or source content."

    source_failures = details.get("source_failures")
    if str(receipt.get("phase", "") or "").casefold() == "launcher_crashed":
        component = "LAUNCHER_PROCESS"
        reason = "The Launcher process exited before this operation produced a terminal receipt."
    elif isinstance(source_failures, dict) and source_failures:
        component = "PYTHON_SOURCE"
        reason = "A watched Python source module failed its syntax or availability check."
    elif isinstance(details.get("runtime"), dict):
        component = "PYTHON_RUNTIME"
        reason = "The active Python ABI is not supported by the packaged runtime."
    else:
        deep = details.get("deep_check")
        if isinstance(deep, dict) and isinstance(deep.get("dependency_contract"), dict):
            component = "PYTHON_DEPENDENCY"
            reason = "The packaged Python dependency contract is not ready."
        elif isinstance(deep, dict) and isinstance(deep.get("writer_importer_contract"), dict):
            component = "WRITER_IMPORTER_CONTRACT"
            reason = "The writer/importer maintenance contract failed."
        elif any(token in probe_text for token in ("permissionerror", "access is denied", "winerror 5", "disk full", "no space")):
            component = "FILESYSTEM"
            reason = "The operation reported a permissions, path, or disk-capacity failure."
        elif any(token in probe_text for token in ("modulenotfounderror", "importerror", "dll load", "winerror 126", "winerror 193", "abi")):
            component = "PYTHON_RUNTIME"
            reason = "The operation reported a Python import, native DLL, or ABI failure."
        elif any(token in probe_text for token in ("codex_python_export_bridge", "memory writer", "writer receipt", "newmod")):
            component = "PYTHON_WRITER"
            reason = "The failure text points to the Python .MOD writer path."
        elif any(token in probe_text for token in ("codex_re6_mod_import_fbx", "importer", "normal mode", "fbx explicit normals")):
            component = "PYTHON_IMPORTER"
            reason = "The failure text points to the Python .MOD-to-FBX importer path."
        elif any(token in probe_text for token in ("protocolerror", "agent", "max pid", "3dsmax", "socket", "timeout")):
            component = "MAX_AGENT"
            reason = "The failure text points to the Max Agent handshake or live Max process."

    repair = snapshot.get("repair")
    detail_payload = {
        "component": component,
        "component_reason": reason,
        "launcher_failure": receipt,
        "health_check": details,
        "automatic_repair": repair if isinstance(repair, dict) else {"attempted": False},
    }
    summary = (
        f"Launcher PID {launcher_pid} {action} failed; Bootstrap could not "
        f"automatically recover the {component} component."
    )
    category = f"OPERATION:{action_key}:LAUNCHER_{launcher_pid}:TASK_{task_key}:{component}"
    return category, summary, detail_payload


def report_health_supervisor_operation(
    log_dir: str | Path | None,
    *,
    action: str,
    phase: str,
    max_process_id: int = 0,
    request_id: str = "",
    detail: str = "",
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Queue lifecycle receipts; only unresolved system failures reach the health TXT."""
    root = _health_supervisor_log_root(log_dir)
    normalized_phase = str(phase or "").strip().casefold()
    launcher_pid = os.getpid()
    payload: dict[str, object] = {
        "action": str(action or "unknown").strip() or "unknown",
        "phase": normalized_phase or "unknown",
        "launcher_pid": launcher_pid,
        "max_process_id": max(0, int(max_process_id or 0)),
        "request_id": str(request_id or "").strip(),
        "detail": str(detail or "").strip(),
    }
    if extra:
        payload["extra"] = dict(extra)
    if normalized_phase not in {"started", "completed", "failed", "cancelled"}:
        payload["health_handoff_ignored"] = "unsupported lifecycle phase"
        return payload

    user_operation_error = (
        normalized_phase == "failed"
        and _health_supervisor_is_user_operation_error(payload)
    )
    receipt_state, receipt_error = _health_supervisor_update_operation_receipts(
        launcher_pid,
        payload,
        retain_failure=normalized_phase == "failed" and not user_operation_error,
    )
    _health_supervisor_set_named_event(
        launcher_pid,
        "active",
        active=bool(receipt_state.get("active")),
        manual_reset=True,
    )

    if user_operation_error:
        payload["health_log_suppressed"] = "expected user or scene validation error"
        _health_supervisor_prune_log(root)
        return payload

    if receipt_error:
        payload["receipt_transport_error"] = receipt_error
        path = _health_supervisor_append_error(
            root,
            kind="BOOTSTRAP_OPERATION_HANDOFF_FAILURE",
            summary="Bootstrap could not update the Launcher operation receipt queue.",
            details={**payload, "component": "BOOTSTRAP_OPERATION_HANDOFF"},
        )
        payload["log_path"] = str(path)
    if normalized_phase == "completed":
        _health_supervisor_resolve_operation_failures(
            root,
            action=str(payload["action"]),
            max_process_id=int(payload["max_process_id"]),
        )
    if normalized_phase == "failed":
        _health_supervisor_set_named_event(launcher_pid, "error", active=True, manual_reset=False)
        payload["diagnosis_pending"] = True
        if not receipt_error and str(payload["action"]).casefold() == "bootstrap_supervisor":
            # The analyzer itself did not start, so it cannot consume its event.
            path = _health_supervisor_append_error(
                root,
                kind="BOOTSTRAP_SUPERVISOR_FAILURE",
                summary="Bootstrap Health Supervisor did not start.",
                details={**payload, "component": "BOOTSTRAP_SUPERVISOR"},
            )
            payload["log_path"] = str(path)
    return payload


@contextmanager
def _health_supervisor_lease(root: Path, launcher_pid: int):
    """Keep one supervisor per Launcher while sharing one serialized error log."""
    path = _health_supervisor_log_path(root)
    identity = f"{path}|launcher_parent={max(0, int(launcher_pid or 0))}"
    with _health_supervisor_named_mutex("Supervisor", identity, timeout_ms=0) as acquired:
        yield {"acquired": bool(acquired), "log_path": str(path)}


class BootstrapHealthSupervisor:
    """Resident checks with zero normal-operation log noise and one rolling error file."""

    def __init__(
        self,
        *,
        log_dir: str | Path | None = None,
        parent_pid: int = 0,
        interval_seconds: float = HEALTH_SUPERVISOR_DEFAULT_INTERVAL_SECONDS,
        deep_interval_seconds: float = HEALTH_SUPERVISOR_DEFAULT_DEEP_INTERVAL_SECONDS,
    ) -> None:
        self.root = _health_supervisor_log_root(log_dir)
        self.parent_pid = max(0, int(parent_pid or 0))
        self.interval_seconds = max(5.0, float(interval_seconds or HEALTH_SUPERVISOR_DEFAULT_INTERVAL_SECONDS))
        self.deep_interval_seconds = max(self.interval_seconds, float(deep_interval_seconds or HEALTH_SUPERVISOR_DEFAULT_DEEP_INTERVAL_SECONDS))
        # Do not spend the first Launcher seconds compiling the full Writer /
        # Importer contract.  A user may start an import/export immediately;
        # the first deep check can wait for the normal maintenance interval.
        self.last_deep_at = time.time()
        self.last_repair_at = 0.0
        self.last_deep_report: dict[str, object] | None = None
        self._retained_operation_receipts = _health_supervisor_open_operation_receipts(
            self.parent_pid,
            retain=True,
        )
        _health_supervisor_prune_log(self.root)

    def _parent_is_alive(self) -> bool:
        return self.parent_pid <= 0 or _process_is_alive(self.parent_pid) is True

    def _snapshot(self, *, deep: bool) -> dict[str, object]:
        source = _health_supervisor_source_report()
        try:
            runtime = get_runtime_support_report()
        except Exception as exc:
            runtime = {
                "supported": False,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        issues = list(source.get("errors", []))
        if runtime.get("supported") is not True and runtime.get("override_enabled") is not True:
            issues.append("current Python ABI is not supported by the packaged runtime")
        deep_report = self.last_deep_report
        if deep and source.get("status") == "PASS":
            deep_report = _health_supervisor_deep_report()
            self.last_deep_report = deep_report
            self.last_deep_at = time.time()
        if isinstance(deep_report, dict) and deep_report.get("status") == "FAIL":
            issues.extend(str(item) for item in deep_report.get("errors", []) if str(item).strip())
        return {
            "schema": HEALTH_SUPERVISOR_SCHEMA,
            "status": "FAIL" if issues else "PASS",
            "supervisor_pid": os.getpid(),
            "parent_pid": self.parent_pid,
            "source": source,
            "runtime": runtime,
            "deep": deep_report,
            "operation_active": _health_supervisor_named_event_is_set(self.parent_pid, "active"),
            "issues": issues,
            **_health_supervisor_timestamp(),
        }

    def run_cycle(
        self,
        *,
        force_deep: bool = False,
        failure_signalled: bool = False,
        parent_exited: bool = False,
    ) -> dict[str, object]:
        receipt_state = _health_supervisor_read_operation_receipts(self.parent_pid)
        active_receipts = [
            dict(row) for row in receipt_state.get("active", []) if isinstance(row, dict)
        ]
        queued_failures = [
            dict(row) for row in receipt_state.get("failures", []) if isinstance(row, dict)
        ]
        now_epoch = time.time()
        stalled_receipts: list[dict[str, object]] = []
        for receipt in active_receipts:
            reported = receipt.get("reported")
            started_epoch = (
                float(reported.get("timestamp_epoch", 0.0) or 0.0)
                if isinstance(reported, dict)
                else 0.0
            )
            if (
                not parent_exited
                and started_epoch > 0.0
                and now_epoch - started_epoch >= HEALTH_SUPERVISOR_OPERATION_STALE_SECONDS
            ):
                stalled = dict(receipt)
                stalled["phase"] = "stalled"
                stalled["detail"] = (
                    "Launcher operation remained active for at least "
                    f"{int(HEALTH_SUPERVISOR_OPERATION_STALE_SECONDS)} seconds "
                    "without a terminal receipt."
                )
                stalled_receipts.append(stalled)

        crash_receipts: list[dict[str, object]] = []
        if parent_exited:
            for receipt in active_receipts:
                crashed = dict(receipt)
                crashed["phase"] = "launcher_crashed"
                crashed["detail"] = (
                    f"Launcher PID {self.parent_pid} exited before "
                    f"{str(receipt.get('action', 'unknown') or 'unknown')} completed."
                )
                crash_receipts.append(crashed)

        signalled_event = (
            _health_supervisor_named_event_is_set(self.parent_pid, "error")
            if not parent_exited
            else False
        )
        diagnostic_receipts = queued_failures + crash_receipts + stalled_receipts
        signalled_error = bool(
            failure_signalled
            or signalled_event
            or diagnostic_receipts
        )
        operation_active = bool(
            not parent_exited
            and (
                bool(active_receipts)
                or _health_supervisor_named_event_is_set(self.parent_pid, "active")
            )
        )
        due_deep = (time.time() - self.last_deep_at) >= self.deep_interval_seconds
        deep_requested = bool(force_deep or signalled_error or due_deep)
        # Deep contract imports/AST audits are maintenance work.  They must
        # never compete with an active import/export; the next idle cycle will
        # retry because last_deep_at is unchanged while the check is deferred.
        deep_deferred = bool(operation_active and deep_requested)
        snapshot = self._snapshot(deep=bool(deep_requested and not operation_active))
        snapshot["operation_active"] = operation_active
        snapshot["deep_check_deferred"] = deep_deferred
        snapshot["operation_receipts"] = {
            "active_count": len(active_receipts),
            "pending_failure_count": len(queued_failures),
            "stalled_count": len(stalled_receipts),
            "launcher_exited": bool(parent_exited),
        }
        if diagnostic_receipts:
            snapshot["launcher_failures"] = [dict(row) for row in diagnostic_receipts]

        repair: dict[str, object] = {"attempted": False}
        deep_report = snapshot.get("deep")
        source_ok = (
            isinstance(snapshot.get("source"), dict)
            and snapshot["source"].get("status") == "PASS"
        )
        can_repair, repair_reason = (
            _health_supervisor_is_repairable(deep_report)
            if isinstance(deep_report, dict)
            else (False, "no deep report available")
        )
        repair_operation = (
            _health_supervisor_repair_target(deep_report)[0]
            if isinstance(deep_report, dict)
            else ""
        )
        failed_operations = {
            _health_supervisor_failure_operation(receipt)
            for receipt in diagnostic_receipts
            if not _health_supervisor_is_user_operation_error(receipt)
        }
        failed_operations.discard("")
        repair_matches_failure = not failed_operations or failed_operations == {repair_operation}
        cooldown_ready = (
            time.time() - self.last_repair_at
        ) >= HEALTH_SUPERVISOR_REPAIR_COOLDOWN_SECONDS
        if (
            can_repair
            and repair_matches_failure
            and source_ok
            and snapshot.get("operation_active") is not True
            and not _health_supervisor_named_event_is_set(self.parent_pid, "active")
            and cooldown_ready
        ):
            repair = {
                "attempted": True,
                "operation": repair_operation,
                "reason": repair_reason,
            }
            self.last_repair_at = time.time()
            try:
                repair["result"] = repair_export_runtime()
                repair["status"] = "PASS"
            except Exception as exc:
                repair.update(
                    {
                        "status": "FAIL",
                        "error": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc(),
                    }
                )
            snapshot = self._snapshot(deep=True)
            snapshot["operation_active"] = operation_active
            snapshot["operation_receipts"] = {
                "active_count": len(active_receipts),
                "pending_failure_count": len(queued_failures),
                "stalled_count": len(stalled_receipts),
                "launcher_exited": bool(parent_exited),
            }
            if diagnostic_receipts:
                snapshot["launcher_failures"] = [dict(row) for row in diagnostic_receipts]
        elif can_repair and diagnostic_receipts and not repair_matches_failure:
            repair = {
                "attempted": False,
                "suppressed": True,
                "operation": repair_operation,
                "failed_operations": sorted(failed_operations) or ["unknown"],
                "reason": "Runtime damage belongs to a different operation domain.",
            }
        elif can_repair and snapshot.get("operation_active") is True:
            repair = {
                "attempted": False,
                "deferred": True,
                "operation": repair_operation,
                "reason": repair_reason,
            }
        snapshot["repair"] = repair

        observed = _health_supervisor_observed_issues(snapshot)
        recovery_succeeded = (
            repair.get("attempted") is True
            and repair.get("status") == "PASS"
            and snapshot.get("status") == "PASS"
        )
        for failure_receipt in diagnostic_receipts:
            if _health_supervisor_is_user_operation_error(failure_receipt):
                continue
            if (
                recovery_succeeded
                and _health_supervisor_failure_operation(failure_receipt) == repair_operation
            ):
                continue
            category, summary, details = _health_supervisor_operation_failure_issue(
                snapshot,
                failure_receipt,
            )
            observed[category] = (summary, details)
        _health_supervisor_reconcile_observed_issues(self.root, observed)

        consumed_failure_ids = {
            str(row.get("receipt_id", "") or "")
            for row in queued_failures
            if str(row.get("receipt_id", "") or "")
        }
        consumed_active_ids = (
            {
                str(row.get("receipt_id", "") or "")
                for row in active_receipts
                if str(row.get("receipt_id", "") or "")
            }
            if parent_exited
            else set()
        )
        if consumed_failure_ids or consumed_active_ids:
            consume_error = _health_supervisor_consume_operation_receipts(
                self.parent_pid,
                active_receipt_ids=consumed_active_ids,
                failure_receipt_ids=consumed_failure_ids,
            )
            if consume_error:
                _health_supervisor_append_error(
                    self.root,
                    kind="BOOTSTRAP_OPERATION_HANDOFF_FAILURE",
                    summary="Bootstrap diagnosed an operation but could not consume its receipt.",
                    details={
                        "launcher_pid": self.parent_pid,
                        "detail": consume_error,
                        "component": "BOOTSTRAP_OPERATION_HANDOFF",
                    },
                )
        if snapshot.get("operation_active") is not True:
            try:
                snapshot["python_runtime_upgrade"] = schedule_python_runtime_upgrade()
            except Exception as exc:
                snapshot["python_runtime_upgrade"] = {
                    "status": "schedule-error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
        return snapshot


    def run(self, *, once: bool = False) -> int:
        try:
            with _health_supervisor_lease(self.root, self.parent_pid) as lease:
                if lease.get("acquired") is not True:
                    return 0
                while True:
                    if self._parent_is_alive() is not True:
                        receipt_state = _health_supervisor_read_operation_receipts(self.parent_pid)
                        if _health_supervisor_operation_state_has_work(receipt_state):
                            self.run_cycle(
                                force_deep=True,
                                failure_signalled=bool(receipt_state.get("failures")),
                                parent_exited=True,
                            )
                        return 0
                    snapshot = self.run_cycle()
                    if once:
                        return 0 if snapshot.get("status") == "PASS" else 1
                    deadline = time.monotonic() + self.interval_seconds
                    while time.monotonic() < deadline:
                        if self._parent_is_alive() is not True:
                            break
                        # Consume failures immediately instead of waiting for the
                        # normal heartbeat; receipts remain queued until diagnosed.
                        if _health_supervisor_named_event_is_set(self.parent_pid, "error"):
                            self.run_cycle(force_deep=True, failure_signalled=True)
                            deadline = time.monotonic() + self.interval_seconds
                            continue
                        time.sleep(min(0.25, max(0.05, deadline - time.monotonic())))
        finally:
            retained = _HEALTH_SUPERVISOR_FAILURE_RECEIPTS.pop(self.parent_pid, None)
            if retained is not None:
                try:
                    retained.close()
                except (BufferError, OSError, ValueError):
                    pass


def run_bootstrap_health_supervisor(
    *,
    log_dir: str | Path | None = None,
    parent_pid: int = 0,
    interval_seconds: float = HEALTH_SUPERVISOR_DEFAULT_INTERVAL_SECONDS,
    deep_interval_seconds: float = HEALTH_SUPERVISOR_DEFAULT_DEEP_INTERVAL_SECONDS,
    once: bool = False,
) -> int:
    try:
        supervisor = BootstrapHealthSupervisor(
            log_dir=log_dir,
            parent_pid=parent_pid,
            interval_seconds=interval_seconds,
            deep_interval_seconds=deep_interval_seconds,
        )
        return supervisor.run(once=once)
    except BaseException as exc:
        try:
            root = _health_supervisor_log_root(log_dir)
            _health_supervisor_append_error(
                root,
                kind="FATAL_SUPERVISOR_FAILURE",
                summary=f"Bootstrap health supervisor stopped: {type(exc).__name__}: {exc}",
                details={"traceback": _health_supervisor_tail(traceback.format_exc(), 4000)},
            )
        except Exception:
            pass
        return 1


# Final single-file policy: this TXT is a small classified error database, not
# an append-only success stream.  Keeping it structured lets Bootstrap update a
# repeated incident in place, verify it after ten days, and promote only a
# still-live incident to the permanent major-bug section.
HEALTH_SUPERVISOR_LOG_STATE_SCHEMA = "pc-rehd-code-x-health-log-v2"


def _health_supervisor_empty_log_state() -> dict[str, object]:
    return {
        "schema": HEALTH_SUPERVISOR_LOG_STATE_SCHEMA,
        "description": "Only current error incidents and permanent major-bug alarms are retained.",
        "active_issues": {},
        "major_bug_alarms": {},
    }


def _health_supervisor_sanitize_log_state(state: dict[str, object]) -> bool:
    """Remove entries that are expected user actions or have no real receipt."""
    changed = False
    for collection_name in ("active_issues", "major_bug_alarms"):
        collection = state.get(collection_name)
        if not isinstance(collection, dict):
            state[collection_name] = {}
            changed = True
            continue
        for category, record in list(collection.items()):
            category_text = str(category or "").upper()
            suppress = category_text.startswith("OPERATION:UNKNOWN:")
            if isinstance(record, dict):
                details = record.get("details")
                receipt = details.get("launcher_failure") if isinstance(details, dict) else None
                if isinstance(receipt, dict) and _health_supervisor_is_user_operation_error(receipt):
                    suppress = True
            if suppress:
                collection.pop(category, None)
                changed = True
    return changed


def _health_supervisor_load_log_state(path: Path) -> tuple[dict[str, object], bool]:
    if path.is_file() is not True:
        return _health_supervisor_empty_log_state(), True
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict) or parsed.get("schema") != HEALTH_SUPERVISOR_LOG_STATE_SCHEMA:
            raise ValueError("unrecognized health log schema")
        if not isinstance(parsed.get("active_issues"), dict):
            parsed["active_issues"] = {}
        if not isinstance(parsed.get("major_bug_alarms"), dict):
            parsed["major_bug_alarms"] = {}
        return parsed, _health_supervisor_sanitize_log_state(parsed)
    except Exception as exc:
        # Preserve a malformed legacy log as one classified incident instead
        # of spawning a second recovery file beside the only permitted TXT.
        legacy_text = _health_supervisor_tail(path.read_text(encoding="utf-8", errors="replace"), 4000)
        state = _health_supervisor_empty_log_state()
        now = _health_supervisor_timestamp()
        state["active_issues"] = {
            "HEALTH_LOG_FORMAT": {
                "category": "HEALTH_LOG_FORMAT",
                "severity": "ERROR",
                "summary": f"Bootstrap health log could not be read: {type(exc).__name__}: {exc}",
                "details": {"legacy_tail": legacy_text},
                "first_seen_epoch": now["timestamp_epoch"],
                "last_seen_epoch": now["timestamp_epoch"],
                "first_seen": now["timestamp"],
                "last_seen": now["timestamp"],
                "occurrences": 1,
            }
        }
        return state, True


def _health_supervisor_write_log_state(path: Path, state: dict[str, object]) -> None:
    state["schema"] = HEALTH_SUPERVISOR_LOG_STATE_SCHEMA
    state["updated"] = _health_supervisor_timestamp()["timestamp"]
    active = state.get("active_issues")
    major = state.get("major_bug_alarms")
    if isinstance(active, dict) and not active and isinstance(major, dict) and not major:
        path.unlink(missing_ok=True)
        return
    _atomic_write_runtime_bytes(path, _health_supervisor_json_bytes(state))


def _health_supervisor_prune_log(root: Path) -> None:
    """Prune an existing error log without creating one during healthy startup."""
    path = _health_supervisor_log_path(root)
    if path.is_file() is not True:
        return
    with _health_supervisor_named_mutex("HealthLog", str(path)) as acquired:
        if acquired is not True:
            return
        state, changed = _health_supervisor_load_log_state(path)
        if changed:
            _health_supervisor_write_log_state(path, state)


def _health_supervisor_normalize_category_text(value: object) -> str:
    text_value = str(value or "").casefold()
    text_value = re.sub(r"[a-z]:\\\\[^\s\"']+", "<path>", text_value)
    text_value = re.sub(r"\b\d+\b", "#", text_value)
    text_value = re.sub(r"\s+", " ", text_value).strip()
    return text_value[:240] or "unknown"


def _health_supervisor_error_type(value: object) -> str:
    match = re.search(r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Timeout))\b", str(value or ""))
    return match.group(1) if match is not None else "UnknownError"


def _health_supervisor_incident_fingerprint(summary: str, details: dict[str, object]) -> str:
    payload = {"summary": str(summary), "details": details}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _health_supervisor_upsert_state_issue(
    state: dict[str, object],
    *,
    category: str,
    summary: str,
    details: dict[str, object],
    now: dict[str, object],
) -> bool:
    active = state.setdefault("active_issues", {})
    major = state.setdefault("major_bug_alarms", {})
    if not isinstance(active, dict) or not isinstance(major, dict):
        raise RuntimeError("Bootstrap health log has invalid issue collections")
    collection = major if category in major else active
    existing = collection.get(category)
    fingerprint = _health_supervisor_incident_fingerprint(summary, details)
    if not isinstance(existing, dict):
        collection[category] = {
            "category": category,
            "severity": "MAJOR_BUG" if collection is major else "ERROR",
            "summary": str(summary),
            "details": details,
            "fingerprint": fingerprint,
            "first_seen_epoch": now["timestamp_epoch"],
            "last_seen_epoch": now["timestamp_epoch"],
            "first_seen": now["timestamp"],
            "last_seen": now["timestamp"],
            "occurrences": 1,
        }
        return True
    if str(existing.get("fingerprint", "")) == fingerprint:
        return False
    existing.update(
        {
            "summary": str(summary),
            "details": details,
            "fingerprint": fingerprint,
            "last_seen_epoch": now["timestamp_epoch"],
            "last_seen": now["timestamp"],
            "occurrences": int(existing.get("occurrences", 0) or 0) + 1,
        }
    )
    return True


def _health_supervisor_upsert_issue(
    root: Path,
    *,
    category: str,
    summary: str,
    details: dict[str, object],
) -> Path:
    path = _health_supervisor_log_path(root)
    with _health_supervisor_named_mutex("HealthLog", str(path)) as acquired:
        if acquired is not True:
            raise RuntimeError("Timed out while updating the Bootstrap health log")
        state, changed = _health_supervisor_load_log_state(path)
        now = _health_supervisor_timestamp()
        changed = _health_supervisor_upsert_state_issue(
            state,
            category=str(category),
            summary=str(summary),
            details=dict(details),
            now=now,
        ) or changed
        if changed:
            _health_supervisor_write_log_state(path, state)
    return path


def _health_supervisor_resolve_operation_failures(
    root: Path,
    *,
    action: str,
    max_process_id: int,
) -> None:
    """A later successful operation is the only safe resolution signal for its failure."""
    path = _health_supervisor_log_path(root)
    if path.is_file() is not True:
        return
    action_key = re.sub(r"[^A-Z0-9_]+", "_", str(action or "unknown").upper()).strip("_") or "UNKNOWN"
    target_pid = max(0, int(max_process_id or 0))
    category_prefix = f"OPERATION:{action_key}:"
    with _health_supervisor_named_mutex("HealthLog", str(path)) as acquired:
        if acquired is not True:
            return
        state, changed = _health_supervisor_load_log_state(path)
        active = state.get("active_issues")
        if not isinstance(active, dict):
            return
        for category, record in list(active.items()):
            if not str(category).startswith(category_prefix) or not isinstance(record, dict):
                continue
            details = record.get("details")
            receipt = details.get("launcher_failure") if isinstance(details, dict) else None
            failed_pid = max(0, int(receipt.get("max_process_id", 0) or 0)) if isinstance(receipt, dict) else 0
            if target_pid > 0 and failed_pid > 0 and failed_pid != target_pid:
                continue
            active.pop(category, None)
            changed = True
        if changed:
            _health_supervisor_write_log_state(path, state)


def _health_supervisor_append_error(
    root: Path,
    *,
    kind: str,
    summary: str,
    details: dict[str, object] | None = None,
) -> Path:
    data = dict(details or {})
    action = str(data.get("action", "") or "").strip()
    detail_text = str(data.get("detail", "") or data.get("error", "") or summary)
    category_parts = [str(kind or "HEALTH_FAILURE").upper()]
    if action:
        category_parts.append(_health_supervisor_normalize_category_text(action).upper())
    category_parts.append(_health_supervisor_error_type(detail_text).upper())
    return _health_supervisor_upsert_issue(
        root,
        category=":".join(category_parts),
        summary=str(summary),
        details=data,
    )


def _health_supervisor_observed_issues(snapshot: dict[str, object]) -> dict[str, tuple[str, dict[str, object]]]:
    details = _health_supervisor_relevant_details(snapshot)
    observed: dict[str, tuple[str, dict[str, object]]] = {}
    source = details.get("source_failures")
    if isinstance(source, dict):
        for name, row in source.items():
            error = row.get("error") if isinstance(row, dict) else row
            category = f"SOURCE:{str(name).upper()}:{_health_supervisor_error_type(error).upper()}"
            observed[category] = (f"Source health check failed for {name}: {error}", {"source": {name: row}})
    runtime = details.get("runtime")
    if isinstance(runtime, dict):
        observed["PYTHON_RUNTIME:UNSUPPORTED_ABI"] = (
            "Current Python runtime is not supported by the packaged RE6 runtime.",
            {"runtime": runtime},
        )
    deep = details.get("deep_check")
    if isinstance(deep, dict):
        dependency = deep.get("dependency_contract")
        if isinstance(dependency, dict):
            observed["DEPENDENCY_RUNTIME:CONTRACT"] = (
                "Python dependency runtime contract is not ready.",
                {"dependency_contract": dependency},
            )
        bridge = deep.get("writer_importer_contract")
        if isinstance(bridge, dict):
            error = bridge.get("error") or bridge.get("error_type") or "contract failure"
            category = f"WRITER_IMPORTER_CONTRACT:{_health_supervisor_error_type(error).upper()}"
            observed[category] = (
                "Writer/importer runtime contract failed: " + str(error),
                {"writer_importer_contract": bridge},
            )
        for row in deep.get("failed_module_imports", []):
            if not isinstance(row, dict):
                continue
            module_name = str(row.get("module", "unknown") or "unknown").upper()
            error = row.get("error") or row.get("stderr") or "isolated import failed"
            category = f"MODULE_IMPORT:{module_name}:{_health_supervisor_error_type(error).upper()}"
            observed[category] = (
                f"Isolated import failed for {module_name}: {error}",
                {"module_import": row},
            )
    repair = details.get("auto_repair")
    if isinstance(repair, dict) and repair.get("status") == "FAIL":
        error = repair.get("error") or "automatic runtime repair failed"
        category = f"AUTO_REPAIR:{_health_supervisor_error_type(error).upper()}"
        observed[category] = ("Automatic runtime repair failed: " + str(error), {"auto_repair": repair})
    if not observed and snapshot.get("status") == "FAIL":
        for issue in snapshot.get("issues", []):
            normalized = _health_supervisor_normalize_category_text(issue)
            observed[f"HEALTH:{normalized.upper()}"] = (str(issue), {"issues": details.get("issues", [])})
    return observed


def _health_supervisor_reconcile_observed_issues(
    root: Path,
    observed: dict[str, tuple[str, dict[str, object]]],
) -> None:
    """Deduplicate current incidents and apply the ten-day resolution/promotion rule."""
    filtered_observed: dict[str, tuple[str, dict[str, object]]] = {}
    for category, issue in observed.items():
        if str(category or "").upper().startswith("OPERATION:UNKNOWN:"):
            continue
        _summary, details = issue
        receipt = details.get("launcher_failure") if isinstance(details, dict) else None
        if isinstance(receipt, dict) and _health_supervisor_is_user_operation_error(receipt):
            continue
        filtered_observed[category] = issue
    observed = filtered_observed
    path = _health_supervisor_log_path(root)
    if not observed and path.is_file() is not True:
        return
    with _health_supervisor_named_mutex("HealthLog", str(path)) as acquired:
        if acquired is not True:
            return
        state, changed = _health_supervisor_load_log_state(path)
        now = _health_supervisor_timestamp()
        active = state.setdefault("active_issues", {})
        major = state.setdefault("major_bug_alarms", {})
        if not isinstance(active, dict) or not isinstance(major, dict):
            state = _health_supervisor_empty_log_state()
            active = state["active_issues"]
            major = state["major_bug_alarms"]
            changed = True
        for category, (summary, details) in observed.items():
            changed = _health_supervisor_upsert_state_issue(
                state,
                category=category,
                summary=summary,
                details=details,
                now=now,
            ) or changed
        for category, record in list(active.items()):
            if not isinstance(record, dict):
                active.pop(category, None)
                changed = True
                continue
            if str(category).startswith("OPERATION:"):
                # A scene/Agent/export failure cannot be declared repaired merely
                # because a later generic runtime probe passes. It remains until
                # the same action completes successfully, or ten days turn it
                # into a permanent major-bug alarm.
                first_seen = float(record.get("first_seen_epoch", now["timestamp_epoch"]) or now["timestamp_epoch"])
                if float(now["timestamp_epoch"]) - first_seen < HEALTH_SUPERVISOR_LOG_RETENTION_SECONDS:
                    continue
                promoted = dict(record)
                promoted.update(
                    {
                        "severity": "MAJOR_BUG",
                        "promoted": now["timestamp"],
                        "promoted_epoch": now["timestamp_epoch"],
                        "promotion_reason": "The failed Launcher operation did not later complete successfully within ten days.",
                    }
                )
                existing_major = major.get(category)
                if isinstance(existing_major, dict):
                    existing_major.update(promoted)
                else:
                    major[category] = promoted
                active.pop(category, None)
                changed = True
                continue
            if category not in observed:
                # Active incidents are only retained while current health checks
                # still reproduce them. Permanent alarms are deliberately separate.
                active.pop(category, None)
                changed = True
                continue
            first_seen = float(record.get("first_seen_epoch", now["timestamp_epoch"]) or now["timestamp_epoch"])
            if float(now["timestamp_epoch"]) - first_seen < HEALTH_SUPERVISOR_LOG_RETENTION_SECONDS:
                continue
            promoted = dict(record)
            promoted.update(
                {
                    "severity": "MAJOR_BUG",
                    "promoted": now["timestamp"],
                    "promoted_epoch": now["timestamp_epoch"],
                    "promotion_reason": "The same health failure was still present after ten days.",
                }
            )
            existing_major = major.get(category)
            if isinstance(existing_major, dict):
                existing_major.update(promoted)
            else:
                major[category] = promoted
            active.pop(category, None)
            changed = True
        if changed:
            _health_supervisor_write_log_state(path, state)


SYSTEM_PREFLIGHT_SCHEMA = "pc-rehd-code-x-system-preflight-v1"


def _system_preflight_check(
    check_id: str,
    label: str,
    status: str,
    detail: str = "",
    repair: str = "",
) -> dict[str, object]:
    return {
        "id": str(check_id),
        "label": str(label),
        "status": str(status or "FAIL").upper(),
        "detail": str(detail or ""),
        "repair": str(repair or ""),
    }


def _system_preflight_status_passes(value: object) -> bool:
    return str(value or "").upper() in {"PASS", "REPAIRED"}


def _probe_system_directory(path: Path) -> dict[str, object]:
    target = path.expanduser().resolve()
    probe: Path | None = None
    try:
        target.mkdir(parents=True, exist_ok=True)
        if target.is_dir() is not True:
            raise NotADirectoryError(str(target))
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target,
            prefix=".system_probe_",
            suffix=".tmp",
            delete=False,
        ) as handle:
            probe = Path(handle.name)
            handle.write(b"PC_REHD_CODE_X")
            handle.flush()
            os.fsync(handle.fileno())
        if probe.read_bytes() != b"PC_REHD_CODE_X":
            raise OSError("directory write/read probe returned different bytes")
        return {"status": "PASS", "path": str(target), "writable": True}
    except Exception as exc:
        return {
            "status": "FAIL",
            "path": str(target),
            "writable": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass


def get_system_directory_contract() -> dict[str, object]:
    local_root = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "PC_REHD_Code_X"
    temp_root = Path(tempfile.gettempdir()) / "PC_REHD_Code_X"
    paths = {
        "source_root": BASE_DIR,
        "state_root": local_root,
        "runtime_root": RUNTIME_ROOT_DIR,
        "temp_root": temp_root,
        "log_root": temp_root,
        "fbx_root": temp_root,
        "cache_root": local_root / "Cache",
    }
    probes = {
        name: _probe_system_directory(path)
        for name, path in paths.items()
        if name != "source_root"
    }
    source_ready = BASE_DIR.is_dir() and os.access(BASE_DIR, os.R_OK)
    return {
        "schema": "pc-rehd-code-x-directory-contract-v1",
        "status": "PASS"
        if source_ready and all(item.get("status") == "PASS" for item in probes.values())
        else "FAIL",
        "paths": {name: str(path.expanduser().resolve()) for name, path in paths.items()},
        "source_readable": source_ready,
        "probes": probes,
        "ownership": {
            "source_root": "read-only-program-source",
            "state_root": "bootstrap-runtime-state",
            "runtime_root": "bootstrap-dependency-lanes",
            "temp_root": "launcher-temporary-work",
            "log_root": "launcher-selected-or-default-diagnostics",
            "fbx_root": "launcher-rolling-three-case-samples",
            "cache_root": "rebuildable-cache",
        },
    }


def run_system_preflight(*, repair: bool) -> dict[str, object]:
    checks: list[dict[str, object]] = []
    runtime = validate_python_runtime()
    runtime_ready = bool(runtime.get("supported", runtime.get("native_abi_supported", False)))
    checks.append(
        _system_preflight_check(
            "bootstrap_python",
            "Bootstrap Python runtime",
            "PASS" if runtime_ready else "FAIL",
            f"{runtime.get('current_python', sys.version.split()[0])} | {sys.executable}",
        )
    )

    directories = get_system_directory_contract()
    failed_directories = [
        name
        for name, item in dict(directories.get("probes", {})).items()
        if not isinstance(item, dict) or item.get("status") != "PASS"
    ]
    checks.append(
        _system_preflight_check(
            "runtime_directories",
            "Runtime directory contract",
            str(directories.get("status", "FAIL")),
            "All runtime paths are writable."
            if not failed_directories
            else "Unwritable: " + ", ".join(failed_directories),
        )
    )

    source_report = _health_supervisor_source_report()
    source_errors = [str(value) for value in source_report.get("errors", [])]
    checks.append(
        _system_preflight_check(
            "python_sources",
            "Python source syntax and presence",
            str(source_report.get("status", "FAIL")),
            f"{len(source_report.get('files', {}))} source files checked."
            if not source_errors
            else " | ".join(source_errors[:4]),
        )
    )

    initialization: dict[str, object] = {
        "requested": bool(repair),
        "attempted": False,
        "status": "NOT_REQUESTED",
        "installed_packages": [],
    }
    if repair and runtime_ready and source_report.get("status") == "PASS":
        initialization["attempted"] = True
        try:
            ensure_report = ensure_export_runtime()
            initialization.update(
                {
                    "status": "PASS",
                    "ensure_report": ensure_report,
                    "installed_packages": list(ensure_report.get("installed_packages", [])),
                }
            )
        except Exception as ensure_error:
            initialization["ensure_error"] = f"{type(ensure_error).__name__}: {ensure_error}"
            try:
                repair_report = repair_export_runtime()
                initialization.update(
                    {
                        "status": "REPAIRED",
                        "repair_report": repair_report,
                        "installed_packages": list(repair_report.get("installed_packages", [])),
                    }
                )
            except Exception as repair_error:
                initialization.update(
                    {
                        "status": "FAIL",
                        "repair_error": f"{type(repair_error).__name__}: {repair_error}",
                        "repair_traceback": traceback.format_exc(),
                    }
                )

    deep_report = _health_supervisor_deep_report()
    if repair and deep_report.get("status") != "PASS":
        repairable, repair_reason = _health_supervisor_is_repairable(deep_report)
        if repairable and initialization.get("status") != "REPAIRED":
            initialization["attempted"] = True
            initialization["repair_reason"] = repair_reason
            try:
                repair_report = repair_export_runtime()
                initialization.update(
                    {
                        "status": "REPAIRED",
                        "repair_report": repair_report,
                        "installed_packages": list(repair_report.get("installed_packages", [])),
                    }
                )
                deep_report = _health_supervisor_deep_report()
            except Exception as repair_error:
                initialization.update(
                    {
                        "status": "FAIL",
                        "repair_error": f"{type(repair_error).__name__}: {repair_error}",
                        "repair_traceback": traceback.format_exc(),
                    }
                )

    dependency = deep_report.get("dependency_contract")
    dependency_ready = isinstance(dependency, dict) and dependency.get("ready") is True
    installed_packages = [str(value) for value in initialization.get("installed_packages", [])]
    dependency_status = (
        "REPAIRED"
        if dependency_ready and (installed_packages or initialization.get("status") == "REPAIRED")
        else ("PASS" if dependency_ready else "FAIL")
    )
    checks.append(
        _system_preflight_check(
            "python_dependencies",
            "Python dependency runtime",
            dependency_status,
            "Installed/rebuilt: " + ", ".join(installed_packages)
            if installed_packages
            else ("Dependency lanes are healthy." if dependency_ready else "Dependency contract is not ready."),
        )
    )

    writer_contract = deep_report.get("bridge_contract")
    if not isinstance(writer_contract, dict):
        writer_contract = {}
    writer_ready = writer_contract.get("writer_ready") is True
    importer_ready = writer_contract.get("re6_mod_import_fbx_ready") is True
    checks.append(
        _system_preflight_check(
            "mod_export",
            ".MOD Python export writer",
            "PASS" if writer_ready else "FAIL",
            "Memory-scene writer and regression gates passed."
            if writer_ready
            else str(writer_contract.get("error", "Writer contract is not ready."))[:1000],
        )
    )
    checks.append(
        _system_preflight_check(
            "mod_import",
            ".MOD Python FBX importer",
            "PASS" if importer_ready else "FAIL",
            "Importer scene and FBX contracts passed."
            if importer_ready
            else str(writer_contract.get("error", "Importer contract is not ready."))[:1000],
        )
    )

    imports = {
        str(row.get("module", "")): row
        for row in deep_report.get("module_imports", [])
        if isinstance(row, dict)
    }
    capability_rows = (
        ("fbx_probe", "FBX parsing capability", "codex_fbx_probe", True),
        ("texture_tools", "Texture decode capability", "codex_re6_tex_decode", False),
    )
    # The aggregate inventory spans every capability and is diagnostic only.
    # Required operation rows below remain authoritative for PASS/FAIL.
    optional_ids: set[str] = {"python_dependencies"}
    for check_id, label, module_name, required in capability_rows:
        row = imports.get(module_name, {})
        ready = isinstance(row, dict) and row.get("status") == "PASS"
        checks.append(
            _system_preflight_check(
                check_id,
                label,
                "PASS" if ready else "FAIL",
                "Isolated import passed."
                if ready
                else str(row.get("error", row.get("stderr", "Isolated import failed.")))[:1000],
            )
        )
        if not required:
            optional_ids.add(check_id)

    operation_domain_reports: dict[str, dict[str, object]] = {}
    for operation_name, label, required in (
        ("import_mod", ".MOD import runtime domain", True),
        ("export_mod", ".MOD export runtime domain", True),
        ("texture", "Texture runtime domain", False),
        ("auxiliary", "SBC/ADR/EMS runtime domain", False),
        ("auxiliary_probe", "Auxiliary FBX Probe runtime domain", False),
    ):
        domain_report = get_operation_runtime_domain_report(
            operation_name,
            repair=False,
            include_optional=True,
        )
        operation_domain_reports[operation_name] = domain_report
        domain_status = str(domain_report.get("status", "FAIL") or "FAIL").upper()
        checks.append(
            _system_preflight_check(
                "runtime_domain_" + operation_name,
                label,
                domain_status,
                str(domain_report.get("receipt", {}).get("detail", "") or "Operation runtime is ready."),
            )
        )
        if not required:
            optional_ids.add("runtime_domain_" + operation_name)

    required_failures = [
        row
        for row in checks
        if row["id"] not in optional_ids and not _system_preflight_status_passes(row.get("status"))
    ]
    optional_failures = [
        row
        for row in checks
        if row["id"] in optional_ids and not _system_preflight_status_passes(row.get("status"))
    ]
    status = "FAIL" if required_failures else ("DEGRADED" if optional_failures else "PASS")
    runtime_upgrade = (
        schedule_python_runtime_upgrade(force=True)
        if repair and _runtime_candidate_session_report().get("authorized") is not True
        else {"status": "candidate-session" if _runtime_candidate_session_report().get("authorized") else "check-only"}
    )
    return {
        "schema": SYSTEM_PREFLIGHT_SCHEMA,
        "status": status,
        "repair_requested": bool(repair),
        "python_runtime": runtime,
        "directories": directories,
        "source_report": source_report,
        "initialization": initialization,
        "deep_report": deep_report,
        "operation_runtime_domains": operation_domain_reports,
        "checks": checks,
        "required_failures": required_failures,
        "optional_failures": optional_failures,
        "python_runtime_ab": get_python_runtime_ab_report(),
        "python_runtime_upgrade": runtime_upgrade,
        "error": "" if status != "FAIL" else "Required system checks failed.",
    }


def _runtime_ab_update_state(mutator: Callable[[dict[str, object]], None]) -> dict[str, object]:
    ensure_python_runtime_ab_state()
    with _bootstrap_state_lock(timeout_seconds=15.0):
        payload = _load_bootstrap_runtime_state()
        runtime_ab = _runtime_ab_payload_from_state(payload)
        mutator(runtime_ab)
        runtime_ab["schema"] = PYTHON_RUNTIME_AB_SCHEMA
        runtime_ab["release_root"] = str(BASE_DIR)
        runtime_ab["dependency_base_dir"] = str(DEPENDENCY_BASE_DIR)
        payload[PYTHON_RUNTIME_AB_STATE_KEY] = runtime_ab
        if _save_bootstrap_runtime_state(payload) is not True:
            raise OSError(f"Unable to persist Python A/B runtime state: {BOOTSTRAP_RUNTIME_STATE_PATH}")
        return dict(runtime_ab)


def _runtime_ab_status_update(**values: object) -> dict[str, object]:
    def update(runtime_ab: dict[str, object]) -> None:
        runtime_ab.update(values)
        runtime_ab["updated_epoch"] = time.time()

    return _runtime_ab_update_state(update)


def _runtime_ab_parse_target(target_python: str | None) -> tuple[int, ...] | None:
    target_text = str(target_python or "").strip()
    if not target_text:
        return None
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?", target_text)
    if match is None:
        raise ValueError("Target Python must be MAJOR.MINOR or MAJOR.MINOR.PATCH.")
    values = [int(match.group(1)), int(match.group(2))]
    if match.group(3) is not None:
        values.append(int(match.group(3)))
    return tuple(values)


def _runtime_ab_read_https_text(url: str, *, timeout_seconds: float = 30.0) -> str:
    if not str(url).casefold().startswith("https://"):
        raise RuntimeError(f"Python runtime discovery rejected a non-HTTPS URL: {url}")
    failures: list[str] = []
    for attempt in range(1, 4):
        request = urllib.request.Request(
            str(url),
            headers={"User-Agent": "PC-REHD-Code-X-Python-Runtime/1"},
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=max(5.0, float(timeout_seconds)),
            ) as response:
                payload = response.read(4 * 1024 * 1024 + 1)
            if len(payload) > 4 * 1024 * 1024:
                raise RuntimeError(f"Python release index exceeded the 4 MiB safety limit: {url}")
            return payload.decode("utf-8", errors="replace")
        except Exception as exc:
            failures.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
            if attempt >= 3:
                raise RuntimeError(
                    "Python release index request failed after 3 attempts: " + " | ".join(failures)
                ) from exc
            time.sleep(float(attempt))
    raise RuntimeError("Python release index retry loop ended unexpectedly.")


def _runtime_ab_local_installer_inventory() -> list[dict[str, object]]:
    filename_pattern = re.compile(r"python-(\d+)\.(\d+)\.(\d+)-amd64\.exe", re.IGNORECASE)
    candidates: list[tuple[Path, str]] = []
    for root, source, recursive in (
        (RUNTIME_INTERPRETER_DOWNLOAD_DIR, "bootstrap-download-cache", False),
        (BASE_DIR, "release-root", False),
    ):
        if root.is_dir() is not True:
            continue
        iterator = root.rglob("python-*-amd64.exe") if recursive else root.glob("python-*-amd64.exe")
        candidates.extend((path, source) for path in iterator if path.is_file())
    inventory: list[dict[str, object]] = []
    seen: set[str] = set()
    for installer_path, source in candidates:
        match = filename_pattern.fullmatch(installer_path.name)
        if match is None:
            continue
        path_key = os.path.normcase(os.path.abspath(str(installer_path)))
        if path_key in seen:
            continue
        seen.add(path_key)
        version_tuple = tuple(int(match.group(index)) for index in range(1, 4))
        inventory.append(
            {
                "path": str(installer_path.resolve()),
                "name": installer_path.name,
                "source": source,
                "version": _python_runtime_version_text(version_tuple),
                "version_tuple": version_tuple,
                "abi": _python_tag(version_tuple),
                "size": installer_path.stat().st_size,
            }
        )
    return sorted(
        inventory,
        key=lambda row: (tuple(row["version_tuple"]), str(row["source"]), str(row["path"])),
        reverse=True,
    )


def _find_required_python_installer_path() -> Path | None:
    for row in _runtime_ab_local_installer_inventory():
        if tuple(row.get("version_tuple", ())) != REQUIRED_PYTHON_RUNTIME:
            continue
        candidate = Path(str(row.get("path", "") or ""))
        if candidate.is_file():
            return candidate
    return None


def discover_official_python_runtime_update(
    *,
    current_version: object | None = None,
    target_python: str | None = None,
) -> dict[str, object]:
    active_version = _python_runtime_version_tuple(current_version)
    target = _runtime_ab_parse_target(target_python)
    local_installers = _runtime_ab_local_installer_inventory()
    local_by_version = {
        tuple(row["version_tuple"]): row
        for row in reversed(local_installers)
    }
    inspected: list[dict[str, object]] = []
    try:
        index_text = _runtime_ab_read_https_text(PYTHON_RUNTIME_RELEASE_INDEX_URL)
    except Exception as exc:
        inspected.append({"status": "root-index-error", "error": str(exc)})
        release_versions: set[tuple[int, int, int]] = set(local_by_version)
    else:
        release_versions = {
            (int(major), int(minor), int(micro))
            for major, minor, micro in re.findall(
                r'href=["\'](\d+)\.(\d+)\.(\d+)/["\']',
                index_text,
                flags=re.IGNORECASE,
            )
        }
        release_versions.update(local_by_version)
    eligible: list[tuple[int, int, int]] = []
    exact_target = target is not None and len(target) >= 3
    for version_tuple in release_versions:
        if exact_target:
            if version_tuple != tuple(target[:3]):
                continue
        elif version_tuple <= active_version:
            continue
        elif target is not None and version_tuple[: len(target)] != target:
            continue
        eligible.append(version_tuple)
    for version_tuple in sorted(eligible, reverse=True):
        version_text = _python_runtime_version_text(version_tuple)
        release_url = f"{PYTHON_RUNTIME_RELEASE_INDEX_URL}{version_text}/"
        installer_name = f"python-{version_text}-amd64.exe"
        local_installer = local_by_version.get(version_tuple)
        try:
            release_text = _runtime_ab_read_https_text(release_url)
        except Exception as exc:
            inspected.append({"version": version_text, "status": "index-error", "error": str(exc)})
            release_text = ""
        exact_pattern = r'href=["\']' + re.escape(installer_name) + r'["\']'
        official_installer_listed = bool(
            release_text and re.search(exact_pattern, release_text, flags=re.IGNORECASE)
        )
        if official_installer_listed is not True and local_installer is None:
            inspected.append(
                {
                    "version": version_text,
                    "status": "prerelease-only-or-no-windows-installer",
                }
            )
            continue
        installer_url = release_url + installer_name
        result: dict[str, object] = {
            "status": "update-available",
            "stable": True,
            "current_version": _python_runtime_version_text(active_version),
            "version": version_text,
            "version_tuple": version_tuple,
            "abi": _python_tag(version_tuple),
            "installer_name": installer_name,
            "installer_url": installer_url,
            "release_url": release_url,
            "inspected": inspected,
            "discovery_source": "official-index" if official_installer_listed else "signed-local-fallback",
        }
        if local_installer is not None:
            result["local_installer_path"] = local_installer["path"]
            result["local_installer_source"] = local_installer["source"]
        return result
    return {
        "status": "target-unavailable" if target is not None else "up-to-date",
        "stable": True,
        "current_version": _python_runtime_version_text(active_version),
        "target_python": str(target_python or ""),
        "inspected": inspected,
    }


def _runtime_ab_verify_authenticode(installer_path: Path) -> dict[str, object]:
    if os.name != "nt":
        raise RuntimeError("Managed Python installer verification is supported only on Windows.")
    system_root = Path(os.environ.get("SystemRoot") or r"C:\Windows")
    system_powershell = (
        system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    )
    powershell = str(system_powershell) if system_powershell.is_file() else shutil.which("powershell.exe")
    if not powershell:
        raise RuntimeError("Authenticode verification requires Windows PowerShell.")
    environment = dict(os.environ)
    environment["PC_REHD_CODE_X_VERIFY_INSTALLER"] = str(installer_path)
    command_text = (
        "$module=Join-Path $env:SystemRoot 'System32\\WindowsPowerShell\\v1.0\\Modules\\Microsoft.PowerShell.Security\\Microsoft.PowerShell.Security.psd1';"
        "Import-Module -Name $module -Force -ErrorAction Stop;"
        "$s=Get-AuthenticodeSignature -LiteralPath $env:PC_REHD_CODE_X_VERIFY_INSTALLER;"
        "$subject='';if($null-ne $s.SignerCertificate){$subject=[string]$s.SignerCertificate.Subject};"
        "[pscustomobject]@{Status=[string]$s.Status;Subject=$subject;Message=[string]$s.StatusMessage}"
        "|ConvertTo-Json -Compress;"
        "if([string]$s.Status-ne 'Valid'){exit 3}"
    )
    failures: list[dict[str, object]] = []
    for attempt in range(1, 3):
        completed = _run_hidden_subprocess(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command_text],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
            env=environment,
        )
        output_text = (completed.stdout or "").strip()
        try:
            payload = json.loads(output_text.splitlines()[-1]) if output_text else {}
        except Exception:
            payload = {}
        status_text = str(payload.get("Status", "") or "") if isinstance(payload, dict) else ""
        subject_text = str(payload.get("Subject", "") or "") if isinstance(payload, dict) else ""
        valid = bool(
            completed.returncode == 0
            and status_text.casefold() == "valid"
            and "python software foundation" in subject_text.casefold()
        )
        if valid:
            return {
                "status": status_text,
                "subject": subject_text,
                "sha256": _sha256_file(installer_path, use_cache=False),
                "path": str(installer_path),
                "attempt": attempt,
            }
        failures.append(
            {
                "attempt": attempt,
                "returncode": completed.returncode,
                "status": status_text,
                "subject": subject_text,
                "stdout": (completed.stdout or "")[-2000:],
                "stderr": (completed.stderr or "")[-2000:],
            }
        )
        if attempt < 2:
            time.sleep(1.0)
    raise RuntimeError(
        "Python installer Authenticode verification failed: "
        + json.dumps(failures, ensure_ascii=False)
    )


def _runtime_ab_download_installer(release: dict[str, object]) -> dict[str, object]:
    installer_url = str(release.get("installer_url", "") or "")
    installer_name = str(release.get("installer_name", "") or "")
    if not installer_url.casefold().startswith("https://www.python.org/"):
        raise RuntimeError(f"Untrusted Python installer origin: {installer_url}")
    if not installer_name.casefold().endswith("-amd64.exe"):
        raise RuntimeError(f"Unexpected Python installer name: {installer_name}")
    RUNTIME_INTERPRETER_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    destination = RUNTIME_INTERPRETER_DOWNLOAD_DIR / installer_name
    if destination.is_file():
        try:
            signature = _runtime_ab_verify_authenticode(destination)
        except Exception:
            rejected = destination.with_name(
                destination.name + f".invalid-{int(time.time())}-{uuid.uuid4().hex[:8]}"
            )
            os.replace(destination, rejected)
        else:
            return {"status": "cached", "path": str(destination), "signature": signature}

    failures: list[str] = []
    local_installer_text = str(release.get("local_installer_path", "") or "").strip()
    if local_installer_text:
        local_installer = Path(local_installer_text)
        approved_local_paths = {
            os.path.normcase(os.path.abspath(str(row["path"])))
            for row in _runtime_ab_local_installer_inventory()
        }
        local_key = os.path.normcase(os.path.abspath(str(local_installer)))
        try:
            if local_key not in approved_local_paths:
                raise RuntimeError(f"Local Python installer is outside the approved inventory: {local_installer}")
            if local_installer.name.casefold() != installer_name.casefold():
                raise RuntimeError(
                    f"Local Python installer name does not match the selected release: {local_installer.name}"
                )
            if local_installer.stat().st_size < 5 * 1024 * 1024:
                raise RuntimeError("Local Python installer is unexpectedly small.")
            signature = _runtime_ab_verify_authenticode(local_installer)
            partial = destination.with_name(
                destination.name + f".{os.getpid()}.{uuid.uuid4().hex}.local-part"
            )
            try:
                with local_installer.open("rb") as source, partial.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                copied_sha256 = _sha256_file(partial, use_cache=False)
                if copied_sha256.casefold() != str(signature.get("sha256", "")).casefold():
                    raise RuntimeError("Local Python installer copy failed its SHA-256 integrity check.")
                os.replace(partial, destination)
            finally:
                partial.unlink(missing_ok=True)
            signature["path"] = str(destination)
            return {
                "status": "local-signed-cache",
                "path": str(destination),
                "source_path": str(local_installer),
                "source": str(release.get("local_installer_source", "local") or "local"),
                "signature": signature,
            }
        except Exception as exc:
            failures.append(f"local signed installer: {type(exc).__name__}: {exc}")

    for attempt in range(1, 4):
        partial = destination.with_name(
            destination.name + f".{os.getpid()}.{uuid.uuid4().hex}.part"
        )
        request = urllib.request.Request(
            installer_url,
            headers={"User-Agent": "PC-REHD-Code-X-Python-Runtime/1"},
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=PYTHON_RUNTIME_DOWNLOAD_TIMEOUT_SECONDS,
            ) as response, partial.open("wb") as output:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if partial.stat().st_size < 5 * 1024 * 1024:
                raise RuntimeError("Downloaded Python installer is unexpectedly small.")
            signature = _runtime_ab_verify_authenticode(partial)
            os.replace(partial, destination)
            signature["path"] = str(destination)
            return {
                "status": "downloaded",
                "path": str(destination),
                "signature": signature,
                "attempt": attempt,
            }
        except Exception as exc:
            failures.append(f"attempt {attempt}: {type(exc).__name__}: {exc}")
            if attempt >= 3:
                raise RuntimeError(
                    "Python installer download failed after 3 attempts: " + " | ".join(failures)
                ) from exc
            time.sleep(float(attempt * 2))
        finally:
            if partial.exists():
                partial.unlink(missing_ok=True)
    raise RuntimeError("Python installer download retry loop ended unexpectedly.")


def _runtime_ab_slot_dir(slot: str) -> Path:
    slot_name = str(slot or "").strip().upper()
    if slot_name not in {"A", "B"}:
        raise ValueError(f"Unsupported Python runtime slot: {slot}")
    return RUNTIME_INTERPRETER_ROOT_DIR / f"slot-{slot_name}"


def _runtime_ab_inactive_slot(runtime_ab: dict[str, object]) -> str:
    active_slot = str(runtime_ab.get("active_slot", "A") or "A").upper()
    return "B" if active_slot == "A" else "A"


def _runtime_ab_quarantine_managed_path(path_value: Path, *, reason: str) -> Path | None:
    path = Path(path_value)
    if path.exists() is not True:
        return None
    interpreter_root = RUNTIME_INTERPRETER_ROOT_DIR.resolve()
    resolved = path.resolve()
    if resolved == interpreter_root or interpreter_root not in resolved.parents:
        raise RuntimeError(f"Refusing to quarantine a path outside the managed interpreter root: {path}")
    RUNTIME_INTERPRETER_QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    target = RUNTIME_INTERPRETER_QUARANTINE_DIR / (
        f"{path.name}-{re.sub(r'[^A-Za-z0-9_.-]+', '-', reason)[:48]}-{stamp}-{uuid.uuid4().hex[:8]}"
    )
    os.replace(path, target)
    return target


def _runtime_ab_install_python(
    release: dict[str, object],
    installer_report: dict[str, object],
    *,
    slot: str,
) -> dict[str, object]:
    target_dir = _runtime_ab_slot_dir(slot)
    managed_root = RUNTIME_INTERPRETER_ROOT_DIR.resolve()
    if managed_root not in target_dir.resolve().parents:
        raise RuntimeError(
            "Refusing to install managed Python over a user-selected directory: "
            + str(target_dir)
        )
    expected_version = _python_runtime_version_tuple(release.get("version"))
    existing_python = target_dir / "python.exe"
    if existing_python.is_file():
        existing_probe = _probe_python_runtime_candidate(existing_python)
        if (
            isinstance(existing_probe, dict)
            and _python_runtime_version_tuple(existing_probe.get("version_tuple")) == expected_version
            and str(existing_probe.get("releaselevel", "") or "").casefold() == "final"
        ):
            return {
                "status": "already-installed",
                "slot": slot,
                "target_dir": str(target_dir),
                "python_exe": str(existing_python.resolve()),
                "probe": existing_probe,
            }
        _runtime_ab_quarantine_managed_path(target_dir, reason="replaced-inactive-slot")
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    installer_path = Path(str(installer_report.get("path", "") or ""))
    arguments = [
        "/quiet",
        "/norestart",
        "InstallAllUsers=0",
        f"TargetDir={target_dir}",
        "PrependPath=0",
        "AppendPath=0",
        "AssociateFiles=0",
        "Include_launcher=0",
        "InstallLauncherAllUsers=0",
        "Include_pip=1",
        "Include_test=0",
        "Include_doc=0",
        "Include_debug=0",
        "Include_symbols=0",
        "Shortcuts=0",
    ]
    completed = _run_hidden_subprocess(
        [str(installer_path), *arguments],
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=PYTHON_RUNTIME_INSTALL_TIMEOUT_SECONDS,
        cwd=BASE_DIR,
    )
    if completed.returncode not in {0, 1641, 3010}:
        raise RuntimeError(
            f"Python {release.get('version')} installer failed with exit code {completed.returncode}. "
            + (completed.stderr or completed.stdout or "")[-4000:]
        )
    installed_python = target_dir / "python.exe"
    probe = _probe_python_runtime_candidate(installed_python)
    repair_completed: subprocess.CompletedProcess[str] | None = None
    if not isinstance(probe, dict):
        # The official installer registers one product per Python minor. If a
        # managed slot was quarantined, rerunning with TargetDir enters Modify
        # mode and can report success without restoring the missing files.
        # Explicit Repair rehydrates the signed registered payload in-place.
        repair_completed = _run_hidden_subprocess(
            [str(installer_path), "/repair", "/quiet", "/norestart"],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=PYTHON_RUNTIME_INSTALL_TIMEOUT_SECONDS,
            cwd=BASE_DIR,
        )
        if repair_completed.returncode in {0, 1641, 3010}:
            probe = _probe_python_runtime_candidate(installed_python)
        if not isinstance(probe, dict):
            quarantine_path = ""
            if target_dir.exists():
                quarantined = _runtime_ab_quarantine_managed_path(
                    target_dir,
                    reason="native-probe-failed-after-repair",
                )
                quarantine_path = str(quarantined) if quarantined is not None else ""
            raise RuntimeError(
                f"Installed Python could not pass its native startup probe after signed repair: {installed_python}; "
                f"repair_exit={repair_completed.returncode}; quarantine={quarantine_path}; "
                + (repair_completed.stderr or repair_completed.stdout or "")[-4000:]
            )
    installed_version = _python_runtime_version_tuple(probe.get("version_tuple"))
    if installed_version != expected_version or str(probe.get("releaselevel", "")).casefold() != "final":
        raise RuntimeError(
            "Installed Python version does not match the stable candidate: "
            + json.dumps(
                {
                    "expected": _python_runtime_version_text(expected_version),
                    "actual": _python_runtime_version_text(installed_version),
                    "releaselevel": probe.get("releaselevel"),
                },
                ensure_ascii=False,
            )
        )
    return {
        "status": "installed-after-repair" if repair_completed is not None else "installed",
        "slot": slot,
        "target_dir": str(target_dir),
        "python_exe": str(installed_python.resolve()),
        "probe": probe,
        "returncode": completed.returncode,
        "stdout": (completed.stdout or "")[-2000:],
        "stderr": (completed.stderr or "")[-2000:],
        "repair_returncode": repair_completed.returncode if repair_completed is not None else None,
        "repair_stdout": (repair_completed.stdout or "")[-2000:] if repair_completed is not None else "",
        "repair_stderr": (repair_completed.stderr or "")[-2000:] if repair_completed is not None else "",
    }


def _runtime_ab_probe_ready_imports(
    probe: dict[str, object],
    import_names: tuple[str, ...] | list[str],
) -> bool:
    rows = {
        str(row.get("import_name", "")): row
        for row in probe.get("imports", [])
        if isinstance(row, dict)
    }
    return all(isinstance(rows.get(name), dict) and rows[name].get("ready") is True for name in import_names)


def _runtime_ab_run_pip_install(
    target_dir: Path,
    specs: tuple[str, ...] | list[str],
    *,
    only_binary: bool,
    timeout_seconds: float = 600.0,
) -> dict[str, object]:
    target_dir.mkdir(parents=True, exist_ok=True)
    _ensure_pip_available(bootstrap_if_missing=True)
    report_path = target_dir.parent / f".{target_dir.name}-{uuid.uuid4().hex}.pip-report.json"
    command = [
        sys.executable,
        "-m",
        "pip",
        "--isolated",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--no-cache-dir",
        "--prefer-binary",
        "--upgrade",
        "--find-links",
        str(LOCAL_PYTHON_BUILD_TOOLS_DIR),
        "--index-url",
        OFFICIAL_PYPI_INDEX_URL,
        "--target",
        str(target_dir),
        "--report",
        str(report_path),
    ]
    if only_binary:
        command.append("--only-binary=:all:")
    command.extend(str(value) for value in specs)
    environment = _isolated_python_child_environment()
    environment.pop("PYTHONPATH", None)
    completed = _run_hidden_subprocess(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(60.0, float(timeout_seconds)),
        env=environment,
    )
    downloads = _online_install_report_entries(report_path)
    report_path.unlink(missing_ok=True)
    report = {
        "specs": list(specs),
        "target_dir": str(target_dir),
        "returncode": completed.returncode,
        "downloads": downloads,
        "stdout": (completed.stdout or "")[-6000:],
        "stderr": (completed.stderr or "")[-6000:],
    }
    if completed.returncode != 0:
        raise OnlineDependencyRepairError(
            "Candidate Python dependency install failed for " + ", ".join(specs),
            report,
        )
    return report


def _runtime_ab_build_patched_ufbx(stage_dir: Path) -> dict[str, object]:
    if PATCHED_UFBX_SOURCE_DIR.is_dir() is not True:
        raise DependencyBundleBrokenError(
            f"Patched ufbx source is missing: {PATCHED_UFBX_SOURCE_DIR}"
        )
    source_fingerprint = _patched_ufbx_source_fingerprint()
    if source_fingerprint != PATCHED_UFBX_APPROVED_SOURCE_FINGERPRINT:
        raise DependencyBundleBrokenError(
            "Patched ufbx source tree is not the approved RE6 build: "
            + source_fingerprint
        )
    with tempfile.TemporaryDirectory(prefix="pc-rehd-ufbx-candidate-") as temporary:
        work_root = Path(temporary)
        project_copy = work_root / "source"
        output_root = work_root / "build"
        build_tools = work_root / "build-tools"
        shutil.copytree(
            PATCHED_UFBX_SOURCE_DIR,
            project_copy,
            ignore=shutil.ignore_patterns("build", "*.egg-info", "__pycache__", "*.pyd"),
        )
        build_tools_report = _runtime_ab_run_pip_install(
            build_tools,
            ("setuptools>=83", "Cython>=3.2.9,<4"),
            only_binary=True,
            timeout_seconds=600.0,
        )
        environment = _isolated_python_child_environment()
        environment["PYTHONPATH"] = os.pathsep.join((str(stage_dir), str(build_tools)))
        environment["SETUPTOOLS_USE_DISTUTILS"] = "local"
        completed = _run_hidden_subprocess(
            [
                sys.executable,
                "setup.py",
                "build",
                "--build-base",
                str(output_root),
            ],
            cwd=project_copy,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900.0,
            env=environment,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "Patched ufbx source build failed for "
                + _python_tag()
                + f" with exit code {completed.returncode}\nSTDOUT:\n{completed.stdout[-6000:]}\nSTDERR:\n{completed.stderr[-6000:]}"
            )
        built_packages = sorted(output_root.glob("lib*/ufbx"))
        if not built_packages:
            built_packages = sorted(output_root.glob("lib*/**/ufbx"))
        if not built_packages:
            raise RuntimeError(f"Patched ufbx build produced no package under {output_root}")
        target_package = stage_dir / "ufbx"
        if target_package.exists():
            shutil.rmtree(target_package, ignore_errors=True)
        shutil.copytree(built_packages[0], target_package)
        return {
            "source": str(PATCHED_UFBX_SOURCE_DIR),
            "source_fingerprint": source_fingerprint,
            "target": str(target_package),
            "build_tools": build_tools_report,
            "stdout": (completed.stdout or "")[-4000:],
            "stderr": (completed.stderr or "")[-4000:],
        }


def _runtime_ab_build_accelerators(stage_dir: Path) -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    for import_name in LOCAL_ACCELERATOR_IMPORTS:
        source_candidates = [
            Path(value)
            for value in _find_local_fixed_accelerator_candidates(import_name)
            if _is_local_source_package_dir(Path(value))
        ]
        if not source_candidates:
            reports.append({"import_name": import_name, "ready": False, "error": "source package missing"})
            continue
        try:
            build_report = _build_local_accelerator_source_without_pip(
                import_name,
                source_candidates[0],
                stage_dir,
            )
            reports.append({"import_name": import_name, "ready": True, **build_report})
        except Exception as exc:
            reports.append(
                {
                    "import_name": import_name,
                    "ready": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
    return reports


def _runtime_ab_build_candidate_dependency_lane() -> dict[str, object]:
    RUNTIME_STAGING_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    stage_dir = Path(
        tempfile.mkdtemp(
            prefix=f"{_python_tag()}-python-candidate-{os.getpid()}-",
            dir=RUNTIME_STAGING_ROOT_DIR,
        )
    )
    committed = False
    try:
        dependency_reports: list[dict[str, object]] = []
        for import_name in ("numpy", "PIL", "orjson"):
            dependency_reports.append(_install_online_import_to_stage(import_name, stage_dir))
        ufbx_report = _runtime_ab_build_patched_ufbx(stage_dir)
        accelerator_reports = _runtime_ab_build_accelerators(stage_dir)
        stage_probe = _run_fresh_lane_probe(
            stage_dir,
            include_packaged=False,
            import_names=REPAIR_HEALTH_IMPORTS,
        )
        if _runtime_ab_probe_ready_imports(stage_probe, APPROVED_IMPORTS) is not True:
            raise RuntimeError(
                "Candidate dependency lane failed required import probes: "
                + json.dumps(stage_probe, ensure_ascii=False)
            )
        previous_lane = _commit_runtime_lane(
            stage_dir,
            VENDOR_PY_DIR,
            quarantine_root=RUNTIME_QUARANTINE_ROOT_DIR,
        )
        committed = True
        configure_vendor_paths()
        committed_probe = _run_fresh_lane_probe(
            VENDOR_PY_DIR,
            include_packaged=False,
            import_names=REPAIR_HEALTH_IMPORTS,
        )
        if _runtime_ab_probe_ready_imports(committed_probe, APPROVED_IMPORTS) is not True:
            restored = (
                _restore_previous_runtime_lane(VENDOR_PY_DIR, previous_lane)
                if previous_lane is not None
                else None
            )
            if restored is None and VENDOR_PY_DIR.exists():
                _quarantine_runtime_lane(VENDOR_PY_DIR, reason_label="rejected-python-candidate-dependencies")
            raise RuntimeError(
                "Committed candidate dependency lane failed required probes: "
                + json.dumps(committed_probe, ensure_ascii=False)
            )
        return {
            "status": "built",
            "python_tag": _python_tag(),
            "vendor_dir": str(VENDOR_PY_DIR),
            "dependency_reports": dependency_reports,
            "ufbx": ufbx_report,
            "accelerators": accelerator_reports,
            "stage_probe": stage_probe,
            "committed_probe": committed_probe,
            "previous_lane": str(previous_lane) if previous_lane is not None else None,
        }
    finally:
        if committed is not True and stage_dir.exists():
            shutil.rmtree(stage_dir, ignore_errors=True)


def _runtime_ab_launcher_contract() -> dict[str, object]:
    launcher_path = BASE_DIR / "PC-REHD Code X Launcher.py"
    if launcher_path.is_file() is not True:
        return {"ready": False, "error": f"Launcher source is missing: {launcher_path}"}
    environment = _isolated_python_child_environment()
    completed = _run_hidden_subprocess(
        [sys.executable, "-B", str(launcher_path), "--runtime-compat-smoke"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=PYTHON_RUNTIME_CONTRACT_TIMEOUT_SECONDS,
        env=environment,
        cwd=BASE_DIR,
    )
    return {
        "ready": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": (completed.stdout or "")[-8000:],
        "stderr": (completed.stderr or "")[-8000:],
        "python_exe": sys.executable,
    }


def run_python_runtime_ab_contract() -> dict[str, object]:
    runtime_report = validate_python_runtime()
    source_report = _health_supervisor_source_report()
    lane_probe = _run_fresh_lane_probe(
        VENDOR_PY_DIR,
        include_packaged=True,
        import_names=REPAIR_HEALTH_IMPORTS,
    )
    preflight = run_system_preflight(repair=False)
    patched_ufbx_contract = get_patched_ufbx_behavior_contract_report()
    launcher_contract = _runtime_ab_launcher_contract()
    required_imports_ready = _runtime_ab_probe_ready_imports(lane_probe, APPROVED_IMPORTS)
    required_preflight_ready = not list(preflight.get("required_failures", []))
    ready = bool(
        runtime_report.get("supported") is True
        and source_report.get("status") == "PASS"
        and required_imports_ready
        and required_preflight_ready
        and patched_ufbx_contract.get("ready") is True
        and launcher_contract.get("ready") is True
    )
    return {
        "schema": "pc-rehd-code-x-python-runtime-contract-v1",
        "ready": ready,
        "python_exe": sys.executable,
        "python_version": _python_runtime_version_text(),
        "python_tag": _python_tag(),
        "runtime": runtime_report,
        "source": source_report,
        "dependency_lane": lane_probe,
        "required_imports": list(APPROVED_IMPORTS),
        "required_imports_ready": required_imports_ready,
        "preflight": preflight,
        "patched_ufbx": patched_ufbx_contract,
        "launcher": launcher_contract,
    }


def prepare_python_runtime_candidate(token: str) -> dict[str, object]:
    session = _runtime_candidate_session_report()
    if session.get("authorized") is not True or str(token or "") != str(
        dict(session.get("candidate", {})).get("token", "")
    ):
        raise RuntimeError("Candidate Python worker token or interpreter identity is invalid.")
    candidate = dict(session["candidate"])

    def mark_preparing(runtime_ab: dict[str, object]) -> None:
        current = dict(runtime_ab.get("candidate", {}))
        if str(current.get("token", "")) != str(token):
            raise RuntimeError("Candidate reservation changed before dependency preparation.")
        current["status"] = "preparing"
        current["stage"] = "dependencies"
        current["updated_epoch"] = time.time()
        runtime_ab["candidate"] = current

    _runtime_ab_update_state(mark_preparing)
    active = _runtime_ab_active_entry()
    active_abi = str(active.get("abi", "") or "").casefold()
    candidate_abi = str(candidate.get("abi", "") or "").casefold()
    existing_probe = _run_fresh_lane_probe(
        VENDOR_PY_DIR,
        include_packaged=True,
        import_names=REPAIR_HEALTH_IMPORTS,
    )
    if candidate_abi == active_abi:
        if _runtime_ab_probe_ready_imports(existing_probe, APPROVED_IMPORTS) is not True:
            raise RuntimeError(
                "Same-ABI candidate refused to mutate the active dependency lane because its required imports are unhealthy."
            )
        dependency_report = {
            "status": "reused-active-abi",
            "vendor_dir": str(VENDOR_PY_DIR),
            "probe": existing_probe,
        }
    elif _runtime_ab_probe_ready_imports(existing_probe, APPROVED_IMPORTS):
        dependency_report = {
            "status": "reused-candidate-abi",
            "vendor_dir": str(VENDOR_PY_DIR),
            "probe": existing_probe,
        }
    else:
        dependency_report = _runtime_ab_build_candidate_dependency_lane()

    def mark_testing(runtime_ab: dict[str, object]) -> None:
        current = dict(runtime_ab.get("candidate", {}))
        if str(current.get("token", "")) != str(token):
            raise RuntimeError("Candidate reservation changed before contract testing.")
        current["status"] = "testing"
        current["stage"] = "full-contract"
        current["updated_epoch"] = time.time()
        runtime_ab["candidate"] = current

    _runtime_ab_update_state(mark_testing)
    contract = run_python_runtime_ab_contract()
    if contract.get("ready") is not True:
        raise RuntimeError(
            "Candidate Python failed the import/export/Launcher contract: "
            + json.dumps(contract, ensure_ascii=False)[-30000:]
        )

    def mark_pass(runtime_ab: dict[str, object]) -> None:
        current = dict(runtime_ab.get("candidate", {}))
        if str(current.get("token", "")) != str(token):
            raise RuntimeError("Candidate reservation changed after contract testing.")
        current["status"] = "contract-pass"
        current["stage"] = "ready-to-promote"
        current["contract_pass_epoch"] = time.time()
        runtime_ab["candidate"] = current

    _runtime_ab_update_state(mark_pass)
    return {
        "ready": True,
        "candidate": candidate,
        "dependencies": dependency_report,
        "contract": contract,
    }


def _runtime_ab_json_payload(text_value: str) -> dict[str, object] | None:
    complete = str(text_value or "").strip()
    if complete:
        try:
            payload = json.loads(complete)
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
    for output_line in reversed(complete.splitlines()):
        try:
            payload = json.loads(output_line)
        except Exception:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _runtime_ab_contract_under_python(
    python_exe: str | Path,
    *,
    candidate_token: str = "",
) -> dict[str, object]:
    python_path = _normalize_existing_python_path(python_exe)
    if python_path is None:
        return {"ready": False, "error": f"Python runtime is missing: {python_exe}"}
    command = [
        str(python_path),
        "-I",
        "-B",
        str(Path(__file__).resolve()),
    ]
    if candidate_token:
        command.extend(
            [
                "--runtime-candidate-prepare",
                "--runtime-candidate-token",
                str(candidate_token),
            ]
        )
    else:
        command.append("--runtime-ab-contract")
    command.append("--json")
    environment = _isolated_python_child_environment()
    if candidate_token:
        environment[PYTHON_RUNTIME_CANDIDATE_TOKEN_ENV] = str(candidate_token)
        environment[PYTHON_RUNTIME_CANDIDATE_PATH_ENV] = str(python_path)
    completed = _run_hidden_subprocess(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=PYTHON_RUNTIME_CONTRACT_TIMEOUT_SECONDS,
        env=environment,
        cwd=BASE_DIR,
    )
    payload = _runtime_ab_json_payload(completed.stdout or "")
    ready = bool(
        completed.returncode == 0
        and isinstance(payload, dict)
        and payload.get("ready") is True
    )
    return {
        "ready": ready,
        "returncode": completed.returncode,
        "payload": payload,
        "stdout": (completed.stdout or "")[-12000:],
        "stderr": (completed.stderr or "")[-12000:],
        "python_exe": str(python_path),
    }


def _runtime_ab_promote_candidate(token: str, contract: dict[str, object]) -> dict[str, object]:
    with _bootstrap_state_lock(timeout_seconds=15.0):
        payload = _load_bootstrap_runtime_state()
        runtime_ab = _runtime_ab_payload_from_state(payload)
        candidate = dict(runtime_ab.get("candidate", {}))
        active = dict(runtime_ab.get("active", {}))
        if str(candidate.get("token", "")) != str(token):
            raise RuntimeError("Candidate token changed before atomic promotion.")
        if str(candidate.get("status", "")).casefold() != "contract-pass":
            raise RuntimeError("Candidate cannot be promoted before its full contract passes.")
        promoted = {
            "status": "approved",
            "slot": str(candidate.get("slot", "") or "").upper(),
            "python_exe": str(candidate.get("python_exe", "") or ""),
            "python_version": str(candidate.get("python_version", "") or ""),
            "abi": str(candidate.get("abi", "") or ""),
            "managed": True,
            "approved_epoch": time.time(),
            "installer_url": candidate.get("installer_url"),
            "installer_sha256": candidate.get("installer_sha256"),
            "contract_summary": {
                "ready": contract.get("ready") is True,
                "returncode": contract.get("returncode"),
            },
        }
        runtime_ab["rollback"] = active
        runtime_ab["active"] = promoted
        runtime_ab["active_slot"] = promoted["slot"]
        runtime_ab["candidate"] = {}
        runtime_ab["last_result"] = "promoted"
        runtime_ab["last_success_epoch"] = time.time()
        runtime_ab["next_check_epoch"] = time.time() + PYTHON_RUNTIME_UPDATE_CHECK_SECONDS
        failed_versions = runtime_ab.get("failed_versions", {})
        if isinstance(failed_versions, dict):
            failed_versions.pop(str(promoted["python_version"]), None)
            runtime_ab["failed_versions"] = failed_versions
        payload[PYTHON_RUNTIME_AB_STATE_KEY] = runtime_ab
        payload["preferred_python_exe"] = promoted["python_exe"]
        payload["preferred_python_version"] = promoted["python_version"]
        if _save_bootstrap_runtime_state(payload) is not True:
            raise OSError("Atomic Python runtime promotion state could not be persisted.")
        return dict(runtime_ab)


def _runtime_ab_record_failure(
    *,
    release: dict[str, object],
    stage: str,
    error: BaseException | str,
    candidate: dict[str, object] | None,
    quarantine_paths: tuple[str, ...] = (),
) -> dict[str, object]:
    version_text = str(release.get("version", "unknown") or "unknown")
    error_text = str(error)

    def update(runtime_ab: dict[str, object]) -> None:
        failed_versions = runtime_ab.get("failed_versions", {})
        if not isinstance(failed_versions, dict):
            failed_versions = {}
        previous = failed_versions.get(version_text, {})
        failure_count = int(previous.get("failure_count", 0) or 0) + 1 if isinstance(previous, dict) else 1
        failed_versions[version_text] = {
            "status": "quarantined",
            "python_version": version_text,
            "abi": release.get("abi"),
            "python_exe": dict(candidate or {}).get("python_exe", ""),
            "failed_stage": str(stage),
            "error_type": type(error).__name__ if isinstance(error, BaseException) else "RuntimeError",
            "error": error_text[-16000:],
            "failure_count": failure_count,
            "failed_epoch": time.time(),
            "retry_after_epoch": time.time() + PYTHON_RUNTIME_FAILED_VERSION_RETRY_SECONDS,
            "installer_url": release.get("installer_url"),
            "installer_sha256": dict(candidate or {}).get("installer_sha256", ""),
            "quarantine_paths": list(quarantine_paths),
        }
        runtime_ab["failed_versions"] = failed_versions
        runtime_ab["candidate"] = {}
        runtime_ab["last_result"] = "candidate-failed-active-preserved"
        runtime_ab["last_failure_epoch"] = time.time()
        runtime_ab["next_check_epoch"] = time.time() + PYTHON_RUNTIME_UPDATE_FAILURE_RETRY_SECONDS

    return _runtime_ab_update_state(update)


def _runtime_ab_quarantine_candidate_lane(abi: str, *, reason: str) -> Path | None:
    abi_text = _normalize_python_runtime_abi(abi)
    target_lane = VENDOR_PY_ROOT_DIR / abi_text
    if target_lane.exists() is not True:
        return None
    RUNTIME_QUARANTINE_ROOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    target = RUNTIME_QUARANTINE_ROOT_DIR / (
        f"{abi_text}-python-candidate-{re.sub(r'[^A-Za-z0-9_.-]+', '-', reason)[:40]}-{stamp}-{uuid.uuid4().hex[:8]}"
    )
    os.replace(target_lane, target)
    return target


def run_python_runtime_upgrade(
    *,
    target_python: str | None = None,
    force: bool = False,
) -> dict[str, object]:
    started = time.perf_counter()
    runtime_ab = ensure_python_runtime_ab_state()
    target_spec = _runtime_ab_parse_target(target_python)
    exact_target = target_spec is not None and len(target_spec) >= 3
    active = dict(runtime_ab.get("active", {}))
    if _runtime_ab_entry_is_usable(active) is not True:
        raise RuntimeError("Python A/B upgrade has no usable active rollback runtime.")
    with _runtime_install_lock(
        timeout_seconds=5.0,
        lock_path=RUNTIME_INTERPRETER_UPDATE_LOCK_PATH,
    ) as lock_report:
        runtime_ab = get_python_runtime_ab_report()
        active = dict(runtime_ab.get("active", {}))
        release = discover_official_python_runtime_update(
            current_version=active.get("python_version", _python_runtime_version_text()),
            target_python=target_python,
        )
        _runtime_ab_status_update(
            last_discovery=release,
            last_discovery_epoch=time.time(),
        )
        if release.get("status") != "update-available":
            _runtime_ab_status_update(
                last_result=str(release.get("status", "up-to-date")),
                next_check_epoch=time.time() + PYTHON_RUNTIME_UPDATE_CHECK_SECONDS,
            )
            return {
                "ready": True,
                "status": str(release.get("status", "up-to-date")),
                "active": active,
                "discovery": release,
                "lock": lock_report,
                "elapsed_seconds": round(time.perf_counter() - started, 6),
            }

        version_text = str(release.get("version", "") or "")
        failed_versions = runtime_ab.get("failed_versions", {})
        failed_entry = (
            dict(failed_versions.get(version_text, {}))
            if isinstance(failed_versions, dict) and isinstance(failed_versions.get(version_text), dict)
            else {}
        )
        retry_after = float(failed_entry.get("retry_after_epoch", 0.0) or 0.0)
        if failed_entry and force is not True and retry_after > time.time():
            return {
                "ready": True,
                "status": "failed-version-cooldown",
                "active": active,
                "discovery": release,
                "failed_version": failed_entry,
                "lock": lock_report,
                "elapsed_seconds": round(time.perf_counter() - started, 6),
            }

        active_contract = _runtime_ab_contract_under_python(active["python_exe"])
        if active_contract.get("ready") is not True and exact_target is not True:
            _runtime_ab_status_update(
                last_result="active-contract-failed-upgrade-not-started",
                last_active_contract=active_contract,
                next_check_epoch=time.time() + PYTHON_RUNTIME_UPDATE_FAILURE_RETRY_SECONDS,
            )
            return {
                "ready": False,
                "status": "active-contract-failed-upgrade-not-started",
                "active": active,
                "active_contract": active_contract,
                "discovery": release,
                "lock": lock_report,
                "elapsed_seconds": round(time.perf_counter() - started, 6),
            }
        if active_contract.get("ready") is not True and exact_target:
            # Exact first-run promotion must be able to repair a machine whose
            # currently active interpreter is old, newer, or otherwise outside
            # the packaged contract.  The target candidate gets the full
            # contract before promotion; the old interpreter is never resumed.
            active_contract = {
                "ready": False,
                "status": "skipped-for-exact-target",
                "python_exe": active.get("python_exe"),
                "target_python": _python_runtime_version_text(target_spec),
            }

        stage = "download"
        candidate_entry: dict[str, object] = {}
        quarantine_paths: list[str] = []
        try:
            _runtime_ab_status_update(
                last_result="candidate-downloading",
                candidate={
                    "status": "downloading",
                    "stage": stage,
                    "python_version": version_text,
                    "abi": release.get("abi"),
                    "installer_url": release.get("installer_url"),
                    "created_epoch": time.time(),
                },
            )
            download = _runtime_ab_download_installer(release)
            stage = "install"
            runtime_ab = get_python_runtime_ab_report()
            slot = _runtime_ab_inactive_slot(runtime_ab)
            install = _runtime_ab_install_python(release, download, slot=slot)
            token = uuid.uuid4().hex
            signature = dict(download.get("signature", {}))
            candidate_entry = {
                "status": "installed",
                "stage": "native-probe-pass",
                "slot": slot,
                "token": token,
                "python_exe": install["python_exe"],
                "python_version": version_text,
                "abi": str(release.get("abi", "") or ""),
                "managed": True,
                "installer_url": release.get("installer_url"),
                "installer_path": download.get("path"),
                "installer_sha256": signature.get("sha256", ""),
                "installer_signer": signature.get("subject", ""),
                "created_epoch": time.time(),
            }

            def reserve(runtime_state: dict[str, object]) -> None:
                runtime_state["candidate"] = dict(candidate_entry)
                runtime_state["last_result"] = "candidate-installed"
                runtime_state["last_active_contract"] = active_contract

            _runtime_ab_update_state(reserve)
            stage = "candidate-contract"
            candidate_contract = _runtime_ab_contract_under_python(
                candidate_entry["python_exe"],
                candidate_token=token,
            )
            if candidate_contract.get("ready") is not True:
                raise RuntimeError(
                    "Candidate interpreter or dependency contract failed: "
                    + json.dumps(candidate_contract, ensure_ascii=False)[-30000:]
                )
            stage = "atomic-promotion"
            promoted_state = _runtime_ab_promote_candidate(token, candidate_contract)
            return {
                "ready": True,
                "status": "promoted",
                "previous_active": active,
                "active": promoted_state.get("active"),
                "rollback": promoted_state.get("rollback"),
                "discovery": release,
                "download": download,
                "install": install,
                "active_contract": active_contract,
                "candidate_contract": candidate_contract,
                "lock": lock_report,
                "elapsed_seconds": round(time.perf_counter() - started, 6),
            }
        except Exception as exc:
            candidate_path = _normalize_existing_python_path(candidate_entry.get("python_exe"))
            if candidate_path is not None:
                slot_root = _runtime_ab_slot_dir(str(candidate_entry.get("slot", "") or ""))
                try:
                    quarantined = _runtime_ab_quarantine_managed_path(
                        slot_root,
                        reason=f"failed-{stage}",
                    )
                    if quarantined is not None:
                        quarantine_paths.append(str(quarantined))
                except Exception as quarantine_error:
                    quarantine_paths.append("interpreter-quarantine-error: " + str(quarantine_error))
            candidate_abi = str(candidate_entry.get("abi", release.get("abi", "")) or "")
            active_abi = str(active.get("abi", "") or "")
            if candidate_abi and candidate_abi.casefold() != active_abi.casefold():
                try:
                    quarantined_lane = _runtime_ab_quarantine_candidate_lane(
                        candidate_abi,
                        reason=f"failed-{stage}",
                    )
                    if quarantined_lane is not None:
                        quarantine_paths.append(str(quarantined_lane))
                except Exception as quarantine_error:
                    quarantine_paths.append("dependency-quarantine-error: " + str(quarantine_error))
            failed_state = _runtime_ab_record_failure(
                release=release,
                stage=stage,
                error=exc,
                candidate=candidate_entry,
                quarantine_paths=tuple(quarantine_paths),
            )
            return {
                "ready": False,
                "status": "candidate-failed-active-preserved",
                "active": active,
                "discovery": release,
                "active_contract": active_contract,
                "failed_stage": stage,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "quarantine_paths": quarantine_paths,
                "state": failed_state,
                "lock": lock_report,
                "elapsed_seconds": round(time.perf_counter() - started, 6),
            }


def rollback_python_runtime_to_previous(*, detail: str) -> dict[str, object]:
    failed_active: dict[str, object] = {}
    restored: dict[str, object] = {}
    with _bootstrap_state_lock(timeout_seconds=15.0):
        payload = _load_bootstrap_runtime_state()
        runtime_ab = _runtime_ab_payload_from_state(payload)
        failed_active = dict(runtime_ab.get("active", {}))
        restored = dict(runtime_ab.get("rollback", {}))
        if _runtime_ab_entry_is_usable(restored) is not True:
            return {
                "status": "rollback-unavailable",
                "failed_active": failed_active,
                "detail": str(detail),
            }
        failed_versions = runtime_ab.get("failed_versions", {})
        if not isinstance(failed_versions, dict):
            failed_versions = {}
        failed_version = str(failed_active.get("python_version", "unknown") or "unknown")
        failed_versions[failed_version] = {
            "status": "quarantined-after-startup",
            "python_version": failed_version,
            "abi": failed_active.get("abi"),
            "python_exe": failed_active.get("python_exe"),
            "failed_stage": "approved-runtime-startup",
            "error": str(detail)[-16000:],
            "failed_epoch": time.time(),
            "retry_after_epoch": time.time() + PYTHON_RUNTIME_FAILED_VERSION_RETRY_SECONDS,
        }
        runtime_ab["failed_versions"] = failed_versions
        runtime_ab["active"] = restored
        runtime_ab["active_slot"] = str(restored.get("slot", "A") or "A").upper()
        runtime_ab["rollback"] = {}
        runtime_ab["candidate"] = {}
        runtime_ab["last_result"] = "rolled-back-after-startup-failure"
        runtime_ab["last_failure_epoch"] = time.time()
        runtime_ab["next_check_epoch"] = time.time() + PYTHON_RUNTIME_UPDATE_FAILURE_RETRY_SECONDS
        payload[PYTHON_RUNTIME_AB_STATE_KEY] = runtime_ab
        payload["preferred_python_exe"] = restored.get("python_exe")
        payload["preferred_python_version"] = restored.get("python_version")
        if _save_bootstrap_runtime_state(payload) is not True:
            raise OSError("Python startup rollback state could not be persisted.")
    quarantine_path = ""
    failed_path = _normalize_existing_python_path(failed_active.get("python_exe"))
    if failed_path is not None and failed_active.get("managed") is True:
        try:
            slot_root = _runtime_ab_slot_dir(str(failed_active.get("slot", "") or ""))
            quarantined = _runtime_ab_quarantine_managed_path(slot_root, reason="startup-failure")
            quarantine_path = str(quarantined) if quarantined is not None else ""
        except Exception as exc:
            quarantine_path = "quarantine-error: " + str(exc)
    return {
        "status": "rolled-back",
        "failed_active": failed_active,
        "active": restored,
        "detail": str(detail),
        "quarantine_path": quarantine_path,
    }


def ensure_active_python_runtime(*, context_label: str = "PC-REHD Code X") -> dict[str, object]:
    candidate_session = _runtime_candidate_session_report()
    if candidate_session.get("authorized") is True:
        return validate_python_runtime()
    runtime_ab = ensure_python_runtime_ab_state()
    active = dict(runtime_ab.get("active", {}))
    active_path = _normalize_existing_python_path(active.get("python_exe"))
    if active_path is None:
        return validate_python_runtime()
    if _same_runtime_path(active_path, sys.executable):
        return validate_python_runtime()
    active_probe = _probe_python_runtime_candidate(active_path)
    if not isinstance(active_probe, dict) or _runtime_ab_path_is_approved(active_path, active_probe.get("version_tuple")) is not True:
        rollback_python_runtime_to_previous(
            detail=f"{context_label} rejected a missing or invalid approved Python runtime: {active_path}"
        )
        return validate_python_runtime()
    environment = _isolated_python_child_environment()
    environment[BOOTSTRAP_REEXEC_PATH_ENV] = str(active_path)
    try:
        completed = _run_hidden_subprocess(
            [str(active_path), *sys.argv],
            check=False,
            env=environment,
            hide_window=False,
        )
    except OSError as exc:
        rollback = rollback_python_runtime_to_previous(
            detail=(
                f"{context_label} could not start approved Python {active.get('python_version')}: "
                f"{type(exc).__name__}: {exc}"
            )
        )
        restored_path = _normalize_existing_python_path(dict(rollback.get("active", {})).get("python_exe"))
        if restored_path is not None and _same_runtime_path(restored_path, sys.executable):
            return validate_python_runtime()
        if restored_path is not None:
            fallback = _run_hidden_subprocess(
                [str(restored_path), *sys.argv],
                check=False,
                env=environment,
                hide_window=False,
            )
            raise SystemExit(fallback.returncode)
        raise
    # Candidate compatibility is proven before promotion by the isolated full
    # runtime contract. Launcher lifetime and user-initiated exits are not a
    # Python health signal and must never mutate A/B state.
    raise SystemExit(completed.returncode)


def _runtime_ab_spawn_upgrade_worker(token: str) -> int:
    command = [
        sys.executable,
        "-I",
        "-B",
        str(Path(__file__).resolve()),
        "--runtime-upgrade-worker",
        "--runtime-upgrade-token",
        str(token),
        "--json",
    ]
    popen_kwargs: dict[str, object] = {
        "cwd": str(BASE_DIR),
        "env": _isolated_python_child_environment(),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= int(getattr(subprocess, "STARTF_USESHOWWINDOW", 0))
        startup_info.wShowWindow = int(getattr(subprocess, "SW_HIDE", 0))
        popen_kwargs["startupinfo"] = startup_info
    process = subprocess.Popen(command, **popen_kwargs)
    return int(process.pid)


def schedule_python_runtime_upgrade(*, force: bool = False) -> dict[str, object]:
    ensure_python_runtime_ab_state()
    token = uuid.uuid4().hex
    reservation: dict[str, object] = {}

    def reserve(state: dict[str, object]) -> None:
        worker_state = dict(state.get("worker", {})) if isinstance(state.get("worker"), dict) else {}
        worker_pid = int(worker_state.get("pid", 0) or 0)
        worker_status = str(worker_state.get("status", "") or "").casefold()
        scheduled_epoch = float(worker_state.get("scheduled_epoch", 0.0) or 0.0)
        reservation_alive = (
            worker_status == "reserved"
            and scheduled_epoch > 0.0
            and (time.time() - scheduled_epoch) < 60.0
        )
        if (worker_pid > 0 and _process_is_alive(worker_pid)) or reservation_alive:
            reservation.update({"status": "already-running", "worker": worker_state})
            return
        next_check = float(state.get("next_check_epoch", 0.0) or 0.0)
        if force is not True and next_check > time.time():
            reservation.update({"status": "not-due", "next_check_epoch": next_check})
            return
        state["worker"] = {
            "status": "reserved",
            "token": token,
            "pid": 0,
            "scheduled_epoch": time.time(),
        }
        state["next_check_epoch"] = time.time() + PYTHON_RUNTIME_UPDATE_CHECK_SECONDS
        reservation.update({"status": "reserved", "token": token})

    _runtime_ab_update_state(reserve)
    if reservation.get("status") != "reserved":
        return reservation
    try:
        worker_pid = _runtime_ab_spawn_upgrade_worker(token)
    except Exception as exc:
        _runtime_ab_status_update(
            worker={"status": "launch-failed", "token": token, "error": str(exc)},
            next_check_epoch=time.time() + PYTHON_RUNTIME_UPDATE_FAILURE_RETRY_SECONDS,
        )
        return {"status": "launch-failed", "error": str(exc)}

    def mark_started(state: dict[str, object]) -> None:
        worker_state = dict(state.get("worker", {}))
        if (
            str(worker_state.get("token", "")) == token
            and str(worker_state.get("status", "") or "").casefold() == "reserved"
        ):
            worker_state.update({"status": "running", "pid": worker_pid, "started_epoch": time.time()})
            state["worker"] = worker_state

    _runtime_ab_update_state(mark_started)
    return {"status": "scheduled", "worker_pid": worker_pid, "token": token}


def run_python_runtime_upgrade_worker(token: str) -> dict[str, object]:
    ensure_python_runtime_ab_state()

    def claim(state: dict[str, object]) -> None:
        worker_state = dict(state.get("worker", {})) if isinstance(state.get("worker"), dict) else {}
        if str(worker_state.get("token", "")) != str(token or ""):
            raise RuntimeError("Python runtime upgrade worker token is invalid or stale.")
        worker_state.update(
            {
                "status": "running",
                "pid": os.getpid(),
                "claimed_epoch": time.time(),
            }
        )
        state["worker"] = worker_state

    _runtime_ab_update_state(claim)
    result = run_python_runtime_upgrade(force=False)

    def complete(state: dict[str, object]) -> None:
        worker_state = dict(state.get("worker", {}))
        if str(worker_state.get("token", "")) != str(token):
            return
        worker_state.update(
            {
                "status": "completed" if result.get("ready") is True else "failed",
                "pid": os.getpid(),
                "completed_epoch": time.time(),
                "result": str(result.get("status", "unknown")),
                "error": str(result.get("error", "") or "")[-4000:],
            }
        )
        state["worker"] = worker_state

    _runtime_ab_update_state(complete)
    return result


def _launcher_restart_process_info(pid: int) -> tuple[bool, str]:
    """Return process liveness and its executable path without guessing by window order."""
    exact_pid = int(pid)
    if os.name != "nt" or exact_pid <= 0:
        return False, ""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, exact_pid)
    if not handle:
        return False, ""
    try:
        exit_code = wintypes.DWORD(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False, ""
        if int(exit_code.value) != 259:
            return False, ""
        capacity = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(int(capacity.value))
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(capacity)
        ):
            return True, ""
        return True, str(buffer.value)
    finally:
        kernel32.CloseHandle(handle)


def _launcher_restart_foreground_pid() -> int:
    if os.name != "nt":
        return 0
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value) if int(pid.value) > 0 else 0


def _validate_launcher_restart_notice_request(
    *,
    launcher_path: str | Path,
    launcher_parent_pid: int,
    target_max_pid: int,
) -> dict[str, object]:
    """Enforce the final recovery gate again in Bootstrap before any restart."""
    if os.name != "nt":
        raise RuntimeError("Launcher recovery restart is supported only on Windows")
    source = Path(launcher_path).expanduser().resolve(strict=False)
    parent_pid = int(launcher_parent_pid)
    target_pid = int(target_max_pid)
    if not source.is_file() or source.name.casefold() != "pc-rehd code x launcher.py":
        raise RuntimeError(f"Launcher recovery source is invalid: {source}")
    if parent_pid <= 0 or target_pid <= 0:
        raise RuntimeError("Launcher recovery refuses PID=0")
    parent_alive, _parent_image = _launcher_restart_process_info(parent_pid)
    if not parent_alive:
        raise RuntimeError(f"Launcher parent PID {parent_pid} is not alive")
    target_alive, target_image = _launcher_restart_process_info(target_pid)
    if not target_alive:
        raise RuntimeError(f"Target Max PID {target_pid} is not alive")
    if not target_image or Path(target_image).name.casefold() != "3dsmax.exe":
        raise RuntimeError(
            f"Target PID {target_pid} is not a verified 3dsmax.exe process: {target_image or '<unknown>'}"
        )
    foreground_pid = _launcher_restart_foreground_pid()
    if foreground_pid != target_pid:
        raise RuntimeError(
            "Launcher recovery refused because the exact target Max process is not foreground: "
            f"target={target_pid}, foreground={foreground_pid}"
        )
    return {
        "launcher_path": str(source),
        "launcher_parent_pid": parent_pid,
        "target_max_pid": target_pid,
        "target_image": target_image,
        "foreground_pid": foreground_pid,
    }


def _show_launcher_restart_notice(target_max_pid: int, *, seconds: int = 3) -> None:
    duration = max(1, min(10, int(seconds)))
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.title("PC-REHD Code X")
        root.configure(background="#111820")
        root.resizable(False, False)
        root.overrideredirect(True)
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        body = tk.Frame(
            root,
            background="#111820",
            highlightbackground="#2D91FF",
            highlightcolor="#2D91FF",
            highlightthickness=1,
            padx=22,
            pady=18,
        )
        body.grid(row=0, column=0, sticky="nsew")
        tk.Frame(body, background="#2D91FF", height=3).grid(
            row=0, column=0, sticky="ew", pady=(0, 14)
        )
        tk.Label(
            body,
            text=runtime_ui_text(
                "Max Agent 多次握手失败",
                "Max Agent handshake failed repeatedly",
            ),
            background="#111820",
            foreground="#F4F7FA",
            font=("Microsoft YaHei", 13, "bold"),
            anchor="w",
        ).grid(row=1, column=0, sticky="ew")
        tk.Label(
            body,
            text=f"MAX PID {int(target_max_pid)}",
            background="#111820",
            foreground="#6DB7FF",
            font=("Microsoft YaHei", 10, "bold"),
            anchor="w",
        ).grid(row=2, column=0, sticky="ew", pady=(5, 10))
        tk.Label(
            body,
            text=runtime_ui_text(
                "仅重启 Launcher；3ds Max 不会关闭，当前场景不会被修改。",
                "Only the Launcher will restart. 3ds Max and the current scene stay open.",
            ),
            background="#111820",
            foreground="#C8D1DB",
            font=("Microsoft YaHei", 10),
            justify="left",
            anchor="w",
            wraplength=440,
        ).grid(row=3, column=0, sticky="ew")
        countdown = tk.StringVar()
        tk.Label(
            body,
            textvariable=countdown,
            background="#111820",
            foreground="#6DB7FF",
            font=("Microsoft YaHei", 10),
            anchor="e",
        ).grid(row=4, column=0, sticky="ew", pady=(14, 0))
        body.columnconfigure(0, weight=1)
        root.update_idletasks()
        width = max(500, int(root.winfo_reqwidth()))
        height = max(190, int(root.winfo_reqheight()))
        x = max(0, (int(root.winfo_screenwidth()) - width) // 2)
        y = max(0, (int(root.winfo_screenheight()) - height) // 2)
        root.geometry(f"{width}x{height}+{x}+{y}")
        deadline = time.monotonic() + float(duration)

        def tick() -> None:
            remaining = max(0, int(deadline - time.monotonic() + 0.999))
            countdown.set(
                runtime_ui_text(
                    f"Launcher 将在 {remaining} 秒后自动重启",
                    f"Launcher restarts automatically in {remaining}s",
                )
            )
            if remaining <= 0:
                root.destroy()
                return
            root.after(100, tick)

        root.deiconify()
        root.lift()
        tick()
        root.mainloop()
    except Exception:
        time.sleep(float(duration))


def _launcher_restart_python_executable() -> Path:
    executable = Path(sys.executable).resolve()
    if os.name == "nt":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.is_file():
            return pythonw
    return executable


def _signal_launcher_restart_ready_event(event_name: str) -> bool:
    name = str(event_name or "").strip()
    if os.name != "nt" or not name.startswith("Local\\PC_REHD_Code_X_LauncherRecovery_"):
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenEventW(0x0002, False, name)
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def run_launcher_restart_notice(
    *,
    launcher_path: str | Path,
    launcher_parent_pid: int,
    target_max_pid: int,
    reason: str = "",
    seconds: int = 3,
    dry_run: bool = False,
    ready_event: str = "",
) -> dict[str, object]:
    gate = _validate_launcher_restart_notice_request(
        launcher_path=launcher_path,
        launcher_parent_pid=launcher_parent_pid,
        target_max_pid=target_max_pid,
    )
    command = [
        str(_launcher_restart_python_executable()),
        str(gate["launcher_path"]),
        "--target-pid",
        str(gate["target_max_pid"]),
        "--recovery-restart-pid",
        str(gate["target_max_pid"]),
    ]
    payload = {
        **gate,
        "status": "validated",
        "reason": str(reason or "")[-2000:],
        "command": command,
        "dry_run": bool(dry_run),
        "ready_event": str(ready_event or ""),
    }
    if dry_run:
        return payload

    if not _signal_launcher_restart_ready_event(ready_event):
        raise RuntimeError("Launcher recovery readiness event is missing or unavailable")
    _show_launcher_restart_notice(int(gate["target_max_pid"]), seconds=seconds)
    parent_pid = int(gate["launcher_parent_pid"])
    deadline = time.monotonic() + 12.0
    while time.monotonic() < deadline:
        parent_alive, _parent_image = _launcher_restart_process_info(parent_pid)
        if not parent_alive:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError(
            f"Launcher parent PID {parent_pid} did not exit; refusing to start a duplicate Launcher"
        )
    target_alive, target_image = _launcher_restart_process_info(
        int(gate["target_max_pid"])
    )
    if not target_alive or Path(target_image).name.casefold() != "3dsmax.exe":
        raise RuntimeError("Target Max exited before Launcher recovery restart")

    environment = _isolated_python_child_environment()
    for variable_name in (
        "PC_REHD_CODE_X_MODE",
        "PC_REHD_CODE_X_PORT",
        "PC_REHD_CODE_X_TOKEN",
    ):
        environment.pop(variable_name, None)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = subprocess.SW_HIDE
    process = subprocess.Popen(
        command,
        cwd=str(Path(str(gate["launcher_path"])).parent),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        startupinfo=startup_info,
    )
    if int(process.pid) <= 0:
        raise RuntimeError("Launcher recovery restart returned PID=0")
    payload["status"] = "restarted"
    payload["replacement_launcher_pid"] = int(process.pid)
    return payload


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PC-REHD Code X Python runtime bootstrap.")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--system-check",
        action="store_true",
        help="Check system, source, dependency, and feature health without installing.",
    )
    mode_group.add_argument(
        "--system-initialize",
        action="store_true",
        help="Perform first initialization and safe dependency/runtime repair, then check all capabilities.",
    )
    mode_group.add_argument(
        "--module",
        action="append",
        choices=sorted(MODULE_REQUIREMENTS.keys()),
        help="Ensure runtime for one or more named bridge modules.",
    )
    mode_group.add_argument(
        "--export-runtime",
        action="store_true",
        help="Ensure the full Python import/export runtime bundle.",
    )
    mode_group.add_argument(
        "--repair-export-runtime",
        action="store_true",
        help="Transactionally diagnose and repair the active ABI export runtime lane.",
    )
    mode_group.add_argument(
        "--approved-imports",
        action="store_true",
        help="Ensure all approved local imports, including optional upgradeable imports.",
    )
    mode_group.add_argument(
        "--runtime-report",
        action="store_true",
        help="Print runtime bundle report for local Python, wheels, and optional accelerators.",
    )
    mode_group.add_argument(
        "--accelerator-sync-check",
        action="store_true",
        help="Fail when accelerator source, release copies, consumers, and bootstrap contract are not synchronized.",
    )
    mode_group.add_argument(
        "--self-test",
        action="store_true",
        help="Run import, path-priority, contract, quarantine, and transactional repair regressions.",
    )
    mode_group.add_argument(
        "--probe-runtime-lane",
        metavar="PATH",
        help=argparse.SUPPRESS,
    )
    mode_group.add_argument(
        "--background-upgrade-import",
        choices=sorted(IMPORT_POLICY.keys()),
        help=argparse.SUPPRESS,
    )
    mode_group.add_argument(
        "--runtime-upgrade",
        action="store_true",
        help="Discover, install, validate, and atomically promote the latest stable Python runtime.",
    )
    mode_group.add_argument(
        "--runtime-upgrade-worker",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    mode_group.add_argument(
        "--runtime-candidate-prepare",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    mode_group.add_argument(
        "--runtime-ab-contract",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    mode_group.add_argument(
        "--health-supervisor",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    mode_group.add_argument(
        "--launcher-restart-notice",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--probe-import", action="append", help=argparse.SUPPRESS)
    parser.add_argument("--probe-include-packaged", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--background-upgrade-token", default="", help=argparse.SUPPRESS)
    parser.add_argument("--background-upgrade-current-version", default="", help=argparse.SUPPRESS)
    parser.add_argument("--runtime-upgrade-token", default="", help=argparse.SUPPRESS)
    parser.add_argument("--runtime-candidate-token", default="", help=argparse.SUPPRESS)
    parser.add_argument("--target-python", default="", help="Optional MAJOR.MINOR or MAJOR.MINOR.PATCH target.")
    parser.add_argument("--force-runtime-upgrade", action="store_true", help="Retry a quarantined release immediately.")
    parser.add_argument("--health-log-dir", default="", help=argparse.SUPPRESS)
    parser.add_argument("--health-parent-pid", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--health-interval-seconds", type=float, default=HEALTH_SUPERVISOR_DEFAULT_INTERVAL_SECONDS, help=argparse.SUPPRESS)
    parser.add_argument("--health-deep-interval-seconds", type=float, default=HEALTH_SUPERVISOR_DEFAULT_DEEP_INTERVAL_SECONDS, help=argparse.SUPPRESS)
    parser.add_argument("--health-once", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--launcher-path", default="", help=argparse.SUPPRESS)
    parser.add_argument("--launcher-parent-pid", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--target-max-pid", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--launcher-restart-reason", default="", help=argparse.SUPPRESS)
    parser.add_argument("--launcher-restart-seconds", type=int, default=3, help=argparse.SUPPRESS)
    parser.add_argument("--launcher-restart-dry-run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--launcher-restart-ready-event", default="", help=argparse.SUPPRESS)
    parser.add_argument("--check-only", action="store_true", help="Validate runtime and report missing imports without installing.")
    parser.add_argument("--json", action="store_true", help="Print JSON payload.")
    return parser


def _build_check_only_payload(module_names: tuple[str, ...] | list[str], checker: ImportChecker) -> dict[str, object]:
    runtime_report = validate_python_runtime()
    reports: list[dict[str, object]] = []
    unique_missing: list[str] = []
    bridge_contract = get_export_bridge_contract_report()
    for module_name in module_names:
        module_dependencies = _expand_policy_import_dependencies(MODULE_REQUIREMENTS[module_name])
        dependency_reports: list[dict[str, object]] = []
        ready_dependencies: list[str] = []
        missing_dependencies: list[str] = []
        for import_name in module_dependencies:
            ready = checker(import_name) is True
            error_text = "" if ready else get_last_import_error_text(import_name)
            dependency_reports.append(
                {
                    "import_name": import_name,
                    "ready": ready,
                    "error": error_text,
                    "policy": dict(IMPORT_POLICY.get(import_name, {})),
                    "metadata_contract": get_vendor_metadata_contract_report(import_name),
                }
            )
            (ready_dependencies if ready else missing_dependencies).append(import_name)
        for import_name in missing_dependencies:
            if import_name not in unique_missing:
                unique_missing.append(import_name)
        reports.append(
            {
                "module": module_name,
                "python_runtime": runtime_report,
                "missing_before_install": missing_dependencies,
                "installed_packages": [],
                "ready_dependencies": ready_dependencies,
                "dependency_reports": dependency_reports,
                "bridge_contract": (
                    bridge_contract
                    if module_name == "codex_python_export_bridge"
                    else None
                ),
            }
        )
    return {
        "modules": [str(name) for name in module_names],
        "python_runtime": runtime_report,
        "missing_dependencies": unique_missing,
        "installed_packages": [],
        "module_reports": reports,
        "bridge_contract": bridge_contract,
        "dependency_contract": get_dependency_bundle_contract_report(include_runtime_health=False),
        "import_errors": {
            import_name: get_last_import_error_text(import_name)
            for import_name in unique_missing
        },
        "check_only": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_cli()
    args = parser.parse_args(argv)
    module_names = tuple(args.module) if args.module else EXPORT_RUNTIME_MODULES
    try:
        if args.runtime_ab_contract:
            payload = run_python_runtime_ab_contract()
        elif args.runtime_candidate_prepare:
            payload = prepare_python_runtime_candidate(str(args.runtime_candidate_token or ""))
        elif args.runtime_upgrade_worker:
            payload = run_python_runtime_upgrade_worker(str(args.runtime_upgrade_token or ""))
        elif args.runtime_upgrade:
            payload = run_python_runtime_upgrade(
                target_python=str(args.target_python or "") or None,
                force=bool(args.force_runtime_upgrade),
            )
        elif args.launcher_restart_notice:
            payload = run_launcher_restart_notice(
                launcher_path=args.launcher_path,
                launcher_parent_pid=args.launcher_parent_pid,
                target_max_pid=args.target_max_pid,
                reason=args.launcher_restart_reason,
                seconds=args.launcher_restart_seconds,
                dry_run=bool(args.launcher_restart_dry_run),
                ready_event=args.launcher_restart_ready_event,
            )
            if args.json:
                _write_json_stdout(payload)
            return 0
        elif args.health_supervisor:
            return run_bootstrap_health_supervisor(
                log_dir=args.health_log_dir,
                parent_pid=args.health_parent_pid,
                interval_seconds=args.health_interval_seconds,
                deep_interval_seconds=args.health_deep_interval_seconds,
                once=bool(args.health_once),
            )
        elif args.system_check or args.system_initialize:
            payload = run_system_preflight(repair=bool(args.system_initialize))
        elif args.background_upgrade_import:
            if not args.background_upgrade_token:
                raise RuntimeError("Background upgrade worker requires a valid scheduling token.")
            payload = _run_background_upgrade_worker(
                args.background_upgrade_import,
                token=str(args.background_upgrade_token),
                current_version=str(args.background_upgrade_current_version or "") or None,
            )
        elif args.probe_runtime_lane:
            payload = _probe_runtime_lane_in_process(
                args.probe_runtime_lane,
                include_packaged=bool(args.probe_include_packaged),
                import_names=tuple(args.probe_import or REPAIR_HEALTH_IMPORTS),
            )
        elif args.self_test:
            payload = run_bootstrap_self_tests()
            if payload.get("passed") is not True:
                raise RuntimeError("Bootstrap self-test reported a regression failure.")
        elif args.repair_export_runtime:
            payload = repair_export_runtime()
        elif args.accelerator_sync_check:
            sync_report = get_accelerator_dependency_sync_report(refresh=True)
            bridge_contract = get_export_bridge_contract_report()
            if sync_report.get("ready") is not True:
                raise RuntimeError(
                    "Accelerator dependency/bootstrap sync check failed:\n"
                    + "\n".join(str(value) for value in sync_report.get("errors", []))
                )
            if bridge_contract.get("ready") is not True:
                raise RuntimeError(
                    "Export bridge maintenance contract check failed: "
                    + json.dumps(bridge_contract, ensure_ascii=False)
                )
            payload = {
                "python_runtime": validate_python_runtime(),
                "accelerator_sync": sync_report,
                "bridge_contract": bridge_contract,
                "installed_packages": [],
            }
        elif args.check_only:
            if args.approved_imports:
                approved_reports = []
                missing_approved = []
                for name in APPROVED_IMPORTS:
                    ready = _default_import_checker(name) is True
                    approved_reports.append(
                        {
                            "import_name": name,
                            "ready": ready,
                            "error": "" if ready else get_last_import_error_text(name),
                            "policy": dict(IMPORT_POLICY.get(name, {})),
                            "metadata_contract": get_vendor_metadata_contract_report(name),
                        }
                    )
                    if ready is not True:
                        missing_approved.append(name)
                payload = {
                    "imports": list(APPROVED_IMPORTS),
                    "python_runtime": validate_python_runtime(),
                    "missing_dependencies": missing_approved,
                    "import_reports": approved_reports,
                    "dependency_contract": get_dependency_bundle_contract_report(include_runtime_health=False),
                    "installed_packages": [],
                    "check_only": True,
                }
            else:
                payload = _build_check_only_payload(module_names, _default_import_checker)
        elif args.runtime_report:
            payload = {
                "python_runtime": get_runtime_support_report(),
                "bundle_report": get_runtime_bundle_report(),
                "bridge_contract": get_export_bridge_contract_report(),
                "installed_packages": [],
            }
        elif args.approved_imports:
            payload = ensure_named_imports_runtime(APPROVED_IMPORTS)
        elif args.export_runtime or not args.module:
            payload = ensure_export_runtime()
        else:
            payload = ensure_named_modules_runtime(module_names)
    except Exception as exc:
        payload = {
            "status": "error",
            "error_type": getattr(exc, "error_type", type(exc).__name__),
            "error": str(exc),
            "error_report": dict(getattr(exc, "report", {})),
            "python_runtime": get_runtime_support_report(),
            "dependency_contract": get_dependency_bundle_contract_report(include_runtime_health=False),
        }
        if isinstance(exc, RuntimeInstallLockTimeout):
            payload["lock"] = dict(exc.report)
        if args.json:
            _write_json_stdout(payload)
        else:
            print(payload["error"])
        return 1

    if args.system_check or args.system_initialize:
        system_status = str(payload.get("status", "FAIL") or "FAIL").upper()
        if args.json:
            _write_json_stdout(payload)
        else:
            for row in payload.get("checks", []):
                if not isinstance(row, dict):
                    continue
                detail = str(row.get("detail", "") or "")
                print(
                    f"[{str(row.get('status', 'FAIL')).upper()}] {row.get('label', row.get('id', 'check'))}"
                    + (f" - {detail}" if detail else "")
                )
        return 0 if system_status == "PASS" else (2 if system_status == "DEGRADED" else 1)

    if (
        args.runtime_upgrade
        or args.runtime_upgrade_worker
        or args.runtime_candidate_prepare
        or args.runtime_ab_contract
    ):
        if args.json:
            _write_json_stdout(payload)
        elif payload.get("ready") is True:
            print("PC-REHD Code X Python A/B runtime: " + str(payload.get("status", "PASS")))
        else:
            print(str(payload.get("error", payload.get("status", "Python A/B runtime contract failed."))))
        return 0 if payload.get("ready") is True else 1

    if args.background_upgrade_import:
        worker_status = str(payload.get("status", "worker-complete") or "worker-complete")
        worker_failed = worker_status in {
            "worker-error",
            "worker-not-claimed",
            "network-unavailable",
            "invalid-online-report",
            "rejected-health",
            "rejected-commit",
        } or worker_status.endswith("-error")
        payload["worker_ok"] = worker_failed is not True
        if args.json:
            _write_json_stdout(payload)
        elif worker_failed:
            print(str(payload.get("error", worker_status) or worker_status))
        else:
            print("PC-REHD Code X background dependency upgrade completed: " + worker_status)
        return 1 if worker_failed else 0

    payload["status"] = "ok"
    if args.json:
        _write_json_stdout(payload)
    else:
        print("PC-REHD Code X Python runtime ready.")
        runtime_payload = payload.get("python_runtime", get_runtime_support_report())
        print("Python:", runtime_payload.get("current_python", "unknown"))
        print("Vendor:", runtime_payload.get("vendor_dir", VENDOR_PY_DIR))
        installed_packages = list(payload.get("installed_packages", []))
        print("Installed:", ", ".join(installed_packages) if installed_packages else "<none>")
    return 0


configure_vendor_paths()
LOADED_NATIVE_IMPORT_REUSE_REGRESSION_STATUS = {
    "status": "NOT_RUN",
    "reason": "Import remains read-only; run --self-test to execute regression guards.",
}


if __name__ == "__main__":
    raise SystemExit(main())
