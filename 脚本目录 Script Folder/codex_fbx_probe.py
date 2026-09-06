from __future__ import annotations

import argparse
import gc
import sys

if __name__ == "__main__" and "--parallel-worker" in sys.argv:
    gc.disable()
import hashlib
import json
import math
import os
import re
import struct
import sys
import time
import traceback
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generator

# ====== BEGIN BOUNDED PARALLEL WORKER TRANSPORT ======

import contextlib
import importlib.util
import queue
import pickle
import subprocess
import threading


# ====== BEGIN EXPORT GC DEFERRED CLEANUP ======
# Nested calls share the outer delivery boundary.  Cleanup is synchronous
# after delivery, never a GC thread competing with the next Python stage.
_EXPORT_GC_LOCK = threading.RLock()
_EXPORT_GC_STACK = []
_LAST_EXPORT_GC_STATS = {}


def _export_gc_begin():
    _EXPORT_GC_LOCK.acquire()
    token = {"was_enabled": gc.isenabled()}
    if not _EXPORT_GC_STACK:
        gc.disable()
    _EXPORT_GC_STACK.append(token)
    return token


def _export_gc_finish(token, *, cleanup=None):
    global _LAST_EXPORT_GC_STATS
    if not _EXPORT_GC_STACK or _EXPORT_GC_STACK[-1] is not token:
        raise RuntimeError("Export GC scopes must finish in their owning thread/order")
    try:
        if len(_EXPORT_GC_STACK) == 1:
            started = time.perf_counter()
            try:
                if cleanup is not None:
                    cleanup()
                collected = gc.collect()
                _LAST_EXPORT_GC_STATS = {
                    "policy": "after_export_delivery",
                    "collected": collected,
                    "cleanup_seconds": round(time.perf_counter() - started, 6),
                }
            finally:
                if token["was_enabled"]:
                    gc.enable()
    finally:
        _EXPORT_GC_STACK.pop()
        _EXPORT_GC_LOCK.release()


# ====== END EXPORT GC DEFERRED CLEANUP ======


class ParallelExecutionError(RuntimeError):
    export_blocking = True


def _parallel_read_exact(stream, size):
    chunks = bytearray(size)
    view = memoryview(chunks)
    offset = 0
    while offset < size:
        count = stream.readinto(view[offset:])
        if not count:
            raise EOFError("Parallel worker pipe closed before the packet completed")
        offset += count
    return chunks


def _parallel_read_packet(stream):
    size = struct.unpack("<Q", _parallel_read_exact(stream, 8))[0]
    return pickle.loads(_parallel_read_exact(stream, size))


def _parallel_write_packet(stream, packet):
    data = pickle.dumps(packet, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(struct.pack("<Q", len(data)))
    stream.write(data)
    stream.flush()


def _parallel_source_spec(worker):
    module = sys.modules[worker.__module__]
    path = Path(module.__file__).resolve()
    source = path.read_bytes()
    digest = hashlib.sha256(source).hexdigest().upper()
    loaded_digest = getattr(module, "__codex_source_sha256__", digest)
    if str(loaded_digest).upper() != digest:
        raise ParallelExecutionError(f"Parallel source changed after loading: {path}")
    return {
        "name": module.__name__, "path": str(path), "source": source,
        "sha256": digest, "function": worker.__name__,
        "fast_load": bool(getattr(module, "__codex_trusted_runtime_fast_load__", False)),
    }


def _parallel_load_worker(spec):
    path = Path(spec["path"])
    source = spec["source"]
    if hashlib.sha256(source).hexdigest().upper() != spec["sha256"]:
        raise RuntimeError("Parallel worker source fingerprint mismatch")
    sys.path.insert(0, str(path.parent))
    module_spec = importlib.util.spec_from_file_location(spec["name"], path)
    module = importlib.util.module_from_spec(module_spec)
    module.__codex_source_sha256__ = spec["sha256"]
    module.__codex_trusted_runtime_fast_load__ = spec["fast_load"]
    module.__codex_parallel_source_load__ = True
    # Install the exact defining name before unpickling FbxNode/task objects.
    sys.modules[spec["name"]] = module
    exec(compile(source, str(path), "exec"), module.__dict__)
    return getattr(module, spec["function"])


def _parallel_watch_parent(parent_pid):
    if sys.platform != "win32":
        while os.getppid() == parent_pid:
            time.sleep(1.0)
        os._exit(72)
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x00100000, False, parent_pid)
    if not handle:
        os._exit(72)
    result = kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
    kernel.CloseHandle(handle)
    if result == 0:
        os._exit(72)


def _parallel_worker_main(parent_pid):
    # pythonw has no sys.stdin/stdout wrappers even when pipe handles exist.
    incoming = os.fdopen(0, "rb", closefd=False)
    outgoing = os.fdopen(1, "wb", closefd=False)
    diagnostics = os.fdopen(2, "w", encoding="utf-8", errors="replace", closefd=False)
    sys.stderr = diagnostics
    sys.stdout = diagnostics
    threading.Thread(target=_parallel_watch_parent, args=(parent_pid,), daemon=True).start()
    gc.disable()
    try:
        worker = _parallel_load_worker(_parallel_read_packet(incoming))
        _parallel_write_packet(outgoing, {
            "kind": "ready", "pid": os.getpid(), "gc_deferred": not gc.isenabled(),
        })
        while True:
            packet = _parallel_read_packet(incoming)
            if packet is None:
                return 0
            index, task = packet
            started = time.perf_counter()
            result = worker(task)
            _parallel_write_packet(outgoing, {
                "kind": "result", "index": index, "value": result,
                "compute_seconds": time.perf_counter() - started,
            })
            del packet, task, result
    except BaseException as exc:
        try:
            _parallel_write_packet(outgoing, {
                "kind": "error", "type": type(exc).__name__, "error": str(exc),
                "traceback": traceback.format_exc()[-12000:],
            })
        except (OSError, EOFError):
            pass
        return 1


def _parallel_exchange(worker_id, process, source_spec, inbox, outbox):
    try:
        _parallel_write_packet(process.stdin, source_spec)
        reply = _parallel_read_packet(process.stdout)
        outbox.put((worker_id, reply))
        if reply.get("kind") != "ready":
            return
        while True:
            task = inbox.get()
            _parallel_write_packet(process.stdin, task)
            if task is None:
                return
            reply = _parallel_read_packet(process.stdout)
            outbox.put((worker_id, reply))
            if reply.get("kind") != "result":
                return
            del task, reply
    except BaseException as exc:
        outbox.put((worker_id, {
            "kind": "error", "type": type(exc).__name__, "error": str(exc),
        }))
    finally:
        for stream in (process.stdin, process.stdout):
            with contextlib.suppress(OSError):
                stream.close()


def _parallel_stderr_reader(stream, messages):
    try:
        while block := stream.read(4096):
            messages.append(block.decode("utf-8", errors="replace"))
            if len(messages) > 4:
                del messages[0]
    finally:
        stream.close()


def _iter_process_results(worker, tasks, *, max_workers, stats=None,
                         startup_timeout=60.0, task_timeout=120.0):
    """Preserve input order; at most one submitted task per worker is buffered.

    Each pipe has one I/O thread in the parent. The controller never blocks on
    a large pipe write or a multiprocessing Queue feeder, including cleanup.
    Worker failures are fatal to this batch; no source-geometry/serial fallback.
    """
    if not tasks:
        return
    stats = stats if stats is not None else {}
    try:
        source_spec = _parallel_source_spec(worker)
    except Exception as exc:
        raise ParallelExecutionError(
            f"Parallel source preparation: {type(exc).__name__}: {exc}") from exc
    worker_count = max(1, min(int(max_workers), len(tasks)))
    stats.update(transport="dedicated_process_pipes_v1", worker_pids=[],
                 completed_tasks=0, status="starting", source_module=source_spec["name"],
                 source_sha256=source_spec["sha256"], task_compute_seconds=0.0,
                 worker_gc_policy="disabled_until_os_process_release")
    started = time.perf_counter()
    outbox = queue.Queue()
    workers = []
    completed = False
    active = {}
    ready_results = {}
    try:
        for worker_id in range(worker_count):
            command = [sys.executable, "-B", "-s", str(Path(__file__).resolve()),
                       "--parallel-worker", str(os.getpid())]
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, creationflags=(
                    subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
            )
            inbox = queue.Queue(maxsize=1)
            messages = []
            thread = threading.Thread(target=_parallel_exchange,
                args=(worker_id, process, source_spec, inbox, outbox), daemon=True)
            reader = threading.Thread(target=_parallel_stderr_reader,
                args=(process.stderr, messages), daemon=True)
            workers.append((process, inbox, thread, reader, messages))
            stats["worker_pids"].append(process.pid)
            reader.start()
            thread.start()

        def receive(deadline):
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise ParallelExecutionError(
                        f"Parallel {worker.__name__} timed out; "
                        f"pids={stats['worker_pids']}, completed={stats['completed_tasks']}")
                try:
                    worker_id, reply = outbox.get(timeout=min(0.2, remaining))
                except queue.Empty:
                    for process, _inbox, _thread, _reader, messages in workers:
                        if process.poll() is not None:
                            raise ParallelExecutionError(
                                f"Parallel worker PID {process.pid} exited ({process.returncode}); "
                                + "".join(messages)[-12000:])
                    continue
                if reply.get("kind") == "error":
                    raise ParallelExecutionError(
                        f"Parallel {worker.__name__} PID {workers[worker_id][0].pid}: "
                        f"{reply.get('type')}: {reply.get('error')}\n{reply.get('traceback', '')}")
                return worker_id, reply

        waiting = set(range(worker_count))
        while waiting:
            worker_id, reply = receive(started + startup_timeout)
            if reply.get("kind") != "ready" or worker_id not in waiting:
                raise ParallelExecutionError("Invalid parallel worker startup receipt")
            if reply.get("gc_deferred") is not True:
                raise ParallelExecutionError("Parallel worker did not defer cyclic GC")
            waiting.remove(worker_id)
        stats["startup_seconds"] = round(time.perf_counter() - started, 6)
        stats["status"] = "running"

        def dispatch(worker_id, index):
            active[worker_id] = (index, time.perf_counter() + task_timeout)
            workers[worker_id][1].put_nowait((index, tasks[index]))

        for index in range(worker_count):
            dispatch(index, index)
        submitted = worker_count
        yielded = 0
        while yielded < len(tasks):
            if yielded not in ready_results:
                worker_id, reply = receive(min(deadline for _index, deadline in active.values()))
                index, _deadline = active.pop(worker_id)
                if reply.get("kind") != "result" or reply.get("index") != index:
                    raise ParallelExecutionError("Parallel worker returned the wrong task identity")
                stats["completed_tasks"] += 1
                stats["task_compute_seconds"] += float(reply.get("compute_seconds", 0.0))
                ready_results[index] = (worker_id, reply["value"])
                del reply
            while yielded in ready_results:
                worker_id, result = ready_results.pop(yielded)
                # The scene consumer closes immediately after its last next().
                # The batch is complete before yielding that final result.
                if yielded + 1 == len(tasks):
                    completed = True
                    stats["status"] = "complete"
                yield result
                del result
                yielded += 1
                if submitted < len(tasks):
                    dispatch(worker_id, submitted)
                    submitted += 1
        completed = True
        stats["status"] = "complete"
    except BaseException as exc:
        if not (completed and isinstance(exc, GeneratorExit)):
            stats["status"] = "cancelled" if isinstance(exc, GeneratorExit) else "error"
            stats["error"] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, Exception) and not isinstance(exc, ParallelExecutionError):
            raise ParallelExecutionError(
                f"Parallel {worker.__name__}: {type(exc).__name__}: {exc}") from exc
        raise
    finally:
        cleanup_started = time.perf_counter()
        for process, inbox, _thread, _reader, _messages in workers:
            with contextlib.suppress(queue.Full):
                inbox.put_nowait(None)
            if not completed and process.poll() is None:
                with contextlib.suppress(OSError):
                    process.kill()
        deadline = time.perf_counter() + 3.0
        for process, _inbox, _thread, _reader, _messages in workers:
            try:
                process.wait(timeout=max(0.0, deadline - time.perf_counter()))
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    process.kill()
        deadline = time.perf_counter() + 2.0
        for process, _inbox, thread, reader, _messages in workers:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=max(0.0, deadline - time.perf_counter()))
            if thread.ident is not None:
                thread.join(timeout=max(0.0, deadline - time.perf_counter()))
            else:
                process.stdin.close()
                process.stdout.close()
            if reader.ident is not None:
                reader.join(timeout=max(0.0, deadline - time.perf_counter()))
            else:
                process.stderr.close()
        stats["cleanup_seconds"] = round(time.perf_counter() - cleanup_started, 6)
        stats["elapsed_seconds"] = round(time.perf_counter() - started, 6)
        stats["worker_exitcodes"] = [p.poll() for p, *_rest in workers]
        if completed and (any(code != 0 for code in stats["worker_exitcodes"])
                          or any(t.is_alive() or r.is_alive() for _p, _i, t, r, _m in workers)):
            stats["status"] = "error"
            raise ParallelExecutionError(f"Parallel workers did not close cleanly: {stats['worker_exitcodes']}")


if (__name__ == "__main__" and not globals().get("__codex_parallel_source_load__")
        and len(sys.argv) == 3 and sys.argv[1] == "--parallel-worker"):
    # Packets are flushed before returning.  These isolated compute workers
    # own no files/transactions; OS teardown avoids an interpreter GC scan
    # while the parent is still exporting subsequent stages.
    os._exit(_parallel_worker_main(int(sys.argv[2])))

# ====== END BOUNDED PARALLEL WORKER TRANSPORT ======

from codex_python_runtime_bootstrap import (
    runtime_json_dumps_text,
    runtime_read_json_file,
    runtime_write_json_file,
    try_import_optional_runtime_module,
)


# Runtime installation belongs to Bootstrap health, never module import.
FBX_PROBE_ACCEL = try_import_optional_runtime_module(
    "codex_fbx_probe_accel",
    repair=False,
)
# AI MAINTENANCE GATE: changing this consumer contract requires synchronized
# edits to the accelerator package, bootstrap health contract, and release copy.
REQUIRED_FBX_ACCELERATOR_CONTRACT_REVISION = 2

FBX_PROBE_RUNTIME_UNAVAILABLE_STATUS = "runtime_unavailable"
FBX_PROBE_DATA_ERROR_STATUS = "data_error"
FBX_PROBE_BRIDGE_RETRY_STATUS = "RUNTIME_RETRY"
BINARY_FBX_NORMAL_FIDELITY_SCHEMA = "pc-rehd-fbx-corner-normal-fidelity-v1"
# Normal-space labels are deliberately explicit. Generic rebuilds emit one
# canonical scene-wide XYZ basis for every supported DCC route.
FBX_NORMAL_AXIS_DOMAIN_CANONICAL = "canonical_xyz"
UFBX_BEHAVIOR_CONTRACT_SCHEMA = "pc-rehd-code-x-patched-ufbx-behavior-v1"
UFBX_BEHAVIOR_FLOAT_DECIMALS = 8
# MOD's position denominator is authored in the same numeric unit domain as
# 3ds Max inches.  Unit conversion is owned by the generic reconstruction
# layer; Probe does not apply producer-specific (Blender/MAX) factors.
FBX_TARGET_UNIT_METERS = 0.0254
FBX_TARGET_UNIT_SCALE_CM = FBX_TARGET_UNIT_METERS * 100.0
CANONICAL_FBX_PROBE_SCHEMA = "pc-rehd-canonical-fbx-probe-v1"
CANONICAL_AXIS_DOMAIN = FBX_NORMAL_AXIS_DOMAIN_CANONICAL
CANONICAL_UNIT_DOMAIN = "max_inches_numeric"
GENERIC_AXIS_OUTPUT_POLICY = "max_xyz"
GENERIC_AXIS_TRANSFORM_SCOPE = "scene_global_once"
# Keep the embedded Probe's generic rebuild receipt names aligned with the
# standalone Generic FBX Converter.  These are aliases, not a second transform
# path; the scene is still canonicalized exactly once.
FBX_AXIS_OUTPUT_POLICY = GENERIC_AXIS_OUTPUT_POLICY
FBX_AXIS_TRANSFORM_CONTRACT = GENERIC_AXIS_TRANSFORM_SCOPE
FBX_CANONICALIZATION_POLICY = "alpha_full_canonical_or_fail_v1"
_LAST_GENERIC_PARALLEL_STATS: dict[str, Any] = {
    "mode": "serial",
    "selected_workers": 1,
    "task_count": 0,
    "elapsed_seconds": 0.0,
}
DYNAMIC_MOD_FBX_MAPPING_SCHEMA = "pc-rehd-dynamic-mod-fbx-mapping-v1"
# Transform-scale receipt is separate from the axis-basis receipt.  A basis
# change is dimensionless; the root scale and the FBX-unit ratio are length
# authorities and must never be hidden in the 4x4 axis matrices.
FBX_TRANSFORM_SCALE_SCHEMA = "pc-rehd-fbx-transform-scale-v1"
# The MOD importer first decodes an on-disk XYZ row into its internal Max
# scene domain as (x, -z, y). Generic Probe publishes one canonical XYZ scene,
# so Writer receives this explicit inverse pair instead of guessing a
# DCC-specific axis route. Unit scale is already owned by Probe and is not
# embedded in either matrix.
MOD_TO_CANONICAL_MATRIX = (
    1.0, 0.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, -1.0, 0.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
)
CANONICAL_TO_MOD_MATRIX = (
    1.0, 0.0, 0.0, 0.0,
    0.0, 0.0, -1.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
)

_ALPHA_SPATIAL_TOKENS = (
    "matrix", "transform", "translation", "rotation", "scaling",
    "position", "pivot", "axis", "quaternion", "euler", "geometric",
)


def _node_has_unverified_spatial_semantics(node: FbxNode) -> bool:
    """Reject unknown cloned records that could still carry source-space data."""
    if node.name.casefold() in {
        "animationcurve", "animationcurvenode", "constraint", "character",
        "controlset", "layerelementtangent", "layerelementbinormal",
    }:
        return True
    if node.name == "P" and node.properties:
        name = str(node.properties[0] or "").casefold()
        if any(token in name for token in _ALPHA_SPATIAL_TOKENS):
            return True
    return any(_node_has_unverified_spatial_semantics(child) for child in node.children)


_ALPHA_SAFE_CLONED_OBJECT_NAMES = {
    "Material", "Texture", "Video", "Implementation", "BindingTable",
    "BindingOperator", "SelectionNode", "SelectionSet",
}

# Animation records are currently passed through unchanged. Their source-space
# semantics are not rebuilt or interpreted by the Generic converter yet.
_ALPHA_ANIMATION_OBJECT_NAMES = {
    "animationcurve", "animationcurvenode", "animationstack", "animationlayer",
    "constraint", "character", "controlset", "layerelementtangent",
    "layerelementbinormal",
}


def _guard_alpha_cloned_object(node: FbxNode) -> None:
    if node.name.casefold() in _ALPHA_ANIMATION_OBJECT_NAMES:
        return
    if node.name in _ALPHA_SAFE_CLONED_OBJECT_NAMES:
        return
    if _node_has_unverified_spatial_semantics(node):
        raise ValueError(
            "ALPHA_UNVERIFIED: object "
            f"{node.name}/{_node_type(node) or '<unknown>'} contains source-space semantics"
        )
_NATIVE_LOADER_ERROR_CODES = frozenset({8, 126, 127, 182, 193, 1114})
_NATIVE_LOADER_ERROR_MARKERS = (
    "dll load failed",
    "dynamic module does not define module export function",
    "not a valid win32 application",
    "undefined symbol",
    "symbol not found",
    "wrong elf class",
    "incompatible architecture",
    "image not found",
)


def _qualified_exception_type(exc: BaseException) -> str:
    exception_type = type(exc)
    return f"{exception_type.__module__}.{exception_type.__qualname__}"


def _traceback_summary(exc: BaseException, *, max_chars: int = 4096) -> str:
    rendered = "".join(
        traceback.TracebackException.from_exception(
            exc,
            limit=12,
            capture_locals=False,
        ).format(chain=True)
    ).strip()
    if len(rendered) <= max_chars:
        return rendered
    return "...<traceback truncated>...\n" + rendered[-max_chars:]


def _looks_like_native_loader_failure(exc: BaseException) -> bool:
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return True
    error_codes = {
        int(value)
        for value in (getattr(exc, "winerror", None), getattr(exc, "errno", None))
        if isinstance(value, int)
    }
    if error_codes & _NATIVE_LOADER_ERROR_CODES:
        return True
    error_text = f"{_qualified_exception_type(exc)} {exc!r}".lower()
    return any(marker in error_text for marker in _NATIVE_LOADER_ERROR_MARKERS)


def classify_fbx_probe_exception(exc: BaseException, *, stage: str = "load_file") -> dict[str, Any]:
    runtime_retryable = stage == "import" or _looks_like_native_loader_failure(exc)
    return {
        "status": FBX_PROBE_RUNTIME_UNAVAILABLE_STATUS if runtime_retryable else FBX_PROBE_DATA_ERROR_STATUS,
        "bridge_status": FBX_PROBE_BRIDGE_RETRY_STATUS if runtime_retryable else "ERROR",
        "classification": "runtime_failure" if runtime_retryable else "fbx_data_error",
        "component": "ufbx",
        "stage": str(stage),
        "runtime_retryable": runtime_retryable,
        "exception_type": _qualified_exception_type(exc),
        "exception_repr": repr(exc),
        "exception_message": str(exc),
        "traceback_summary": _traceback_summary(exc),
        "errno": getattr(exc, "errno", None),
        "winerror": getattr(exc, "winerror", None),
    }


class FbxProbeRuntimeUnavailableError(ImportError):
    status = FBX_PROBE_RUNTIME_UNAVAILABLE_STATUS
    bridge_status = FBX_PROBE_BRIDGE_RETRY_STATUS
    runtime_retryable = True
    component = "ufbx"

    def __init__(self, details: dict[str, Any]) -> None:
        self.details = dict(details)
        self.stage = str(self.details.get("stage", "unknown"))
        self.exception_type = str(self.details.get("exception_type", "Exception"))
        self.exception_repr = str(self.details.get("exception_repr", ""))
        self.traceback_summary = str(self.details.get("traceback_summary", ""))
        super().__init__(
            f"ufbx runtime unavailable during {self.stage}: "
            f"{self.exception_type}: {self.exception_repr}"
        )

    def to_status_dict(self) -> dict[str, Any]:
        payload = dict(self.details)
        payload.update(
            {
                "status": self.status,
                "bridge_status": self.bridge_status,
                "runtime_retryable": True,
            }
        )
        return payload


UFBX_IMPORT_ERROR: Exception | None = None
UFBX_IMPORT_FAILURE: dict[str, Any] | None = None
try:
    import ufbx
except Exception as exc:  # pragma: no cover - environment-dependent import path
    ufbx = None
    UFBX_IMPORT_ERROR = exc
    UFBX_IMPORT_FAILURE = classify_fbx_probe_exception(exc, stage="import")


def get_fbx_probe_runtime_status() -> dict[str, Any]:
    if ufbx is not None:
        return {
            "status": "ok",
            "bridge_status": "OK",
            "classification": "runtime_available",
            "component": "ufbx",
            "mode": "ufbx_native",
            "substitute_available": True,
            "runtime_retryable": False,
        }
    if isinstance(UFBX_IMPORT_FAILURE, dict):
        payload = dict(UFBX_IMPORT_FAILURE)
    else:
        missing_error = UFBX_IMPORT_ERROR or ModuleNotFoundError("ufbx is not available")
        payload = classify_fbx_probe_exception(missing_error, stage="import")
    # UFBX is an accelerator for the Probe, not a prerequisite for MOD export.
    # Keep the original import diagnostics, but advertise the deterministic
    # binary-reader lane so Launcher health checks do not block the operation.
    payload.update(
        {
            "status": "ok",
            "bridge_status": "OK",
            "classification": "runtime_substitute_available",
            "mode": "ufbx_missed_substitute",
            "substitute_available": True,
            "runtime_retryable": False,
            "native_failure": True,
        }
    )
    return payload


MESH_SLOT_RE = re.compile(r"\bMESH[_\s-]*(\d{1,4})(?=\b|[_\s-])", re.IGNORECASE)
COMPACT_MESH_SLOT_RE = re.compile(r"\bMESH(\d{3,4})(?=\b|[_\s-])", re.IGNORECASE)
LOD_RE = re.compile(r"\bLOD[_\s-]*([0-9]+)\b", re.IGNORECASE)
BLENDER_COMPACT_RE6_MESH_NAME_RE = re.compile(
    r"(?i)^(?P<mesh_slot>0*[1-9]\d*)_X[0-9A-F]{8}_"
    r"(?P<lod_level>-?\d+)_-?\d+_-?\d+_-?\d+_-?\d+"
    r"(?P<legacy_suffix>(?:_(?:Import(?:[2-9]|[1-9]\d+)(?:_[1-9]\d*)?|"
    r"LODx-?\d+|MatID:-?\d+|Group:-?\d+|DisplayMode:-?\d+|Type:-?\d+))*)$"
)
BLENDER_COMPACT_LEGACY_LOD_RE = re.compile(r"(?i)_LODx(?P<lod_level>-?\d+)(?:_|$)")
MAX_FBX_ROUTE_USER_PROPERTY = "CodexRe6FbxRouteHandle"
MAX_FBX_ROUTE_USER_PROPERTY_RE = re.compile(
    rf"^\s*{re.escape(MAX_FBX_ROUTE_USER_PROPERTY)}\s*=\s*(?P<handle>\d+)\s*[Pp]?\s*$"
)
# A Max scene may contain duplicate Mesh names.  Its FBX route therefore
# cannot use Blender's unmarked name/slot fallback.
MAX_ALLOW_UNMARKED_ROUTE_FALLBACK = False
# The embedded Generic FBX reconstruction is used by the two DCC export
# lanes. Keep this as one exact predicate so an unknown route can never enter
# the normalizer merely because a caller passed a truthy option.
MAX_GENERIC_REBUILD_BACKEND = "max_fbx"
GENERIC_REBUILD_BACKENDS = frozenset({"max_fbx", "blender_fbx"})


def _is_max_generic_rebuild_backend(value: Any) -> bool:
    return str(value or "").strip().casefold() == MAX_GENERIC_REBUILD_BACKEND


def _uses_generic_rebuild_backend(value: Any) -> bool:
    return str(value or "").strip().casefold() in GENERIC_REBUILD_BACKENDS


# ====== BEGIN ROUTE METADATA (MAX HANDLE / BLENDER UNIT FLAG ONLY) ======

@dataclass(slots=True)
class BoundingBox:
    min_x: float = math.inf
    min_y: float = math.inf
    min_z: float = math.inf
    max_x: float = -math.inf
    max_y: float = -math.inf
    max_z: float = -math.inf

    def include(self, x: float, y: float, z: float) -> None:
        self.min_x = min(self.min_x, x)
        self.min_y = min(self.min_y, y)
        self.min_z = min(self.min_z, z)
        self.max_x = max(self.max_x, x)
        self.max_y = max(self.max_y, y)
        self.max_z = max(self.max_z, z)

    def to_dict(self) -> dict[str, float]:
        if math.isinf(self.min_x):
            return {
                "min_x": 0.0,
                "min_y": 0.0,
                "min_z": 0.0,
                "max_x": 0.0,
                "max_y": 0.0,
                "max_z": 0.0,
            }
        return {
            "min_x": round(self.min_x, 6),
            "min_y": round(self.min_y, 6),
            "min_z": round(self.min_z, 6),
            "max_x": round(self.max_x, 6),
            "max_y": round(self.max_y, 6),
            "max_z": round(self.max_z, 6),
        }


def normalize_match_name(name: str | None) -> str:
    if not name:
        return ""
    return re.sub(r"[^A-Z0-9]+", "", name.upper())


# Probe receives only a bounded, read-only description of the Launcher
# buckets.  It is an optimization hint, never a writer authorization.
FBX_PROBE_ROUTE_HINT_SCHEMA = "pc-rehd-fbx-probe-route-hints-v1"
FBX_PROBE_ROUTE_RECEIPT_SCHEMA = "pc-rehd-fbx-probe-route-receipt-v1"
_PROBE_SOURCE_ROUTES = frozenset(
    {
        "skin_without_fbx_faces",
        "unskinned_with_fbx_faces",
        "unskinned_without_fbx_faces",
        "max_model_null_header_only",
    }
)
_UNSKINNED_MESH_EDIT_EXPORT_AUTHORIZED_REASONS = frozenset(
    {
        "enabled_for_ordinary_export",
        "enabled_for_bones_plus_mesh",
    }
)


def _int_or_default(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _normalize_probe_route_hints(route_hints: Any) -> dict[str, Any]:
    """Normalize Launcher lane facts without accepting a final topology route."""
    if not isinstance(route_hints, dict):
        return {
            "status": "absent",
            "schema": FBX_PROBE_ROUTE_HINT_SCHEMA,
            "rows": [],
            "by_handle": {},
            "by_fbx_route_handle": {},
            "by_slot": {},
            "by_name": {},
            "explicit_route_required": False,
            "backend_kind": "",
        }
    schema = str(route_hints.get("schema", "") or "")
    if schema not in {"", FBX_PROBE_ROUTE_HINT_SCHEMA}:
        return {
            "status": "invalid_schema",
            "schema": schema,
            "rows": [],
            "by_handle": {},
            "by_fbx_route_handle": {},
            "by_slot": {},
            "by_name": {},
            "explicit_route_required": False,
            "backend_kind": "",
        }
    backend_kind = str(route_hints.get("backend_kind", "") or "").strip().lower()
    explicit_route_required = bool(
        route_hints.get("explicit_route_required") is True
        or _is_max_generic_rebuild_backend(backend_kind)
    ) and not MAX_ALLOW_UNMARKED_ROUTE_FALLBACK
    normalized_rows: list[dict[str, Any]] = []
    for raw_row in route_hints.get("rows", []):
        if not isinstance(raw_row, dict):
            continue
        raw_lane = str(raw_row.get("lane", "") or "").strip().lower()
        lane = "header" if raw_lane == "lod0" else raw_lane
        if lane not in {"header", "delete", "modify"}:
            continue
        handle = _int_or_default(raw_row.get("scene_node_handle"), 0)
        fbx_route_handle = _int_or_default(
            raw_row.get("fbx_route_handle", raw_row.get("expected_fbx_route_handle")),
            0,
        )
        slot = _int_or_default(raw_row.get("mesh_slot"), 0)
        names = {
            normalize_match_name(raw_row.get(field_name))
            for field_name in (
                "scene_node",
                "node_name",
                "mesh_name",
                "match_name",
                "mesh_name_match",
            )
            if normalize_match_name(raw_row.get(field_name))
        }
        if handle <= 0 and slot <= 0 and not names:
            continue
        # The lane is the only fact accepted from the hint.  Modify routes
        # remain pending until Probe reads the current FBX topology.
        route = (
            "header_only"
            if lane == "header"
            else "delete"
            if lane == "delete"
            else "topology_pending"
        )
        normalized_rows.append(
            {
                "lane": lane,
                "route": route,
                "scene_node_handle": handle,
                "fbx_route_handle": fbx_route_handle,
                "expected_fbx_route_handle": fbx_route_handle,
                "mesh_slot": slot,
                "names": sorted(names),
                "requires_selected_fbx": bool(raw_row.get("requires_selected_fbx", False)),
            }
        )

    raw_unskinned_policy = route_hints.get("unskinned_mesh_edit_export")
    if not isinstance(raw_unskinned_policy, dict):
        raw_unskinned_policy = {}
    policy_requested = _coerce_bool(raw_unskinned_policy.get("requested"), False)
    policy_enabled = _coerce_bool(raw_unskinned_policy.get("enabled"), False)
    policy_backend = str(
        raw_unskinned_policy.get("backend_kind", "") or ""
    ).strip().lower()
    policy_reason = str(
        raw_unskinned_policy.get("policy_reason", "") or ""
    ).strip()
    unskinned_policy = {
        "requested": policy_requested,
        "enabled": policy_enabled,
        "backend_kind": policy_backend,
        "policy_reason": policy_reason,
        "authorized": bool(
            policy_requested
            and policy_enabled
            and _is_max_generic_rebuild_backend(policy_backend)
            and policy_reason in _UNSKINNED_MESH_EDIT_EXPORT_AUTHORIZED_REASONS
        ),
    }

    by_handle: dict[int, list[dict[str, Any]]] = {}
    by_fbx_route_handle: dict[int, list[dict[str, Any]]] = {}
    by_slot: dict[int, list[dict[str, Any]]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for row in normalized_rows:
        handle = _int_or_default(row.get("scene_node_handle"), 0)
        if handle > 0:
            by_handle.setdefault(handle, []).append(row)
        fbx_route_handle = _int_or_default(row.get("fbx_route_handle"), 0)
        if fbx_route_handle > 0:
            by_fbx_route_handle.setdefault(fbx_route_handle, []).append(row)
        slot = _int_or_default(row.get("mesh_slot"), 0)
        if slot > 0:
            by_slot.setdefault(slot, []).append(row)
        for name in row.get("names", []):
            if name:
                candidates = by_name.setdefault(str(name), [])
                if row not in candidates:
                    candidates.append(row)
    return {
        "status": "ok",
        "schema": FBX_PROBE_ROUTE_HINT_SCHEMA,
        "authority": str(
            route_hints.get("authority", "launcher_bucket_receipt") or "launcher_bucket_receipt"
        ),
        "unskinned_mesh_edit_export": unskinned_policy,
        "rows": normalized_rows,
        "by_handle": by_handle,
        "by_fbx_route_handle": by_fbx_route_handle,
        "by_slot": by_slot,
        "by_name": by_name,
        "explicit_route_required": explicit_route_required,
        "backend_kind": backend_kind,
    }


def _probe_route_hint_for_mesh(
    mesh: Any,
    instance_node: Any | None,
    binary_identity: dict[str, Any],
    route_hints: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(route_hints, dict) or route_hints.get("status") != "ok":
        return None, str(route_hints.get("status", "absent") if isinstance(route_hints, dict) else "absent")

    explicit_route_required = bool(route_hints.get("explicit_route_required", False))
    route_handle = _int_or_default(binary_identity.get("fbx_route_handle"), 0)
    if explicit_route_required:
        # MAX identity is authoritative.  A missing or wrong marker must not
        # fall through to a duplicate name/slot; the caller will discard this
        # instance and leave the selected row for the correctly marked node.
        if route_handle > 0:
            candidates = route_hints.get("by_fbx_route_handle", {}).get(route_handle, [])
            if len(candidates) == 1:
                return candidates[0], "route_handle"
            if len(candidates) > 1:
                return None, "ambiguous_route_handle"
            return None, "identity_excluded"
        possible = []
        for value in (
            getattr(instance_node, "name", "") if instance_node is not None else "",
            getattr(mesh, "name", ""),
            binary_identity.get("fbx_model_name", ""),
        ):
            name_key = normalize_match_name(value)
            if name_key:
                possible.extend(route_hints.get("by_name", {}).get(name_key, []))
            slot_hint = infer_mesh_slot_hint(str(value or ""))
            slot = _int_or_default(slot_hint.get("slot"), 0) if isinstance(slot_hint, dict) else 0
            if slot > 0:
                possible.extend(route_hints.get("by_slot", {}).get(slot, []))
        if possible:
            return None, "identity_excluded"
        return None, "unmatched"

    if route_handle > 0:
        candidates = route_hints.get("by_handle", {}).get(route_handle, [])
        if len(candidates) == 1:
            return candidates[0], "route_handle"
        if len(candidates) > 1:
            return None, "ambiguous_route_handle"

    slot_values: set[int] = set()
    for value in (
        getattr(instance_node, "name", "") if instance_node is not None else "",
        getattr(mesh, "name", ""),
        binary_identity.get("fbx_model_name", ""),
    ):
        slot_hint = infer_mesh_slot_hint(str(value or ""))
        if isinstance(slot_hint, dict):
            slot = _int_or_default(slot_hint.get("slot"), 0)
            if slot > 0:
                slot_values.add(slot)
    for slot in sorted(slot_values):
        candidates = route_hints.get("by_slot", {}).get(slot, [])
        if len(candidates) == 1:
            return candidates[0], "mesh_slot"
        if len(candidates) > 1:
            return None, "ambiguous_mesh_slot"

    for value in (
        getattr(instance_node, "name", "") if instance_node is not None else "",
        getattr(mesh, "name", ""),
        binary_identity.get("fbx_model_name", ""),
    ):
        name_key = normalize_match_name(value)
        candidates = route_hints.get("by_name", {}).get(name_key, []) if name_key else []
        if len(candidates) == 1:
            return candidates[0], "name"
        if len(candidates) > 1:
            return None, "ambiguous_name"
    return None, "unmatched"


def _probe_topology_route(
    has_skin: bool,
    triangle_count: int,
    *,
    allow_unskinned_geometry: bool = False,
) -> str:
    """The single Probe-owned four-way topology policy."""
    if has_skin and triangle_count <= 0:
        return "skin_without_fbx_faces"
    if not has_skin and triangle_count > 0 and allow_unskinned_geometry:
        return "fbx_geometry"
    if not has_skin and triangle_count > 0:
        return "unskinned_with_fbx_faces"
    if not has_skin:
        return "unskinned_without_fbx_faces"
    return "fbx_geometry"


def _infer_blender_compact_mesh_facts(name: str | None) -> dict[str, int] | None:
    match = BLENDER_COMPACT_RE6_MESH_NAME_RE.fullmatch(str(name or "").strip())
    if match is None:
        return None
    facts = {
        "slot": int(match.group("mesh_slot")),
        "lod_level": int(match.group("lod_level")),
    }
    legacy_suffix = str(match.group("legacy_suffix") or "")
    for override in BLENDER_COMPACT_LEGACY_LOD_RE.finditer(legacy_suffix):
        facts["lod_level"] = int(override.group("lod_level"))
    return facts


def infer_mesh_slot_hint(name: str | None) -> dict[str, Any] | None:
    if not name:
        return None
    blender_compact = _infer_blender_compact_mesh_facts(name)
    if blender_compact is not None:
        return {
            "slot": int(blender_compact["slot"]),
            "basis": "blender_compact_re6_name",
        }
    compact_match = COMPACT_MESH_SLOT_RE.search(name)
    if compact_match:
        return {
            "slot": int(compact_match.group(1)),
            "basis": "zero_based_compact_token",
        }
    match = MESH_SLOT_RE.search(name)
    if match:
        return {
            "slot": int(match.group(1)),
            "basis": "one_based_name_token",
        }
    return None


def infer_lod_hint(name: str | None) -> int | None:
    if not name:
        return None
    blender_compact = _infer_blender_compact_mesh_facts(name)
    if blender_compact is not None:
        return int(blender_compact["lod_level"])
    match = LOD_RE.search(name)
    if not match:
        return None
    return int(match.group(1))


# ====== END ROUTE METADATA (MAX HANDLE / BLENDER UNIT FLAG ONLY) ======
# ====== BEGIN CANONICAL VECTOR / MATRIX MATH ======

def _vec3_to_list(vec: Any) -> list[float]:
    if hasattr(vec, "x"):
        return [round(float(vec.x), 6), round(float(vec.y), 6), round(float(vec.z), 6)]
    return [round(float(vec[0]), 6), round(float(vec[1]), 6), round(float(vec[2]), 6)]


def _vec2_to_list(vec: Any) -> list[float]:
    if hasattr(vec, "x"):
        return [round(float(vec.x), 6), round(float(vec.y), 6)]
    return [round(float(vec[0]), 6), round(float(vec[1]), 6)]


def _take_preview_values(values: Any, limit: int, converter: Any) -> list[Any]:
    out: list[Any] = []
    count = min(limit, len(values))
    for index in range(count):
        out.append(converter(values[index]))
    return out


def _bbox_from_vertex_values(values: Any) -> dict[str, float]:
    box = BoundingBox()
    for index in range(len(values)):
        vec = values[index]
        if hasattr(vec, "x"):
            box.include(float(vec.x), float(vec.y), float(vec.z))
        else:
            box.include(float(vec[0]), float(vec[1]), float(vec[2]))
    return box.to_dict()


def _flatten_matrix4x4(matrix: Any) -> list[float]:
    if isinstance(matrix, _PreparedRowMajorTransform):
        return list(matrix.flat)
    if isinstance(matrix, (list, tuple)) and len(matrix) >= 16:
        try:
            return [float(value) for value in matrix[:16]]
        except (TypeError, ValueError):
            pass
    flat: list[float] = []
    for row in _safe_list(matrix):
        if hasattr(row, "x"):
            flat.extend([float(row.x), float(row.y), float(row.z), float(getattr(row, "w", 0.0))])
            continue
        try:
            values = list(row)
        except Exception:
            flat.append(float(row))
            continue
        flat.extend(float(value) for value in values[:4])
    return flat[:16]


@dataclass(frozen=True, slots=True)
class _PreparedRowMajorTransform:
    flat: tuple[float, ...]
    normal: tuple[float, ...] | None


def _prepare_row_major_transform(matrix: Any) -> _PreparedRowMajorTransform:
    if isinstance(matrix, _PreparedRowMajorTransform):
        return matrix
    flat = tuple(_flatten_matrix4x4(matrix))
    if len(flat) < 16:
        return _PreparedRowMajorTransform(flat=flat, normal=None)

    a00, a01, a02 = flat[0], flat[1], flat[2]
    a10, a11, a12 = flat[4], flat[5], flat[6]
    a20, a21, a22 = flat[8], flat[9], flat[10]
    c00 = (a11 * a22) - (a12 * a21)
    c01 = (a12 * a20) - (a10 * a22)
    c02 = (a10 * a21) - (a11 * a20)
    determinant = (a00 * c00) + (a01 * c01) + (a02 * c02)
    if abs(determinant) <= 0.000000000001:
        normal = (
            flat[0], flat[4], flat[8],
            flat[1], flat[5], flat[9],
            flat[2], flat[6], flat[10],
        )
    else:
        inverse_det = 1.0 / determinant
        normal = (
            c00 * inverse_det,
            ((a02 * a21) - (a01 * a22)) * inverse_det,
            ((a01 * a12) - (a02 * a11)) * inverse_det,
            c01 * inverse_det,
            ((a00 * a22) - (a02 * a20)) * inverse_det,
            ((a02 * a10) - (a00 * a12)) * inverse_det,
            c02 * inverse_det,
            ((a01 * a20) - (a00 * a21)) * inverse_det,
            ((a00 * a11) - (a01 * a10)) * inverse_det,
        )
    return _PreparedRowMajorTransform(flat=flat, normal=normal)


def _transform_position_row_major(vec: Any, matrix: Any) -> list[float]:
    xyz = _vec3_to_list(vec)
    flat = _prepare_row_major_transform(matrix).flat
    if len(flat) < 16:
        return xyz
    x, y, z = xyz
    return [
        round((x * flat[0]) + (y * flat[4]) + (z * flat[8]) + flat[12], 6),
        round((x * flat[1]) + (y * flat[5]) + (z * flat[9]) + flat[13], 6),
        round((x * flat[2]) + (y * flat[6]) + (z * flat[10]) + flat[14], 6),
    ]


def _transform_direction_row_major(vec: Any, matrix: Any) -> list[float]:
    xyz = _vec3_to_list(vec)
    flat = _prepare_row_major_transform(matrix).flat
    if len(flat) < 16:
        return _normalize_vec3(xyz)
    x, y, z = xyz
    return _normalize_vec3(
        [
            (x * flat[0]) + (y * flat[4]) + (z * flat[8]),
            (x * flat[1]) + (y * flat[5]) + (z * flat[9]),
            (x * flat[2]) + (y * flat[6]) + (z * flat[10]),
        ],
    )


def _normal_vec3_to_list(vec: Any) -> list[float]:
    if isinstance(vec, (list, tuple)) and len(vec) >= 3:
        return [float(vec[0]), float(vec[1]), float(vec[2])]
    if hasattr(vec, "x"):
        return [float(vec.x), float(vec.y), float(vec.z)]
    try:
        values = list(vec)
    except Exception:
        return [0.0, 0.0, 0.0]
    while len(values) < 3:
        values.append(0.0)
    return [float(values[0]), float(values[1]), float(values[2])]


def _normalize_normal_vec3(vec: Any, fallback: tuple[float, float, float] = (0.0, 0.0, 1.0)) -> list[float]:
    """Normalize a normal without discarding precision before MOD byte quantization."""
    xyz = _normal_vec3_to_list(vec)
    length = math.sqrt((xyz[0] * xyz[0]) + (xyz[1] * xyz[1]) + (xyz[2] * xyz[2]))
    if length <= 0.000001:
        return [float(fallback[0]), float(fallback[1]), float(fallback[2])]
    return [xyz[0] / length, xyz[1] / length, xyz[2] / length]


def _transform_normal_row_major(vec: Any, matrix: Any) -> list[float]:
    """Transform a row-vector normal by inverse-transpose of the linear matrix."""
    xyz = _normal_vec3_to_list(vec)
    prepared = _prepare_row_major_transform(matrix)
    if prepared.normal is None:
        return _normalize_normal_vec3(xyz)
    normal = prepared.normal
    x, y, z = xyz
    return _normalize_normal_vec3(
        [
            (normal[0] * x) + (normal[1] * y) + (normal[2] * z),
            (normal[3] * x) + (normal[4] * y) + (normal[5] * z),
            (normal[6] * x) + (normal[7] * y) + (normal[8] * z),
        ]
    )


def _fbx_world_to_max_vec3(vec: Any) -> list[float]:
    return _vec3_to_list(vec)


def _fbx_world_to_max_normal(vec: Any) -> list[float]:
    return _normalize_normal_vec3(vec)


def _scene_axis_receipt(scene: Any) -> dict[str, Any]:
    labels = {0: "+X", 1: "-X", 2: "+Y", 3: "-Y", 4: "+Z", 5: "-Z"}
    axes = getattr(getattr(scene, "settings", None), "axes", None)
    if axes is None:
        return {"status": "missing", "signature": [], "right": "", "up": "", "front": ""}
    try:
        signature = (int(axes.right), int(axes.up), int(axes.front))
    except Exception:
        return {"status": "invalid", "signature": [], "right": "", "up": "", "front": ""}
    return {
        "status": "reported",
        "signature": list(signature),
        "right": labels.get(signature[0], f"UNKNOWN({signature[0]})"),
        "up": labels.get(signature[1], f"UNKNOWN({signature[1]})"),
        "front": labels.get(signature[2], f"UNKNOWN({signature[2]})"),
        "source": "ufbx_scene_settings",
    }


def _scene_unit_receipt(scene: Any) -> dict[str, Any]:
    """Report the FBX scene unit used by the already-loaded UFBX scene.

    UFBX exposes positions in scene units.  The MOD writer's position basis is
    centimetres, so consumers need this request-scoped fact to convert MAX's
    inch exports without re-reading the FBX.  Missing metadata is deliberately
    reported rather than guessed so legacy callers keep their prior behavior.
    """
    settings = getattr(scene, "settings", None)
    try:
        unit_meters = float(getattr(settings, "unit_meters"))
        original_unit_meters = float(
            getattr(settings, "original_unit_meters", unit_meters)
        )
    except (AttributeError, TypeError, ValueError, OverflowError):
        return {"status": "missing", "source": "ufbx_scene_settings"}
    if (
        not math.isfinite(unit_meters)
        or not math.isfinite(original_unit_meters)
        or unit_meters <= 0.0
        or original_unit_meters <= 0.0
    ):
        return {"status": "invalid", "source": "ufbx_scene_settings"}
    return {
        "status": "reported",
        "unit_meters": unit_meters,
        "original_unit_meters": original_unit_meters,
        "scale_to_centimeters": unit_meters / 0.01,
        "source": "ufbx_scene_settings",
    }


def _restore_max_space_geometry(
    geometry: dict[str, Any],
    mesh: Any,
    instance_node: Any | None,
) -> dict[str, Any]:
    """Replace every legacy V4 Y-up derivative with the Max Z-up FBX value."""
    # EXPIRED_AXIS_COMPAT_PATH: DELETE_AFTER_GENERIC_ONLY
    # Dead legacy V4 Y-up cleanup.  Generic reconstruction now owns the
    # document-level axis normalization and this helper has no live callers.
    if not isinstance(geometry, dict):
        return geometry
    for stale_key in (
        "max_positions",
        "world_positions",
        "max_normals",
        "skinned_max_positions",
        "skinned_world_positions",
        "skinned_max_normals",
        "skinned_is_local",
    ):
        geometry.pop(stale_key, None)
    return _augment_geometry_with_skinned_pose_channels(geometry, mesh, instance_node)


def _max_normal_to_re6_game_normal(vec: Any) -> list[float]:
    # Generic Probe owns the one scene-level axis conversion. Keep this legacy
    # API as an identity adapter so no caller can re-apply X,+Z,-Y.
    return _normalize_normal_vec3(vec)


def _fbx_authored_corner_normal_to_max(normal: Any) -> list[float]:
    """Keep an authored FBX corner normal out of the Mesh position transform.

    Blender serializes ``LayerElementNormal`` in the Geometry's authored
    surface space.  The Mesh node matrix carries object placement/export axes
    for positions, not a second transform that must be baked into this normal
    lane.  Applying it here reverses large parts of imported head meshes when
    the node has the common mirrored import basis.
    """
    return _normalize_normal_vec3(normal)


def _encode_re6_normal_key_from_fbx_corner(normal: Any) -> tuple[int, int, int]:
    """Return the writer RGB key for one canonical polygon-corner normal."""
    game_normal = _normalize_normal_vec3(_fbx_authored_corner_normal_to_max(normal))
    return tuple(max(0, min(255, int((axis * 127.0) + 127.0))) for axis in game_normal)


def _encode_re6_normal_key_from_fbx_local(normal: Any, node_to_world: Any) -> tuple[int, int, int]:
    """Return the canonical XYZ normal key without a per-Mesh axis transform."""
    game_normal = _normalize_normal_vec3(normal)
    return tuple(max(0, min(255, int((axis * 127.0) + 127.0))) for axis in game_normal)


def _max_import_matrix_from_fbx_matrix(matrix: Any) -> list[float] | None:
    prepared_transform = _prepare_row_major_transform(matrix)
    if len(prepared_transform.flat) < 16:
        return None
    origin_fbx = _transform_position_row_major([0.0, 0.0, 0.0], prepared_transform)
    axis_x_fbx = _transform_position_row_major([1.0, 0.0, 0.0], prepared_transform)
    axis_y_fbx = _transform_position_row_major([0.0, 1.0, 0.0], prepared_transform)
    axis_z_fbx = _transform_position_row_major([0.0, 0.0, 1.0], prepared_transform)
    origin = _fbx_world_to_max_vec3(origin_fbx)
    axis_x = _fbx_world_to_max_vec3(axis_x_fbx)
    axis_y = _fbx_world_to_max_vec3(axis_y_fbx)
    axis_z = _fbx_world_to_max_vec3(axis_z_fbx)
    row1 = [round(axis_x[index] - origin[index], 6) for index in range(3)]
    row2 = [round(axis_y[index] - origin[index], 6) for index in range(3)]
    row3 = [round(axis_z[index] - origin[index], 6) for index in range(3)]
    return [
        row1[0],
        row1[1],
        row1[2],
        0.0,
        row2[0],
        row2[1],
        row2[2],
        0.0,
        row3[0],
        row3[1],
        row3[2],
        0.0,
        origin[0],
        origin[1],
        origin[2],
        1.0,
    ]


def _mesh_node_pairs(scene: Any) -> list[tuple[Any, Any | None]]:
    mesh_nodes = [node for node in scene.nodes if getattr(node, "mesh", None) is not None]
    if mesh_nodes:
        # The Objects section and hierarchy traversal are independent FBX
        # orderings.  Pairing scene.meshes[i] with mesh_nodes[i] silently
        # attaches the wrong Geometry whenever an exporter writes Geometry in
        # physical-slot order but groups Models under LOD parents.  The node's
        # actual FBX connection is authoritative and also preserves instances.
        return [(node.mesh, node) for node in mesh_nodes]
    return [(mesh, None) for mesh in scene.meshes]


def _mesh_material_names(mesh: Any) -> list[str]:
    names: list[str] = []
    for material in getattr(mesh, "materials", []) or []:
        names.append(str(getattr(material, "name", "") or ""))
    return names


def _resolve_vertex_attr_index(
    value_count: int,
    *,
    position_index: int,
    corner_index: int,
    vertex_count: int,
    index_count: int,
) -> int | None:
    if value_count <= 0:
        return None
    if value_count == index_count and 0 <= corner_index < value_count:
        return corner_index
    if value_count == vertex_count and 0 <= position_index < value_count:
        return position_index
    if 0 <= corner_index < value_count:
        return corner_index
    if 0 <= position_index < value_count:
        return position_index
    return None


def _default_vec3() -> list[float]:
    return [0.0, 0.0, 0.0]


def _default_normal() -> list[float]:
    return [0.0, 0.0, 1.0]


def _default_vec2() -> list[float]:
    return [0.0, 0.0]


def _safe_list(value: Any) -> list[Any]:
    if value is None:
        return []
    try:
        return list(value)
    except Exception:
        return []


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return bool(value)


def _get_mesh_skinned_vec3_rows(mesh: Any, *attr_names: str) -> list[Any]:
    for attr_name in attr_names:
        try:
            value = getattr(mesh, attr_name, None)
        except Exception:
            value = None
        rows = _safe_list(value)
        if len(rows) > 0:
            return rows
    return []


def _distance_sq_vec3(a: Any, b: Any) -> float:
    av = _vec3_to_list(a)
    bv = _vec3_to_list(b)
    dx = av[0] - bv[0]
    dy = av[1] - bv[1]
    dz = av[2] - bv[2]
    return (dx * dx) + (dy * dy) + (dz * dz)


FBX_BINARY_SKIN_EVALUATION_SCHEMA = "pc-rehd-fbx-binary-skin-evaluation-v1"
_FBX_BINARY_SIGNATURE = b"Kaydara FBX Binary  \x00\x1a\x00"
_FBX_BINARY_ARRAY_LAYOUTS = {
    "f": ("f", 4),
    "d": ("d", 8),
    "i": ("i", 4),
    "l": ("q", 8),
    "b": ("B", 1),
    "c": ("B", 1),
}
_FBX_BINARY_SHARED_DECODE_ARRAY_NAMES = frozenset(
    {
        "Indexes",
        "Weights",
        "Transform",
        "TransformLink",
        "Vertices",
        "PolygonVertexIndex",
        "Normals",
        "NormalsIndex",
        "UV",
        "UVIndex",
    }
)


@dataclass(slots=True)
class _BinaryFbxLazyArray:
    data: bytes
    offset: int
    stored_size: int
    value_count: int
    encoding: int
    property_type: str
    decoded: list[Any] | None = None

    def decode(self) -> list[Any]:
        if self.decoded is not None:
            return self.decoded
        raw_value = self.data[self.offset : self.offset + self.stored_size]
        if self.encoding == 1:
            raw_value = zlib.decompress(raw_value)
        elif self.encoding != 0:
            raise ValueError(f"Binary FBX array has unsupported encoding {self.encoding}")
        item_format, item_size = _FBX_BINARY_ARRAY_LAYOUTS[self.property_type]
        expected_size = int(self.value_count) * item_size
        if len(raw_value) != expected_size:
            raise ValueError("Binary FBX array byte count does not match its element count")
        self.decoded = list(
            struct.unpack(f"<{int(self.value_count)}{item_format}", raw_value)
        )
        return self.decoded


@dataclass(slots=True)
class _BinaryFbxNode:
    name: str
    properties: list[Any]
    children: list["_BinaryFbxNode"]


@dataclass(slots=True)
class _BinaryFbxDocument:
    """Request-scoped binary FBX parse shared by Probe consumers.

    The document deliberately owns the raw bytes and parsed tree for one
    request only.  It is not a path/global cache, so a later export cannot
    observe mutable state from an earlier export.
    """

    path: Path
    data: bytes
    version: int
    roots: list[_BinaryFbxNode]
    decode_array_names: frozenset[str]
    read_count: int = 1
    tree_build_count: int = 1

    def receipt(self) -> dict[str, Any]:
        lazy_total = 0
        lazy_decoded = 0
        pending = list(self.roots)
        while pending:
            node = pending.pop()
            pending.extend(node.children)
            for value in node.properties:
                if isinstance(value, _BinaryFbxLazyArray):
                    lazy_total += 1
                    if value.decoded is not None:
                        lazy_decoded += 1
        return {
            "status": "available",
            "path": str(self.path),
            "read_count": int(self.read_count),
            "tree_build_count": int(self.tree_build_count),
            "decoded_array_name_count": len(self.decode_array_names),
            "lazy_array_count": lazy_total,
            "lazy_array_decoded_count": lazy_decoded,
        }


def _binary_fbx_require_range(data: bytes, offset: int, size: int, *, label: str) -> None:
    if offset < 0 or size < 0 or offset > len(data) - size:
        raise ValueError(f"Binary FBX {label} exceeds the file bounds")


def _read_binary_fbx_property(
    data: bytes,
    offset: int,
    *,
    decode_array: bool,
) -> tuple[Any, int]:
    _binary_fbx_require_range(data, offset, 1, label="property type")
    property_type = chr(data[offset])
    offset += 1
    scalar_layouts = {
        "Y": ("h", 2),
        "I": ("i", 4),
        "F": ("f", 4),
        "D": ("d", 8),
        "L": ("q", 8),
    }
    if property_type in scalar_layouts:
        fmt, size = scalar_layouts[property_type]
        _binary_fbx_require_range(data, offset, size, label=f"{property_type} property")
        return struct.unpack_from("<" + fmt, data, offset)[0], offset + size
    if property_type == "C":
        _binary_fbx_require_range(data, offset, 1, label="C property")
        return bool(data[offset]), offset + 1
    if property_type in {"S", "R"}:
        _binary_fbx_require_range(data, offset, 4, label=f"{property_type} property length")
        value_size = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        _binary_fbx_require_range(data, offset, value_size, label=f"{property_type} property")
        raw_value = data[offset : offset + value_size]
        offset += value_size
        if property_type == "S":
            return raw_value.decode("utf-8", errors="replace"), offset
        return None, offset
    if property_type in _FBX_BINARY_ARRAY_LAYOUTS:
        _binary_fbx_require_range(data, offset, 12, label=f"{property_type} array header")
        value_count, encoding, stored_size = struct.unpack_from("<III", data, offset)
        offset += 12
        _binary_fbx_require_range(data, offset, stored_size, label=f"{property_type} array")
        array_offset = offset
        offset += stored_size
        lazy_array = _BinaryFbxLazyArray(
            data=data,
            offset=array_offset,
            stored_size=int(stored_size),
            value_count=int(value_count),
            encoding=int(encoding),
            property_type=property_type,
        )
        return (lazy_array.decode() if decode_array else lazy_array), offset
    raise ValueError(f"Binary FBX has unsupported property type {property_type!r}")


def _read_binary_fbx_node(
    data: bytes,
    offset: int,
    *,
    version: int,
    decode_array_names: frozenset[str],
) -> tuple[_BinaryFbxNode | None, int]:
    uses_wide_headers = version >= 7500
    header_format = "<QQQB" if uses_wide_headers else "<IIIB"
    header_size = 25 if uses_wide_headers else 13
    _binary_fbx_require_range(data, offset, header_size, label="node header")
    end_offset, property_count, property_bytes, name_size = struct.unpack_from(header_format, data, offset)
    if end_offset == 0:
        return None, offset + header_size
    if end_offset > len(data):
        raise ValueError("Binary FBX node end offset exceeds the file size")
    offset += header_size
    _binary_fbx_require_range(data, offset, name_size, label="node name")
    name = data[offset : offset + name_size].decode("utf-8", errors="replace")
    offset += name_size
    property_end = offset + int(property_bytes)
    if property_end > end_offset:
        raise ValueError("Binary FBX property data exceeds its node")
    decode_array = name in decode_array_names
    properties: list[Any] = []
    for _ in range(int(property_count)):
        property_value, offset = _read_binary_fbx_property(
            data,
            offset,
            decode_array=decode_array,
        )
        properties.append(property_value)
    if offset != property_end:
        raise ValueError("Binary FBX property length does not match its node header")

    children: list[_BinaryFbxNode] = []
    null_record_size = header_size
    while offset < end_offset - null_record_size:
        child, offset = _read_binary_fbx_node(
            data,
            offset,
            version=version,
            decode_array_names=decode_array_names,
        )
        if child is not None:
            children.append(child)
    if offset > end_offset:
        raise ValueError("Binary FBX child node exceeds its parent")
    return _BinaryFbxNode(name=name, properties=properties, children=children), int(end_offset)


# ====== END CANONICAL VECTOR / MATRIX MATH ======
# ====== BEGIN BINARY FBX DOCUMENT READER ======

def _read_binary_fbx_roots(
    path: Path,
    *,
    decode_array_names: frozenset[str] | None = None,
    binary_document: _BinaryFbxDocument | None = None,
) -> list[_BinaryFbxNode]:
    if binary_document is not None:
        if Path(path).resolve() != binary_document.path.resolve():
            raise ValueError("Binary FBX document belongs to a different path")
        return binary_document.roots
    # All real-file binary reads share the canonical Generic document.  This
    # guard prevents private identity/diagnostic helpers from reopening the
    # producer's raw bytes behind the public handoff.
    if Path(path).is_file():
        canonical_document, _receipt = _generic_memory_document_for_path(path)
        return canonical_document.roots
    data = path.read_bytes()
    if not data.startswith(_FBX_BINARY_SIGNATURE):
        raise ValueError("FBX is not a supported binary FBX file")
    _binary_fbx_require_range(data, len(_FBX_BINARY_SIGNATURE), 4, label="version")
    version = struct.unpack_from("<I", data, len(_FBX_BINARY_SIGNATURE))[0]
    if version < 7000:
        raise ValueError(f"Binary FBX version {version} is not supported for skin evaluation")
    header_size = 25 if version >= 7500 else 13
    decode_names = (
        frozenset({"Indexes", "Weights", "Transform", "TransformLink"})
        if decode_array_names is None
        else frozenset(decode_array_names)
    )
    roots: list[_BinaryFbxNode] = []
    offset = len(_FBX_BINARY_SIGNATURE) + 4
    while offset < len(data) - header_size:
        root, offset = _read_binary_fbx_node(
            data,
            offset,
            version=version,
            decode_array_names=decode_names,
        )
        if root is None:
            break
        roots.append(root)
    return roots


def _build_binary_fbx_document(
    path: Path,
    *,
    decode_array_names: frozenset[str] | None = None,
    data: bytes | bytearray | memoryview | None = None,
) -> _BinaryFbxDocument:
    """Read and parse one binary FBX for all consumers in one Probe request."""
    raw_data = bytes(data) if data is not None else Path(path).read_bytes()
    if not raw_data.startswith(_FBX_BINARY_SIGNATURE):
        raise ValueError("FBX is not a supported binary FBX file")
    _binary_fbx_require_range(raw_data, len(_FBX_BINARY_SIGNATURE), 4, label="version")
    version = struct.unpack_from("<I", raw_data, len(_FBX_BINARY_SIGNATURE))[0]
    if version < 7000:
        raise ValueError(f"Binary FBX version {version} is not supported for skin evaluation")
    header_size = 25 if version >= 7500 else 13
    decode_names = (
        _FBX_BINARY_SHARED_DECODE_ARRAY_NAMES
        if decode_array_names is None
        else frozenset(decode_array_names)
    )
    roots: list[_BinaryFbxNode] = []
    offset = len(_FBX_BINARY_SIGNATURE) + 4
    while offset < len(raw_data) - header_size:
        root, offset = _read_binary_fbx_node(
            raw_data,
            offset,
            version=version,
            decode_array_names=frozenset(decode_names),
        )
        if root is None:
            break
        roots.append(root)
    return _BinaryFbxDocument(
        path=Path(path),
        data=raw_data,
        version=int(version),
        roots=roots,
        decode_array_names=frozenset(decode_names),
    )


def _binary_fbx_node_child_value(node: _BinaryFbxNode, child_name: str) -> Any:
    for child in node.children:
        if child.name == child_name and child.properties:
            value = child.properties[0]
            if isinstance(value, _BinaryFbxLazyArray):
                return value.decode()
            return value
    return None


# ====== END BINARY FBX DOCUMENT READER ======
# ====== BEGIN GENERIC FBX IN-MEMORY NORMALIZER (MAX + BLENDER) ======

import hashlib
import json
import math
import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Binary FBX reader/writer
#
# This section is deliberately self-contained. It reads the tagged binary
# FBX tree, decodes standard FBX property types, and writes the same tree
# back. No DCC application or third-party FBX runtime is needed.

FBX_MAGIC = b"Kaydara FBX Binary  \x00\x1a\x00"
FBX_VERSION_DEFAULT = 7400
FBX_NULL_RECORD_NARROW = b"\x00" * 13
FBX_NULL_RECORD_WIDE = b"\x00" * 25
FBX_FILE_ID = bytes.fromhex("28b32aebb624ccc2bfc8b02aa92bfcf1")
FBX_FOOT_ID = bytes.fromhex("fabcab09d0c8d466b176fb831cf7267e")
FBX_FOOT_MAGIC = bytes.fromhex("f85a8c6adef5d97eece90ce3758f290b")
FBX_ARRAY_LAYOUTS = {
    "b": ("B", 1),
    "c": ("B", 1),
    "i": ("i", 4),
    "l": ("q", 8),
    "f": ("f", 4),
    "d": ("d", 8),
}
FBX_ALWAYS_BLOCK = {"AnimationStack", "AnimationLayer"}
ROUTE_HANDLE_PROPERTY = "CodexRe6FbxRouteHandle"
_ROUTE_HANDLE_RE = re.compile(
    rf"(?im)^\s*{re.escape(ROUTE_HANDLE_PROPERTY)}\s*=\s*([1-9]\d*)\s*$"
)


@dataclass
class FbxNode:
    name: str
    properties: list[Any] = field(default_factory=list)
    property_types: list[str] = field(default_factory=list)
    children: list["FbxNode"] = field(default_factory=list)

    def add(self, name: str, *properties: tuple[str, Any]) -> "FbxNode":
        child = FbxNode(
            name=name,
            properties=[value for _kind, value in properties],
            property_types=[kind for kind, _value in properties],
        )
        self.children.append(child)
        return child

    def typed_properties(self) -> list[tuple[str, Any]]:
        if len(self.property_types) != len(self.properties):
            raise ValueError(f"FBX node {self.name!r} has an invalid property contract")
        return list(zip(self.property_types, self.properties))


def _require_range(data: bytes, offset: int, size: int, label: str) -> None:
    if offset < 0 or size < 0 or offset > len(data) - size:
        raise ValueError(f"Binary FBX {label} exceeds the file bounds")


def _read_property(data: bytes, offset: int) -> tuple[Any, int, str]:
    _require_range(data, offset, 1, "property type")
    kind = chr(data[offset])
    offset += 1
    scalar = {"Y": ("h", 2), "I": ("i", 4), "F": ("f", 4), "D": ("d", 8), "L": ("q", 8)}
    if kind in scalar:
        fmt, size = scalar[kind]
        _require_range(data, offset, size, f"{kind} property")
        return struct.unpack_from("<" + fmt, data, offset)[0], offset + size, kind
    if kind == "C":
        _require_range(data, offset, 1, "C property")
        return bool(data[offset]), offset + 1, kind
    if kind in {"S", "R"}:
        _require_range(data, offset, 4, f"{kind} property length")
        size = struct.unpack_from("<I", data, offset)[0]
        offset += 4
        _require_range(data, offset, size, f"{kind} property")
        raw = data[offset : offset + size]
        offset += size
        if kind == "S":
            return raw.decode("utf-8", errors="replace"), offset, kind
        return bytes(raw), offset, kind
    if kind not in FBX_ARRAY_LAYOUTS:
        raise ValueError(f"Binary FBX has unsupported property type {kind!r}")
    _require_range(data, offset, 12, f"{kind} array header")
    count, encoding, stored_size = struct.unpack_from("<III", data, offset)
    offset += 12
    _require_range(data, offset, stored_size, f"{kind} array")
    raw = data[offset : offset + stored_size]
    offset += stored_size
    if encoding == 1:
        raw = zlib.decompress(raw)
    elif encoding != 0:
        raise ValueError(f"Binary FBX array has unsupported encoding {encoding}")
    item_fmt, item_size = FBX_ARRAY_LAYOUTS[kind]
    expected = int(count) * item_size
    if len(raw) != expected:
        raise ValueError("Binary FBX array byte count does not match its element count")
    if not count:
        return [], offset, kind
    return list(struct.unpack(f"<{int(count)}{item_fmt}", raw)), offset, kind


def _read_node(data: bytes, offset: int, *, version: int) -> tuple[FbxNode | None, int]:
    wide = version >= 7500
    header_format = "<QQQB" if wide else "<IIIB"
    header_size = 25 if wide else 13
    _require_range(data, offset, header_size, "node header")
    end_offset, property_count, property_bytes, name_size = struct.unpack_from(header_format, data, offset)
    if end_offset == 0:
        return None, offset + header_size
    if end_offset > len(data) or end_offset < offset + header_size:
        raise ValueError("Binary FBX node end offset is invalid")
    offset += header_size
    _require_range(data, offset, name_size, "node name")
    name = data[offset : offset + name_size].decode("utf-8", errors="replace")
    offset += name_size
    property_end = offset + int(property_bytes)
    if property_end > end_offset:
        raise ValueError("Binary FBX property data exceeds its node")
    properties: list[Any] = []
    property_types: list[str] = []
    for _index in range(int(property_count)):
        value, offset, kind = _read_property(data, offset)
        properties.append(value)
        property_types.append(kind)
    if offset != property_end:
        raise ValueError("Binary FBX property length does not match its node header")
    children: list[FbxNode] = []
    null_size = header_size
    while offset < end_offset - null_size:
        child, next_offset = _read_node(data, offset, version=version)
        if next_offset <= offset:
            raise ValueError("Binary FBX child parser made no progress")
        offset = next_offset
        if child is not None:
            children.append(child)
    if offset > end_offset:
        raise ValueError("Binary FBX child node exceeds its parent")
    return FbxNode(name, properties, property_types, children), int(end_offset)


def read_fbx(
    path: str | Path | bytes | bytearray | memoryview,
    *,
    include_footer_id: bool = False,
) -> tuple[int, list[FbxNode]] | tuple[int, list[FbxNode], bytes | None]:
    data = (
        bytes(path)
        if isinstance(path, (bytes, bytearray, memoryview))
        else Path(path).resolve().read_bytes()
    )
    if not data.startswith(FBX_MAGIC):
        raise ValueError("仅支持二进制 FBX 文件 / Binary FBX files are required")
    _require_range(data, len(FBX_MAGIC), 4, "version")
    version = struct.unpack_from("<I", data, len(FBX_MAGIC))[0]
    if version < 7000:
        raise ValueError(f"Binary FBX version {version} is not supported")
    header_size = 25 if version >= 7500 else 13
    roots: list[FbxNode] = []
    footer_id: bytes | None = None
    offset = len(FBX_MAGIC) + 4
    while offset < len(data) - header_size:
        node, next_offset = _read_node(data, offset, version=version)
        if next_offset <= offset:
            raise ValueError("Binary FBX root parser made no progress")
        offset = next_offset
        if node is None:
            if (
                offset + 16 <= len(data)
                and data.endswith(FBX_FOOT_MAGIC)
            ):
                footer_id = bytes(data[offset : offset + 16])
            break
        roots.append(node)
    if not roots:
        raise ValueError("Binary FBX contains no root nodes")
    if include_footer_id:
        return version, roots, footer_id
    return version, roots


def _property_bytes(kind: str, value: Any) -> bytes:
    scalar = {"Y": "h", "I": "i", "F": "f", "D": "d", "L": "q"}
    if kind in scalar:
        return struct.pack("<" + scalar[kind], value)
    if kind == "C":
        return bytes((1 if value else 0,))
    if kind == "S":
        return str(value).encode("utf-8")
    if kind == "R":
        return bytes(value)
    if kind in FBX_ARRAY_LAYOUTS:
        if not isinstance(value, list):
            raise ValueError(f"FBX array {kind!r} was not decoded")
        item_fmt, _item_size = FBX_ARRAY_LAYOUTS[kind]
        if kind in {"b", "c"}:
            return bytes(int(item) & 0xFF for item in value)
        if not value:
            return b""
        return struct.pack(f"<{len(value)}{item_fmt}", *value)
    raise ValueError(f"Unsupported FBX property type {kind!r}")


def _encode_property(kind: str, value: Any) -> bytes:
    if kind in FBX_ARRAY_LAYOUTS:
        values = value if isinstance(value, list) else list(value)
        raw = _property_bytes(kind, values)
        encoding = 1 if len(raw) > 128 else 0
        payload = zlib.compress(raw, 1) if encoding else raw
        return kind.encode("ascii") + struct.pack("<III", len(values), encoding, len(payload)) + payload
    payload = _property_bytes(kind, value)
    if kind in {"S", "R"}:
        return kind.encode("ascii") + struct.pack("<I", len(payload)) + payload
    return kind.encode("ascii") + payload


def _encode_node(node: FbxNode, start_offset: int, *, version: int, is_last: bool) -> bytes:
    wide = version >= 7500
    header_size = 25 if wide else 13
    null_record = FBX_NULL_RECORD_WIDE if wide else FBX_NULL_RECORD_NARROW
    name = node.name.encode("utf-8")
    property_blob = b"".join(_encode_property(kind, value) for kind, value in node.typed_properties())
    body_offset = start_offset + header_size + len(name) + len(property_blob)
    child_blobs: list[bytes] = []
    cursor = body_offset
    for child_index, child in enumerate(node.children):
        encoded = _encode_node(child, cursor, version=version, is_last=child_index == len(node.children) - 1)
        child_blobs.append(encoded)
        cursor += len(encoded)
    if node.children or (not node.properties and not is_last) or node.name in FBX_ALWAYS_BLOCK:
        child_blobs.append(null_record)
        cursor += len(null_record)
    if wide:
        header = struct.pack("<QQQB", cursor, len(node.properties), len(property_blob), len(name))
    else:
        header = struct.pack("<IIIB", cursor, len(node.properties), len(property_blob), len(name))
    return b"".join((header, name, property_blob, *child_blobs))


# ---------------------------------------------------------------------------
# Generic conversion and verification

def _walk(nodes: Iterable[FbxNode]) -> Iterable[FbxNode]:
    for node in nodes:
        yield node
        yield from _walk(node.children)


def _first_root(roots: Iterable[FbxNode], name: str) -> FbxNode | None:
    return next((node for node in roots if node.name == name), None)


def _node_type(node: FbxNode) -> str:
    return str(node.properties[2] or "") if len(node.properties) >= 3 else ""


def _property_name(node: FbxNode) -> str:
    return str(node.properties[0] or "") if node.name == "P" and node.properties else ""


def _route_handle_from_property(node: FbxNode) -> int:
    """Read one Max route handle from a custom Properties70 record."""
    property_name = _property_name(node)
    values = node.properties
    if property_name == ROUTE_HANDLE_PROPERTY:
        candidates = values[4:] if len(values) > 4 else values[1:]
        for value in reversed(candidates):
            text = str(value or "").strip().strip('"')
            if text.isdigit() and int(text) > 0:
                return int(text)
        return 0
    if property_name != "UDP3DSMAX" or not values:
        return 0
    matches = {
        int(match)
        for match in _ROUTE_HANDLE_RE.findall(str(values[-1] or ""))
        if match.isdigit() and int(match) > 0
    }
    return next(iter(matches)) if len(matches) == 1 else 0


def _model_route_handle(node: FbxNode) -> int:
    """Return the one route handle authored on a Model, if it is unambiguous.

    The source may store the marker in Max's ``UDP3DSMAX`` string or as a
    direct ``CodexRe6FbxRouteHandle`` property.  Both forms describe the same
    Model identity; the rebuilt file always normalizes the value back to the
    UDP3DSMAX form used by the Probe/Launcher contract.
    """
    properties = _child_node(node, "Properties70")
    if properties is None:
        return 0
    handles: set[int] = set()
    for property_node in properties.children:
        if property_node.name != "P":
            continue
        handle = _route_handle_from_property(property_node)
        if handle > 0:
            handles.add(handle)
    return next(iter(handles)) if len(handles) == 1 else 0


def _canonical_route_handle_property(route_handle: int) -> FbxNode:
    """Create the stable, Probe-readable route marker for a rebuilt Model."""
    return FbxNode(
        "P",
        [
            "UDP3DSMAX",
            "KString",
            "",
            "U",
            f"{ROUTE_HANDLE_PROPERTY} = {int(route_handle)}\r\n",
        ],
        ["S", "S", "S", "S", "S"],
        [],
    )


def _node_digest(node: FbxNode) -> str:
    digest = hashlib.sha256()
    digest.update(node.name.encode("utf-8"))
    for kind, value in node.typed_properties():
        digest.update(kind.encode("ascii"))
        payload = _property_bytes(kind, value)
        digest.update(struct.pack("<Q", len(payload)))
        digest.update(payload)
    for child in node.children:
        digest.update(bytes.fromhex(_node_digest(child)))
    return digest.hexdigest()


def _media_payload_digests(roots: Iterable[FbxNode]) -> list[str]:
    result: list[str] = []
    for node in _walk(roots):
        if node.name != "Video":
            continue
        for child in node.children:
            if child.name == "Content" and child.property_types == ["R"]:
                content = bytes(child.properties[0])
                if content:
                    result.append(hashlib.sha256(content).hexdigest())
    return sorted(result)


def _axis_signature(roots: Iterable[FbxNode]) -> list[int] | None:
    settings = _first_root(roots, "GlobalSettings")
    if settings is None:
        return None
    values: dict[str, int] = {}
    for node in _walk(settings.children):
        name = _property_name(node)
        if name not in {"CoordAxis", "CoordAxisSign", "UpAxis", "UpAxisSign", "FrontAxis", "FrontAxisSign"} or not node.properties:
            continue
        try:
            values[name] = int(node.properties[-1])
        except (TypeError, ValueError, OverflowError):
            pass
    required = ("CoordAxis", "CoordAxisSign", "UpAxis", "UpAxisSign", "FrontAxis", "FrontAxisSign")
    if not all(key in values for key in required):
        return None
    return [
        values["CoordAxis"] * 2 + (0 if values["CoordAxisSign"] >= 0 else 1),
        values["UpAxis"] * 2 + (0 if values["UpAxisSign"] >= 0 else 1),
        values["FrontAxis"] * 2 + (0 if values["FrontAxisSign"] >= 0 else 1),
    ]


def collect_stats(roots: list[FbxNode]) -> dict[str, Any]:
    objects = _first_root(roots, "Objects")
    connections = _first_root(roots, "Connections")
    all_nodes = list(_walk(roots))
    geometry_nodes = [node for node in all_nodes if node.name == "Geometry"]
    model_nodes = [node for node in all_nodes if node.name == "Model"]
    deformer_nodes = [node for node in all_nodes if node.name == "Deformer"]
    skin_clusters = [node for node in deformer_nodes if _node_type(node).casefold() == "cluster"]
    skin_deformers = [node for node in deformer_nodes if _node_type(node).casefold() == "skin"]
    normal_layers = [node for node in all_nodes if node.name == "LayerElementNormal"]
    uv_layers = [node for node in all_nodes if node.name == "LayerElementUV"]
    max_metadata = [
        node
        for node in all_nodes
        if node.name == "P"
        and (
            _property_name(node) == "MaxHandle"
            or (
                _property_name(node) == "UDP3DSMAX"
                and _route_handle_from_property(node) <= 0
            )
        )
    ]
    arrays = [kind for node in all_nodes for kind in node.property_types if kind.islower()]
    geometry_digests = sorted(_node_digest(node) for node in geometry_nodes)
    skin_digests = sorted(_node_digest(node) for node in skin_clusters)
    connection_digests = sorted(_node_digest(node) for node in (connections.children if connections else []))
    return {
        "node_count": len(all_nodes),
        "model_count": len(model_nodes),
        "geometry_count": len(geometry_nodes),
        "normal_layer_count": len(normal_layers),
        "uv_layer_count": len(uv_layers),
        "skin_deformer_count": len(skin_deformers),
        "skin_cluster_count": len(skin_clusters),
        "material_count": sum(1 for node in all_nodes if node.name == "Material"),
        "texture_count": sum(1 for node in all_nodes if node.name == "Texture"),
        "video_count": sum(1 for node in all_nodes if node.name == "Video"),
        "embedded_media_sha256": _media_payload_digests(roots),
        "connection_count": len(connections.children) if connections else 0,
        "animation_stack_count": sum(1 for node in all_nodes if node.name == "AnimationStack"),
        "animation_layer_count": sum(1 for node in all_nodes if node.name == "AnimationLayer"),
        "array_count": len(arrays),
        "max_metadata_count": len(max_metadata),
        "axis_signature": _axis_signature(roots),
        "geometry_digests": geometry_digests,
        "skin_cluster_digests": skin_digests,
        "connection_digests": connection_digests,
        "objects_child_count": len(objects.children) if objects else 0,
    }


def _set_creator(roots: list[FbxNode]) -> None:
    creator = _first_root(roots, "Creator")
    if creator is None:
        roots.insert(min(3, len(roots)), FbxNode("Creator", ["Generic FBX Converter"], ["S"], []))
        return
    for index, kind in enumerate(creator.property_types):
        if kind == "S":
            creator.properties[index] = "Generic FBX Converter"
            return
    creator.properties.append("Generic FBX Converter")
    creator.property_types.append("S")


def _remove_max_metadata(roots: list[FbxNode]) -> int:
    removed = 0
    objects = _first_root(roots, "Objects")
    if objects is None:
        return removed
    for model in objects.children:
        if model.name != "Model":
            continue
        for container in model.children:
            if container.name != "Properties70":
                continue
            kept: list[FbxNode] = []
            for property_node in container.children:
                property_name = _property_name(property_node)
                if property_name == "MaxHandle":
                    removed += 1
                elif property_name == "UDP3DSMAX":
                    route_handle = _route_handle_from_property(property_node)
                    if route_handle > 0 and property_node.properties:
                        retained = FbxNode(
                            "P",
                            list(property_node.properties),
                            list(property_node.property_types),
                            [],
                        )
                        retained.properties[-1] = (
                            f"{ROUTE_HANDLE_PROPERTY} = {route_handle}\r\n"
                        )
                        kept.append(retained)
                    else:
                        removed += 1
                else:
                    kept.append(property_node)
            container.children = kept
    return removed


def _child_node(node: FbxNode, name: str) -> FbxNode | None:
    return next((child for child in node.children if child.name == name), None)


def _child_value(node: FbxNode, name: str) -> Any:
    child = _child_node(node, name)
    return child.properties[0] if child is not None and child.properties else None


def _set_child_array(node: FbxNode, name: str, kind: str, values: list[Any]) -> None:
    child = _child_node(node, name)
    if child is None:
        node.children.append(FbxNode(name, [values], [kind], []))
    else:
        child.properties = [values]
        child.property_types = [kind]


def _set_layer_text(node: FbxNode, name: str, value: str) -> None:
    child = _child_node(node, name)
    if child is None:
        node.children.append(FbxNode(name, [value], ["S"], []))
    else:
        child.properties = [value]
        child.property_types = ["S"]


def _remove_child(node: FbxNode, name: str) -> None:
    node.children = [child for child in node.children if child.name != name]


# ---------------------------------------------------------------------------
# V5 canonical transform helpers
#
# FBX stores the affine transform as a row-major matrix in the binary tree.
# These helpers intentionally stay local to the standalone converter so the
# tool remains independent from the production Probe module.

def _identity_matrix() -> list[float]:
    return [
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    ]


def _dynamic_mod_fbx_mapping_receipt() -> dict[str, Any]:
    """Publish the explicit canonical-XYZ <-> MOD transform contract.

    Axis basis and length scale are intentionally separate.  The importer
    keeps the matrix 3x3 rows in file order and remaps only the translation;
    consequently the Bridge must not smuggle a unit/root scale into this
    dimensionless receipt.
    """
    return {
        "schema": DYNAMIC_MOD_FBX_MAPPING_SCHEMA,
        "status": "mapped",
        "source": "re6_mod_import_inverse",
        "source_domain": CANONICAL_AXIS_DOMAIN,
        "target_domain": "mod_file",
        "vector_convention": "row_vector_row_major",
        "matrix_semantics": "axis_basis_only",
        "position_mapping": "canonical_to_mod_affine_then_scale",
        "bone_mapping": "canonical_to_mod_inverse_conjugation",
        "normal_mapping": "inverse_transpose_of_position_basis",
        "root_basis_policy": "importer_root_basis_once",
        "scale_owner": "transform_scale_receipt",
        "units_baked": False,
        "mod_to_canonical_matrix": list(MOD_TO_CANONICAL_MATRIX),
        "canonical_to_mod_matrix": list(CANONICAL_TO_MOD_MATRIX),
        "applied_once": False,
    }


def _fbx_transform_scale_receipt(
    payload: dict[str, Any],
    fbx_units: dict[str, Any],
    *,
    backend_kind: Any = "",
) -> dict[str, Any]:
    """Publish the measured root/unit scale without altering scene rows.

    Generic reconstruction may bake a producer root scale into Geometry rows.
    A skipped/header-only Mesh can still carry an old producer Model matrix,
    but that matrix is not evidence that the *geometry payload* needs the
    inverse scale.  The root-scale vote is therefore made only over effective
    geometry rows (``fbx_probe_geometry_required`` and not skipped, or a
    populated legacy geometry row).  We retain an all-row diagnostic so this
    distinction remains visible without allowing placeholder rows to select
    the scale.  A dominant isotropic matrix scale is accepted only when it is
    present on at least half of the effective geometry rows; otherwise the
    source-MOD root scale supplied by the bridge is authoritative.
    """
    backend = str(backend_kind or "").strip().lower()
    source_unit = None
    if isinstance(fbx_units, dict):
        try:
            candidate = float(fbx_units.get("unit_meters"))
            if math.isfinite(candidate) and candidate > 0.0:
                source_unit = candidate
        except (TypeError, ValueError, OverflowError):
            source_unit = None
    units_baked = False
    model_scale = payload.get("fbx_model_scale_transform")
    if isinstance(model_scale, dict):
        units_baked = str(model_scale.get("status", "") or "").strip().lower() in {
            "applied",
            "already_applied",
            "not_needed",
        } and backend == "blender_fbx"
    unit_ratio = 1.0
    if source_unit is not None and not units_baked:
        unit_ratio = FBX_TARGET_UNIT_METERS / source_unit

    # ``all_scales`` is diagnostic only.  ``geometry_scales`` is the sole
    # population allowed to elect the receipt's root scale.
    all_scales: list[float] = []
    geometry_scales: list[float] = []
    excluded_scales: list[float] = []
    geometry_row_count = 0
    excluded_row_count = 0
    geometry_matrix_count = 0
    excluded_matrix_count = 0
    exclusion_reasons: dict[str, int] = {}

    def _effective_geometry_row(mesh: dict[str, Any]) -> tuple[bool, str]:
        """Classify one contract row for root-scale voting.

        Full handoffs publish explicit Stage-A route facts.  Older lightweight
        callers do not, so a conservative populated-array/count fallback is
        retained for those rows.  Explicitly skipped rows never vote even when
        UFBX reports a non-zero placeholder vertex count (e.g. BoundSphere).
        """
        has_required = "fbx_probe_geometry_required" in mesh
        has_skipped = "fbx_probe_geometry_skipped" in mesh
        if has_required or has_skipped:
            required = _coerce_bool(mesh.get("fbx_probe_geometry_required"), False)
            skipped = _coerce_bool(mesh.get("fbx_probe_geometry_skipped"), False)
            if skipped:
                return False, str(mesh.get("fbx_probe_skip_reason", "skipped") or "skipped")
            if required:
                return True, "required_geometry"
            return False, str(mesh.get("fbx_probe_skip_reason", "not_required") or "not_required")

        # Legacy/no-route metadata: require actual payload evidence.  Counts
        # are accepted only when positive and no explicit skip marker exists.
        for key in (
            "positions",
            "max_positions",
            "skinned_positions",
            "skinned_max_positions",
            "skinned_world_positions",
        ):
            rows = mesh.get(key)
            if isinstance(rows, (list, tuple)) and len(rows) > 0:
                return True, "populated_position_rows"
        for key in ("vertex_count", "triangle_count", "face_count"):
            try:
                if int(mesh.get(key, 0) or 0) > 0:
                    return True, "positive_geometry_count"
            except (TypeError, ValueError, OverflowError):
                continue
        return False, "no_geometry_payload"

    def _stats(values: list[float]) -> dict[str, Any]:
        finite = [value for value in values if math.isfinite(value)]
        if not finite:
            return {"count": 0}
        ordered = sorted(finite)
        middle = len(ordered) // 2
        median = (
            ordered[middle]
            if len(ordered) % 2
            else (ordered[middle - 1] + ordered[middle]) / 2.0
        )
        return {
            "count": len(ordered),
            "min": ordered[0],
            "max": ordered[-1],
            "median": median,
        }

    meshes = payload.get("contract_meshes")
    if isinstance(meshes, list):
        for mesh in meshes:
            if not isinstance(mesh, dict):
                continue
            is_geometry, reason = _effective_geometry_row(mesh)
            if is_geometry:
                geometry_row_count += 1
            else:
                excluded_row_count += 1
                exclusion_reasons[reason] = exclusion_reasons.get(reason, 0) + 1
            matrix = _flatten_matrix4x4(mesh.get("fbx_node_to_world_matrix"))
            if len(matrix) != 16:
                continue
            rows = [
                math.sqrt(sum(matrix[offset + axis] ** 2 for axis in range(3)))
                for offset in (0, 4, 8)
            ]
            if min(rows) <= 1.0e-9 or max(rows) / min(rows) > 1.001:
                continue
            scale = sum(rows) / 3.0
            if not math.isfinite(scale):
                continue
            if is_geometry:
                geometry_matrix_count += 1
                if scale > 1.001:
                    geometry_scales.append(scale)
            else:
                excluded_matrix_count += 1
                if scale > 1.001:
                    excluded_scales.append(scale)
            if scale > 1.001:
                all_scales.append(scale)
    root_scale = 1.0
    root_source = "bridge_source_mod_root_world"
    # Only effective geometry rows can elect a producer root scale.  If there
    # are no such rows, or the non-unit vote does not reach the same half-row
    # quorum as the legacy policy, leave ownership with the Bridge source MOD.
    if geometry_row_count and len(geometry_scales) * 2 >= geometry_row_count:
        ordered = sorted(geometry_scales)
        middle = len(ordered) // 2
        root_scale = ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2.0
        root_source = "probe_dominant_geometry_mesh_node_scale"
    geometry_factor = 1.0 / (root_scale * unit_ratio)
    bone_translation_factor = 1.0 / unit_ratio
    return {
        "schema": FBX_TRANSFORM_SCALE_SCHEMA,
        "status": "reported",
        "backend": backend,
        "source_unit_meters": source_unit,
        "target_unit_meters": FBX_TARGET_UNIT_METERS,
        "unit_ratio_target_over_source": unit_ratio,
        "root_scale": root_scale,
        "root_scale_source": root_source,
        "root_scale_selection_policy": "effective_geometry_rows_only",
        "root_scale_geometry_row_count": geometry_row_count,
        "root_scale_geometry_matrix_count": geometry_matrix_count,
        "root_scale_geometry_scale_count": len(geometry_scales),
        "root_scale_excluded_row_count": excluded_row_count,
        "root_scale_excluded_matrix_count": excluded_matrix_count,
        "root_scale_excluded_scale_count": len(excluded_scales),
        "root_scale_all_row_scale_count": len(all_scales),
        "root_scale_all_row_scale_stats": _stats(all_scales),
        "root_scale_geometry_scale_stats": _stats(geometry_scales),
        "root_scale_excluded_scale_stats": _stats(excluded_scales),
        "root_scale_exclusion_reasons": dict(sorted(exclusion_reasons.items())),
        "geometry_factor": geometry_factor,
        "bone_translation_factor": bone_translation_factor,
        "units_baked": units_baked,
        "formula": "geometry = canonical / (root_scale * target_unit/source_unit); bone_translation = canonical / (target_unit/source_unit)",
        "applied_once": False,
    }


def _finite_matrix(value: Any, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 16:
        raise ValueError(f"{label} must contain 16 values")
    result = [float(item) for item in value]
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{label} contains non-finite values")
    return result


def _generic_multiply_row_major_matrices(left: list[float], right: list[float]) -> list[float]:
    left = _finite_matrix(left, "left matrix")
    right = _finite_matrix(right, "right matrix")
    return [
        sum(left[row * 4 + item] * right[item * 4 + column] for item in range(4))
        for row in range(4)
        for column in range(4)
    ]


def _generic_invert_row_major_matrix(matrix: list[float]) -> list[float] | None:
    try:
        values = _finite_matrix(matrix, "matrix")
    except (TypeError, ValueError, OverflowError):
        return None
    work = [
        [values[row * 4 + column] for column in range(4)]
        + [1.0 if row == column else 0.0 for column in range(4)]
        for row in range(4)
    ]
    for pivot_column in range(4):
        pivot_row = max(
            range(pivot_column, 4),
            key=lambda row: abs(work[row][pivot_column]),
        )
        if abs(work[pivot_row][pivot_column]) <= 1.0e-12:
            return None
        work[pivot_column], work[pivot_row] = work[pivot_row], work[pivot_column]
        pivot = work[pivot_column][pivot_column]
        work[pivot_column] = [value / pivot for value in work[pivot_column]]
        for row in range(4):
            if row == pivot_column:
                continue
            factor = work[row][pivot_column]
            if abs(factor) <= 1.0e-18:
                continue
            work[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(work[row], work[pivot_column])
            ]
    return [work[row][column] for row in range(4) for column in range(4, 8)]


def _generic_transform_position_values_prevalidated(
    values: Any,
    matrix: list[float],
) -> list[float]:
    """Transform one position with a matrix already validated for this Geometry."""
    if len(values) < 3:
        raise ValueError("Position row must contain three values")
    x, y, z = (float(values[0]), float(values[1]), float(values[2]))
    return [
        (x * matrix[0]) + (y * matrix[4]) + (z * matrix[8]) + matrix[12],
        (x * matrix[1]) + (y * matrix[5]) + (z * matrix[9]) + matrix[13],
        (x * matrix[2]) + (y * matrix[6]) + (z * matrix[10]) + matrix[14],
    ]


def _generic_prepare_normal_transform(matrix: list[float]) -> tuple[bool, tuple[float, ...]]:
    """Cache fixed 3x3 inverse coefficients from a validated normal matrix."""
    a00, a01, a02 = matrix[0], matrix[1], matrix[2]
    a10, a11, a12 = matrix[4], matrix[5], matrix[6]
    a20, a21, a22 = matrix[8], matrix[9], matrix[10]
    c00 = (a11 * a22) - (a12 * a21)
    c01 = (a12 * a20) - (a10 * a22)
    c02 = (a10 * a21) - (a11 * a20)
    determinant = (a00 * c00) + (a01 * c01) + (a02 * c02)
    if abs(determinant) <= 1.0e-12:
        return False, (
            a00, a01, a02,
            a10, a11, a12,
            a20, a21, a22,
        )
    inverse_det = 1.0 / determinant
    return True, (
        c00 * inverse_det,
        ((a02 * a21) - (a01 * a22)) * inverse_det,
        ((a01 * a12) - (a02 * a11)) * inverse_det,
        c01 * inverse_det,
        ((a00 * a22) - (a02 * a20)) * inverse_det,
        ((a02 * a10) - (a00 * a12)) * inverse_det,
        c02 * inverse_det,
        ((a01 * a20) - (a00 * a21)) * inverse_det,
        ((a00 * a11) - (a01 * a10)) * inverse_det,
    )


def _generic_transform_normal_values_prevalidated(
    values: Any,
    prepared: tuple[bool, tuple[float, ...]],
) -> list[float]:
    """Transform one normal using coefficients prepared for this Geometry."""
    if len(values) < 3:
        raise ValueError("Normal row must contain three values")
    inverse, coefficients = prepared
    a00, a01, a02, a10, a11, a12, a20, a21, a22 = coefficients
    x, y, z = (float(values[0]), float(values[1]), float(values[2]))
    if not inverse:
        transformed = [
            (x * a00) + (y * a10) + (z * a20),
            (x * a01) + (y * a11) + (z * a21),
            (x * a02) + (y * a12) + (z * a22),
        ]
    else:
        transformed = [
            (a00 * x) + (a01 * y) + (a02 * z),
            (a10 * x) + (a11 * y) + (a12 * z),
            (a20 * x) + (a21 * y) + (a22 * z),
        ]
    length = math.sqrt(sum(value * value for value in transformed))
    if length <= 1.0e-12:
        return [0.0, 0.0, 1.0]
    return [value / length for value in transformed]


def _generic_transform_position_row_major(row: Any, matrix: list[float]) -> list[float]:
    values = list(row)
    if len(values) < 3:
        raise ValueError("Position row must contain three values")
    return _generic_transform_position_values_prevalidated(
        values,
        _finite_matrix(matrix, "position matrix"),
    )


def _generic_transform_normal_row_major(row: Any, matrix: list[float]) -> list[float]:
    values = list(row)
    if len(values) < 3:
        raise ValueError("Normal row must contain three values")
    return _generic_transform_normal_values_prevalidated(
        values,
        _generic_prepare_normal_transform(_finite_matrix(matrix, "normal matrix")),
    )


def _matrix_basis_mean_scale(matrix: Any, *, label: str) -> float:
    return sum(_matrix_basis_scales(matrix, label=label)) / 3.0


def _matrix_basis_scales(matrix: Any, *, label: str) -> list[float]:
    """Return the three row-basis lengths of an affine row-major matrix."""
    values = _finite_matrix(matrix, label)
    lengths = [
        math.sqrt(sum(values[offset + axis] ** 2 for axis in range(3)))
        for offset in (0, 4, 8)
    ]
    if any(not math.isfinite(length) or length <= 1.0e-9 for length in lengths):
        raise ValueError(f"{label} has a degenerate basis")
    return lengths


def _scale_affine_matrix(matrix: Any, scale: float, *, label: str) -> list[float]:
    values = _finite_matrix(matrix, label)
    factor = float(scale)
    if not math.isfinite(factor) or factor <= 0.0:
        raise ValueError(f"{label} has an invalid scale")
    for index in (0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 13, 14):
        values[index] *= factor
    return values


def _matrices_match(left: list[float], right: list[float]) -> bool:
    if len(left) != 16 or len(right) != 16:
        return False
    magnitude = max(1.0, *(abs(float(value)) for value in left), *(abs(float(value)) for value in right))
    return max(abs(float(left[index]) - float(right[index])) for index in range(16)) <= magnitude * 1.0e-4


_PRODUCER_TRANSFORM_PROPERTIES = {
    "Lcl Translation",
    "Lcl Rotation",
    "Lcl Scaling",
    "PreRotation",
    "PostRotation",
    "RotationActive",
    "RotationOffset",
    "RotationPivot",
    "ScalingOffset",
    "ScalingPivot",
    "GeometricTranslation",
    "GeometricRotation",
    "GeometricScaling",
    "ScalingMax",
    "MaxHandle",
    "RotationOrder",
    "InheritType",
}


def _model_property_vector(
    source: FbxNode, name: str, width: int, default: list[float]
) -> list[float]:
    properties = _child_node(source, "Properties70")
    if properties is None:
        return list(default)
    for property_node in properties.children:
        if property_node.name != "P" or _property_name(property_node) != name:
            continue
        if len(property_node.properties) < width + 4:
            continue
        try:
            values = [float(value) for value in property_node.properties[-width:]]
        except (TypeError, ValueError, OverflowError):
            continue
        if all(math.isfinite(value) for value in values):
            return values
    return list(default)


def _model_property_scalar(source: FbxNode, name: str, default: int | float) -> int | float:
    properties = _child_node(source, "Properties70")
    if properties is None:
        return default
    for property_node in properties.children:
        if property_node.name != "P" or _property_name(property_node) != name:
            continue
        for value in reversed(property_node.properties[4:]):
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                return value
    return default


def _translation_matrix(vector: list[float]) -> list[float]:
    result = _identity_matrix()
    result[12:15] = [float(value) for value in vector[:3]]
    return result


def _scaling_matrix(vector: list[float]) -> list[float]:
    result = _identity_matrix()
    result[0], result[5], result[10] = [float(value) for value in vector[:3]]
    return result


def _axis_rotation_matrix(axis: str, radians: float) -> list[float]:
    cosine = math.cos(float(radians))
    sine = math.sin(float(radians))
    result = _identity_matrix()
    if axis == "X":
        result[5], result[6], result[9], result[10] = cosine, sine, -sine, cosine
    elif axis == "Y":
        result[0], result[2], result[8], result[10] = cosine, -sine, sine, cosine
    elif axis == "Z":
        result[0], result[1], result[4], result[5] = cosine, sine, -sine, cosine
    else:
        raise ValueError(f"Unsupported rotation axis: {axis}")
    return result


def _rotation_matrix(rotation: list[float], rotation_order: int = 0) -> list[float]:
    orders = ("XYZ", "XZY", "YZX", "YXZ", "ZXY", "ZYX")
    order = orders[rotation_order] if 0 <= int(rotation_order) < len(orders) else "XYZ"
    angles = {
        "X": math.radians(float(rotation[0])),
        "Y": math.radians(float(rotation[1])),
        "Z": math.radians(float(rotation[2])),
    }
    result = _identity_matrix()
    for axis in order:
        result = _generic_multiply_row_major_matrices(
            result,
            _axis_rotation_matrix(axis, angles[axis]),
        )
    return result


def _local_trs_matrix(
    translation: list[float], rotation: list[float], scale: list[float]
) -> list[float]:
    return _generic_multiply_row_major_matrices(
        _generic_multiply_row_major_matrices(_scaling_matrix(scale), _rotation_matrix(rotation)),
        _translation_matrix(translation),
    )


def _source_model_local_parts(source: FbxNode) -> dict[str, Any]:
    """Evaluate the full ordinary FBX Model transform in row-vector form."""
    translation = _model_property_vector(source, "Lcl Translation", 3, [0.0, 0.0, 0.0])
    rotation = _model_property_vector(source, "Lcl Rotation", 3, [0.0, 0.0, 0.0])
    scale = _model_property_vector(source, "Lcl Scaling", 3, [1.0, 1.0, 1.0])
    pre_rotation = _model_property_vector(source, "PreRotation", 3, [0.0, 0.0, 0.0])
    post_rotation = _model_property_vector(source, "PostRotation", 3, [0.0, 0.0, 0.0])
    rotation_offset = _model_property_vector(source, "RotationOffset", 3, [0.0, 0.0, 0.0])
    rotation_pivot = _model_property_vector(source, "RotationPivot", 3, [0.0, 0.0, 0.0])
    scaling_offset = _model_property_vector(source, "ScalingOffset", 3, [0.0, 0.0, 0.0])
    scaling_pivot = _model_property_vector(source, "ScalingPivot", 3, [0.0, 0.0, 0.0])
    rotation_order = int(_model_property_scalar(source, "RotationOrder", 0) or 0)
    inherit_type = int(_model_property_scalar(source, "InheritType", 1) or 0)
    local_rotation = _rotation_matrix(rotation, rotation_order)
    pre = _rotation_matrix(pre_rotation, 0)
    post_inverse = _generic_invert_row_major_matrix(_rotation_matrix(post_rotation, 0))
    if post_inverse is None:
        raise ValueError("PostRotation is not invertible")
    total_rotation = _generic_multiply_row_major_matrices(
        _generic_multiply_row_major_matrices(post_inverse, local_rotation),
        pre,
    )
    # FBX row-vector order is the reverse of the SDK's published column-vector
    # formula. RotationActive is authoring metadata; it does not erase stored
    # pre/post or pivot values during evaluation.
    matrix = _identity_matrix()
    for component in (
        _translation_matrix([-value for value in scaling_pivot]),
        _scaling_matrix(scale),
        _translation_matrix(scaling_pivot),
        _translation_matrix(scaling_offset),
        _translation_matrix([-value for value in rotation_pivot]),
        total_rotation,
        _translation_matrix(rotation_pivot),
        _translation_matrix(rotation_offset),
        _translation_matrix(translation),
    ):
        matrix = _generic_multiply_row_major_matrices(matrix, component)
    evaluated_translation = list(matrix[12:15])
    unscaled = _generic_multiply_row_major_matrices(
        total_rotation,
        _translation_matrix(evaluated_translation),
    )
    return {
        "matrix": matrix,
        "unscaled_matrix": unscaled,
        "translation": evaluated_translation,
        "rotation_matrix": total_rotation,
        "scale": [float(value) for value in scale],
        "inherit_type": inherit_type if inherit_type in {0, 1, 2} else 1,
    }


def _source_model_local_matrix(source: FbxNode) -> list[float]:
    return list(_source_model_local_parts(source)["matrix"])


def _source_geometric_matrix(source: FbxNode) -> list[float]:
    """Return Geometry-local Geometric TRS in the source row-vector domain."""
    translation = _model_property_vector(
        source, "GeometricTranslation", 3, [0.0, 0.0, 0.0]
    )
    rotation = _model_property_vector(
        source, "GeometricRotation", 3, [0.0, 0.0, 0.0]
    )
    scale = _model_property_vector(
        source, "GeometricScaling", 3, [1.0, 1.0, 1.0]
    )
    return _local_trs_matrix(translation, rotation, scale)


def _orthonormalize_rotation_rows(rows: list[list[float]]) -> list[list[float]]:
    first = list(rows[0][:3])
    first_length = math.sqrt(sum(value * value for value in first))
    if first_length <= 1.0e-10:
        raise ValueError("Model transform has a degenerate X axis")
    x_axis = [value / first_length for value in first]
    second = list(rows[1][:3])
    projection = sum(value * axis for value, axis in zip(second, x_axis))
    second = [value - projection * axis for value, axis in zip(second, x_axis)]
    second_length = math.sqrt(sum(value * value for value in second))
    if second_length <= 1.0e-10:
        raise ValueError("Model transform has a degenerate Y axis")
    y_axis = [value / second_length for value in second]
    z_axis = [
        x_axis[1] * y_axis[2] - x_axis[2] * y_axis[1],
        x_axis[2] * y_axis[0] - x_axis[0] * y_axis[2],
        x_axis[0] * y_axis[1] - x_axis[1] * y_axis[0],
    ]
    z_length = math.sqrt(sum(value * value for value in z_axis))
    if z_length <= 1.0e-10:
        raise ValueError("Model transform has a degenerate Z axis")
    return [x_axis, y_axis, [value / z_length for value in z_axis]]


def _matrix_to_trs(matrix: list[float], *, label: str) -> tuple[list[float], list[float], list[float]]:
    values = _finite_matrix(matrix, label)
    scales = [
        math.sqrt(sum(values[offset + axis] ** 2 for axis in range(3)))
        for offset in (0, 4, 8)
    ]
    if any(scale <= 1.0e-10 or not math.isfinite(scale) for scale in scales):
        raise ValueError(f"{label} has a degenerate basis")
    rows = [
        [values[row * 4 + axis] / scales[row] for axis in range(3)]
        for row in range(3)
    ]
    determinant = (
        rows[0][0] * (rows[1][1] * rows[2][2] - rows[1][2] * rows[2][1])
        - rows[0][1] * (rows[1][0] * rows[2][2] - rows[1][2] * rows[2][0])
        + rows[0][2] * (rows[1][0] * rows[2][1] - rows[1][1] * rows[2][0])
    )
    if determinant < 0.0:
        scales[0] = -scales[0]
        rows[0] = [-value for value in rows[0]]
    rotation = _orthonormalize_rotation_rows(rows)
    r00, r01, r02 = rotation[0][0], rotation[1][0], rotation[2][0]
    r10, r11, r12 = rotation[0][1], rotation[1][1], rotation[2][1]
    r20, r21, r22 = rotation[0][2], rotation[1][2], rotation[2][2]
    sy = max(-1.0, min(1.0, -r20))
    y = math.asin(sy)
    if abs(sy) < 0.999999:
        x = math.atan2(r21, r22)
        z = math.atan2(r10, r00)
    else:
        x = math.atan2(-r12, r11)
        z = 0.0
    return (
        [values[12], values[13], values[14]],
        [math.degrees(x), math.degrees(y), math.degrees(z)],
        scales,
    )


def _source_model_world_matrices(
    model_nodes: list[FbxNode], parent_ids: dict[int, int]
) -> dict[int, list[float]]:
    by_id = {_object_id(node): node for node in model_nodes}
    local = {model_id: _source_model_local_parts(node) for model_id, node in by_id.items()}
    worlds: dict[int, list[float]] = {}
    unscaled_worlds: dict[int, list[float]] = {}
    inherit_scales: dict[int, list[float]] = {}
    inherit_scale_nodes: dict[int, int] = {}
    visiting: set[int] = set()

    def resolve(model_id: int) -> list[float]:
        if model_id in worlds:
            return worlds[model_id]
        if model_id in visiting:
            raise ValueError(f"Model hierarchy contains a cycle at {model_id}")
        visiting.add(model_id)
        parent_id = int(parent_ids.get(model_id, 0) or 0)
        if parent_id in by_id:
            parent_world = resolve(parent_id)
            parent_parts = local[parent_id]
            parts = local[model_id]
            inherit_type = int(parts["inherit_type"])
            if inherit_type == 1:
                world = _generic_multiply_row_major_matrices(parts["matrix"], parent_world)
                unscaled_world = _generic_multiply_row_major_matrices(
                    parts["unscaled_matrix"], parent_world
                )
                inherit_scale = list(parts["scale"])
                inherit_scale_node = 0
            else:
                inherit_scale_node = (
                    parent_id if inherit_type == 0 else inherit_scale_nodes.get(parent_id, 0)
                )
                inherited_scale = inherit_scales.get(inherit_scale_node, [1.0, 1.0, 1.0])
                adjusted_scale = [
                    float(parts["scale"][axis]) * float(inherited_scale[axis])
                    for axis in range(3)
                ]
                adjusted_translation = [
                    float(parts["translation"][axis]) * float(inherit_scales[parent_id][axis])
                    for axis in range(3)
                ]
                adjusted = _generic_multiply_row_major_matrices(
                    _generic_multiply_row_major_matrices(
                        _scaling_matrix(adjusted_scale), parts["rotation_matrix"]
                    ),
                    _translation_matrix(adjusted_translation),
                )
                adjusted_unscaled = _generic_multiply_row_major_matrices(
                    parts["rotation_matrix"],
                    _translation_matrix(adjusted_translation),
                )
                world = _generic_multiply_row_major_matrices(adjusted, unscaled_worlds[parent_id])
                unscaled_world = _generic_multiply_row_major_matrices(
                    adjusted_unscaled, unscaled_worlds[parent_id]
                )
                inherit_scale = adjusted_scale
        else:
            parts = local[model_id]
            world = list(parts["matrix"])
            unscaled_world = list(parts["unscaled_matrix"])
            inherit_scale = list(parts["scale"])
            inherit_scale_node = 0
        visiting.remove(model_id)
        worlds[model_id] = world
        unscaled_worlds[model_id] = unscaled_world
        inherit_scales[model_id] = inherit_scale
        inherit_scale_nodes[model_id] = inherit_scale_node
        return world

    for model_id in sorted(by_id):
        resolve(model_id)
    return worlds


def _global_unit_scale(roots: list[FbxNode]) -> float:
    settings = _first_root(roots, "GlobalSettings")
    if settings is None:
        return 1.0
    # UnitScaleFactor is the active scene unit. OriginalUnitScaleFactor is
    # historical authoring metadata and may differ; traversal order is not a
    # semantic priority because exporters are free to write Original first.
    values: dict[str, float] = {}
    for node in _walk(settings.children):
        property_name = _property_name(node)
        if node.name != "P" or property_name not in {"UnitScaleFactor", "OriginalUnitScaleFactor"}:
            continue
        try:
            value = float(node.properties[-1])
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and value > 0.0:
            values[property_name] = value
    if "UnitScaleFactor" in values:
        return values["UnitScaleFactor"]
    if "OriginalUnitScaleFactor" in values:
        return values["OriginalUnitScaleFactor"]
    return 1.0


def _canonical_unit_conversion(roots: list[FbxNode]) -> tuple[float, float, float]:
    """Return source cm/unit, target cm/unit, and source-to-target numeric factor."""
    source_unit_scale_cm = float(_global_unit_scale(roots))
    target_unit_scale_cm = float(FBX_TARGET_UNIT_SCALE_CM)
    if (
        not math.isfinite(source_unit_scale_cm)
        or source_unit_scale_cm <= 0.0
        or not math.isfinite(target_unit_scale_cm)
        or target_unit_scale_cm <= 0.0
    ):
        raise ValueError("Generic FBX unit conversion has an invalid unit declaration")
    unit_factor = source_unit_scale_cm / target_unit_scale_cm
    if not math.isfinite(unit_factor) or unit_factor <= 0.0:
        raise ValueError("Generic FBX unit conversion produced an invalid factor")
    return source_unit_scale_cm, target_unit_scale_cm, unit_factor


def _generic_axis_conversion_matrix(roots: list[FbxNode]) -> list[float]:
    """Map source storage coordinates to the converter's X/Y/Z target axes."""
    signature = _axis_signature(roots)
    if not isinstance(signature, list) or len(signature) != 3:
        return _identity_matrix()
    decoded = [
        (int(value) // 2, 1.0 if int(value) % 2 == 0 else -1.0)
        for value in signature
    ]
    if sorted(axis for axis, _sign in decoded) != [0, 1, 2]:
        return _identity_matrix()
    conversion = [0.0] * 16
    for logical_axis, (storage_axis, sign) in enumerate(decoded):
        conversion[(storage_axis * 4) + logical_axis] = sign
    conversion[15] = 1.0
    return conversion


def _generic_axis_signature_is_valid(roots: list[FbxNode]) -> bool:
    signature = _axis_signature(roots)
    if not isinstance(signature, list) or len(signature) != 3:
        return False
    decoded_axes = [int(value) // 2 for value in signature]
    return sorted(decoded_axes) == [0, 1, 2]


def _output_local_from_world(
    model_id: int,
    worlds: dict[int, list[float]],
    parent_ids: dict[int, int],
) -> list[float]:
    world = _finite_matrix(worlds[model_id], f"output Model world {model_id}")
    parent_id = int(parent_ids.get(model_id, 0) or 0)
    if parent_id == 0:
        return world
    parent_world = worlds.get(parent_id)
    if parent_world is None:
        raise ValueError(f"Model {model_id} references missing parent {parent_id}")
    inverse = _generic_invert_row_major_matrix(parent_world)
    if inverse is None:
        raise ValueError(f"Model parent {parent_id} is not invertible")
    return _generic_multiply_row_major_matrices(world, inverse)


def _v5_scene_context(
    roots: list[FbxNode], source_graph: dict[str, Any], parents_by_child: dict[int, list[int]]
) -> dict[str, Any]:
    """Derive V5 bind/domain data from the source FBX graph alone."""
    model_nodes = list(source_graph.get("models", []))
    objects_by_id = dict(source_graph.get("objects_by_id", {}))
    source_cluster_ids = {
        _object_id(node)
        for node in source_graph.get("deformers", [])
        if _node_type(node).casefold() == "cluster" and _object_id(node) > 0
    }
    source_associate_cluster_ids = {
        _object_id(node)
        for node in source_graph.get("deformers", [])
        if _node_type(node).casefold() == "cluster"
        and _object_id(node) > 0
        and isinstance(_child_value(node, "TransformAssociateModel"), list)
    }
    parent_ids = dict(source_graph.get("model_parent_ids", {}))
    worlds = _source_model_world_matrices(model_nodes, parent_ids)
    axis_conversion = _generic_axis_conversion_matrix(roots)
    source_unit_scale_cm, target_unit_scale_cm, unit_factor = (
        _canonical_unit_conversion(roots)
    )
    axis_worlds = {
        model_id: _generic_multiply_row_major_matrices(world, axis_conversion)
        for model_id, world in worlds.items()
    }
    target_worlds = {
        model_id: _scale_affine_matrix(
            axis_world,
            unit_factor,
            label=f"Model {model_id} canonical unit world",
        )
        for model_id, axis_world in axis_worlds.items()
    }
    mesh_ids = set(int(value) for value in source_graph.get("mesh_model_ids", []))
    geometry_by_id = {
        _object_id(node): node
        for node in source_graph.get("geometries", [])
        if _object_id(node) > 0
    }
    model_geometry_ids = dict(source_graph.get("model_geometry_ids", {}))
    unit_scale = source_unit_scale_cm
    children_by_parent: dict[int, list[int]] = {}
    for child_id, parent_ids_for_child in parents_by_child.items():
        for parent_id in parent_ids_for_child:
            children_by_parent.setdefault(int(parent_id), []).append(int(child_id))
    mesh_clusters: dict[int, list[dict[str, Any]]] = {model_id: [] for model_id in mesh_ids}
    cluster_by_id: dict[int, dict[str, Any]] = {}
    for cluster in source_graph.get("deformers", []):
        if _node_type(cluster).casefold() != "cluster":
            continue
        cluster_id = _object_id(cluster)
        if cluster_id <= 0:
            continue
        bone_ids = [
            parent_id
            for parent_id in parents_by_child.get(cluster_id, [])
            if objects_by_id.get(parent_id) is not None
            and objects_by_id[parent_id].name == "Model"
            and _node_type(objects_by_id[parent_id]).casefold() == "limbnode"
        ]
        if not bone_ids:
            # A few exporters reverse the Cluster/Bone OO edge. Accept that
            # equivalent representation while keeping the one-bone contract.
            bone_ids = [
                child_id
                for child_id in children_by_parent.get(cluster_id, [])
                if objects_by_id.get(child_id) is not None
                and objects_by_id[child_id].name == "Model"
                and _node_type(objects_by_id[child_id]).casefold() == "limbnode"
            ]
        bone_ids = sorted(set(int(value) for value in bone_ids))
        cluster_neighbors = set(parents_by_child.get(cluster_id, [])) | set(
            children_by_parent.get(cluster_id, [])
        )
        skin_ids = [
            object_id
            for object_id in cluster_neighbors
            if objects_by_id.get(object_id) is not None
            and objects_by_id[object_id].name == "Deformer"
            and _node_type(objects_by_id[object_id]).casefold() == "skin"
        ]
        geometry_ids: set[int] = set()
        for skin_id in skin_ids:
            skin_neighbors = set(parents_by_child.get(skin_id, [])) | set(
                children_by_parent.get(skin_id, [])
            )
            geometry_ids.update(int(value) for value in skin_neighbors if value in geometry_by_id)
        if not geometry_ids:
            geometry_ids.update(
                int(parent_id)
                for parent_id in parents_by_child.get(cluster_id, [])
                if parent_id in geometry_by_id
            )
        mesh_model_ids: set[int] = set()
        for geometry_id in geometry_ids:
            geometry_neighbors = set(parents_by_child.get(geometry_id, [])) | set(
                children_by_parent.get(geometry_id, [])
            )
            mesh_model_ids.update(int(value) for value in geometry_neighbors if value in mesh_ids)
        if len(bone_ids) != 1 or not mesh_model_ids:
            continue
        raw_indexes = _child_value(cluster, "Indexes")
        raw_weights = _child_value(cluster, "Weights")
        raw_transform = _child_value(cluster, "Transform")
        raw_link = _child_value(cluster, "TransformLink")
        # A connected, weightless Cluster is still part of the Skin binding
        # graph. Normalize it with an empty influence list instead of letting
        # the writer fall back to the source-space matrices.
        if raw_indexes is None:
            raw_indexes = []
        if raw_weights is None:
            raw_weights = []
        if (
            not isinstance(raw_indexes, list)
            or not isinstance(raw_weights, list)
            or len(raw_indexes) != len(raw_weights)
        ):
            raise ValueError(f"Cluster {cluster_id} has mismatched Indexes/Weights")
        positive_weight_count = sum(
            isinstance(weight, (int, float))
            and math.isfinite(float(weight))
            and float(weight) > 0.0
            for weight in raw_weights
        )
        link = _finite_matrix(raw_link, f"Cluster {cluster_id} TransformLink")
        transform = (
            _finite_matrix(raw_transform, f"Cluster {cluster_id} Transform")
            if isinstance(raw_transform, list)
            else None
        )
        row = {
            "cluster_id": cluster_id,
            "bone_model_id": int(bone_ids[0]),
            "indexes": [int(value) for value in raw_indexes],
            "weights": [float(value) for value in raw_weights],
            "positive_weight_count": positive_weight_count,
            "transform": transform,
            "transform_link": link,
            "transform_associate": (
                _finite_matrix(
                    _child_value(cluster, "TransformAssociateModel"),
                    f"Cluster {cluster_id} TransformAssociateModel",
                )
                if isinstance(_child_value(cluster, "TransformAssociateModel"), list)
                else None
            ),
        }
        cluster_by_id[cluster_id] = row
        for mesh_model_id in sorted(mesh_model_ids):
            mesh_clusters.setdefault(mesh_model_id, []).append(row)

    # Every weighted Mesh is rebuilt through the canonical LOCAL_BIND path.
    # Do not infer the vertex domain from a unit-looking
    # Transform*TransformLink product; it does not prove that control points
    # are already in the canonical geometry domain.
    geometry_domain = "LOCAL_BIND"
    pose_matrices_by_model: dict[int, list[list[float]]] = {}
    for pose in source_graph.get("poses", []):
        for pose_node in (child for child in pose.children if child.name == "PoseNode"):
            node = _child_node(pose_node, "Node")
            matrix = _child_node(pose_node, "Matrix")
            if node is None or matrix is None or not node.properties or not matrix.properties:
                continue
            try:
                model_id = int(node.properties[0])
                values = _finite_matrix(
                    matrix.properties[0], label=f"Pose Model {model_id} matrix"
                )
            except (TypeError, ValueError, OverflowError):
                continue
            pose_matrices_by_model.setdefault(model_id, []).append(values)

    # A Model world is the current pose.  It must stay separate from the
    # binding pose stored by a Cluster's TransformLink; using the current Model
    # world as the link turns every newly exported pose into a new rest pose.
    canonical_bone_worlds: dict[int, list[float]] = {
        bone_id: list(target_worlds[bone_id])
        for bone_id in sorted(int(value) for value in source_graph.get("bone_model_ids", []))
        if bone_id in target_worlds
    }

    domain_scales: dict[int, float] = {}
    bind_mesh_matrices: dict[int, list[float]] = {}
    geometry_bind_bake_by_mesh: dict[int, bool] = {}
    canonical_cluster_matrices: dict[int, tuple[list[float], list[float]]] = {}
    canonical_associate_matrices: dict[int, list[float]] = {}
    canonical_bind_by_model: dict[int, list[float]] = {}
    cluster_domain_scales: dict[int, list[float]] = {}
    for mesh_model_id in sorted(mesh_ids):
        clusters = mesh_clusters.get(mesh_model_id, [])
        if not clusters:
            # Unskinned Meshes keep their source Model transform.  Baking that
            # transform into Geometry as well would apply the world transform twice.
            domain_scales[mesh_model_id] = 1.0
            continue
        active_clusters = [
            cluster
            for cluster in clusters
            if int(cluster.get("positive_weight_count", 0) or 0) > 0
        ]
        if not active_clusters:
            # A connected Cluster with no positive influence is only an
            # exporter placeholder.  It must not turn the Mesh into a
            # skinned route or cause its Model world to be zeroed/baked.
            domain_scales[mesh_model_id] = 1.0
            continue
        ratios: list[float] = []
        for cluster in active_clusters:
            bone_world = worlds.get(int(cluster["bone_model_id"]))
            if bone_world is None:
                continue
            link_scales = _matrix_basis_scales(
                cluster["transform_link"],
                label=f"Cluster {cluster['cluster_id']} TransformLink",
            )
            bone_scales = _matrix_basis_scales(
                bone_world, label=f"Bone {cluster['bone_model_id']} world"
            )
            # A scene-unit conversion is isotropic. If the current bone and
            # bind-link scales disagree by axis, that difference is authored
            # pose scale (or shear), not a unit factor; do not bake it into
            # the Mesh domain estimate.
            axis_ratios = [
                bone_scales[axis] / link_scales[axis]
                for axis in range(3)
                if link_scales[axis] > 1.0e-9
            ]
            if len(axis_ratios) != 3:
                continue
            ratio_min = min(axis_ratios)
            ratio_max = max(axis_ratios)
            if ratio_max - ratio_min > max(1.0e-3, abs(ratio_max) * 1.0e-3):
                continue
            ratio = sum(axis_ratios) / 3.0
            if math.isfinite(ratio) and ratio > 0.0:
                ratios.append(ratio)
        if ratios:
            ratios.sort()
            middle = len(ratios) // 2
            median = ratios[middle] if len(ratios) % 2 else (ratios[middle - 1] + ratios[middle]) / 2.0
            nearest = min((1.0, unit_scale), key=lambda candidate: abs(median - candidate))
            domain = nearest if abs(median - nearest) <= max(1.0e-3, abs(nearest) * 1.0e-3) else median
        else:
            domain = 1.0
        domain_scales[mesh_model_id] = float(domain)
        for cluster in clusters:
            cluster_domain_scales.setdefault(int(cluster["cluster_id"]), []).append(float(domain))
        candidates: list[list[float]] = []
        for cluster in active_clusters:
            transform = cluster.get("transform")
            transform_link = cluster.get("transform_link")
            if transform is None or transform_link is None:
                continue
            # FBX Cluster.Transform is mesh-side while TransformLink is
            # bone-side. Their product is the common bind Mesh world matrix;
            # Transform alone legitimately differs for every bone.
            candidates.append(
                _generic_multiply_row_major_matrices(transform, transform_link)
            )
        if not candidates and mesh_model_id in worlds:
            candidates.append(list(worlds[mesh_model_id]))
        if candidates:
            first = candidates[0]
            for candidate in candidates[1:]:
                if not _matrices_match(first, candidate):
                    raise ValueError(
                        f"Mesh {mesh_model_id} bind matrix disagreement"
                    )
            bind_axis = _generic_multiply_row_major_matrices(
                _scale_affine_matrix(
                    first, domain, label=f"Mesh {mesh_model_id} bind matrix"
                ),
                axis_conversion,
            )
            bind_mesh_matrices[mesh_model_id] = _scale_affine_matrix(
                bind_axis,
                unit_factor,
                label=f"Mesh {mesh_model_id} canonical unit bind matrix",
            )

    # RISK EXPERIMENT V2: keep the established per-Cluster product which
    # resolves to one common MeshBind, but always bake that common bind into
    # Geometry. Skinned Mesh Models are emitted with identity world matrices;
    # leaving a 2.54 / 0.3937008 bind only in the Cluster makes ordinary
    # max_positions too small while skinned_max_positions remains correct.
    for mesh_model_id in bind_mesh_matrices:
        geometry_bind_bake_by_mesh[mesh_model_id] = True

    # Row-vector FBX bind contract:
    #
    #   unbaked Geometry: Transform = MeshBind * inverse(BoneBind)
    #   baked Geometry:   Transform =            inverse(BoneBind)
    #
    # Dropping MeshBind from the unbaked branch removes the authored pose.
    cluster_geometry_contracts: dict[int, list[tuple[list[float], bool]]] = {}
    for mesh_model_id, clusters in mesh_clusters.items():
        geometry_basis = bind_mesh_matrices.get(int(mesh_model_id))
        if geometry_basis is None:
            continue
        geometry_baked = bool(geometry_bind_bake_by_mesh.get(int(mesh_model_id), False))
        for cluster in clusters:
            cluster_id = int(cluster.get("cluster_id", 0) or 0)
            if cluster_id <= 0:
                continue
            cluster_geometry_contracts.setdefault(cluster_id, []).append(
                (list(geometry_basis), geometry_baked)
            )

    bind_candidates_by_bone: dict[int, list[list[float]]] = {}
    for cluster_id, cluster in cluster_by_id.items():
        bone_id = int(cluster["bone_model_id"])
        has_positive_influence = int(cluster.get("positive_weight_count", 0) or 0) > 0
        # TransformLink is the source of truth for the bind pose. Normalize it
        # into the same axis/unit domain as the rebuilt geometry.
        link = _generic_multiply_row_major_matrices(
            cluster["transform_link"], axis_conversion
        )
        scales = cluster_domain_scales.get(cluster_id, [])
        if scales:
            scales = sorted(float(value) for value in scales if math.isfinite(float(value)) and float(value) > 0.0)
        if scales:
            middle = len(scales) // 2
            bind_scale = (
                scales[middle]
                if len(scales) % 2
                else (scales[middle - 1] + scales[middle]) / 2.0
            )
        elif not has_positive_influence:
            # A weightless helper Cluster is not part of the bind contract.
            # Keep its source scale untouched so it cannot contaminate the
            # authored bind pose of the bone it happens to reference.
            bind_scale = 1.0
        else:
            bind_scale = float(unit_scale) if math.isfinite(float(unit_scale)) and float(unit_scale) > 0.0 else 1.0
        link = _scale_affine_matrix(
            link,
            bind_scale * unit_factor,
            label=f"Bone {bone_id} canonical bind",
        )
        link = _finite_matrix(link, label=f"Bone {bone_id} canonical bind")
        if _generic_invert_row_major_matrix(link) is None:
            raise ValueError(f"Bone {bone_id} canonical bind is not invertible")
        geometry_contracts = cluster_geometry_contracts.get(int(cluster_id), [])
        if geometry_contracts:
            geometry_basis, geometry_baked = geometry_contracts[0]
            for candidate_basis, candidate_baked in geometry_contracts[1:]:
                if (
                    candidate_baked != geometry_baked
                    or not _matrices_match(geometry_basis, candidate_basis)
                ):
                    raise ValueError(
                        f"Cluster {cluster_id} is shared by incompatible Geometry bind contracts"
                    )
            inverse_link = _generic_invert_row_major_matrix(link)
            if inverse_link is None:
                raise ValueError(
                    f"Cluster {cluster_id} canonical TransformLink is not invertible"
                )
            canonical_transform = (
                list(inverse_link)
                if geometry_baked
                else _generic_multiply_row_major_matrices(
                    geometry_basis,
                    inverse_link,
                )
            )
        else:
            canonical_transform = _identity_matrix()
        canonical_cluster_matrices[int(cluster_id)] = (
            canonical_transform,
            link,
        )
        associate = cluster.get("transform_associate")
        if associate is not None:
            canonical_associate_matrices[int(cluster_id)] = _scale_affine_matrix(
                _generic_multiply_row_major_matrices(associate, axis_conversion),
                unit_factor,
                label=f"Cluster {cluster_id} canonical TransformAssociateModel",
            )
        if has_positive_influence:
            bind_candidates_by_bone.setdefault(bone_id, []).append(list(link))

    # Pose records are bind-pose records too.  Prefer a normalized Cluster
    # link for bones that have Skin data; use the authored Pose only for bones
    # that have no usable Cluster row, and keep the current Model world as the
    # final fallback for an incomplete source file.
    for bone_id in sorted(int(value) for value in source_graph.get("bone_model_ids", [])):
        candidates = bind_candidates_by_bone.get(bone_id, [])
        if candidates:
            canonical_bind_by_model[bone_id] = list(candidates[0])
            continue
        pose_candidates = pose_matrices_by_model.get(bone_id, [])
        if pose_candidates:
            pose = _generic_multiply_row_major_matrices(pose_candidates[0], axis_conversion)
            # Unweighted/helper bones often already store Pose in the active
            # scene unit.  Do not multiply every authored Pose by UnitScale;
            # that turns an otherwise valid helper into a floating bone.  Only
            # convert when its basis is demonstrably in the pre-scaled domain
            # relative to the corresponding current Model world.
            pose_scale = 1.0
            try:
                active_unit = float(unit_scale)
                pose_scales = _matrix_basis_scales(
                    pose, label=f"Bone {bone_id} authored bind"
                )
                current_scales = _matrix_basis_scales(
                    axis_worlds[bone_id],
                    label=f"Bone {bone_id} current world",
                )
                ratios = [
                    current_scales[index] / pose_scales[index]
                    for index in range(3)
                    if pose_scales[index] > 1.0e-9
                ]
                if (
                    math.isfinite(active_unit)
                    and active_unit > 0.0
                    and len(ratios) == 3
                ):
                    ratio_min = min(ratios)
                    ratio_max = max(ratios)
                    ratio = sum(ratios) / 3.0
                    tolerance = max(1.0e-3, abs(active_unit) * 1.0e-3)
                    if (
                        ratio_max - ratio_min <= tolerance
                        and abs(ratio - active_unit) <= tolerance
                    ):
                        pose_scale = active_unit
            except (KeyError, TypeError, ValueError, OverflowError):
                # Keep the authored Pose untouched when there is no reliable
                # basis comparison; this is safer than an unconditional scale.
                pose_scale = 1.0
            canonical_bind_by_model[bone_id] = _scale_affine_matrix(
                pose,
                pose_scale * unit_factor,
                label=f"Bone {bone_id} authored bind",
            )
        elif bone_id in canonical_bone_worlds:
            canonical_bind_by_model[bone_id] = list(canonical_bone_worlds[bone_id])

    # Only a Mesh with at least one positive-weight Cluster is skinned.
    # Weightless helper/placeholder Clusters are intentionally excluded from
    # the geometry-bake and Model-world reset contract.
    skinned_mesh_ids = {
        mesh_id
        for mesh_id, rows in mesh_clusters.items()
        if any(int(row.get("positive_weight_count", 0) or 0) > 0 for row in rows)
    }
    output_worlds = {
        model_id: (
            _identity_matrix() if model_id in skinned_mesh_ids else list(world)
        )
        for model_id, world in target_worlds.items()
    }
    return {
        "source_worlds": worlds,
        "source_cluster_ids": source_cluster_ids,
        "source_associate_cluster_ids": source_associate_cluster_ids,
        "output_unit_worlds": target_worlds,
        "output_worlds": output_worlds,
        "domain_scales": domain_scales,
        "bind_mesh_matrices": bind_mesh_matrices,
        "geometry_bind_bake_by_mesh": geometry_bind_bake_by_mesh,
        "mesh_clusters": mesh_clusters,
        "cluster_matrices": canonical_cluster_matrices,
        "associate_matrices": canonical_associate_matrices,
        "canonical_bind_by_model": canonical_bind_by_model,
        "skinned_mesh_ids": skinned_mesh_ids,
        "unit_scale": unit_scale,
        "source_unit_scale_cm": source_unit_scale_cm,
        "target_unit_scale_cm": target_unit_scale_cm,
        "unit_factor": unit_factor,
        "geometry_domain": geometry_domain,
        "axis_conversion": axis_conversion,
    }


def _decode_polygon_corners(
    polygon_indices: Any,
    *,
    position_count: int,
) -> tuple[list[int], list[tuple[int, int]], list[int]]:
    if not isinstance(polygon_indices, list) or len(polygon_indices) < 3:
        raise ValueError("PolygonVertexIndex is missing or too short")
    source_indices: list[int] = []
    faces: list[tuple[int, int]] = []
    corner_faces: list[int] = []
    face_begin = 0
    face_index = 0
    for corner_index, raw_value in enumerate(polygon_indices):
        raw_index = int(raw_value)
        is_face_end = raw_index < 0
        position_index = ~raw_index if is_face_end else raw_index
        if position_index < 0 or position_index >= position_count:
            raise ValueError(
                f"PolygonVertexIndex[{corner_index}] references position {position_index}"
            )
        source_indices.append(position_index)
        corner_faces.append(face_index)
        if is_face_end:
            face_size = len(source_indices) - face_begin
            if face_size < 3:
                raise ValueError(f"Polygon {face_index} has fewer than three corners")
            faces.append((face_begin, face_size))
            face_begin = len(source_indices)
            face_index += 1
    if face_begin != len(source_indices):
        raise ValueError("PolygonVertexIndex does not terminate its final polygon")
    return source_indices, faces, corner_faces


def _decode_corner_layer(
    layer: FbxNode,
    *,
    value_child: str,
    index_child: str,
    tuple_size: int,
    source_indices: list[int],
    corner_faces: list[int],
    face_count: int,
    position_count: int,
) -> list[tuple[float, ...]]:
    raw_values = _child_value(layer, value_child)
    if not isinstance(raw_values, list) or not raw_values or len(raw_values) % tuple_size:
        raise ValueError(f"{value_child} is missing or malformed")
    values = [
        tuple(float(raw_values[offset + axis]) for axis in range(tuple_size))
        for offset in range(0, len(raw_values), tuple_size)
    ]
    mapping = str(_child_value(layer, "MappingInformationType") or "").casefold()
    if mapping == "bypolygonvertex":
        mapped = list(range(len(source_indices)))
        expected_count = len(source_indices)
    elif mapping in {"byvertice", "byvertex"}:
        mapped = list(source_indices)
        expected_count = position_count
    elif mapping == "bypolygon":
        mapped = list(corner_faces)
        expected_count = face_count
    elif mapping == "allsame":
        mapped = [0] * len(source_indices)
        expected_count = 1
    else:
        raise ValueError(
            f"unsupported MappingInformationType={mapping or '<missing>'}"
        )

    reference = str(_child_value(layer, "ReferenceInformationType") or "").casefold()
    if reference == "direct":
        direct = mapped
    elif reference == "indextodirect":
        raw_indices = _child_value(layer, index_child)
        if isinstance(raw_indices, list):
            if len(raw_indices) != expected_count:
                raise ValueError(f"{index_child} has an unexpected row count")
            direct = [int(raw_indices[index]) for index in mapped]
        elif value_child == "Materials" and len(values) == expected_count:
            # Blender and several FBX writers encode material slots as the
            # per-polygon Materials array while retaining the IndexToDirect
            # label; unlike UV/normal layers, no MaterialIndex child exists.
            # In that form the polygon mapping selects the authored value
            # directly.
            direct = mapped
        elif value_child == "Materials" and mapping == "allsame" and len(values) == 1:
            # The AllSame material form similarly stores its one slot directly
            # in Materials.
            direct = [0] * len(source_indices)
        else:
            raise ValueError(f"{index_child} has an unexpected row count")
    else:
        raise ValueError(
            f"unsupported ReferenceInformationType={reference or '<missing>'}"
        )
    if any(index < 0 or index >= len(values) for index in direct):
        raise ValueError(f"{value_child} direct index is outside its value domain")
    return [values[index] for index in direct]


def _canonicalize_layer(
    layer: FbxNode,
    *,
    value_child: str,
    values: list[tuple[float, ...]],
) -> None:
    flat = [component for value in values for component in value]
    _set_child_array(layer, value_child, "d", flat)
    _set_layer_text(layer, "MappingInformationType", "ByPolygonVertex")
    _set_layer_text(layer, "ReferenceInformationType", "Direct")
    _remove_child(layer, "NormalsIndex")
    _remove_child(layer, "UVIndex")


def _rebuild_geometry_node(geometry: FbxNode) -> dict[str, Any]:
    """Legacy in-place helper retained for compatibility; not used by conversion."""
    raw_positions = _child_value(geometry, "Vertices")
    raw_indices = _child_value(geometry, "PolygonVertexIndex")
    if (
        not isinstance(raw_positions, list)
        or len(raw_positions) < 9
        or len(raw_positions) % 3
        or not isinstance(raw_indices, list)
    ):
        return {"status": "skipped", "reason": "geometry_without_triangle_data", "cp_to_output": {}}
    try:
        position_count = len(raw_positions) // 3
        source_indices, faces, corner_faces = _decode_polygon_corners(
            raw_indices,
            position_count=position_count,
        )
        positions = [
            tuple(float(raw_positions[offset + axis]) for axis in range(3))
            for offset in range(0, len(raw_positions), 3)
        ]
    except (TypeError, ValueError, OverflowError) as exc:
        return {"status": "skipped", "reason": f"invalid_geometry:{exc}", "cp_to_output": {}}

    normal_nodes = [child for child in geometry.children if child.name == "LayerElementNormal"]
    if len(normal_nodes) > 1:
        return {"status": "skipped", "reason": "ambiguous_normal_layers", "cp_to_output": {}}
    try:
        normal_values = (
            _decode_corner_layer(
                normal_nodes[0],
                value_child="Normals",
                index_child="NormalsIndex",
                tuple_size=3,
                source_indices=source_indices,
                corner_faces=corner_faces,
                face_count=len(faces),
                position_count=position_count,
            )
            if normal_nodes
            else None
        )
        uv_nodes = [child for child in geometry.children if child.name == "LayerElementUV"]
        uv_values = [
            _decode_corner_layer(
                layer,
                value_child="UV",
                index_child="UVIndex",
                tuple_size=2,
                source_indices=source_indices,
                corner_faces=corner_faces,
                face_count=len(faces),
                position_count=position_count,
            )
            for layer in uv_nodes
        ]
    except (TypeError, ValueError, OverflowError) as exc:
        return {"status": "skipped", "reason": f"invalid_corner_layer:{exc}", "cp_to_output": {}}

    output_positions: list[tuple[float, float, float]] = []
    corner_normals: list[tuple[float, float, float]] = []
    corner_uvs: list[list[tuple[float, float]]] = [[] for _ in uv_values]
    output_indices: list[int] = []
    cp_to_output: dict[int, list[int]] = {index: [] for index in range(position_count)}
    key_to_output: dict[tuple[Any, ...], int] = {}
    corner_output: list[int] = []
    for corner_index, source_index in enumerate(source_indices):
        normal_key = normal_values[corner_index] if normal_values is not None else None
        uv_key = tuple(channel[corner_index] for channel in uv_values)
        key = (source_index, normal_key, uv_key)
        output_index = key_to_output.get(key)
        if output_index is None:
            output_index = len(output_positions)
            key_to_output[key] = output_index
            output_positions.append(positions[source_index])
            cp_to_output[source_index].append(output_index)
        if normal_values is not None:
            corner_normals.append(normal_values[corner_index])
        for channel_index, channel in enumerate(uv_values):
            corner_uvs[channel_index].append(channel[corner_index])
        corner_output.append(output_index)
    for face_begin, face_size in faces:
        for local_index in range(face_size):
            output_index = corner_output[face_begin + local_index]
            output_indices.append(~output_index if local_index == face_size - 1 else output_index)

    _set_child_array(
        geometry,
        "Vertices",
        "d",
        [component for position in output_positions for component in position],
    )
    _set_child_array(geometry, "PolygonVertexIndex", "i", output_indices)
    if normal_nodes and normal_values is not None:
        _canonicalize_layer(normal_nodes[0], value_child="Normals", values=corner_normals)
    for layer, values in zip(
        [child for child in geometry.children if child.name == "LayerElementUV"],
        corner_uvs,
    ):
        _canonicalize_layer(layer, value_child="UV", values=values)
    return {
        "status": "rebuilt",
        "reason": "corner_attributes_expanded",
        "cp_to_output": cp_to_output,
        "source_vertex_count": position_count,
        "output_vertex_count": len(output_positions),
        "normal_split": bool(normal_values is not None),
        "uv_channel_count": len(uv_values),
    }


def _object_id(node: FbxNode) -> int:
    if not node.properties or not isinstance(node.properties[0], int):
        return 0
    return int(node.properties[0])


def _rebuild_skin_indices(
    objects: FbxNode | None,
    connections: FbxNode | None,
    geometry_maps: dict[int, dict[int, list[int]]],
) -> int:
    if objects is None or connections is None or not geometry_maps:
        return 0
    objects_by_id = {
        _object_id(node): node
        for node in objects.children
        if _object_id(node) > 0
    }
    parents_by_child: dict[int, list[int]] = {}
    for connection in connections.children:
        values = connection.properties
        if connection.name != "C" or len(values) < 3 or values[0] != "OO":
            continue
        if not isinstance(values[1], int) or not isinstance(values[2], int):
            continue
        parents_by_child.setdefault(int(values[1]), []).append(int(values[2]))

    changed = 0
    for cluster_id, cluster in objects_by_id.items():
        if cluster.name != "Deformer" or len(cluster.properties) < 3:
            continue
        if str(cluster.properties[2]).casefold() != "cluster":
            continue
        skin_ids = [
            parent_id
            for parent_id in parents_by_child.get(cluster_id, [])
            if objects_by_id.get(parent_id) is not None
            and objects_by_id[parent_id].name == "Deformer"
            and len(objects_by_id[parent_id].properties) >= 3
            and str(objects_by_id[parent_id].properties[2]).casefold() == "skin"
        ]
        geometry_ids = [
            geometry_id
            for skin_id in skin_ids
            for geometry_id in parents_by_child.get(skin_id, [])
            if geometry_id in geometry_maps
        ]
        geometry_ids = sorted(set(geometry_ids))
        if len(geometry_ids) != 1:
            continue
        mapping = geometry_maps[geometry_ids[0]]
        indexes_node = _child_node(cluster, "Indexes")
        weights_node = _child_node(cluster, "Weights")
        if (
            indexes_node is None
            or weights_node is None
            or not indexes_node.properties
            or not weights_node.properties
            or not isinstance(indexes_node.properties[0], list)
            or not isinstance(weights_node.properties[0], list)
            or len(indexes_node.properties[0]) != len(weights_node.properties[0])
        ):
            continue
        expanded: dict[int, float] = {}
        order: list[int] = []
        for raw_index, raw_weight in zip(indexes_node.properties[0], weights_node.properties[0]):
            source_index = int(raw_index)
            output_indices = mapping.get(source_index)
            if not output_indices:
                output_indices = [source_index]
            for output_index in output_indices:
                if output_index not in expanded:
                    order.append(output_index)
                    expanded[output_index] = 0.0
                expanded[output_index] += float(raw_weight)
        new_indexes = [int(index) for index in order]
        new_weights = [float(expanded[index]) for index in order]
        if new_indexes != indexes_node.properties[0] or new_weights != weights_node.properties[0]:
            indexes_node.properties[0] = new_indexes
            weights_node.properties[0] = new_weights
            changed += 1
    return changed


def _safe_rebuild_semantic_geometry(roots: list[FbxNode]) -> dict[str, Any]:
    objects = _first_root(roots, "Objects")
    connections = _first_root(roots, "Connections")
    if objects is None:
        return {
            "geometry_rebuilt_count": 0,
            "geometry_skipped_count": 0,
            "skin_clusters_remapped": 0,
            "semantic_rebuild": "binary_semantic_safe_rebuilder",
        }
    geometry_maps: dict[int, dict[int, list[int]]] = {}
    rebuilt = 0
    skipped = 0
    skipped_reasons: list[str] = []
    for geometry in objects.children:
        if geometry.name != "Geometry":
            continue
        geometry_id = _object_id(geometry)
        result = _rebuild_geometry_node(geometry)
        if result.get("status") == "rebuilt":
            rebuilt += 1
            if geometry_id > 0:
                geometry_maps[geometry_id] = result["cp_to_output"]
        else:
            skipped += 1
            reason = str(result.get("reason", "unknown"))
            if reason not in skipped_reasons:
                skipped_reasons.append(reason)
    clusters_remapped = _rebuild_skin_indices(objects, connections, geometry_maps)
    return {
        "geometry_rebuilt_count": rebuilt,
        "geometry_skipped_count": skipped,
        "geometry_skip_reasons": skipped_reasons,
        "skin_clusters_remapped": clusters_remapped,
        "semantic_rebuild": "binary_semantic_safe_rebuilder",
    }


def normalize_generic_tree(roots: list[FbxNode]) -> dict[str, Any]:
    """Apply document-level cleanup before the dedicated semantic rebuild."""
    removed = _remove_max_metadata(roots)
    _set_creator(roots)
    axis_conversion = _generic_axis_conversion_matrix(roots)
    return {
        "removed_max_metadata": removed,
        "axis_policy": "normalize_to_xyz_axes",
        # This is a document-level conversion, matching the embedded Probe
        # contract.  Consumers must treat the resulting scene as canonical
        # XYZ/Y-up and skip any later per-Mesh axis inference.
        "fbx_axis_output_policy": FBX_AXIS_OUTPUT_POLICY,
        "axis_transform_contract": FBX_AXIS_TRANSFORM_CONTRACT,
        "canonicalization_policy": FBX_CANONICALIZATION_POLICY,
        "use_global_axis_domain": True,
        "source_axis_signature": _axis_signature(roots) or [],
        "axis_conversion_matrix": list(axis_conversion),
        "canonical_to_source_axis_matrix": (
            _generic_invert_row_major_matrix(axis_conversion)
            or _identity_matrix()
        ),
        "scene_policy": "binary_semantic_safe_rebuilder",
        "geometry_rebuilt_count": 0,
        "geometry_skipped_count": 0,
        "geometry_skip_reasons": [],
        "skin_clusters_remapped": 0,
        "semantic_rebuild": "binary_semantic_safe_rebuilder",
    }


def _clone_generic_node(
    node: FbxNode,
    *,
    id_map: dict[int, int] | None = None,
    strip_max_metadata: bool = True,
    in_pose_node: bool = False,
) -> FbxNode | None:
    """Clone one FBX node while removing producer-only metadata and remapping IDs."""
    values = list(node.properties)
    types = list(node.property_types)
    if strip_max_metadata and node.name == "P" and values:
        if _property_name(node) in {"MaxHandle", "UDP3DSMAX"}:
            return None
    if id_map and values and isinstance(values[0], int):
        values[0] = id_map.get(int(values[0]), int(values[0]))
    if in_pose_node and node.name == "Node" and values and isinstance(values[0], int):
        values[0] = id_map.get(int(values[0]), int(values[0])) if id_map else values[0]
    children: list[FbxNode] = []
    for child in node.children:
        cloned = _clone_generic_node(
            child,
            id_map=id_map,
            strip_max_metadata=strip_max_metadata,
            in_pose_node=in_pose_node or node.name == "PoseNode",
        )
        if cloned is not None:
            children.append(cloned)
    return FbxNode(node.name, values, types, children)


def _generic_global_settings(
    source: FbxNode,
    *,
    canonicalize_axes: bool = True,
) -> FbxNode:
    """Clone GlobalSettings and declare the converter's canonical X/Y/Z axes."""
    cloned = _clone_generic_node(source, strip_max_metadata=False)
    if cloned is None:
        cloned = FbxNode("GlobalSettings", [], [], [])
    if not canonicalize_axes:
        # Without a complete source axis signature, changing only the
        # metadata would reinterpret every existing vertex and Model matrix.
        # Preserve the producer's settings and let the caller use the
        # original-coordinate fallback instead.
        return cloned
    properties = _child_node(cloned, "Properties70")
    if properties is None:
        properties = cloned.add("Properties70")
    target = {
        "CoordAxis": 0,
        "CoordAxisSign": 1,
        "UpAxis": 1,
        "UpAxisSign": 1,
        "FrontAxis": 2,
        "FrontAxisSign": 1,
    }
    found: set[str] = set()
    for property_node in properties.children:
        name = _property_name(property_node)
        if name not in target or not property_node.properties:
            continue
        property_node.properties[-1] = target[name]
        if property_node.property_types:
            property_node.property_types[-1] = "I"
        found.add(name)
    for name, value in target.items():
        if name in found:
            continue
        properties.add(
            "P",
            ("S", name),
            ("S", "int"),
            ("S", "Integer"),
            ("S", ""),
            ("I", value),
        )
    unit_target = {
        "UnitScaleFactor": float(FBX_TARGET_UNIT_SCALE_CM),
        "OriginalUnitScaleFactor": float(FBX_TARGET_UNIT_SCALE_CM),
    }
    unit_found: set[str] = set()
    for property_node in properties.children:
        name = _property_name(property_node)
        if name not in unit_target or not property_node.properties:
            continue
        property_node.properties[-1] = unit_target[name]
        if property_node.property_types:
            property_node.property_types[-1] = "D"
        unit_found.add(name)
    for name, value in unit_target.items():
        if name in unit_found:
            continue
        properties.add(
            "P",
            ("S", name),
            ("S", "double"),
            ("S", "Number"),
            ("S", ""),
            ("D", value),
        )
    return cloned


def _validate_generic_unit_conversion(
    source_roots: list[FbxNode],
    generic_roots: list[FbxNode],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Hard gate proving that the Generic scene owns one global unit conversion."""
    source_unit_scale_cm, target_unit_scale_cm, expected_factor = (
        _canonical_unit_conversion(source_roots)
    )
    actual_factor = float(context.get("unit_factor", 0.0) or 0.0)
    tolerance = 1.0e-9
    if abs(actual_factor - expected_factor) > tolerance * max(1.0, abs(expected_factor)):
        raise ValueError(
            "Generic FBX unit validation failed: context factor does not match UnitScaleFactor"
        )
    output_unit_scale_cm = float(_global_unit_scale(generic_roots))
    if abs(output_unit_scale_cm - target_unit_scale_cm) > tolerance:
        raise ValueError(
            "Generic FBX unit validation failed: output GlobalSettings is not canonical"
        )

    source_worlds = context.get("source_worlds")
    target_worlds = context.get("output_unit_worlds")
    axis_conversion = context.get("axis_conversion")
    if not isinstance(source_worlds, dict) or not isinstance(target_worlds, dict):
        raise ValueError("Generic FBX unit validation is missing Model world matrices")
    axis_matrix = _finite_matrix(axis_conversion, "Generic unit validation axis matrix")
    if set(source_worlds) != set(target_worlds):
        raise ValueError("Generic FBX unit validation Model world coverage mismatch")
    for model_id, source_world in source_worlds.items():
        expected_world = _scale_affine_matrix(
            _generic_multiply_row_major_matrices(source_world, axis_matrix),
            expected_factor,
            label=f"Model {model_id} expected canonical unit world",
        )
        actual_world = _finite_matrix(
            target_worlds[model_id],
            f"Model {model_id} actual canonical unit world",
        )
        if not _matrices_match(expected_world, actual_world):
            raise ValueError(
                f"Generic FBX unit validation failed for Model {model_id}"
            )

    bind_mesh_matrices = context.get("bind_mesh_matrices")
    cluster_matrices = context.get("cluster_matrices")
    if not isinstance(bind_mesh_matrices, dict) or not isinstance(cluster_matrices, dict):
        raise ValueError("Generic FBX unit validation is missing Skin bind matrices")
    source_cluster_ids = set(context.get("source_cluster_ids", set()))
    if set(cluster_matrices) != source_cluster_ids:
        missing = sorted(source_cluster_ids - set(cluster_matrices))
        extra = sorted(set(cluster_matrices) - source_cluster_ids)
        raise ValueError(
            "Generic FBX unit validation Cluster coverage mismatch: "
            f"missing={missing[:8]}, extra={extra[:8]}"
        )
    associate_matrices = context.get("associate_matrices")
    source_associate_ids = set(context.get("source_associate_cluster_ids", set()))
    if not isinstance(associate_matrices, dict) or set(associate_matrices) != source_associate_ids:
        raise ValueError(
            "Generic FBX unit validation TransformAssociateModel coverage mismatch"
        )
    for mesh_id, matrix in bind_mesh_matrices.items():
        _finite_matrix(matrix, f"Mesh {mesh_id} validated canonical bind")
    for cluster_id, pair in cluster_matrices.items():
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ValueError(
                f"Generic FBX unit validation Cluster {cluster_id} has an invalid matrix pair"
            )
        transform = _finite_matrix(pair[0], f"Cluster {cluster_id} Transform")
        link = _finite_matrix(pair[1], f"Cluster {cluster_id} TransformLink")
        if _generic_invert_row_major_matrix(transform) is None:
            raise ValueError(
                f"Generic FBX unit validation Cluster {cluster_id} Transform is not invertible"
            )
        if _generic_invert_row_major_matrix(link) is None:
            raise ValueError(
                f"Generic FBX unit validation Cluster {cluster_id} TransformLink is not invertible"
            )
    return {
        "schema": "pc-rehd-generic-unit-validation-v1",
        "status": "PASS",
        "source_unit_scale_cm": source_unit_scale_cm,
        "target_unit_scale_cm": target_unit_scale_cm,
        "unit_factor": expected_factor,
        "unit_conversion_applied": abs(expected_factor - 1.0) > tolerance,
        "validated_model_count": len(source_worlds),
        "validated_bind_mesh_count": len(bind_mesh_matrices),
        "validated_cluster_count": len(cluster_matrices),
        "applied_once": True,
    }


def _generic_object_name(node: FbxNode, fallback: str) -> str:
    if len(node.properties) > 1:
        value = str(node.properties[1] or "")
        value = value.split("\x00", 1)[0].strip()
        if value:
            return value
    return fallback


def _unique_model_names(models: Iterable[FbxNode]) -> dict[int, str]:
    """Give every public Model name a stable source-ID-backed identity."""
    rows = [
        (_object_id(model), _generic_object_name(model, "Model"))
        for model in models
        if _object_id(model) > 0
    ]
    counts: dict[str, int] = {}
    for _source_id, base in rows:
        counts[base] = counts.get(base, 0) + 1
    used: set[str] = set()
    output: dict[int, str] = {}
    for source_id, base in sorted(rows):
        candidate = (
            base
            if counts.get(base, 0) == 1
            else f"{base}__CIX_{source_id}"
        )
        if candidate in used:
            candidate = f"{candidate}_{source_id}"
        used.add(candidate)
        output[source_id] = candidate
    return output


def _copy_semantic_properties(
    source: FbxNode,
    *,
    excluded_names: set[str] | None = None,
) -> FbxNode | None:
    """Read authored non-transform Properties70 values into a fresh container."""
    source_properties = _child_node(source, "Properties70")
    if source_properties is None:
        return None
    properties = FbxNode("Properties70", [], [], [])
    for property_node in source_properties.children:
        if property_node.name != "P" or not property_node.properties:
            continue
        excluded = {"MaxHandle"} if excluded_names is None else excluded_names
        if _property_name(property_node) in excluded:
            continue
        property_name = _property_name(property_node)
        if any(token in property_name.casefold() for token in _ALPHA_SPATIAL_TOKENS):
            raise ValueError(
                f"ALPHA_UNVERIFIED: {source.name} property {property_name!r} carries unmapped spatial semantics"
            )
        properties.children.append(
            FbxNode(
                "P",
                list(property_node.properties),
                list(property_node.property_types),
                [],
            )
        )
    return properties if properties.children else None


def _safe_float_rows(values: Any, width: int) -> list[list[float]]:
    if not isinstance(values, list) or len(values) % width:
        return []
    rows: list[list[float]] = []
    for offset in range(0, len(values), width):
        row = [float(values[offset + axis]) for axis in range(width)]
        if not all(math.isfinite(value) for value in row):
            return []
        rows.append(row)
    return rows


def _extract_geometry_semantics(
    source: FbxNode,
    *,
    position_matrix: list[float] | None = None,
    normal_matrix: list[float] | None = None,
) -> dict[str, Any]:
    """Decode one source Geometry into an independent canonical payload."""
    raw_positions = _child_value(source, "Vertices")
    raw_indices = _child_value(source, "PolygonVertexIndex")
    if not isinstance(raw_positions, list) or len(raw_positions) < 9 or len(raw_positions) % 3:
        return {"status": "header_only", "reason": "geometry_without_positions", "cp_to_output": {}}
    try:
        positions = _safe_float_rows(raw_positions, 3)
    except (TypeError, ValueError, OverflowError):
        positions = []
    if not positions:
        return {"status": "header_only", "reason": "geometry_with_invalid_positions", "cp_to_output": {}}
    if isinstance(raw_indices, list) and not raw_indices:
        prepared_position_matrix = (
            _finite_matrix(position_matrix, "position matrix")
            if position_matrix is not None
            else None
        )
        output_positions = []
        for position in positions:
            row = list(position)
            if prepared_position_matrix is not None:
                row = _generic_transform_position_values_prevalidated(
                    row,
                    prepared_position_matrix,
                )
            output_positions.append(row)
        return {
            "status": "rebuilt",
            "reason": "canonical_point_geometry",
            "vertices": output_positions,
            "faces": [],
            "loop_normals": [],
            "tangents": [],
            "binormals": [],
            "uv_channels": [],
            "colors": [],
            "edges": [],
            "materials": [],
            "cp_to_output": {index: [index] for index in range(len(output_positions))},
            "source_vertex_count": len(output_positions),
            "output_vertex_count": len(output_positions),
            "normal_split": False,
            "uv_channel_count": 0,
            "preserve_point_uv": True,
        }
    try:
        source_indices, source_faces, corner_faces = _decode_polygon_corners(
            raw_indices,
            position_count=len(positions),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        return {"status": "header_only", "reason": f"geometry_without_faces:{exc}", "cp_to_output": {}}

    raw_edges = _child_value(source, "Edges")
    edges: list[int] | None = None
    if isinstance(raw_edges, list):
        try:
            edges = [int(value) for value in raw_edges]
        except (TypeError, ValueError, OverflowError):
            edges = None

    normal_values: list[tuple[float, ...]] | None = None
    normal_nodes = [child for child in source.children if child.name == "LayerElementNormal"]
    if len(normal_nodes) > 1:
        return {
            "status": "header_only",
            "reason": "ambiguous_normal_layers",
            "cp_to_output": {},
        }
    if len(normal_nodes) == 1:
        try:
            normal_values = _decode_corner_layer(
                normal_nodes[0],
                value_child="Normals",
                index_child="NormalsIndex",
                tuple_size=3,
                source_indices=source_indices,
                corner_faces=corner_faces,
                face_count=len(source_faces),
                position_count=len(positions),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            return {
                "status": "header_only",
                "reason": f"invalid_normal_layer:{exc}",
                "cp_to_output": {},
            }

    vector_layers: dict[str, list[tuple[float, ...]] | None] = {
        "LayerElementTangent": None,
        "LayerElementBinormal": None,
    }
    vector_specs = {
        "LayerElementTangent": ("Tangents", "TangentsIndex"),
        "LayerElementBinormal": ("Binormals", "BinormalsIndex"),
    }
    for layer_name, (value_child, index_child) in vector_specs.items():
        nodes = [child for child in source.children if child.name == layer_name]
        if len(nodes) > 1:
            return {
                "status": "header_only",
                "reason": f"ambiguous_{layer_name.casefold()}",
                "cp_to_output": {},
            }
        if not nodes:
            continue
        try:
            vector_layers[layer_name] = _decode_corner_layer(
                nodes[0],
                value_child=value_child,
                index_child=index_child,
                tuple_size=3,
                source_indices=source_indices,
                corner_faces=corner_faces,
                face_count=len(source_faces),
                position_count=len(positions),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            return {
                "status": "header_only",
                "reason": f"invalid_{layer_name.casefold()}:{exc}",
                "cp_to_output": {},
            }

    uv_values: list[list[tuple[float, ...]]] = []
    uv_names: list[str] = []
    for uv_node in (child for child in source.children if child.name == "LayerElementUV"):
        try:
            values = _decode_corner_layer(
                uv_node,
                value_child="UV",
                index_child="UVIndex",
                tuple_size=2,
                source_indices=source_indices,
                corner_faces=corner_faces,
                face_count=len(source_faces),
                position_count=len(positions),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            return {
                "status": "header_only",
                "reason": f"invalid_uv_layer:{exc}",
                "cp_to_output": {},
            }
        uv_values.append(values)
        uv_names.append(str(_child_value(uv_node, "Name") or f"map{len(uv_values)}"))

    color_values: list[list[tuple[float, ...]]] = []
    for color_node in (child for child in source.children if child.name == "LayerElementColor"):
        try:
            values = _decode_corner_layer(
                color_node,
                value_child="Colors",
                index_child="ColorIndex",
                tuple_size=4,
                source_indices=source_indices,
                corner_faces=corner_faces,
                face_count=len(source_faces),
                position_count=len(positions),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            return {
                "status": "header_only",
                "reason": f"invalid_color_layer:{exc}",
                "cp_to_output": {},
            }
        color_values.append(values)

    material_face_values: list[list[int]] = []
    for material_node in (child for child in source.children if child.name == "LayerElementMaterial"):
        try:
            values = _decode_corner_layer(
                material_node,
                value_child="Materials",
                index_child="MaterialIndex",
                tuple_size=1,
                source_indices=source_indices,
                corner_faces=corner_faces,
                face_count=len(source_faces),
                position_count=len(positions),
            )
            cursor = 0
            face_values: list[int] = []
            for _face_begin, face_size in source_faces:
                if cursor >= len(values):
                    raise ValueError("Material layer has no value for a polygon")
                face_values.append(int(round(float(values[cursor][0]))))
                cursor += face_size
            material_face_values.append(face_values)
        except (TypeError, ValueError, OverflowError) as exc:
            return {
                "status": "header_only",
                "reason": f"invalid_material_layer:{exc}",
                "cp_to_output": {},
            }

    # Prepare only after layer validation, preserving header-only error returns.
    prepared_position_matrix = (
        _finite_matrix(position_matrix, "position matrix")
        if position_matrix is not None and source_indices
        else None
    )
    prepared_normal_matrix = (
        _generic_prepare_normal_transform(_finite_matrix(normal_matrix, "normal matrix"))
        if normal_matrix is not None and source_indices and (
            normal_values is not None or any(rows is not None for rows in vector_layers.values())
        )
        else None
    )
    output_positions: list[list[float]] = []
    output_faces: list[list[int]] = []
    corner_normals: list[list[float]] = []
    corner_tangents: list[list[float]] = []
    corner_binormals: list[list[float]] = []
    corner_uvs: list[list[list[float]]] = [[] for _ in uv_values]
    cp_to_output: dict[int, list[int]] = {index: [] for index in range(len(positions))}
    corner_output: list[int] = []
    key_to_output: dict[tuple[Any, ...], int] = {}
    for corner_index, source_index in enumerate(source_indices):
        face_index = corner_faces[corner_index]
        normal_key: tuple[float, ...] | None = None
        normal_row: tuple[float, ...] | None = None
        if normal_values is not None:
            normal_row = tuple(float(value) for value in normal_values[corner_index][:3])
            normal_key = normal_row
        uv_key = tuple(channel[corner_index] for channel in uv_values)
        color_key = tuple(channel[corner_index] for channel in color_values)
        key = (source_index, normal_key, uv_key, color_key)
        output_index = key_to_output.get(key)
        if output_index is None:
            output_index = len(output_positions)
            key_to_output[key] = output_index
            position = list(positions[source_index])
            if prepared_position_matrix is not None:
                position = _generic_transform_position_values_prevalidated(
                    position,
                    prepared_position_matrix,
                )
            output_positions.append(position)
            cp_to_output[source_index].append(output_index)
        if normal_row is not None:
            normal = list(normal_row)
            if prepared_normal_matrix is not None:
                normal = _generic_transform_normal_values_prevalidated(
                    normal,
                    prepared_normal_matrix,
                )
            corner_normals.append(normal)
        for layer_name, output_rows in (
            ("LayerElementTangent", corner_tangents),
            ("LayerElementBinormal", corner_binormals),
        ):
            rows = vector_layers.get(layer_name)
            if rows is not None:
                vector = list(rows[corner_index][:3])
                if prepared_normal_matrix is not None:
                    vector = _generic_transform_normal_values_prevalidated(
                        vector,
                        prepared_normal_matrix,
                    )
                output_rows.append(vector)
        for channel_index, channel in enumerate(uv_values):
            corner_uvs[channel_index].append([float(value) for value in channel[corner_index][:2]])
        corner_output.append(output_index)

    cursor = 0
    for _face_begin, face_size in source_faces:
        output_faces.append(corner_output[cursor : cursor + face_size])
        cursor += face_size

    uv_channels = [
        {
            "channel": channel_index + 1,
            "name": uv_names[channel_index],
            "values": values,
            "corner_indices": list(range(len(values))),
        }
        for channel_index, values in enumerate(corner_uvs)
    ]
    colors = [
        {
            "name": f"Color{channel_index + 1}",
            "values": [list(value) for value in values],
            "corner_indices": list(range(len(values))),
        }
        for channel_index, values in enumerate(
            [
                [
                    tuple(float(component) for component in color_values[channel_index][corner_index])
                    for corner_index in range(len(source_indices))
                ]
                for channel_index in range(len(color_values))
            ]
        )
    ]
    return {
        "status": "rebuilt",
        "reason": "semantic_geometry_decode",
        "vertices": output_positions,
        "faces": output_faces,
        "loop_normals": corner_normals if normal_values is not None else [],
        "tangents": corner_tangents if vector_layers["LayerElementTangent"] is not None else [],
        "binormals": corner_binormals if vector_layers["LayerElementBinormal"] is not None else [],
        "uv_channels": uv_channels,
        "colors": colors,
        "edges": edges,
        "materials": [
            {
                "values": values,
                "indices": list(range(len(values))),
            }
            for values in material_face_values
        ],
        "cp_to_output": cp_to_output,
        "source_vertex_count": len(positions),
        "output_vertex_count": len(output_positions),
        "normal_split": bool(normal_values is not None),
        "uv_channel_count": len(uv_channels),
    }


def _generic_geometry_semantics_worker(
    task: tuple[int, FbxNode, list[float] | None, list[float] | None],
) -> tuple[int, dict[str, Any]]:
    """Extract one Geometry payload in a worker without touching scene state."""
    source_id, source_geometry, position_matrix, normal_matrix = task
    payload = _extract_geometry_semantics(
        source_geometry,
        position_matrix=position_matrix,
        normal_matrix=normal_matrix,
    )
    return int(source_id), payload


def _iter_generic_geometry_payloads(
    tasks: list[tuple[int, FbxNode, list[float] | None, list[float] | None]],
) -> Generator[tuple[int, dict[str, Any]], None, None]:
    """Yield Geometry payloads in source order with bounded process IPC."""
    global _LAST_GENERIC_PARALLEL_STATS
    started_at = time.perf_counter()
    corner_counts: list[int] = []
    for _source_id, source_geometry, _position_matrix, _normal_matrix in tasks:
        indices = _child_value(source_geometry, "PolygonVertexIndex")
        corner_counts.append(len(indices) if isinstance(indices, list) else 0)
    requested_workers = _int_or_default(os.environ.get("GENERIC_FBX_WORKERS"), 0)
    substantial_tasks = sum(count >= 20_000 for count in corner_counts)
    cpu_count = os.cpu_count() or 1
    max_workers = max(
        1,
        min(len(tasks), requested_workers or substantial_tasks, cpu_count, 8),
    )
    if (
        os.environ.get("GENERIC_FBX_PARALLEL", "1") == "0"
        or max_workers < 2
        or sum(corner_counts) < 100_000
        or substantial_tasks < 2
    ):
        _LAST_GENERIC_PARALLEL_STATS = {
            "mode": "serial",
            "selected_workers": 1,
            "task_count": len(tasks),
            "elapsed_seconds": 0.0,
        }
        try:
            for task in tasks:
                result = _generic_geometry_semantics_worker(task)
                yield result
        finally:
            _LAST_GENERIC_PARALLEL_STATS["elapsed_seconds"] = round(time.perf_counter() - started_at, 6)
        return

    _LAST_GENERIC_PARALLEL_STATS = {
        "mode": "process_pool",
        "selected_workers": max_workers,
        "task_count": len(tasks),
        "elapsed_seconds": 0.0,
    }
    try:
        yield from _iter_process_results(
            _generic_geometry_semantics_worker, tasks,
            max_workers=max_workers, stats=_LAST_GENERIC_PARALLEL_STATS,
        )
    finally:
        _LAST_GENERIC_PARALLEL_STATS["elapsed_seconds"] = round(time.perf_counter() - started_at, 6)


def _apply_current_pose_inverse_skin_to_geometry_payload(
    payload: dict[str, Any],
    *,
    clusters: list[dict[str, Any]],
    cluster_matrices: dict[int, tuple[list[float], list[float]]],
    current_bone_worlds: dict[int, list[float]],
) -> dict[str, Any]:
    """Move visible posed Geometry back to bind space without losing pose."""
    if payload.get("status") != "rebuilt":
        return {"status": "skipped", "reason": "geometry_not_rebuilt"}
    vertices = payload.get("vertices")
    cp_to_output = payload.get("cp_to_output")
    if not isinstance(vertices, list) or not isinstance(cp_to_output, dict):
        return {"status": "skipped", "reason": "geometry_rows_unavailable"}
    source_count = int(payload.get("source_vertex_count", 0) or 0)
    if source_count <= 0:
        return {"status": "skipped", "reason": "empty_geometry"}

    accumulated = [[0.0] * 16 for _ in range(source_count)]
    weight_sums = [0.0] * source_count
    active_cluster_count = 0
    for cluster in clusters:
        cluster_id = int(cluster.get("cluster_id", 0) or 0)
        bone_id = int(cluster.get("bone_model_id", 0) or 0)
        canonical = cluster_matrices.get(cluster_id)
        bone_world = current_bone_worlds.get(bone_id)
        if canonical is None or bone_world is None:
            continue
        deformation = _generic_multiply_row_major_matrices(canonical[0], bone_world)
        used = False
        for raw_index, raw_weight in zip(cluster.get("indexes", []), cluster.get("weights", [])):
            source_index = int(raw_index)
            weight = float(raw_weight)
            if source_index < 0 or source_index >= source_count or weight <= 0.0:
                continue
            used = True
            for component in range(16):
                accumulated[source_index][component] += deformation[component] * weight
            weight_sums[source_index] += weight
        if used:
            active_cluster_count += 1
    if active_cluster_count <= 0:
        return {"status": "skipped", "reason": "no_active_clusters"}

    output_inverse: dict[int, list[float]] = {}
    transformed_source_count = 0
    max_roundtrip_error = 0.0
    for source_index, weight_sum in enumerate(weight_sums):
        if weight_sum <= 1.0e-12:
            continue
        deformation = [value / weight_sum for value in accumulated[source_index]]
        inverse = _generic_invert_row_major_matrix(deformation)
        if inverse is None:
            raise ValueError(f"Current-pose inverse Skin matrix is singular at vertex {source_index}")
        for output_index in cp_to_output.get(source_index, []):
            output_index = int(output_index)
            if output_index < 0 or output_index >= len(vertices):
                raise ValueError("Current-pose inverse Skin output vertex is out of range")
            visible = [float(value) for value in vertices[output_index][:3]]
            bind = _generic_transform_position_row_major(visible, inverse)
            roundtrip = _generic_transform_position_row_major(bind, deformation)
            max_roundtrip_error = max(
                max_roundtrip_error,
                *(abs(roundtrip[axis] - visible[axis]) for axis in range(3)),
            )
            vertices[output_index] = bind
            output_inverse[output_index] = inverse
        transformed_source_count += 1

    loop_normals = payload.get("loop_normals")
    faces = payload.get("faces")
    if isinstance(loop_normals, list) and isinstance(faces, list):
        corner_index = 0
        for face in faces:
            for output_index in face:
                if corner_index >= len(loop_normals):
                    break
                inverse = output_inverse.get(int(output_index))
                if inverse is not None:
                    loop_normals[corner_index] = _generic_transform_normal_row_major(
                        loop_normals[corner_index], inverse
                    )
                corner_index += 1
    if max_roundtrip_error > 0.001:
        raise ValueError(
            f"Current-pose inverse Skin roundtrip error is too large: {max_roundtrip_error}"
        )
    return {
        "status": "applied",
        "active_cluster_count": active_cluster_count,
        "transformed_source_vertex_count": transformed_source_count,
        "max_roundtrip_error": max_roundtrip_error,
    }


def _polygon_indices_from_faces(faces: list[list[int]]) -> list[int]:
    output: list[int] = []
    for face in faces:
        for index, vertex in enumerate(face):
            output.append(~int(vertex) if index == len(face) - 1 else int(vertex))
    return output


def _safe_rebuilder_source_graph(roots: list[FbxNode]) -> dict[str, Any]:
    """Read the source Model/Geometry/Skeleton graph before rebuilding it."""
    objects = _first_root(roots, "Objects")
    connections = _first_root(roots, "Connections")
    if objects is None:
        return {
            "object_nodes": [],
            "models": [],
            "geometries": [],
            "deformers": [],
            "poses": [],
            "model_parent_ids": {},
            "model_geometry_ids": {},
            "model_attribute_ids": {},
            "mesh_model_ids": [],
            "bone_model_ids": [],
            "structural_model_ids": [],
            "source_connection_count": 0,
        }

    object_nodes = [node for node in objects.children if _object_id(node) > 0]
    objects_by_id: dict[int, FbxNode] = {}
    duplicate_ids: list[int] = []
    for node in object_nodes:
        object_id = _object_id(node)
        if object_id in objects_by_id:
            duplicate_ids.append(object_id)
        else:
            objects_by_id[object_id] = node
    if duplicate_ids:
        raise ValueError(
            "Safe Rebuilder source Objects contain duplicate IDs: "
            + ", ".join(str(value) for value in sorted(set(duplicate_ids))[:8])
        )

    models = [node for node in object_nodes if node.name == "Model"]
    geometries = [node for node in object_nodes if node.name == "Geometry"]
    deformers = [node for node in object_nodes if node.name == "Deformer"]
    poses = [node for node in object_nodes if node.name == "Pose"]
    model_parent_candidates: dict[int, list[int]] = {}
    model_geometry_ids: dict[int, list[int]] = {}
    model_attribute_ids: dict[int, list[int]] = {}
    source_connection_count = 0
    if connections is not None:
        for connection in connections.children:
            if connection.name != "C" or len(connection.properties) < 3:
                continue
            if not isinstance(connection.properties[1], int) or not isinstance(
                connection.properties[2], int
            ):
                continue
            source_connection_count += 1
            kind = str(connection.properties[0] or "")
            if kind != "OO":
                continue
            child_id = int(connection.properties[1])
            parent_id = int(connection.properties[2])
            child_node = objects_by_id.get(child_id)
            parent_node = objects_by_id.get(parent_id)
            if child_node is None:
                continue
            if child_node.name == "Model" and (
                parent_id == 0 or (parent_node is not None and parent_node.name == "Model")
            ):
                model_parent_candidates.setdefault(child_id, []).append(parent_id)
            elif (
                child_node.name == "Geometry"
                and parent_node is not None
                and parent_node.name == "Model"
            ):
                model_geometry_ids.setdefault(parent_id, []).append(child_id)
            elif (
                child_node.name == "NodeAttribute"
                and parent_node is not None
                and parent_node.name == "Model"
            ):
                model_attribute_ids.setdefault(parent_id, []).append(child_id)

    normalized_parents: dict[int, int] = {}
    for model in models:
        model_id = _object_id(model)
        candidates = list(dict.fromkeys(model_parent_candidates.get(model_id, [])))
        non_root = [value for value in candidates if value != 0]
        normalized_parents[model_id] = non_root[0] if non_root else 0
        model_geometry_ids[model_id] = sorted(
            set(model_geometry_ids.get(model_id, []))
        )
        model_attribute_ids[model_id] = sorted(
            set(model_attribute_ids.get(model_id, []))
        )

    mesh_model_ids = [
        _object_id(model)
        for model in models
        if model_geometry_ids.get(_object_id(model))
    ]
    bone_model_ids = [
        _object_id(model)
        for model in models
        if _node_type(model).casefold() == "limbnode"
    ]
    mesh_set = set(mesh_model_ids)
    bone_set = set(bone_model_ids)
    structural_model_ids = [
        _object_id(model)
        for model in models
        if _object_id(model) not in mesh_set and _object_id(model) not in bone_set
    ]
    return {
        "object_nodes": object_nodes,
        "objects_by_id": objects_by_id,
        "models": models,
        "geometries": geometries,
        "deformers": deformers,
        "poses": poses,
        "model_parent_ids": normalized_parents,
        "model_geometry_ids": model_geometry_ids,
        "model_attribute_ids": model_attribute_ids,
        "mesh_model_ids": mesh_model_ids,
        "bone_model_ids": bone_model_ids,
        "structural_model_ids": structural_model_ids,
        "source_connection_count": source_connection_count,
    }


def _generic_model_node(
    source: FbxNode,
    object_id: int,
    *,
    model_type: str | None = None,
    local_matrix: list[float] | None = None,
    name_override: str | None = None,
) -> FbxNode:
    name = str(name_override) if name_override is not None else _generic_object_name(source, "Model")
    # Model IDs and display names are regenerated below.  The route handle is
    # the source node's stable identity, so capture it before any new ID/name
    # is assigned and attach it to this exact rebuilt Model.
    route_handle = _model_route_handle(source)
    resolved_type = (
        str(model_type)
        if model_type is not None
        else (str(source.properties[2] or "Null") if len(source.properties) > 2 else "Null")
    )
    model = FbxNode(
        "Model",
        [int(object_id), f"{name}\x00\x01Model", resolved_type],
        ["L", "S", "S"],
        [],
    )
    source_version = _child_value(source, "Version")
    try:
        version = int(source_version) if source_version is not None else 232
    except (TypeError, ValueError, OverflowError):
        version = 232
    model.add("Version", ("I", version))
    try:
        transform = (
            _finite_matrix(local_matrix, f"Model {name} local matrix")
            if local_matrix is not None
            else _source_model_local_matrix(source)
        )
        translation, rotation, scale = _matrix_to_trs(
            transform, label=f"Model {name} local matrix"
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"Alpha canonical Model {object_id} matrix cannot be decomposed: {exc}"
        ) from exc
    semantic_properties = _copy_semantic_properties(
        source, excluded_names=_PRODUCER_TRANSFORM_PROPERTIES
    )
    if semantic_properties is None:
        semantic_properties = FbxNode("Properties70", [], [], [])
    if route_handle > 0:
        # Replace any source spelling of the marker (direct property or
        # UDP3DSMAX blob) with one canonical Probe-readable row.  This keeps
        # the value paired with this Model even when its ID/name changes and
        # avoids stale or duplicate route rows in the rebuilt node.
        semantic_properties.children = [
            property_node
            for property_node in semantic_properties.children
            if not (
                property_node.name == "P"
                and _route_handle_from_property(property_node) > 0
            )
        ]
        semantic_properties.children.append(
            _canonical_route_handle_property(route_handle)
        )
    semantic_properties.children.insert(
        0,
        FbxNode(
            "P",
            ["Lcl Translation", "Lcl Translation", "", "A", *translation],
            ["S", "S", "S", "S", "D", "D", "D"],
            [],
        ),
    )
    semantic_properties.children.insert(
        1,
        FbxNode(
            "P",
            ["Lcl Rotation", "Lcl Rotation", "", "A", *rotation],
            ["S", "S", "S", "S", "D", "D", "D"],
            [],
        ),
    )
    semantic_properties.children.insert(
        2,
        FbxNode(
            "P",
            ["Lcl Scaling", "Lcl Scaling", "", "A", *scale],
            ["S", "S", "S", "S", "D", "D", "D"],
            [],
        ),
    )
    semantic_properties.children.insert(
        3,
        FbxNode(
            "P",
            ["RotationOrder", "enum", "", "", 0],
            ["S", "S", "S", "S", "I"],
            [],
        ),
    )
    semantic_properties.children.insert(
        4,
        FbxNode(
            "P",
            ["InheritType", "enum", "", "", 1],
            ["S", "S", "S", "S", "I"],
            [],
        ),
    )
    model.children.append(semantic_properties)
    for child_name, kind in (
        ("MultiLayer", "I"),
        ("MultiTake", "I"),
    ):
        value = _child_value(source, child_name)
        if value is not None:
            try:
                model.add(child_name, (kind, int(value)))
            except (TypeError, ValueError, OverflowError):
                pass
    shading = _child_value(source, "Shading")
    model.add("Shading", ("C", bool(shading) if shading is not None else True))
    culling = _child_value(source, "Culling")
    model.add("Culling", ("S", str(culling or "CullingOff")))
    return model


def _generic_structure_model_node(
    source: FbxNode,
    object_id: int,
    *,
    local_matrix: list[float] | None = None,
    name_override: str | None = None,
) -> FbxNode:
    """Rebuild a non-bone, non-geometry structural Model."""
    return _generic_model_node(
        source,
        object_id,
        local_matrix=local_matrix,
        name_override=name_override,
    )


def _generic_mesh_model_node(
    source: FbxNode,
    object_id: int,
    *,
    local_matrix: list[float] | None = None,
    name_override: str | None = None,
) -> FbxNode:
    """Rebuild one Mesh Model from the source Model identity."""
    model = _generic_model_node(
        source,
        object_id,
        model_type="Mesh",
        local_matrix=local_matrix,
        name_override=name_override,
    )
    properties = _child_node(model, "Properties70")
    if properties is None:
        properties = FbxNode("Properties70", [], [], [])
        model.children.insert(1, properties)
    if not any(
        _property_name(property_node) == "DefaultAttributeIndex"
        for property_node in properties.children
    ):
        # MAX uses this Model property to classify even a zero-face Geometry as
        # a Mesh. Without it, an otherwise valid empty Mesh imports as a Dummy.
        properties.children.append(
            FbxNode(
                "P",
                ["DefaultAttributeIndex", "int", "Integer", "", 0],
                ["S", "S", "S", "S", "I"],
                [],
            )
        )
    return model


def _generic_bone_model_node(
    source: FbxNode,
    object_id: int,
    *,
    local_matrix: list[float] | None = None,
    name_override: str | None = None,
) -> FbxNode:
    """Rebuild one LimbNode Model from the source skeleton identity."""
    return _generic_model_node(
        source,
        object_id,
        model_type="LimbNode",
        local_matrix=local_matrix,
        name_override=name_override,
    )


def _generic_node_attribute(
    object_id: int,
    model_name: str,
    model_type: str,
    *,
    bone: bool = False,
) -> FbxNode:
    attribute_type = "LimbNode" if bone or model_type.casefold() == "limbnode" else "Null"
    attribute = FbxNode(
        "NodeAttribute",
        [int(object_id), f"{model_name}\x00\x01NodeAttribute", attribute_type],
        ["L", "S", "S"],
        [],
    )
    attribute.add(
        "TypeFlags", ("S", "Skeleton" if attribute_type == "LimbNode" else "Null")
    )
    if attribute_type == "LimbNode":
        properties = attribute.add("Properties70")
        properties.add(
            "P",
            ("S", "Size"),
            ("S", "double"),
            ("S", "Number"),
            ("S", ""),
            ("D", 1.5),
        )
    return attribute


def _generic_geometry_node(
    source: FbxNode,
    object_id: int,
    *,
    name_override: str | None = None,
    payload: dict[str, Any] | None = None,
) -> FbxNode:
    """Write a fresh Geometry node from decoded topology and corner channels."""
    name = (
        str(name_override)
        if name_override is not None
        else _generic_object_name(source, "Geometry")
    )
    payload = payload if isinstance(payload, dict) else _extract_geometry_semantics(source)
    geometry = FbxNode(
        "Geometry",
        [int(object_id), f"{name}\x00\x01Geometry", "Mesh"],
        ["L", "S", "S"],
        [],
    )
    source_geometry_id = _object_id(source)
    if source_geometry_id > 0:
        properties = geometry.add("Properties70")
        properties.add(
            "P",
            ("S", "CodexSourceGeometryId"),
            ("S", "KString"),
            ("S", ""),
            ("S", "U"),
            ("S", str(source_geometry_id)),
        )
    geometry.add("GeometryVersion", ("I", 124))
    generated_children = {
        "Properties70",
        "GeometryVersion",
        "Vertices",
        "PolygonVertexIndex",
        "Edges",
        "LayerElementSmoothing",
        "LayerElementNormal",
        "LayerElementTangent",
        "LayerElementBinormal",
        "LayerElementUV",
        "LayerElementColor",
        "LayerElementMaterial",
        "Layer",
    }

    def preserve_unknown_children() -> None:
        for child in source.children:
            if child.name in generated_children:
                continue
            if _node_has_unverified_spatial_semantics(child):
                raise ValueError(
                    f"Alpha canonical Geometry {object_id} contains unverified spatial child {child.name}"
                )
            cloned = _clone_generic_node(
                child,
                strip_max_metadata=True,
            )
            if cloned is not None:
                geometry.children.append(cloned)

    if payload.get("status") != "rebuilt":
        raw_vertices = _child_value(source, "Vertices")
        raw_polygon_indices = _child_value(source, "PolygonVertexIndex")
        # Empty structural Geometry is a valid placeholder used by the MOD
        # contract. It has no source-space vertex payload to mix with the
        # rebuilt scene, so keep only non-spatial metadata for that case.
        if not raw_vertices and not raw_polygon_indices:
            preserve_unknown_children()
            return geometry
        raise ValueError(
            f"ALPHA_UNVERIFIED: Geometry {object_id} cannot be fully canonicalized: "
            f"{payload.get('reason', 'unknown reason')}"
        )

    vertices = payload.get("vertices", [])
    faces = payload.get("faces", [])
    normals = payload.get("loop_normals", [])
    tangents = payload.get("tangents", [])
    binormals = payload.get("binormals", [])
    uv_channels = payload.get("uv_channels", [])
    colors = payload.get("colors", [])
    materials = payload.get("materials", [])
    geometry.add(
        "Vertices",
        ("d", [float(component) for row in vertices for component in row[:3]]),
    )
    geometry.add("PolygonVertexIndex", ("i", _polygon_indices_from_faces(faces)))
    edges = payload.get("edges")
    if isinstance(edges, list):
        geometry.add("Edges", ("i", [int(value) for value in edges]))
    smoothing = geometry.add("LayerElementSmoothing", ("I", 0))
    smoothing.add("Version", ("I", 102))
    smoothing.add("Name", ("S", ""))
    smoothing.add("MappingInformationType", ("S", "ByPolygon"))
    smoothing.add("ReferenceInformationType", ("S", "Direct"))
    smoothing.add("Smoothing", ("i", [1] * len(faces)))
    if isinstance(normals, list) and len(normals) == sum(len(face) for face in faces):
        normal_layer = geometry.add("LayerElementNormal", ("I", 0))
        normal_layer.add("Version", ("I", 101))
        normal_layer.add("Name", ("S", ""))
        normal_layer.add("MappingInformationType", ("S", "ByPolygonVertex"))
        normal_layer.add("ReferenceInformationType", ("S", "Direct"))
        normal_layer.add(
            "Normals",
            ("d", [float(component) for row in normals for component in row[:3]]),
        )
    for layer_name, value_name, rows in (
        ("LayerElementTangent", "Tangents", tangents),
        ("LayerElementBinormal", "Binormals", binormals),
    ):
        if isinstance(rows, list) and len(rows) == sum(len(face) for face in faces):
            layer = geometry.add(layer_name, ("I", 0))
            layer.add("Version", ("I", 101))
            layer.add("Name", ("S", ""))
            layer.add("MappingInformationType", ("S", "ByPolygonVertex"))
            layer.add("ReferenceInformationType", ("S", "Direct"))
            layer.add(
                value_name,
                ("d", [float(component) for row in rows for component in row[:3]]),
            )
    source_uv_nodes = [
        child for child in source.children if child.name == "LayerElementUV"
    ]
    uv_nodes_by_index: dict[int, FbxNode] = {}
    uv_node_order: list[tuple[int, FbxNode]] = []
    if payload.get("preserve_point_uv"):
        for channel_index, source_uv in enumerate(source_uv_nodes):
            typed_index = (
                int(source_uv.properties[0])
                if source_uv.properties and isinstance(source_uv.properties[0], int)
                else channel_index
            )
            cloned_uv = _clone_generic_node(source_uv, strip_max_metadata=True)
            if cloned_uv is not None:
                geometry.children.append(cloned_uv)
                uv_nodes_by_index[typed_index] = cloned_uv
                uv_node_order.append((typed_index, cloned_uv))
    for channel_index, channel in enumerate(uv_channels):
        if not isinstance(channel, dict):
            continue
        values = channel.get("values", [])
        indices = channel.get("corner_indices", [])
        typed_index = channel_index
        source_uv = None
        if channel_index < len(source_uv_nodes):
            source_uv = source_uv_nodes[channel_index]
            if source_uv.properties and isinstance(source_uv.properties[0], int):
                typed_index = int(source_uv.properties[0])
        # Keep the producer's UV table/index contract intact.  The rebuilt
        # polygon corners are unchanged, so duplicating UV values per corner
        # is unnecessary and makes 3ds Max reinterpret the channel.  Only
        # synthesize a channel when the source did not provide one.
        source_mapping = (
            str(_child_value(source_uv, "MappingInformationType") or "").casefold()
            if source_uv is not None
            else ""
        )
        if (
            source_uv is not None
            and source_mapping not in {"byvertice", "byvertex"}
            and typed_index not in uv_nodes_by_index
        ):
            cloned_uv = _clone_generic_node(source_uv, strip_max_metadata=True)
            if cloned_uv is not None:
                geometry.children.append(cloned_uv)
                uv_nodes_by_index[typed_index] = cloned_uv
                uv_node_order.append((typed_index, cloned_uv))
                continue
        if typed_index in uv_nodes_by_index:
            typed_index = channel_index
        uv_layer = geometry.add("LayerElementUV", ("I", typed_index))
        uv_layer.add("Version", ("I", 101))
        uv_layer.add("Name", ("S", str(channel.get("name") or f"map{channel_index + 1}")))
        uv_layer.add("MappingInformationType", ("S", "ByPolygonVertex"))
        uv_layer.add("ReferenceInformationType", ("S", "IndexToDirect"))
        uv_layer.add("UV", ("d", [float(component) for row in values for component in row[:2]]))
        uv_layer.add("UVIndex", ("i", [int(value) for value in indices]))
        uv_nodes_by_index[typed_index] = uv_layer
        uv_node_order.append((typed_index, uv_layer))
    for color_index, color in enumerate(colors):
        if not isinstance(color, dict):
            continue
        values = color.get("values", [])
        indices = color.get("corner_indices", [])
        color_layer = geometry.add("LayerElementColor", ("I", color_index))
        color_layer.add("Version", ("I", 101))
        color_layer.add("Name", ("S", str(color.get("name") or f"Color{color_index + 1}")))
        color_layer.add("MappingInformationType", ("S", "ByPolygonVertex"))
        color_layer.add("ReferenceInformationType", ("S", "IndexToDirect"))
        color_layer.add("Colors", ("d", [float(component) for row in values for component in row[:4]]))
        color_layer.add("ColorIndex", ("i", [int(value) for value in indices]))
    source_material_nodes = [
        child for child in source.children if child.name == "LayerElementMaterial"
    ]
    for material_index, material in enumerate(materials):
        if not isinstance(material, dict):
            continue
        # MAX-authored files use a non-standard but meaningful material
        # contract: ByPolygon + IndexToDirect with the per-face slot values
        # stored directly in Materials and no MaterialIndex child. Rebuild
        # that LayerElement from the source so MAX does not reinterpret each
        # face number as a different material slot.
        if material_index < len(source_material_nodes):
            cloned_material = _clone_generic_node(
                source_material_nodes[material_index],
                strip_max_metadata=True,
            )
            if cloned_material is not None:
                geometry.children.append(cloned_material)
                continue
        values = [int(value) for value in material.get("values", [])]
        material_layer = geometry.add("LayerElementMaterial", ("I", material_index))
        material_layer.add("Version", ("I", 101))
        material_layer.add("Name", ("S", ""))
        material_layer.add("MappingInformationType", ("S", "ByPolygon"))
        material_layer.add("ReferenceInformationType", ("S", "Direct"))
        material_layer.add("Materials", ("i", values))
    generated_layer_types = {
        "LayerElementSmoothing",
        "LayerElementNormal",
        "LayerElementTangent",
        "LayerElementBinormal",
        "LayerElementUV",
        "LayerElementColor",
        "LayerElementMaterial",
    }
    generated_bindings: dict[tuple[str, int], bool] = {
        ("LayerElementSmoothing", 0): True,
        ("LayerElementNormal", 0): isinstance(normals, list)
        and len(normals) == sum(len(face) for face in faces),
        ("LayerElementTangent", 0): isinstance(tangents, list)
        and len(tangents) == sum(len(face) for face in faces),
        ("LayerElementBinormal", 0): isinstance(binormals, list)
        and len(binormals) == sum(len(face) for face in faces),
    }
    generated_bindings.update(
        {
            ("LayerElementUV", typed_index): True
            for typed_index, _node in uv_node_order
        }
    )
    generated_bindings.update(
        {
            ("LayerElementColor", color_index): True
            for color_index, _color in enumerate(colors)
        }
    )
    generated_bindings.update(
        {
            ("LayerElementMaterial", material_index): True
            for material_index, _material in enumerate(materials)
        }
    )

    def layer_reference(layer_type: str, typed_index: int) -> FbxNode:
        reference = FbxNode("LayerElement", [], [], [])
        reference.add("Type", ("S", layer_type))
        reference.add("TypedIndex", ("I", int(typed_index)))
        return reference

    source_layers = [child for child in source.children if child.name == "Layer"]
    output_layers: list[FbxNode] = []
    bound_keys: set[tuple[str, int]] = set()
    for source_layer in source_layers:
        layer = FbxNode(
            "Layer",
            list(source_layer.properties),
            list(source_layer.property_types),
            [],
        )
        for source_layer_element in source_layer.children:
            if source_layer_element.name != "LayerElement":
                if _node_has_unverified_spatial_semantics(source_layer_element):
                    raise ValueError(
                        f"Alpha canonical Geometry {object_id} contains unverified spatial layer {source_layer_element.name}"
                    )
                cloned = _clone_generic_node(
                    source_layer_element,
                    strip_max_metadata=True,
                )
                if cloned is not None:
                    layer.children.append(cloned)
                continue
            layer_type = str(_child_value(source_layer_element, "Type") or "")
            if layer_type not in generated_layer_types:
                if _node_has_unverified_spatial_semantics(source_layer_element):
                    raise ValueError(
                        f"Alpha canonical Geometry {object_id} contains unverified spatial layer {layer_type or '<missing>'}"
                    )
                cloned = _clone_generic_node(
                    source_layer_element,
                    strip_max_metadata=True,
                )
                if cloned is not None:
                    layer.children.append(cloned)
                continue
            raw_typed_index = _child_value(source_layer_element, "TypedIndex")
            try:
                typed_index = int(raw_typed_index)
            except (TypeError, ValueError, OverflowError):
                typed_index = 0
            key = (layer_type, typed_index)
            if generated_bindings.get(key):
                layer.children.append(layer_reference(layer_type, typed_index))
                bound_keys.add(key)
        output_layers.append(layer)

    if not output_layers:
        output_layers.append(FbxNode("Layer", [0], ["I"], []))
        output_layers[0].add("Version", ("I", 100))

    # A source Layer layout is authoritative for channel identity. Any
    # generated element absent from that layout is attached to the first Layer
    # as a compatibility fallback, while UV1/UV2 remain on their source Layers.
    fallback_order: list[tuple[str, int]] = [
        ("LayerElementSmoothing", 0),
    ]
    if generated_bindings.get(("LayerElementNormal", 0)):
        fallback_order.append(("LayerElementNormal", 0))
    if generated_bindings.get(("LayerElementTangent", 0)):
        fallback_order.append(("LayerElementTangent", 0))
    if generated_bindings.get(("LayerElementBinormal", 0)):
        fallback_order.append(("LayerElementBinormal", 0))
    fallback_order.extend(("LayerElementUV", typed_index) for typed_index, _node in uv_node_order)
    fallback_order.extend(
        ("LayerElementColor", color_index) for color_index, _color in enumerate(colors)
    )
    fallback_order.extend(
        ("LayerElementMaterial", material_index)
        for material_index, _material in enumerate(materials)
    )
    for key in fallback_order:
        if generated_bindings.get(key) and key not in bound_keys:
            output_layers[0].children.append(layer_reference(*key))
            bound_keys.add(key)

    def split_shared_uv_layers(layers: list[FbxNode]) -> list[FbxNode]:
        """Keep every UV channel on its own Layer for MAX channel identity."""
        used_layer_ids: set[int] = set()
        for layer in layers:
            if layer.properties and isinstance(layer.properties[0], int):
                used_layer_ids.add(int(layer.properties[0]))
        next_layer_id = max(used_layer_ids, default=-1) + 1

        def allocate_layer_id(preferred: int) -> int:
            nonlocal next_layer_id
            if preferred not in used_layer_ids:
                used_layer_ids.add(preferred)
                return preferred
            while next_layer_id in used_layer_ids:
                next_layer_id += 1
            allocated = next_layer_id
            used_layer_ids.add(allocated)
            next_layer_id += 1
            return allocated

        split_layers: list[FbxNode] = []
        for layer in layers:
            uv_elements = [
                element
                for element in layer.children
                if element.name == "LayerElement"
                and _child_value(element, "Type") == "LayerElementUV"
            ]
            if len(uv_elements) <= 1:
                split_layers.append(layer)
                continue

            first_uv = uv_elements[0]
            kept = FbxNode(
                "Layer",
                list(layer.properties),
                list(layer.property_types),
                [],
            )
            first_seen = False
            for child in layer.children:
                if child is not first_uv and child.name == "LayerElement" and _child_value(child, "Type") == "LayerElementUV":
                    continue
                kept.children.append(child)
                if child is first_uv:
                    first_seen = True
            if not first_seen:
                kept.children.append(first_uv)
            split_layers.append(kept)

            for extra_uv in uv_elements[1:]:
                raw_index = _child_value(extra_uv, "TypedIndex")
                try:
                    preferred_id = int(raw_index)
                except (TypeError, ValueError, OverflowError):
                    preferred_id = next_layer_id
                extra_layer = FbxNode(
                    "Layer",
                    [allocate_layer_id(preferred_id)],
                    ["I"],
                    [],
                )
                for child in layer.children:
                    if child.name == "LayerElement":
                        continue
                    cloned = _clone_generic_node(child, strip_max_metadata=True)
                    if cloned is not None:
                        extra_layer.children.append(cloned)
                cloned_uv = _clone_generic_node(extra_uv, strip_max_metadata=True)
                if cloned_uv is not None:
                    extra_layer.children.append(cloned_uv)
                split_layers.append(extra_layer)
        return split_layers

    output_layers = split_shared_uv_layers(output_layers)
    geometry.children.extend(output_layers)
    preserve_unknown_children()
    return geometry


def _generic_empty_mesh_geometry(object_id: int, name: str) -> FbxNode:
    """Build the empty Geometry used by the established OtherMesh contract."""
    source = FbxNode(
        "Geometry",
        [0, f"{name}\x00\x01Geometry", "Mesh"],
        ["L", "S", "S"],
        [],
    )
    return _generic_geometry_node(
        source,
        object_id,
        name_override=name,
        payload={
            "status": "rebuilt",
            "vertices": [],
            "faces": [],
            "loop_normals": None,
            "uv_channels": [],
            "colors": [],
            "materials": [],
            "cp_to_output": {},
        },
    )


def _generic_deformer_node(
    source: FbxNode,
    object_id: int,
    *,
    index_map: dict[int, list[int]] | None = None,
    canonical_matrices: tuple[list[float], list[float]] | None = None,
    canonical_associate_matrix: list[float] | None = None,
) -> FbxNode:
    """Write a fresh Skin/Cluster node from its semantic arrays."""
    deformer_type = _node_type(source) or "Deformer"
    name = _generic_object_name(source, deformer_type)
    node = FbxNode(
        "Deformer",
        [int(object_id), f"{name}\x00\x01Deformer", deformer_type],
        ["L", "S", "S"],
        [],
    )
    normalized_type = deformer_type.casefold()
    if normalized_type not in {"skin", "cluster"}:
        cloned = _clone_generic_node(source, strip_max_metadata=True)
        if cloned is not None:
            cloned.name = "Deformer"
            if cloned.properties:
                cloned.properties[0] = int(object_id)
                cloned.property_types[0] = "L"
            return cloned
    if normalized_type == "skin":
        version = _child_value(source, "Version")
        try:
            version = int(version) if version is not None else 101
        except (TypeError, ValueError, OverflowError):
            version = 101
        node.add("Version", ("I", version))
        accuracy = _child_value(source, "Link_DeformAcuracy")
        try:
            accuracy = float(accuracy) if accuracy is not None else 50.0
        except (TypeError, ValueError, OverflowError):
            accuracy = 50.0
        node.add("Link_DeformAcuracy", ("D", accuracy))
    elif normalized_type == "cluster":
        if canonical_matrices is None:
            raise ValueError(
                f"ALPHA_UNVERIFIED: Cluster {object_id} has no canonical matrix contract"
            )
        version = _child_value(source, "Version")
        try:
            version = int(version) if version is not None else 100
        except (TypeError, ValueError, OverflowError):
            version = 100
        node.add("Version", ("I", version))
        user_data = _child_node(source, "UserData")
        if user_data is not None and len(user_data.properties) >= 2:
            node.add("UserData", ("S", str(user_data.properties[0] or "")), ("S", str(user_data.properties[1] or "")))
        else:
            node.add("UserData", ("S", ""), ("S", ""))
        raw_indexes = _child_value(source, "Indexes")
        raw_weights = _child_value(source, "Weights")
        indexes: list[int] = []
        weights: list[float] = []
        if isinstance(raw_indexes, list) and isinstance(raw_weights, list):
            expanded: dict[int, float] = {}
            order: list[int] = []
            for raw_index, raw_weight in zip(raw_indexes, raw_weights):
                try:
                    source_index = int(raw_index)
                    weight = float(raw_weight)
                except (TypeError, ValueError, OverflowError):
                    continue
                targets = (index_map or {}).get(source_index, [source_index])
                for target in targets:
                    target_index = int(target)
                    if target_index not in expanded:
                        order.append(target_index)
                        expanded[target_index] = 0.0
                    expanded[target_index] += weight
            indexes = order
            weights = [expanded[index] for index in order]
        node.add("Indexes", ("i", indexes))
        node.add("Weights", ("d", weights))
        for child_name in ("Transform", "TransformLink"):
            matrix = canonical_matrices[0 if child_name == "Transform" else 1]
            values = []
            if isinstance(matrix, list):
                try:
                    values = [float(value) for value in matrix]
                except (TypeError, ValueError, OverflowError):
                    values = []
            node.add(child_name, ("d", values))
        if canonical_associate_matrix is not None:
            node.add(
                "TransformAssociateModel",
                ("d", [float(value) for value in canonical_associate_matrix]),
            )
        elif _child_node(source, "TransformAssociateModel") is not None:
            # Alpha policy: a spatial child may not be silently copied from
            # the source domain. The caller must provide its canonical form.
            raise ValueError(
                f"Cluster {object_id} TransformAssociateModel has no canonical mapping"
            )
    else:
        # Unknown deformer kinds are outside the Skin contract; keep only the
        # authored Properties70 values so they remain identifiable without
        # copying producer-only child records.
        semantic_properties = _copy_semantic_properties(source)
        if semantic_properties is not None:
            node.children.append(semantic_properties)
    semantic_properties = _copy_semantic_properties(source)
    if semantic_properties is not None and normalized_type in {"skin", "cluster"}:
        node.children.append(semantic_properties)
    return node


def _generic_pose_node(
    source: FbxNode,
    object_id: int,
    id_map: dict[int, int],
    *,
    canonical_bind_matrices: dict[int, list[float]] | None = None,
) -> FbxNode:
    """Write a fresh Pose containing only valid model matrices."""
    name = _generic_object_name(source, "BindPose")
    pose_type = str(source.properties[2] or "BindPose") if len(source.properties) > 2 else "BindPose"
    pose = FbxNode(
        "Pose",
        [int(object_id), f"{name}\x00\x01Pose", pose_type],
        ["L", "S", "S"],
        [],
    )
    source_type = _child_value(source, "Type")
    pose.add("Type", ("S", str(source_type or pose_type)))
    source_version = _child_value(source, "Version")
    try:
        version = int(source_version) if source_version is not None else 100
    except (TypeError, ValueError, OverflowError):
        version = 100
    pose.add("Version", ("I", version))
    valid_nodes: list[tuple[int, list[float]]] = []
    for pose_node in (child for child in source.children if child.name == "PoseNode"):
        source_node = _child_node(pose_node, "Node")
        if source_node is None or not source_node.properties:
            raise ValueError("ALPHA_UNVERIFIED: PoseNode has no valid Model reference")
        try:
            source_model_id = int(source_node.properties[0])
        except (TypeError, ValueError, OverflowError):
            raise ValueError("ALPHA_UNVERIFIED: PoseNode Model reference is invalid")
        output_model_id = id_map.get(source_model_id)
        if canonical_bind_matrices is None or source_model_id not in canonical_bind_matrices:
            raise ValueError(
                f"Alpha canonical PoseNode {source_model_id} has no canonical matrix"
            )
        matrix = list(canonical_bind_matrices[source_model_id])
        if output_model_id is None or len(matrix) != 16 or not all(math.isfinite(value) for value in matrix):
            continue
        valid_nodes.append((output_model_id, matrix))
    pose.add("NbPoseNodes", ("I", len(valid_nodes)))
    for model_id, matrix in valid_nodes:
        rebuilt_node = pose.add("PoseNode")
        rebuilt_node.add("Node", ("L", int(model_id)))
        rebuilt_node.add("Matrix", ("d", matrix))
    return pose


def _safe_rebuilder_validate_graph(
    objects: FbxNode,
    connections: FbxNode,
) -> dict[str, Any]:
    """Check the rebuilt object graph for duplicate IDs and dangling edges."""
    object_ids: set[int] = set()
    duplicate_ids = 0
    for node in objects.children:
        object_id = _object_id(node)
        if object_id <= 0:
            continue
        if object_id in object_ids:
            duplicate_ids += 1
        object_ids.add(object_id)
    dangling = 0
    for connection in connections.children:
        if connection.name != "C" or len(connection.properties) < 3:
            continue
        child_id, parent_id = connection.properties[1:3]
        if not isinstance(child_id, int) or not isinstance(parent_id, int):
            dangling += 1
            continue
        if child_id not in object_ids or (parent_id != 0 and parent_id not in object_ids):
            dangling += 1
    return {
        "object_ids": len(object_ids),
        "duplicate_ids": duplicate_ids,
        "dangling_connections": dangling,
    }


def _generic_definitions(objects: FbxNode) -> FbxNode:
    counts: dict[str, int] = {}
    for child in objects.children:
        counts[child.name] = counts.get(child.name, 0) + 1
    definitions = FbxNode("Definitions", [], [], [])
    definitions.add("Version", ("I", 100))
    definitions.add("Count", ("I", sum(counts.values())))
    for object_type, count in sorted(counts.items()):
        definition = definitions.add("ObjectType", ("S", object_type))
        definition.add("Count", ("I", count))
    return definitions


def _safe_rebuild_generic_scene(roots: list[FbxNode]) -> tuple[list[FbxNode], dict[str, Any]]:
    """Rebuild the source FBX graph through explicit structure, bone and Mesh passes."""
    source_objects = _first_root(roots, "Objects")
    source_connections = _first_root(roots, "Connections")
    if source_objects is None:
        raise ValueError("Generic FBX rebuild requires an Objects root")

    source_graph = _safe_rebuilder_source_graph(roots)
    object_nodes = list(source_graph["object_nodes"])
    objects_by_id = dict(source_graph["objects_by_id"])
    model_nodes = list(source_graph["models"])
    geometry_nodes = list(source_graph["geometries"])
    deformer_nodes = list(source_graph["deformers"])
    pose_nodes = list(source_graph["poses"])
    mesh_model_ids = set(source_graph["mesh_model_ids"])
    bone_model_ids = set(source_graph["bone_model_ids"])
    structural_model_ids = set(source_graph["structural_model_ids"])
    model_parent_ids = dict(source_graph["model_parent_ids"])
    model_geometry_ids = dict(source_graph["model_geometry_ids"])

    parents_by_child: dict[int, list[int]] = {}
    if source_connections is not None:
        for connection in source_connections.children:
            if connection.name != "C" or len(connection.properties) < 3:
                continue
            if connection.properties[0] != "OO":
                continue
            child_id, parent_id = connection.properties[1:3]
            if isinstance(child_id, int) and isinstance(parent_id, int):
                parents_by_child.setdefault(int(child_id), []).append(int(parent_id))

    v5_context = _v5_scene_context(roots, source_graph, parents_by_child)
    output_worlds = dict(v5_context["output_worlds"])
    canonical_bind_by_model = dict(v5_context["canonical_bind_by_model"])
    # Publish the exact document-level basis used by the rebuild.  All
    # Geometry/Model/Cluster/Pose passes below belong to this one canonical
    # Y-up scene domain; no later per-Mesh legacy-axis inference is allowed.
    axis_conversion = list(
        v5_context.get("axis_conversion") or _identity_matrix()
    )
    for mesh_model_id in v5_context["skinned_mesh_ids"]:
        canonical_bind_by_model[int(mesh_model_id)] = _identity_matrix()
    # Every PoseNode must be written in the rebuilt scene domain. Bones retain
    # their authoritative bind matrices above; structural and unskinned Models
    # use their canonical output world instead of a source Pose fallback.
    for model_id, world in output_worlds.items():
        canonical_bind_by_model.setdefault(int(model_id), list(world))

    id_map: dict[int, int] = {}
    next_id = 100000

    def allocate(source_id: int) -> int:
        nonlocal next_id
        if source_id in id_map:
            return id_map[source_id]
        id_map[source_id] = next_id
        next_id += 1
        return id_map[source_id]

    # Preserve the source FBX's numeric identity ordering. Object declaration
    # position is not authoritative and may place larger IDs before smaller IDs.
    source_id_order = sorted(
        _object_id(source)
        for source in object_nodes
        if source.name != "NodeAttribute"
    )
    source_by_id = {
        _object_id(source): source
        for source in object_nodes
        if source.name != "NodeAttribute"
    }
    # MAX resolves duplicate Video records by their first declaration.  When
    # one duplicate carries embedded bytes and another is an empty shell, the
    # embedded record must receive the earlier output ID so the sorted output
    # still lets MAX materialize the high-resolution image first.
    video_positions: dict[tuple[str, str, str], list[int]] = {}
    for position, source_id in enumerate(source_id_order):
        source = source_by_id.get(source_id)
        if source is None or source.name != "Video":
            continue
        name = str(source.properties[1]) if len(source.properties) > 1 else ""
        relative = str(_child_value(source, "RelativeFilename") or "")
        file_name = str(_child_value(source, "FileName") or "")
        video_positions.setdefault((name, relative, file_name), []).append(position)
    for positions in video_positions.values():
        if len(positions) < 2:
            continue
        group_ids = [source_id_order[position] for position in positions]
        group_ids.sort(
            key=lambda source_id: bool(
                _child_value(source_by_id[source_id], "Content")
            ),
            reverse=True,
        )
        for position, source_id in zip(positions, group_ids):
            source_id_order[position] = source_id
    for source_id in source_id_order:
        allocate(source_id)

    model_by_id = {_object_id(node): node for node in model_nodes}
    model_names = _unique_model_names(model_nodes)
    # MAX needs an actual Mesh Model/Geometry edge for empty OtherMesh slots.
    # The source exporter represents these slots as Null Models, but the
    # established MOD importer emits an empty Geometry so MAX creates a Mesh
    # object instead of a Dummy. Keep this scoped to direct Mesh_* children of
    # OtherMesh (including the import-suffixed helper variant).
    other_mesh_placeholder_ids: set[int] = set()
    for source_id in structural_model_ids:
        source = model_by_id[source_id]
        model_name = model_names.get(source_id, _generic_object_name(source, ""))
        if not model_name.casefold().startswith("mesh_"):
            continue
        parent_id = model_parent_ids.get(source_id, 0)
        parent = model_by_id.get(parent_id)
        parent_name = _generic_object_name(parent, "") if parent is not None else ""
        parent_folded = parent_name.casefold()
        if parent_folded == "othermesh" or parent_folded.startswith("othermesh_import"):
            other_mesh_placeholder_ids.add(source_id)

    output_attribute_ids: dict[int, int] = {}
    for source_id in sorted(
        (structural_model_ids | bone_model_ids) - other_mesh_placeholder_ids
    ):
        output_attribute_ids[source_id] = next_id
        next_id += 1

    generic_objects = FbxNode("Objects", [], [], [])
    emitted_object_ids: set[int] = set()
    other_mesh_geometry_ids: dict[int, int] = {}

    # Materials, animation nodes and other non-scene objects are retained as
    # cloned source objects so this pass remains focused on scene semantics.
    for source in object_nodes:
        if source.name in {"Model", "Geometry", "NodeAttribute", "Deformer", "Pose"}:
            continue
        _guard_alpha_cloned_object(source)
        cloned = _clone_generic_node(source, id_map=id_map, strip_max_metadata=True)
        if cloned is not None:
            generic_objects.children.append(cloned)
            emitted_object_ids.add(_object_id(source))

    # Structural Models are rebuilt first so parent links are available to the
    # later bone and Mesh passes. Empty OtherMesh slots are the one intentional
    # exception: they are promoted to Mesh and receive an empty Geometry.
    for source_id in sorted(structural_model_ids):
        source = model_by_id[source_id]
        if source_id in other_mesh_placeholder_ids:
            model_name = model_names.get(source_id, "Mesh")
            geometry_id = next_id
            next_id += 1
            other_mesh_geometry_ids[source_id] = geometry_id
            generic_objects.children.append(
                _generic_empty_mesh_geometry(geometry_id, model_name)
            )
            generic_objects.children.append(
                _generic_mesh_model_node(
                    source,
                    id_map[source_id],
                    local_matrix=_output_local_from_world(
                        source_id, output_worlds, source_graph["model_parent_ids"]
                    ),
                    name_override=model_name,
                )
            )
            emitted_object_ids.add(source_id)
            continue
        generic_objects.children.append(
            _generic_structure_model_node(
                source,
                id_map[source_id],
                local_matrix=_output_local_from_world(
                    source_id, output_worlds, source_graph["model_parent_ids"]
                ),
                name_override=model_names.get(source_id),
            )
        )
        generic_objects.children.append(
            _generic_node_attribute(
                output_attribute_ids[source_id],
                model_names.get(source_id, "Model"),
                _node_type(source),
            )
        )
        emitted_object_ids.add(source_id)

    # Bones are read from the source LimbNode Models and rebuilt independently
    # from Mesh Models, including a fresh skeleton NodeAttribute.
    for source_id in sorted(bone_model_ids):
        source = model_by_id[source_id]
        generic_objects.children.append(
            _generic_bone_model_node(
                source,
                id_map[source_id],
                local_matrix=_output_local_from_world(
                    source_id, output_worlds, source_graph["model_parent_ids"]
                ),
                name_override=model_names.get(source_id),
            )
        )
        generic_objects.children.append(
            _generic_node_attribute(
                output_attribute_ids[source_id],
                model_names.get(source_id, "Bone"),
                "LimbNode",
                bone=True,
            )
        )
        emitted_object_ids.add(source_id)

    geometry_by_id = {
        _object_id(node): node for node in geometry_nodes if _object_id(node) > 0
    }
    geometry_output_ids = {
        geometry_id: id_map[geometry_id] for geometry_id in geometry_by_id
    }
    # Match the embedded Probe's Generic contract: every emitted Geometry is
    # interpreted inside the same rebuilt scene-global Y-up axis domain.
    canonical_normal_geometry_ids: set[int] = set()
    normal_axis_domain_by_geometry_id: dict[str, str] = {}

    def record_normal_axis_domain(
        source_geometry_id: int,
        *,
        applied_matrix: list[float] | None,
    ) -> None:
        output_geometry_id = _int_or_default(
            geometry_output_ids.get(int(source_geometry_id)),
            0,
        )
        if output_geometry_id <= 0:
            return
        domain = FBX_NORMAL_AXIS_DOMAIN_CANONICAL
        normal_axis_domain_by_geometry_id[str(output_geometry_id)] = domain
        canonical_normal_geometry_ids.add(output_geometry_id)

    emitted_geometry_ids: set[int] = set()
    geometry_cp_maps: dict[int, dict[int, list[int]]] = {}
    geometry_rebuilt_count = 0
    geometry_header_only_count = 0
    geometry_skip_reasons: list[str] = []
    skin_clusters_remapped = 0
    # Geometry decoding is pure once matrices and IDs are known. Prepare all
    # unique tasks first, then merge payloads in the exact source-use order.
    geometry_tasks: list[tuple[int, FbxNode, list[float] | None, list[float] | None]] = []
    geometry_task_ids: set[int] = set()
    geometry_task_matrices: dict[int, tuple[list[float] | None, list[float] | None]] = {}
    for source_id in sorted(mesh_model_ids):
        source_model = model_by_id[source_id]
        for geometry_id in model_geometry_ids.get(source_id, []):
            source_geometry = geometry_by_id.get(geometry_id)
            if source_geometry is None or geometry_id in geometry_task_ids:
                continue
            geometry_bind_matrix = (
                v5_context["bind_mesh_matrices"].get(int(source_id))
                if v5_context["geometry_bind_bake_by_mesh"].get(int(source_id), False)
                else None
            )
            geometric_matrix = _source_geometric_matrix(source_model)
            geometry_position_matrix = (
                _generic_multiply_row_major_matrices(geometric_matrix, geometry_bind_matrix)
                if geometry_bind_matrix is not None
                else geometric_matrix
            )
            geometry_tasks.append((int(geometry_id), source_geometry, geometry_position_matrix, geometry_position_matrix))
            geometry_task_ids.add(int(geometry_id))
            geometry_task_matrices[int(geometry_id)] = (geometry_position_matrix, geometry_position_matrix)
    for geometry_id, source_geometry in geometry_by_id.items():
        if geometry_id in geometry_task_ids:
            continue
        unlinked_geometry_matrix = _scale_affine_matrix(
            axis_conversion,
            float(v5_context["unit_factor"]),
            label=f"Geometry {geometry_id} canonical unit matrix",
        )
        geometry_tasks.append((int(geometry_id), source_geometry, unlinked_geometry_matrix, unlinked_geometry_matrix))
        geometry_task_ids.add(int(geometry_id))
        geometry_task_matrices[int(geometry_id)] = (unlinked_geometry_matrix, unlinked_geometry_matrix)

    geometry_payloads = _iter_generic_geometry_payloads(geometry_tasks)
    try:
        for source_id in sorted(mesh_model_ids):
            source_model = model_by_id[source_id]
            model_name = model_names.get(source_id, "Mesh")
            for geometry_id in model_geometry_ids.get(source_id, []):
                source_geometry = geometry_by_id.get(geometry_id)
                if source_geometry is None or geometry_id in emitted_geometry_ids:
                    continue
                payload_id, geometry_payload = next(geometry_payloads)
                if payload_id != geometry_id:
                    raise ValueError("Probe Geometry result ID does not match the source")
                record_normal_axis_domain(geometry_id, applied_matrix=geometry_task_matrices[int(geometry_id)][0])
                generic_objects.children.append(
                    _generic_geometry_node(
                        source_geometry,
                        geometry_output_ids[geometry_id],
                        name_override=model_name,
                        payload=geometry_payload,
                    )
                )
                geometry_cp_maps[geometry_id] = dict(geometry_payload.get("cp_to_output", {}))
                if geometry_payload.get("status") == "rebuilt":
                    geometry_rebuilt_count += 1
                else:
                    geometry_header_only_count += 1
                    reason = str(geometry_payload.get("reason", "geometry_header_only"))
                    if reason not in geometry_skip_reasons:
                        geometry_skip_reasons.append(reason)
                emitted_geometry_ids.add(geometry_id)
            generic_objects.children.append(
                _generic_mesh_model_node(
                    source_model,
                    id_map[source_id],
                    local_matrix=_output_local_from_world(source_id, output_worlds, source_graph["model_parent_ids"]),
                    name_override=model_name,
                )
            )
            emitted_object_ids.add(source_id)

        # Preserve valid unlinked Geometry objects as standalone records.
        for geometry_id, source_geometry in geometry_by_id.items():
            if geometry_id in emitted_geometry_ids:
                continue
            payload_id, geometry_payload = next(geometry_payloads)
            if payload_id != geometry_id:
                raise ValueError("Probe Geometry result ID does not match the source")
            record_normal_axis_domain(geometry_id, applied_matrix=geometry_task_matrices[int(geometry_id)][0])
            generic_objects.children.append(
                _generic_geometry_node(source_geometry, geometry_output_ids[geometry_id], payload=geometry_payload)
            )
            geometry_cp_maps[geometry_id] = dict(geometry_payload.get("cp_to_output", {}))
            if geometry_payload.get("status") == "rebuilt":
                geometry_rebuilt_count += 1
            else:
                geometry_header_only_count += 1
                reason = str(geometry_payload.get("reason", "geometry_header_only"))
                if reason not in geometry_skip_reasons:
                    geometry_skip_reasons.append(reason)
            emitted_geometry_ids.add(geometry_id)
    finally:
        geometry_payloads.close()

    for source in deformer_nodes:
        source_id = _object_id(source)
        index_map: dict[int, list[int]] | None = None
        if _node_type(source).casefold() == "cluster":
            geometry_ids = [
                parent_id
                for parent_id in parents_by_child.get(source_id, [])
                if parent_id in geometry_cp_maps
            ]
            if not geometry_ids:
                for skin_id in parents_by_child.get(source_id, []):
                    skin_node = objects_by_id.get(skin_id)
                    if skin_node is None or _node_type(skin_node).casefold() != "skin":
                        continue
                    geometry_ids.extend(
                        parent_id
                        for parent_id in parents_by_child.get(skin_id, [])
                        if parent_id in geometry_cp_maps
                    )
            if geometry_ids:
                index_map = geometry_cp_maps.get(sorted(set(geometry_ids))[0])
                if index_map:
                    raw_indexes = _child_value(source, "Indexes")
                    if isinstance(raw_indexes, list):
                        expanded_indexes = [
                            target
                            for raw_index in raw_indexes
                            for target in index_map.get(int(raw_index), [int(raw_index)])
                        ]
                        if expanded_indexes != [int(value) for value in raw_indexes]:
                            skin_clusters_remapped += 1
        canonical_matrices = (
            v5_context["cluster_matrices"].get(source_id)
            if _node_type(source).casefold() == "cluster"
            else None
        )
        if _node_type(source).casefold() == "cluster" and canonical_matrices is None:
            raise ValueError(
                f"ALPHA_UNVERIFIED: Cluster {source_id} is connected to the rebuilt scene but has no canonical matrix"
            )
        canonical_associate_matrix = (
            v5_context.get("associate_matrices", {}).get(source_id)
            if _node_type(source).casefold() == "cluster"
            else None
        )
        generic_objects.children.append(
            _generic_deformer_node(
                source,
                id_map[source_id],
                index_map=index_map,
                canonical_matrices=canonical_matrices,
                canonical_associate_matrix=canonical_associate_matrix,
            )
        )
        emitted_object_ids.add(source_id)
    for source in pose_nodes:
        source_id = _object_id(source)
        generic_objects.children.append(
            _generic_pose_node(
                source,
                id_map[source_id],
                id_map,
                canonical_bind_matrices=canonical_bind_by_model,
            )
        )
        emitted_object_ids.add(source_id)

    # Every source object handled by a dedicated scene pass is already emitted.
    # Do not feed those records through the generic fallback: doing so would
    # duplicate Geometry/Model/Deformer/Pose nodes and reuse their remapped IDs.
    # Keep the fallback for newly introduced FBX object kinds so they cannot
    # vanish silently from the rebuilt scene.
    for source in object_nodes:
        source_id = _object_id(source)
        if source.name in {"Model", "Geometry", "NodeAttribute", "Deformer", "Pose"} or source_id in emitted_object_ids:
            continue
        _guard_alpha_cloned_object(source)
        cloned = _clone_generic_node(source, id_map=id_map, strip_max_metadata=True)
        if cloned is not None:
            generic_objects.children.append(cloned)
            emitted_object_ids.add(source_id)

    # Emit in the same ascending identity order used by the source FBX.
    generic_objects.children.sort(key=_object_id)

    generic_connections = FbxNode("Connections", [], [], [])
    existing_edges: set[tuple[Any, ...]] = set()
    existing_model_parent: set[int] = set()
    existing_geometry_links: set[tuple[int, int]] = set()
    dropped_connections = 0
    if source_connections is not None:
        for source_connection in source_connections.children:
            if source_connection.name != "C" or len(source_connection.properties) < 3:
                cloned = _clone_generic_node(source_connection, id_map=id_map)
                if cloned is not None:
                    generic_connections.children.append(cloned)
                continue
            values = list(source_connection.properties)
            child_source, parent_source = values[1], values[2]
            if not isinstance(child_source, int) or not isinstance(parent_source, int):
                dropped_connections += 1
                continue
            child_source = int(child_source)
            parent_source = int(parent_source)
            child_node = objects_by_id.get(child_source)
            parent_node = objects_by_id.get(parent_source)
            # NodeAttributes are deliberately regenerated per Model; retaining
            # their old edges would recreate dangling or duplicate attributes.
            if (child_node is not None and child_node.name == "NodeAttribute") or (
                parent_node is not None and parent_node.name == "NodeAttribute"
            ):
                dropped_connections += 1
                continue
            child = id_map.get(child_source)
            parent = 0 if parent_source == 0 else id_map.get(parent_source)
            if child is None or parent is None:
                dropped_connections += 1
                continue
            values[1] = child
            values[2] = parent
            edge_key = tuple(values)
            if edge_key in existing_edges:
                continue
            existing_edges.add(edge_key)
            generic_connections.children.append(
                FbxNode("C", values, list(source_connection.property_types), [])
            )
            kind = str(values[0] or "")
            if kind == "OO" and child_node is not None:
                if child_node.name == "Model" and (
                    parent_source == 0
                    or (parent_node is not None and parent_node.name == "Model")
                ):
                    existing_model_parent.add(child_source)
                if child_node.name == "Geometry" and parent_node is not None and parent_node.name == "Model":
                    existing_geometry_links.add((parent_source, child_source))

    def add_connection(kind: str, child: int, parent: int) -> None:
        key = (kind, child, parent)
        if key in existing_edges:
            return
        existing_edges.add(key)
        generic_connections.add(
            "C", ("S", kind), ("L", int(child)), ("L", int(parent))
        )

    # Add exactly one generated attribute link per structural/bone Model.
    for source_id, attribute_id in sorted(output_attribute_ids.items()):
        add_connection("OO", attribute_id, id_map[source_id])

    # Empty OtherMesh slots were promoted to Mesh above. Their generated
    # Geometry edges are absent from the source graph, so add them explicitly.
    for source_id, geometry_id in sorted(other_mesh_geometry_ids.items()):
        add_connection("OO", geometry_id, id_map[source_id])

    # Close the scene hierarchy even when the source exporter omitted a root
    # connection. Cluster->Bone edges are retained above but do not count as a
    # Model hierarchy parent.
    for source in model_nodes:
        source_id = _object_id(source)
        if source_id not in existing_model_parent:
            parent_source = model_parent_ids.get(source_id, 0)
            parent_output = id_map.get(parent_source, 0) if parent_source else 0
            add_connection("OO", id_map[source_id], parent_output)

    # The source Model->Geometry edge is the Mesh identity authority. If a
    # malformed source omitted it, rebuild it from the graph we just read.
    for source_id in sorted(mesh_model_ids):
        for geometry_id in model_geometry_ids.get(source_id, []):
            if (source_id, geometry_id) not in existing_geometry_links:
                add_connection(
                    "OO", geometry_output_ids[geometry_id], id_map[source_id]
                )

    graph_check = _safe_rebuilder_validate_graph(generic_objects, generic_connections)
    if graph_check["duplicate_ids"] or graph_check["dangling_connections"]:
        # Never publish a rebuilt FBX whose object graph is internally
        # inconsistent. The embedded Probe caller can safely fall back to
        # the original bytes, while the standalone converter reports the
        # precise validation failure instead of emitting a corrupt scene.
        raise ValueError(
            "Safe Rebuilder graph validation failed: "
            f"duplicate_ids={graph_check['duplicate_ids']}, "
            f"dangling_connections={graph_check['dangling_connections']}"
        )

    # Replace producer-specific roots while retaining unrelated document metadata.
    root_by_name = {node.name: node for node in roots}
    generic_roots: list[FbxNode] = []
    preferred_order = [
        "FBXHeaderExtension", "FileId", "CreationTime", "GlobalSettings",
        "Documents", "Definitions", "Objects", "Connections", "Takes",
    ]
    header = _clone_generic_node(root_by_name.get("FBXHeaderExtension", FbxNode("FBXHeaderExtension")))
    if header is None:
        header = FbxNode("FBXHeaderExtension", [], [], [])
    header_creator = _child_node(header, "Creator")
    if header_creator is None:
        header.add("Creator", ("S", "Generic FBX Converter"))
    elif header_creator.properties:
        header_creator.properties[0] = "Generic FBX Converter"
        header_creator.property_types = ["S"]
    root_replacements = {
        "FBXHeaderExtension": header,
        "GlobalSettings": _generic_global_settings(
            root_by_name.get("GlobalSettings", FbxNode("GlobalSettings")),
            # The complete semantic rebuild above owns the source-axis
            # conversion.  Always publish the rebuilt document as canonical
            # X-right/Y-up/Z-front instead of retaining a producer branch.
            canonicalize_axes=True,
        ),
        "Definitions": _generic_definitions(generic_objects),
        "Objects": generic_objects,
        "Connections": generic_connections,
    }
    for name in preferred_order:
        if name in root_replacements:
            generic_roots.append(root_replacements[name])
        elif name in root_by_name:
            _guard_alpha_cloned_object(root_by_name[name])
            cloned = _clone_generic_node(root_by_name[name])
            if cloned is not None:
                generic_roots.append(cloned)
    for source_root in roots:
        if source_root.name in preferred_order or source_root.name == "Creator":
            continue
        _guard_alpha_cloned_object(source_root)
        cloned = _clone_generic_node(source_root)
        if cloned is not None:
            generic_roots.append(cloned)
    creator = next((node for node in generic_roots if node.name == "Creator"), None)
    if creator is None:
        generic_roots.append(FbxNode("Creator", ["Generic FBX Converter"], ["S"], []))
    elif creator.properties:
        creator.properties[0] = "Generic FBX Converter"
    unit_validation = _validate_generic_unit_conversion(roots, generic_roots, v5_context)
    return generic_roots, {
        "safe_rebuilder_status": "rebuilt",
        "canonicalization_policy": FBX_CANONICALIZATION_POLICY,
        "safe_rebuilder_object_count": len(generic_objects.children),
        "safe_rebuilder_model_count": len(model_nodes),
        "safe_rebuilder_mesh_count": len(mesh_model_ids) + len(other_mesh_placeholder_ids),
        "safe_rebuilder_bone_count": len(bone_model_ids),
        "safe_rebuilder_geometry_count": len(emitted_geometry_ids) + len(other_mesh_geometry_ids),
        "other_mesh_empty_geometry_count": len(other_mesh_geometry_ids),
        "geometry_rebuilt_count": geometry_rebuilt_count,
        "geometry_skipped_count": geometry_header_only_count,
        "geometry_skip_reasons": geometry_skip_reasons,
        "skin_geometry_domain": v5_context["geometry_domain"],
        "canonical_normal_geometry_ids": sorted(canonical_normal_geometry_ids),
        "normal_axis_domain_by_geometry_id": dict(normal_axis_domain_by_geometry_id),
        "skin_clusters_remapped": skin_clusters_remapped,
        "safe_rebuilder_deformer_count": len(deformer_nodes),
        "safe_rebuilder_pose_count": len(pose_nodes),
        "safe_rebuilder_source_connection_count": int(
            source_graph["source_connection_count"]
        ),
        "safe_rebuilder_dropped_connections": dropped_connections,
        "safe_rebuilder_dangling_connections": int(
            graph_check["dangling_connections"]
        ),
        "safe_rebuilder_id_base": 100000,
        "source_unit_scale_cm": v5_context["source_unit_scale_cm"],
        "target_unit_scale_cm": v5_context["target_unit_scale_cm"],
        "unit_factor": v5_context["unit_factor"],
        "unit_conversion_validation": unit_validation,
        "generic_parallel": dict(_LAST_GENERIC_PARALLEL_STATS),
        # Keep the source->canonical basis used by rebuilt Model rows. The
        # downstream Writer can apply its inverse without assuming every Max
        # FBX used the same axis signature.
        "source_axis_signature": _axis_signature(roots) or [],
        "axis_conversion_matrix": list(axis_conversion),
        "canonical_to_source_axis_matrix": (
            _generic_invert_row_major_matrix(axis_conversion)
            or _identity_matrix()
        ),
    }

def _generic_encode_fbx_bytes(
    version: int,
    roots: Iterable[FbxNode],
    *,
    footer_id: bytes | None = None,
) -> bytes:
    """Encode a Generic FBX tree directly to bytes; no temporary file is used."""
    roots_list = list(roots)
    if not roots_list:
        raise ValueError("Cannot encode an FBX without root nodes")
    output_version = int(version) if int(version) >= 7000 else FBX_VERSION_DEFAULT
    selected_footer_id = FBX_FOOT_ID if footer_id is None else bytes(footer_id)
    if len(selected_footer_id) != 16:
        raise ValueError("FBX footer identity must contain exactly 16 bytes")
    body_parts: list[bytes] = []
    cursor = len(FBX_MAGIC) + 4
    for index, root in enumerate(roots_list):
        encoded = _encode_node(
            root,
            cursor,
            version=output_version,
            is_last=index == len(roots_list) - 1,
        )
        body_parts.append(encoded)
        cursor += len(encoded)
    null_record = (
        FBX_NULL_RECORD_WIDE
        if output_version >= 7500
        else FBX_NULL_RECORD_NARROW
    )
    body_parts.append(null_record)
    body = b"".join(body_parts)
    before_footer_version = (
        len(FBX_MAGIC) + 4 + len(body) + len(selected_footer_id) + 4
    )
    padding = (-before_footer_version) % 16
    if padding == 0:
        padding = 16
    footer = (
        selected_footer_id
        + b"\x00" * 4
        + b"\x00" * padding
        + struct.pack("<I", output_version)
        + b"\x00" * 120
        + FBX_FOOT_MAGIC
    )
    return b"".join((FBX_MAGIC, struct.pack("<I", output_version), body, footer))
# The converter is embedded in Probe.  This request-scoped bridge keeps the
# normalized FBX in memory and never creates a Generic FBX intermediate file.
_GENERIC_FBX_MEMORY_CACHE: dict[str, tuple[int, int, _BinaryFbxDocument, dict[str, Any]]] = {}
_GENERIC_FBX_MEMORY_CACHE_MAX = 2

def _generic_prepare_fbx_bytes(source: Path) -> tuple[bytes, dict[str, Any]]:
    raw_bytes = source.read_bytes()
    # ====== GENERIC REBUILD ONLY ======
    # A failed rebuild is a hard Probe error. Returning raw bytes here would
    # silently revive the retired direct-UFBX/producer-axis path.
    version, roots, footer_id = read_fbx(raw_bytes, include_footer_id=True)
    normalization = normalize_generic_tree(roots)
    rebuilt_roots, rebuild_receipt = _safe_rebuild_generic_scene(roots)
    normalization.update(rebuild_receipt)
    normalized_bytes = _generic_encode_fbx_bytes(
        version,
        rebuilt_roots,
        footer_id=footer_id,
    )
    # Parse the generated bytes before exposing them to UFBX/Probe.
    read_fbx(normalized_bytes)
    return normalized_bytes, {
        "status": "normalized",
        "source_size": len(raw_bytes),
        "output_size": len(normalized_bytes),
        "fbx_axis_output_policy": GENERIC_AXIS_OUTPUT_POLICY,
        "axis_transform_contract": GENERIC_AXIS_TRANSFORM_SCOPE,
        "canonical_probe_schema": CANONICAL_FBX_PROBE_SCHEMA,
        "canonical_axis_domain": CANONICAL_AXIS_DOMAIN,
        "canonical_unit_domain": CANONICAL_UNIT_DOMAIN,
        "normalization": normalization,
    }


def _generic_memory_document_for_path(
    path: str | Path,
) -> tuple[_BinaryFbxDocument, dict[str, Any]]:
    source = Path(path).resolve()
    stat = source.stat()
    key = str(source)
    cached = _GENERIC_FBX_MEMORY_CACHE.get(key)
    if cached is not None:
        cached_size, cached_mtime, document, receipt = cached
        if cached_size == int(stat.st_size) and cached_mtime == int(stat.st_mtime_ns):
            checked_receipt = _require_canonical_generic_receipt(receipt)
            return document, checked_receipt
    normalized_bytes, receipt = _generic_prepare_fbx_bytes(source)
    checked_receipt = _require_canonical_generic_receipt(receipt)
    # Keep large geometry arrays lazy; the generic pass already completed its
    # own read, and Probe decodes only arrays consumed by the selected stage.
    document = _build_binary_fbx_document(
        source,
        data=normalized_bytes,
        decode_array_names=frozenset(),
    )
    if key in _GENERIC_FBX_MEMORY_CACHE:
        _GENERIC_FBX_MEMORY_CACHE.pop(key, None)
    while len(_GENERIC_FBX_MEMORY_CACHE) >= _GENERIC_FBX_MEMORY_CACHE_MAX:
        _GENERIC_FBX_MEMORY_CACHE.pop(next(iter(_GENERIC_FBX_MEMORY_CACHE)))
    _GENERIC_FBX_MEMORY_CACHE[key] = (
        int(stat.st_size),
        int(stat.st_mtime_ns),
        document,
        dict(checked_receipt),
    )
    return document, checked_receipt


def _require_canonical_generic_receipt(receipt: Any) -> dict[str, Any]:
    """Validate the one Generic-to-canonical contract at the loader boundary."""
    if not isinstance(receipt, dict):
        raise RuntimeError("Generic FBX normalization receipt is missing")
    status = str(receipt.get("status", "") or "").strip().lower()
    policy = str(receipt.get("fbx_axis_output_policy", "") or "").strip().lower()
    scope = str(receipt.get("axis_transform_contract", "") or "").strip().lower()
    schema = str(receipt.get("canonical_probe_schema", "") or "").strip()
    axis_domain = str(receipt.get("canonical_axis_domain", "") or "").strip().lower()
    unit_domain = str(receipt.get("canonical_unit_domain", "") or "").strip().lower()
    if (
        status != "normalized"
        or policy != GENERIC_AXIS_OUTPUT_POLICY
        or scope != GENERIC_AXIS_TRANSFORM_SCOPE
        or schema != CANONICAL_FBX_PROBE_SCHEMA
        or axis_domain != CANONICAL_AXIS_DOMAIN
        or unit_domain != CANONICAL_UNIT_DOMAIN
    ):
        detail = str(receipt.get("error", "") or status or "invalid_receipt")
        raise RuntimeError(
            "Generic FBX normalization is mandatory for MAX/Blender exports: "
            f"{detail}"
        )
    return dict(receipt)
# ====== END GENERIC FBX IN-MEMORY NORMALIZER (MAX + BLENDER) ======
# ====== BEGIN CANONICAL SCENE / SKIN EXTRACTION ======


def _clean_binary_fbx_object_name(value: Any) -> str:
    return str(value or "").split("\x00", 1)[0]


def _parse_max_fbx_route_handle(user_property_buffer: Any) -> int:
    """Read the temporary Max Anim Handle route marker from UDP3DSMAX."""
    text = str(user_property_buffer or "")
    matches: set[int] = set()
    for line in re.split(r"[\r\n\x00]+", text):
        match = MAX_FBX_ROUTE_USER_PROPERTY_RE.fullmatch(line)
        if match is None:
            continue
        try:
            handle = int(match.group("handle"))
        except (TypeError, ValueError, OverflowError):
            continue
        if handle > 0:
            matches.add(handle)
    return next(iter(matches)) if len(matches) == 1 else 0


def _binary_fbx_geometry_fingerprint(
    vertices: Any,
    polygon_vertex_indices: Any,
) -> str:
    """Return the raw Geometry fingerprint shared by binary FBX and UFBX."""
    if not isinstance(vertices, list) or not isinstance(polygon_vertex_indices, list):
        return ""
    if len(vertices) < 3 or len(vertices) % 3 != 0 or len(polygon_vertex_indices) < 3:
        return ""
    try:
        digest = hashlib.sha256()
        digest.update(b"PC_REHD_FBX_GEOMETRY_V1\0")
        digest.update(struct.pack("<II", len(vertices) // 3, len(polygon_vertex_indices)))
        for value in vertices:
            digest.update(struct.pack("<f", float(value)))
        for raw_index in polygon_vertex_indices:
            index = int(raw_index)
            if index < 0:
                index = ~index
            if index < 0:
                return ""
            digest.update(struct.pack("<I", index))
        return digest.hexdigest()
    except (TypeError, ValueError, OverflowError, struct.error):
        return ""


def _ufbx_mesh_geometry_fingerprint(mesh: Any) -> str:
    positions = getattr(mesh, "vertex_positions", None)
    indices = getattr(mesh, "indices", None)
    if positions is None or indices is None:
        return ""
    try:
        flat_positions = [float(value) for row in positions for value in row]
        flat_indices = [int(value) for value in indices]
    except (TypeError, ValueError, OverflowError):
        return ""
    return _binary_fbx_geometry_fingerprint(flat_positions, flat_indices)


def _binary_fbx_polygon_vertex_stream(
    polygon_vertex_indices: Any,
    *,
    position_count: int,
) -> tuple[list[int], list[tuple[int, int]], list[int]]:
    """Decode FBX polygon corners without relying on UFBX's expanded streams."""
    if not isinstance(polygon_vertex_indices, list) or len(polygon_vertex_indices) < 3:
        raise ValueError("PolygonVertexIndex is missing or too short")
    source_indices: list[int] = []
    faces: list[tuple[int, int]] = []
    corner_faces: list[int] = []
    face_begin = 0
    face_index = 0
    for corner_index, raw_value in enumerate(polygon_vertex_indices):
        try:
            raw_index = int(raw_value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"PolygonVertexIndex[{corner_index}] is not an integer") from exc
        is_face_end = raw_index < 0
        position_index = ~raw_index if is_face_end else raw_index
        if position_index < 0 or position_index >= position_count:
            raise ValueError(
                f"PolygonVertexIndex[{corner_index}] references position {position_index}, "
                f"outside 0..{position_count - 1}"
            )
        source_indices.append(position_index)
        corner_faces.append(face_index)
        if is_face_end:
            face_size = len(source_indices) - face_begin
            if face_size < 3:
                raise ValueError(f"Polygon {face_index} has fewer than three corners")
            faces.append((face_begin, face_size))
            face_begin = len(source_indices)
            face_index += 1
    if face_begin != len(source_indices):
        raise ValueError("PolygonVertexIndex does not terminate its final polygon")
    return source_indices, faces, corner_faces


def _binary_fbx_layer_text(node: _BinaryFbxNode, child_name: str) -> str:
    value = _binary_fbx_node_child_value(node, child_name)
    return str(value or "").strip()


def _binary_fbx_decode_layer_element(
    node: _BinaryFbxNode,
    *,
    value_child: str,
    index_child: str,
    tuple_size: int,
    corner_count: int,
    position_count: int,
    face_count: int,
    corner_faces: list[int],
) -> dict[str, Any]:
    """Resolve one FBX LayerElement to a direct value index for every corner.

    The mapping and reference modes are serialized by FBX itself.  They are
    deliberately interpreted here instead of inferred from array lengths.
    """
    raw_values = _binary_fbx_node_child_value(node, value_child)
    if not isinstance(raw_values, list) or len(raw_values) == 0 or len(raw_values) % tuple_size != 0:
        raise ValueError(f"{value_child} is missing or not a {tuple_size}-component array")
    try:
        values = [
            [float(raw_values[index + axis]) for axis in range(tuple_size)]
            for index in range(0, len(raw_values), tuple_size)
        ]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{value_child} contains a non-numeric value") from exc

    mapping = _binary_fbx_layer_text(node, "MappingInformationType")
    reference = _binary_fbx_layer_text(node, "ReferenceInformationType")
    mapping_key = mapping.casefold()
    reference_key = reference.casefold()
    if mapping_key == "bypolygonvertex":
        mapped_indices = list(range(corner_count))
        expected_mapping_count = corner_count
    elif mapping_key in {"byvertice", "byvertex"}:
        # FBX historically spells this ByVertice. Both spellings occur.
        mapped_indices = []
        expected_mapping_count = position_count
        for corner_index in range(corner_count):
            # The caller supplies the decoded position stream below.
            mapped_indices.append(-1)
    elif mapping_key == "bypolygon":
        mapped_indices = list(corner_faces)
        expected_mapping_count = face_count
    elif mapping_key == "allsame":
        mapped_indices = [0 for _ in range(corner_count)]
        expected_mapping_count = 1
    else:
        raise ValueError(f"unsupported MappingInformationType={mapping or '<missing>'}")

    return {
        "mapping": mapping,
        "mapping_key": mapping_key,
        "reference": reference,
        "reference_key": reference_key,
        "values": values,
        "mapped_indices": mapped_indices,
        "expected_mapping_count": expected_mapping_count,
        "index_child": index_child,
    }


def _binary_fbx_bind_layer_element_corners(
    layer: dict[str, Any],
    *,
    node: _BinaryFbxNode,
    source_indices: list[int],
) -> dict[str, Any]:
    """Bind a decoded LayerElement to actual polygon-corner direct indices."""
    mapping_key = str(layer.get("mapping_key", ""))
    values = layer.get("values")
    mapped_indices = list(layer.get("mapped_indices", []))
    if not isinstance(values, list) or len(mapped_indices) != len(source_indices):
        raise ValueError("LayerElement mapping rows do not align with PolygonVertexIndex")
    if mapping_key in {"byvertice", "byvertex"}:
        mapped_indices = list(source_indices)

    reference_key = str(layer.get("reference_key", ""))
    expected_mapping_count = int(layer.get("expected_mapping_count", 0))
    if reference_key == "direct":
        direct_indices = mapped_indices
    elif reference_key == "indextodirect":
        raw_indices = _binary_fbx_node_child_value(node, str(layer.get("index_child", "")))
        if not isinstance(raw_indices, list) or len(raw_indices) != expected_mapping_count:
            raise ValueError(
                f"{layer.get('index_child', 'Index')} has {len(raw_indices) if isinstance(raw_indices, list) else 0} "
                f"rows; expected {expected_mapping_count} for {layer.get('mapping', '<missing>')}"
            )
        direct_indices = []
        for corner_index, mapped_index in enumerate(mapped_indices):
            if mapped_index < 0 or mapped_index >= len(raw_indices):
                raise ValueError(f"LayerElement mapping index {mapped_index} is invalid at corner {corner_index}")
            try:
                direct_indices.append(int(raw_indices[mapped_index]))
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"LayerElement index row {mapped_index} is not an integer") from exc
    else:
        raise ValueError(
            f"unsupported ReferenceInformationType={layer.get('reference', '') or '<missing>'}"
        )

    for corner_index, direct_index in enumerate(direct_indices):
        if direct_index < 0 or direct_index >= len(values):
            raise ValueError(
                f"LayerElement direct index {direct_index} is invalid at corner {corner_index}; "
                f"value count is {len(values)}"
            )
    result = dict(layer)
    result["corner_indices"] = direct_indices
    result.pop("mapped_indices", None)
    result.pop("index_child", None)
    result.pop("expected_mapping_count", None)
    return result


def _binary_fbx_read_corner_geometry(node: _BinaryFbxNode) -> dict[str, Any]:
    """Read the authored polygon-corner channels from one binary FBX Geometry."""
    raw_positions = _binary_fbx_node_child_value(node, "Vertices")
    raw_polygon_indices = _binary_fbx_node_child_value(node, "PolygonVertexIndex")
    if not isinstance(raw_positions, list) or len(raw_positions) < 3 or len(raw_positions) % 3 != 0:
        raise ValueError("Vertices is missing or not a three-component array")
    position_count = len(raw_positions) // 3
    source_indices, faces, corner_faces = _binary_fbx_polygon_vertex_stream(
        raw_polygon_indices,
        position_count=position_count,
    )
    fingerprint = _binary_fbx_geometry_fingerprint(raw_positions, raw_polygon_indices)
    if fingerprint == "":
        raise ValueError("Geometry fingerprint could not be calculated")

    result: dict[str, Any] = {
        "geometry_id": int(node.properties[0]) if node.properties and isinstance(node.properties[0], int) else 0,
        "geometry_name": _clean_binary_fbx_object_name(node.properties[1]) if len(node.properties) > 1 else "",
        "fingerprint": fingerprint,
        "position_count": position_count,
        "source_indices": source_indices,
        "faces": faces,
        "corner_count": len(source_indices),
        "normal": None,
        "uv_channels": [],
        "uv_errors": [],
        "strict_error": "",
    }

    normal_nodes = [child for child in node.children if child.name == "LayerElementNormal"]
    if len(normal_nodes) != 1:
        result["strict_error"] = (
            "normal_layer_missing" if len(normal_nodes) == 0 else "normal_layer_ambiguous"
        )
        return result
    try:
        normal = _binary_fbx_decode_layer_element(
            normal_nodes[0],
            value_child="Normals",
            index_child="NormalsIndex",
            tuple_size=3,
            corner_count=len(source_indices),
            position_count=position_count,
            face_count=len(faces),
            corner_faces=corner_faces,
        )
        result["normal"] = _binary_fbx_bind_layer_element_corners(
            normal,
            node=normal_nodes[0],
            source_indices=source_indices,
        )
    except Exception as exc:
        result["strict_error"] = f"normal_layer_invalid: {exc}"
        return result

    for uv_layer_index, uv_node in enumerate(
        child for child in node.children if child.name == "LayerElementUV"
    ):
        try:
            uv = _binary_fbx_decode_layer_element(
                uv_node,
                value_child="UV",
                index_child="UVIndex",
                tuple_size=2,
                corner_count=len(source_indices),
                position_count=position_count,
                face_count=len(faces),
                corner_faces=corner_faces,
            )
            uv = _binary_fbx_bind_layer_element_corners(
                uv,
                node=uv_node,
                source_indices=source_indices,
            )
            uv["channel"] = uv_layer_index + 1
            uv["name"] = _binary_fbx_layer_text(uv_node, "Name") or f"map{uv_layer_index + 1}"
            result["uv_channels"].append(uv)
        except Exception as exc:
            # UV channels are optional for normal fidelity.  A missing Map 2
            # (or an empty optional UV layer emitted beside Map 1) must not
            # downgrade the already decoded corner normalId stream to the
            # legacy vertex-normal path.
            result["uv_errors"].append(
                f"uv_layer_{uv_layer_index + 1}_invalid: {exc}"
            )
            continue
    return result


def _build_binary_fbx_corner_geometry_context(
    path: Path,
    *,
    binary_document: _BinaryFbxDocument | None = None,
    target_geometry_ids: set[int] | None = None,
    target_geometry_names: set[str] | None = None,
    target_filter_active: bool = False,
) -> dict[str, Any]:
    """Build an exact Geometry fingerprint lookup for authored corner channels.

    This is advisory at the file level: a malformed Geometry must not block
    other Meshes or the export itself. Each Mesh receives its own audit record.
    """
    context: dict[str, Any] = {
        "by_fingerprint": {},
        "by_geometry_id": {},
        "status": "available",
        "error": "",
    }
    try:
        roots = _read_binary_fbx_roots(
            path,
            binary_document=binary_document,
            decode_array_names=frozenset(
                {
                    "Vertices",
                    "PolygonVertexIndex",
                    "Normals",
                    "NormalsIndex",
                    "UV",
                    "UVIndex",
                }
            ),
        )
        objects_root = next((node for node in roots if node.name == "Objects"), None)
        if objects_root is None:
            raise ValueError("Objects node is missing")
        for node in objects_root.children:
            if node.name != "Geometry":
                continue
            geometry_id = int(node.properties[0]) if node.properties and isinstance(node.properties[0], int) else 0
            geometry_name = _clean_binary_fbx_object_name(node.properties[1]) if len(node.properties) > 1 else ""
            if target_filter_active:
                id_match = geometry_id > 0 and geometry_id in (target_geometry_ids or set())
                name_match = normalize_match_name(geometry_name) in (target_geometry_names or set())
                if not id_match and not name_match:
                    continue
            try:
                geometry = _binary_fbx_read_corner_geometry(node)
            except Exception as exc:
                # No fingerprint means it cannot be safely paired. Record this
                # at file level and leave compatible UFBX extraction available.
                context["error"] = str(exc)
                continue
            fingerprint = str(geometry.get("fingerprint", ""))
            if fingerprint != "":
                context["by_fingerprint"].setdefault(fingerprint, []).append(geometry)
            geometry_id = int(geometry.get("geometry_id", 0) or 0)
            if geometry_id > 0:
                context["by_geometry_id"].setdefault(geometry_id, []).append(geometry)
    except Exception as exc:
        context["status"] = "unavailable"
        context["error"] = f"binary_corner_reader_error: {type(exc).__name__}: {exc}"
    return context


def _binary_fbx_normal_fidelity_audit(
    *,
    status: str,
    reason: str = "",
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": BINARY_FBX_NORMAL_FIDELITY_SCHEMA,
        "status": status,
        "reason": reason,
        "mode": "binary_fbx_corner_layer" if status == "exact" else "legacy_ufbx_compatibility",
    }
    if isinstance(geometry, dict):
        normal = geometry.get("normal") if isinstance(geometry.get("normal"), dict) else {}
        payload.update(
            {
                "geometry_id": int(geometry.get("geometry_id", 0) or 0),
                "geometry_name": str(geometry.get("geometry_name", "") or ""),
                "input_position_count": int(geometry.get("position_count", 0) or 0),
                "input_corner_count": int(geometry.get("corner_count", 0) or 0),
                "normal_mapping": str(normal.get("mapping", "") or ""),
                "normal_reference": str(normal.get("reference", "") or ""),
                "normal_value_count": len(normal.get("values", [])) if isinstance(normal.get("values"), list) else 0,
                "normal_index_count": len(normal.get("corner_indices", [])) if isinstance(normal.get("corner_indices"), list) else 0,
                "uv_channel_count": len(geometry.get("uv_channels", [])) if isinstance(geometry.get("uv_channels"), list) else 0,
            }
        )
    return payload


def _disable_ufbx_normal_sources(audit: dict[str, Any]) -> dict[str, Any]:
    """Mark the legacy UFBX normal streams as intentionally unused."""
    payload = dict(audit)
    if str(payload.get("status", "") or "") != "exact":
        payload.update(
            {
                "normal_source": "disabled_ufbx_vertex_normals",
                "skinned_normal_source": "disabled_ufbx_skinned_normals",
            }
        )
    return payload


def _select_binary_fbx_corner_geometry(
    mesh: Any,
    context: dict[str, Any] | None,
    binary_identity: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not isinstance(context, dict) or context.get("status") != "available":
        reason = str(context.get("error", "") if isinstance(context, dict) else "") or "binary_corner_reader_unavailable"
        return None, _binary_fbx_normal_fidelity_audit(status="fallback", reason=reason)
    fingerprint = _ufbx_mesh_geometry_fingerprint(mesh)
    geometry_id = int(
        binary_identity.get("fbx_geometry_id", 0)
        if isinstance(binary_identity, dict)
        else 0
    )
    if geometry_id > 0:
        candidates = context.get("by_geometry_id", {}).get(geometry_id, [])
        if len(candidates) != 1:
            reason = "raw_geometry_id_not_found" if len(candidates) == 0 else "raw_geometry_id_ambiguous"
            return None, _binary_fbx_normal_fidelity_audit(status="fallback", reason=reason)
        geometry = candidates[0]
        selected_fingerprint = str(geometry.get("fingerprint", "") or "")
        if fingerprint != "" and selected_fingerprint != "" and fingerprint != selected_fingerprint:
            return None, _binary_fbx_normal_fidelity_audit(
                status="fallback",
                reason="raw_geometry_identity_fingerprint_mismatch",
                geometry=geometry,
            )
    else:
        if fingerprint == "":
            return None, _binary_fbx_normal_fidelity_audit(
                status="fallback",
                reason="ufbx_geometry_fingerprint_unavailable",
            )
        candidates = context.get("by_fingerprint", {}).get(fingerprint, [])
        if len(candidates) != 1:
            reason = "raw_geometry_not_found" if len(candidates) == 0 else "raw_geometry_ambiguous"
            return None, _binary_fbx_normal_fidelity_audit(status="fallback", reason=reason)
        geometry = candidates[0]
    strict_error = str(geometry.get("strict_error", "") or "")
    if strict_error != "":
        return None, _binary_fbx_normal_fidelity_audit(
            status="fallback",
            reason=strict_error,
            geometry=geometry,
        )
    return geometry, _binary_fbx_normal_fidelity_audit(status="exact", geometry=geometry)


def _binary_fbx_model_identity_context(
    path: Path,
    *,
    binary_document: _BinaryFbxDocument | None = None,
    target_handles: set[int] | None = None,
    target_names: set[str] | None = None,
    target_slots: set[int] | None = None,
    target_filter_active: bool = False,
    explicit_route_required: bool = False,
) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], list[dict[str, Any]]]:
    """Read Max FBX Model identity and the explicit Anim Handle route marker.

    FBX ``MaxHandle`` is an exporter-local serialization ID, not the Max
    scene's Anim Handle. The Launcher therefore places its temporary route
    marker in ``Model/Properties70/UDP3DSMAX`` before export. This sidecar
    pairs that marker with UFBX nodes without collapsing duplicate display
    names.
    """
    roots = _read_binary_fbx_roots(
        path,
        binary_document=binary_document,
        decode_array_names=frozenset(
            {"Indexes", "Weights", "Transform", "TransformLink", "Vertices", "PolygonVertexIndex"}
        ),
    )
    objects_root = next((node for node in roots if node.name == "Objects"), None)
    connections_root = next((node for node in roots if node.name == "Connections"), None)
    if objects_root is None:
        return {}, []

    models: dict[int, dict[str, Any]] = {}
    route_models: dict[int, dict[str, Any]] = {}
    geometry_nodes: dict[int, _BinaryFbxNode] = {}
    for node in objects_root.children:
        if node.name == "Geometry" and node.properties and isinstance(node.properties[0], int):
            geometry_nodes[int(node.properties[0])] = node
        if node.name != "Model" or len(node.properties) < 3:
            continue
        model_id = node.properties[0]
        if not isinstance(model_id, int):
            continue
        model_name = _clean_binary_fbx_object_name(node.properties[1])
        model_type = str(node.properties[2] or "")
        max_handle = 0
        route_handle = 0
        for container in node.children:
            if container.name != "Properties70":
                continue
            for property_node in container.children:
                values = property_node.properties
                if property_node.name != "P" or not values:
                    continue
                property_name = str(values[0] or "")
                if property_name == "MaxHandle" and len(values) >= 5:
                    try:
                        max_handle = int(values[4])
                    except (TypeError, ValueError, OverflowError):
                        max_handle = 0
                elif property_name == "UDP3DSMAX" and len(values) >= 5:
                    route_handle = _parse_max_fbx_route_handle(values[-1])
        model_identity = {
            "fbx_model_id": int(model_id),
            "fbx_model_name": model_name,
            "fbx_model_type": model_type,
            "fbx_max_handle": max_handle if max_handle > 0 else 0,
            "fbx_route_handle": route_handle if route_handle > 0 else 0,
            "fbx_route_protocol": (
                "CodexRe6FbxRouteHandle/UDP3DSMAX" if route_handle > 0 else ""
            ),
            "fbx_parent_model_id": 0,
            "fbx_parent_name": "",
            "fbx_geometry_id": 0,
            "fbx_geometry_fingerprint": "",
        }
        if route_handle > 0:
            route_models[int(model_id)] = {
                **model_identity,
                "fbx_geometry_connected": False,
            }
        if model_type.casefold() == "mesh" and model_name != "":
            models[int(model_id)] = model_identity

    geometry_ids_by_model: dict[int, list[int]] = {}
    if connections_root is not None:
        for node in connections_root.children:
            values = node.properties
            if node.name != "C" or len(values) < 3 or values[0] != "OO":
                continue
            child_id, parent_id = values[1], values[2]
            if not isinstance(child_id, int):
                continue
            if not isinstance(parent_id, int):
                continue
            if int(child_id) in models:
                models[int(child_id)]["fbx_parent_model_id"] = int(parent_id)
                parent = models.get(int(parent_id))
                if parent is not None:
                    models[int(child_id)]["fbx_parent_name"] = str(
                        parent.get("fbx_model_name", "") or ""
                    )
            if int(parent_id) in models and int(child_id) in geometry_nodes:
                geometry_ids_by_model.setdefault(int(parent_id), []).append(int(child_id))
            if int(parent_id) in route_models and int(child_id) in geometry_nodes:
                route_models[int(parent_id)]["fbx_geometry_connected"] = True

    for model_id, row in models.items():
        geometry_ids = list(dict.fromkeys(geometry_ids_by_model.get(model_id, [])))
        if len(geometry_ids) == 1:
            geometry_id = geometry_ids[0]
            row["fbx_geometry_id"] = geometry_id
        elif len(geometry_ids) > 1:
            # A Model linked to multiple Geometry objects has no single raw
            # identity. Leave the ID empty so the existing non-blocking
            # fingerprint fallback decides whether a unique match exists.
            row["fbx_geometry_connection_count"] = len(geometry_ids)

    normalized_handles = {
        _int_or_default(value, 0)
        for value in (target_handles or set())
        if _int_or_default(value, 0) > 0
    }
    normalized_names = {
        normalize_match_name(value)
        for value in (target_names or set())
        if normalize_match_name(value)
    }
    normalized_slots = {
        _int_or_default(value, 0)
        for value in (target_slots or set())
        if _int_or_default(value, 0) > 0
    }
    selected_model_ids: set[int] = set()
    for model_id, row in models.items():
        model_name = str(row.get("fbx_model_name", "") or "")
        slot_hint = infer_mesh_slot_hint(model_name)
        slot = (
            _int_or_default(slot_hint.get("slot"), 0)
            if isinstance(slot_hint, dict)
            else 0
        )
        if explicit_route_required:
            # MAX duplicate names are not identity.  Decode only Models whose
            # FBX marker is one of the Launcher route Handles; unmarked
            # siblings remain lightweight candidates for identity exclusion.
            selected = (
                _int_or_default(row.get("fbx_route_handle"), 0) in normalized_handles
            )
        else:
            selected = not target_filter_active or (
                _int_or_default(row.get("fbx_route_handle"), 0) in normalized_handles
                or normalize_match_name(model_name) in normalized_names
                or slot in normalized_slots
            )
        geometry_id = _int_or_default(row.get("fbx_geometry_id"), 0)
        geometry_node = geometry_nodes.get(geometry_id)
        if not selected or geometry_node is None:
            continue
        selected_model_ids.add(int(model_id))

    queues: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for model_id in sorted(models):
        row = models[model_id]
        key = (
            str(row["fbx_model_name"]).casefold(),
            str(row["fbx_parent_name"]).casefold(),
        )
        queues.setdefault(key, []).append(row)
    # Geometry fingerprints are needed only when identity lookup is
    # ambiguous (duplicate name/parent keys or duplicate bare names).  A
    # unique Model identity is already sufficient and must not decode its
    # large Vertices/PolygonVertexIndex arrays during Stage A.
    rows_by_name: dict[str, list[dict[str, Any]]] = {}
    for rows in queues.values():
        for row in rows:
            rows_by_name.setdefault(
                normalize_match_name(row.get("fbx_model_name", "")), []
            ).append(row)
    for key, rows in queues.items():
        ambiguous_key = len(rows) > 1
        for row in rows:
            model_id = _int_or_default(row.get("fbx_model_id"), 0)
            model_name_key = normalize_match_name(row.get("fbx_model_name", ""))
            if (
                model_id not in selected_model_ids
                or not model_name_key
                or not (ambiguous_key or len(rows_by_name.get(model_name_key, [])) > 1)
            ):
                continue
            geometry_id = _int_or_default(row.get("fbx_geometry_id"), 0)
            geometry_node = geometry_nodes.get(geometry_id)
            if geometry_node is None:
                continue
            # This is deliberately the only Stage-A path that may decode
            # authored Geometry arrays, and only for an ambiguous identity.
            row["fbx_geometry_fingerprint"] = _binary_fbx_geometry_fingerprint(
                _binary_fbx_node_child_value(geometry_node, "Vertices"),
                _binary_fbx_node_child_value(geometry_node, "PolygonVertexIndex"),
            )
    observations = [route_models[model_id] for model_id in sorted(route_models)]
    return queues, observations


def _binary_fbx_mesh_model_identity_queues(
    path: Path,
    *,
    binary_document: _BinaryFbxDocument | None = None,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    queues, _observations = _binary_fbx_model_identity_context(
        path,
        binary_document=binary_document,
    )
    return queues


def extract_fbx_route_model_observations(path: str | Path) -> list[dict[str, Any]]:
    """Return route-marked FBX Model facts without making export decisions."""
    fbx_path = Path(path).resolve()
    binary_document, _receipt = _generic_memory_document_for_path(fbx_path)
    _queues, observations = _binary_fbx_model_identity_context(
        fbx_path,
        binary_document=binary_document,
    )
    return observations


def _take_binary_fbx_mesh_model_identity(
    queues: dict[tuple[str, str], list[dict[str, Any]]],
    instance_node: Any | None,
    *,
    preferred_route_handles: set[int] | None = None,
) -> dict[str, Any]:
    if instance_node is None:
        return {}
    node_name = str(getattr(instance_node, "name", "") or "")
    parent_name = str(getattr(getattr(instance_node, "parent", None), "name", "") or "")
    key = (node_name.casefold(), parent_name.casefold())
    rows = queues.get(key)
    if rows and len(rows) == 1:
        return dict(rows.pop(0))
    if rows and preferred_route_handles:
        preferred = [
            row
            for row in rows
            if _int_or_default(row.get("fbx_route_handle"), 0) in preferred_route_handles
        ]
        if len(preferred) == 1:
            rows.remove(preferred[0])
            return dict(preferred[0])
    geometry_fingerprint = ""
    if rows and len(rows) > 1:
        geometry_fingerprint = _ufbx_mesh_geometry_fingerprint(
            getattr(instance_node, "mesh", None)
        )
    if rows and geometry_fingerprint:
        matches = [
            row
            for row in rows
            if str(row.get("fbx_geometry_fingerprint", "") or "") == geometry_fingerprint
        ]
        if len(matches) == 1:
            rows.remove(matches[0])
            return dict(matches[0])
    # Max can omit a Null parent in selected-only FBX exports.  A bare-name
    # fallback remains safe when geometry identifies exactly one Model row.
    candidate_keys = [candidate for candidate in queues if candidate[0] == node_name.casefold()]
    candidate_rows = [row for candidate in candidate_keys for row in queues[candidate]]
    if len(candidate_rows) == 1:
        row = candidate_rows[0]
        for candidate in candidate_keys:
            if row in queues[candidate]:
                queues[candidate].remove(row)
                break
        return dict(row)
    if candidate_rows and not geometry_fingerprint:
        geometry_fingerprint = _ufbx_mesh_geometry_fingerprint(
            getattr(instance_node, "mesh", None)
        )
    if geometry_fingerprint:
        matches = [
            row
            for row in candidate_rows
            if str(row.get("fbx_geometry_fingerprint", "") or "") == geometry_fingerprint
        ]
        if len(matches) == 1:
            row = matches[0]
            for candidate in candidate_keys:
                if row in queues[candidate]:
                    queues[candidate].remove(row)
                    break
            return dict(row)
    return {}


def _clone_binary_fbx_model_identity_queues(
    queues: dict[tuple[str, str], list[dict[str, Any]]] | None,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Clone identity rows so route discovery cannot consume final rows."""
    if not isinstance(queues, dict):
        return {}
    return {
        key: [dict(row) for row in rows if isinstance(row, dict)]
        for key, rows in queues.items()
        if isinstance(rows, list)
    }


def _binary_fbx_target_selector(
    queues: dict[tuple[str, str], list[dict[str, Any]]] | None,
    *,
    target_handles: set[int],
    target_names: set[str],
    target_slots: set[int],
) -> tuple[set[int], set[str], set[str]]:
    """Resolve requested export identities before decoding exact Geometry."""
    geometry_ids: set[int] = set()
    geometry_names: set[str] = set()
    mesh_names: set[str] = set()
    for rows in (queues or {}).values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            route_handle = _int_or_default(row.get("fbx_route_handle"), 0)
            model_name = str(row.get("fbx_model_name", "") or "")
            model_name_key = normalize_match_name(model_name)
            slot_hint = infer_mesh_slot_hint(model_name)
            slot = _int_or_default(slot_hint.get("slot"), 0) if isinstance(slot_hint, dict) else 0
            selected = (
                route_handle > 0
                and route_handle in target_handles
            ) or (
                model_name_key != ""
                and model_name_key in target_names
            ) or (
                slot > 0
                and slot in target_slots
            )
            if not selected:
                continue
            geometry_id = _int_or_default(row.get("fbx_geometry_id"), 0)
            if geometry_id > 0:
                geometry_ids.add(geometry_id)
            if model_name_key:
                geometry_names.add(model_name_key)
                mesh_names.add(model_name_key)
    # A Blender export may not carry the Max UDP route marker.  The caller's
    # stable scene names remain a bounded fallback for Geometry selection.
    geometry_names.update(target_names)
    mesh_names.update(target_names)
    return geometry_ids, geometry_names, mesh_names


def _probe_mesh_triangle_count(mesh: Any) -> int:
    triangle_count = int(getattr(mesh, "num_triangles", 0) or 0)
    if triangle_count > 0:
        return triangle_count
    total = 0
    for face in _safe_list(getattr(mesh, "faces", None)):
        try:
            total += max(0, int(face[1]) - 2)
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
    return total


def _probe_mesh_has_skin(mesh: Any) -> bool:
    return len(_safe_list(getattr(mesh, "skin_deformers", None))) > 0


def _probe_stage_a_mesh_target(
    mesh: Any,
    instance_node: Any | None,
    binary_identity: dict[str, Any],
    *,
    target_handles: set[int],
    target_names: set[str],
    target_slots: set[int],
    filter_active: bool,
) -> bool:
    if not filter_active:
        return True
    route_handle = _int_or_default(binary_identity.get("fbx_route_handle"), 0)
    if route_handle > 0 and route_handle in target_handles:
        return True
    for value in (
        getattr(instance_node, "name", "") if instance_node is not None else "",
        getattr(mesh, "name", ""),
        binary_identity.get("fbx_model_name", ""),
    ):
        if normalize_match_name(value) in target_names:
            return True
    for value in (
        infer_mesh_slot_hint(getattr(instance_node, "name", "") if instance_node is not None else ""),
        infer_mesh_slot_hint(getattr(mesh, "name", "")),
    ):
        if isinstance(value, dict) and _int_or_default(value.get("slot"), 0) in target_slots:
            return True
    return False


def _build_lightweight_mesh_geometry(
    mesh: Any,
    *,
    normal_fidelity: dict[str, Any],
    vertex_count: int | None = None,
    index_count: int | None = None,
    triangle_count: int | None = None,
) -> dict[str, Any]:
    """Return route-only facts without allocating authored vertex rows."""
    if vertex_count is None:
        vertex_count = int(getattr(mesh, "num_vertices", 0) or 0)
    if index_count is None:
        index_count = int(getattr(mesh, "num_indices", 0) or 0)
    if triangle_count is None:
        triangle_count = _probe_mesh_triangle_count(mesh)
    return {
        "positions": [],
        "max_positions": [],
        "world_positions": [],
        "skinned_positions": [],
        "skinned_max_positions": [],
        "skinned_world_positions": [],
        "normals": [],
        "max_normals": [],
        "skinned_normals": [],
        "skinned_max_normals": [],
        "skinned_is_local": False,
        "uvs": [],
        "face_indices": [],
        "source_vertex_indices": [],
        "fbx_export_corner_indices": [],
        "fbx_geom_face_indices": [],
        "fbx_export_face_indices": [],
        "fbx_uv_channels": [],
        "vertex_count": vertex_count,
        "index_count": index_count,
        "triangle_count": triangle_count,
        "normal_fidelity": dict(normal_fidelity),
        "fbx_skin_pose_evaluation_status": "stage_a_route_discovery",
    }


def _build_lightweight_skin_summary(
    mesh: Any,
    *,
    has_skin: bool | None = None,
) -> dict[str, Any]:
    """Expose only Skin presence for Stage A route discovery."""
    if has_skin is None:
        has_skin = _probe_mesh_has_skin(mesh)
    return {
        "skin_deformer_count": 1 if has_skin else 0,
        "bone_names": [],
        "max_weights_per_vertex": 0,
        "weighted_vertex_count": 0,
        "weight_rows_available": False,
    }


def _binary_fbx_matrix(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 16:
        return None
    try:
        matrix = [float(item) for item in value]
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(item) for item in matrix):
        return None
    return matrix


def _multiply_row_major_matrices(left: list[float], right: list[float]) -> list[float]:
    return [
        sum(left[row * 4 + item] * right[item * 4 + column] for item in range(4))
        for row in range(4)
        for column in range(4)
    ]


def _invert_row_major_matrix(matrix: list[float]) -> list[float] | None:
    if len(matrix) != 16:
        return None
    work = [
        [float(matrix[row * 4 + column]) for column in range(4)]
        + [1.0 if row == column else 0.0 for column in range(4)]
        for row in range(4)
    ]
    for pivot_column in range(4):
        pivot_row = max(range(pivot_column, 4), key=lambda row: abs(work[row][pivot_column]))
        if abs(work[pivot_row][pivot_column]) <= 0.000000000001:
            return None
        work[pivot_column], work[pivot_row] = work[pivot_row], work[pivot_column]
        pivot = work[pivot_column][pivot_column]
        work[pivot_column] = [value / pivot for value in work[pivot_column]]
        for row in range(4):
            if row == pivot_column:
                continue
            factor = work[row][pivot_column]
            work[row] = [value - factor * pivot_value for value, pivot_value in zip(work[row], work[pivot_column])]
    return [work[row][column] for row in range(4) for column in range(4, 8)]


def _row_major_matrices_match(left: list[float], right: list[float]) -> bool:
    if len(left) != 16 or len(right) != 16:
        return False
    magnitude = max(1.0, *(abs(value) for value in left), *(abs(value) for value in right))
    return max(abs(left[index] - right[index]) for index in range(16)) <= (magnitude * 0.00001)


def _shared_cluster_prebind(bindings: list[dict[str, Any]]) -> list[float] | None:
    """Return a common Cluster bind basis only when every Cluster agrees."""
    if not bindings:
        return None
    candidate = bindings[0].get("max_prebind")
    if not isinstance(candidate, list) or len(candidate) != 16:
        return None
    if not all(
        isinstance(binding.get("max_prebind"), list)
        and _row_major_matrices_match(candidate, binding["max_prebind"])
        for binding in bindings
    ):
        return None
    return list(candidate)


def _build_binary_fbx_skin_clusters(
    path: Path,
    *,
    binary_document: _BinaryFbxDocument | None = None,
    target_mesh_names: set[str] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], str]:
    try:
        roots = _read_binary_fbx_roots(path, binary_document=binary_document)
    except Exception as exc:
        return {}, f"binary_parse_error:{type(exc).__name__}:{exc}"
    objects_root = next((node for node in roots if node.name == "Objects"), None)
    connections_root = next((node for node in roots if node.name == "Connections"), None)
    if objects_root is None or connections_root is None:
        return {}, "binary_graph_missing_objects_or_connections"

    objects_by_id: dict[int, _BinaryFbxNode] = {}
    for node in objects_root.children:
        if not node.properties or not isinstance(node.properties[0], int):
            continue
        objects_by_id[int(node.properties[0])] = node
    parents_by_child: dict[int, list[int]] = {}
    children_by_parent: dict[int, list[int]] = {}
    for node in connections_root.children:
        values = node.properties
        if node.name != "C" or len(values) < 3 or values[0] != "OO":
            continue
        child_id, parent_id = values[1], values[2]
        if not isinstance(child_id, int) or not isinstance(parent_id, int):
            continue
        parents_by_child.setdefault(int(child_id), []).append(int(parent_id))
        children_by_parent.setdefault(int(parent_id), []).append(int(child_id))

    def object_type_matches(object_id: int, node_name: str, class_name: str) -> bool:
        node = objects_by_id.get(object_id)
        return bool(
            node is not None
            and node.name == node_name
            and len(node.properties) >= 3
            and str(node.properties[2]) == class_name
        )

    normalized_target_mesh_names = (
        None
        if target_mesh_names is None
        else {
            normalize_match_name(value)
            for value in target_mesh_names
            if normalize_match_name(value)
        }
    )
    # Keep each connected Mesh Model separate. Display names are not unique
    # in exported scenes, and merging two Models under one name mixes their
    # Cluster matrices before the bind-convention check.
    result: dict[str, list[dict[str, Any]]] = {}
    mesh_name_counts: dict[str, int] = {}
    for node in objects_root.children:
        if (
            node.name == "Model"
            and len(node.properties) >= 3
            and str(node.properties[2]) == "Mesh"
        ):
            model_name = _clean_binary_fbx_object_name(node.properties[1])
            if model_name:
                name_key = model_name.casefold()
                mesh_name_counts[name_key] = mesh_name_counts.get(name_key, 0) + 1
    geometry_fingerprint_cache: dict[int, str] = {}

    def geometry_fingerprint(geometry_id: int) -> str:
        cached = geometry_fingerprint_cache.get(geometry_id)
        if cached is not None:
            return cached
        geometry_node = objects_by_id.get(geometry_id)
        fingerprint = (
            _binary_fbx_geometry_fingerprint(
                _binary_fbx_node_child_value(geometry_node, "Vertices"),
                _binary_fbx_node_child_value(geometry_node, "PolygonVertexIndex"),
            )
            if geometry_node is not None
            else ""
        )
        geometry_fingerprint_cache[geometry_id] = fingerprint
        return fingerprint
    for cluster_id, cluster in objects_by_id.items():
        if not object_type_matches(cluster_id, "Deformer", "Cluster"):
            continue
        bone_ids = [
            object_id
            for object_id in children_by_parent.get(cluster_id, [])
            if object_type_matches(object_id, "Model", "LimbNode")
        ]
        skin_ids = [
            object_id
            for object_id in parents_by_child.get(cluster_id, [])
            if object_type_matches(object_id, "Deformer", "Skin")
        ]
        if len(bone_ids) != 1 or len(skin_ids) != 1:
            continue
        geometry_ids = [
            object_id
            for object_id in parents_by_child.get(skin_ids[0], [])
            if objects_by_id.get(object_id) is not None and objects_by_id[object_id].name == "Geometry"
        ]
        if len(geometry_ids) != 1:
            continue
        mesh_model_ids = list(dict.fromkeys(
            [
            object_id
            for object_id in parents_by_child.get(geometry_ids[0], [])
            if object_type_matches(object_id, "Model", "Mesh")
            ]
        ))
        # FBX permits multiple Mesh Models to instance the same Geometry.
        # The Skin/Cluster facts belong to that shared Geometry and must be
        # exposed to every connected Mesh Model; rejecting this graph drops
        # the posed data for an otherwise valid duplicate-name Max node.
        if not mesh_model_ids:
            continue
        bone_model = objects_by_id[bone_ids[0]]
        if len(bone_model.properties) < 2:
            continue
        bone_name = _clean_binary_fbx_object_name(bone_model.properties[1])
        if bone_name == "":
            continue
        transform = _binary_fbx_matrix(
            _binary_fbx_node_child_value(cluster, "Transform")
        )
        transform_link = _binary_fbx_matrix(
            _binary_fbx_node_child_value(cluster, "TransformLink")
        )
        indexes = _binary_fbx_node_child_value(cluster, "Indexes")
        weights = _binary_fbx_node_child_value(cluster, "Weights")
        if (
            transform is None
            or transform_link is None
            or not isinstance(indexes, list)
            or not isinstance(weights, list)
            or len(indexes) != len(weights)
        ):
            continue
        try:
            parsed_indexes = [int(value) for value in indexes]
            parsed_weights = [float(value) for value in weights]
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(value) and value >= 0.0 for value in parsed_weights):
            continue
        for mesh_model_id in mesh_model_ids:
            mesh_model = objects_by_id.get(mesh_model_id)
            if mesh_model is None or len(mesh_model.properties) < 2:
                continue
            mesh_name = _clean_binary_fbx_object_name(mesh_model.properties[1])
            if mesh_name == "":
                continue
            if (
                normalized_target_mesh_names is not None
                and normalize_match_name(mesh_name) not in normalized_target_mesh_names
            ):
                continue
            geometry_fingerprint_value = (
                geometry_fingerprint(geometry_ids[0])
                if mesh_name_counts.get(mesh_name.casefold(), 0) > 1
                else ""
            )
            mesh_key = f"{mesh_name}\x00{int(mesh_model_id)}"
            result.setdefault(mesh_key, []).append(
                {
                    "bone_name": bone_name,
                    "indexes": parsed_indexes,
                    "weights": parsed_weights,
                    "transform": transform,
                    "transform_link": transform_link,
                    "mesh_model_id": int(mesh_model_id),
                    "geometry_id": int(geometry_ids[0]),
                    "geometry_fingerprint": geometry_fingerprint_value,
                    "mesh_name": mesh_name,
                }
            )
    return result, "ok"


def _build_binary_fbx_skin_evaluation_context(
    path: Path,
    scene: Any,
    *,
    binary_document: _BinaryFbxDocument | None = None,
    target_mesh_names: set[str] | None = None,
    use_global_axis_domain: bool = False,
) -> dict[str, Any]:
    clusters_by_mesh, graph_status = _build_binary_fbx_skin_clusters(
        path,
        binary_document=binary_document,
        target_mesh_names=target_mesh_names,
    )
    context: dict[str, Any] = {
        "schema": FBX_BINARY_SKIN_EVALUATION_SCHEMA,
        "status": graph_status,
        "axis_domain": FBX_NORMAL_AXIS_DOMAIN_CANONICAL,
        "unit_domain": "max_inches_numeric",
        "evaluation_policy": "fbx_standard_mesh_bind_inverse_bone_bind_current_bone",
        "global_axis_domain_requested": bool(use_global_axis_domain),
        "meshes": {},
    }
    if graph_status != "ok":
        return context

    nodes_by_name = {
        str(getattr(node, "name", "") or ""): node
        for node in _safe_list(getattr(scene, "nodes", None))
        if str(getattr(node, "name", "") or "") != ""
    }
    mesh_nodes_by_name: dict[str, list[Any]] = {}
    for node in _safe_list(getattr(scene, "nodes", None)):
        node_name = str(getattr(node, "name", "") or "")
        if getattr(node, "mesh", None) is not None and node_name != "":
            mesh_nodes_by_name.setdefault(node_name, []).append(node)
    for mesh_key, clusters in clusters_by_mesh.items():
        # The binary graph keeps Mesh Models separate as ``name\\0model_id``.
        # Native UFBX does not expose that Model ID, so resolve the scene node
        # by the name prefix first and use Geometry ID when duplicate names
        # are present.  A plain-name lookup here would lose the entire Skin
        # context after duplicate-name isolation was enabled.
        mesh_name, _separator, model_id_text = str(mesh_key).partition("\x00")
        mesh_candidates = mesh_nodes_by_name.get(mesh_name, [])
        mesh_node = mesh_candidates[0] if len(mesh_candidates) == 1 else None
        if mesh_node is None and mesh_candidates:
            geometry_id = _int_or_default(
                clusters[0].get("geometry_id") if clusters else 0,
                0,
            )
            geometry_matches = [
                candidate
                for candidate in mesh_candidates
                if _int_or_default(
                    getattr(getattr(candidate, "mesh", None), "geometry_id", 0),
                    0,
                )
                == geometry_id
            ]
            if len(geometry_matches) == 1:
                mesh_node = geometry_matches[0]
            else:
                geometry_fingerprint = str(
                    (clusters[0].get("geometry_fingerprint") if clusters else "")
                    or ""
                )
                if geometry_fingerprint:
                    fingerprint_matches = [
                        candidate
                        for candidate in mesh_candidates
                        if _ufbx_mesh_geometry_fingerprint(
                            getattr(candidate, "mesh", None)
                        )
                        == geometry_fingerprint
                    ]
                    if len(fingerprint_matches) == 1:
                        mesh_node = fingerprint_matches[0]
        mesh = getattr(mesh_node, "mesh", None) if mesh_node is not None else None
        source_positions = _safe_list(getattr(mesh, "vertex_positions", None))
        mesh_world = _binary_fbx_matrix(_flatten_matrix4x4(getattr(mesh_node, "node_to_world", None)))
        # A Generic rebuild has already put the complete scene in one file-level
        # XYZ/Y-up domain.  Never infer an axis helper from this Mesh's own
        # rotation/scale: that would turn authored transforms into per-Mesh
        # axis conversions and rotate Skin with each object.
        # Generic normalization owns the only axis conversion at file scope.
        # A Mesh transform is authored placement (including scale), never an
        # axis-conversion source.  Keep the post-normalization basis identity.
        mesh_world_to_max = _identity_matrix() if mesh_world is not None else None
        if mesh_node is None or mesh is None or mesh_world is None or mesh_world_to_max is None or not source_positions:
            context["meshes"][mesh_key] = {
                "status": "missing_mesh_node_or_geometry",
                "mesh_name": mesh_name,
                "mesh_model_id": _int_or_default(model_id_text, 0),
            }
            continue

        bindings: list[dict[str, Any]] = []
        for cluster in clusters:
            # A Cluster with no positive influence cannot affect any vertex.
            # Do not let its unrelated bind matrices veto the convention used
            # by the active Clusters (for example Mesh 062's b_47_68 helper).
            if not any(
                isinstance(weight, (int, float))
                and math.isfinite(float(weight))
                and float(weight) > 0.0
                for weight in _safe_list(cluster.get("weights"))
            ):
                continue
            bone_node = nodes_by_name.get(str(cluster["bone_name"]))
            bone_world = _binary_fbx_matrix(
                _flatten_matrix4x4(getattr(bone_node, "node_to_world", None))
            )
            if bone_world is None:
                bindings = []
                break
            transform = list(cluster["transform"])
            transform_link = list(cluster["transform_link"])
            bindings.append(
                {
                    **cluster,
                    "bone_world": bone_world,
                    "max_prebind": transform,
                }
            )
        if not bindings:
            context["meshes"][mesh_key] = {
                "status": "missing_bone_world_matrix",
                "mesh_name": mesh_name,
                "mesh_model_id": _int_or_default(model_id_text, 0),
            }
            continue

        # Generic publishes Transform = inverse(BoneBind), so the row-vector
        # deformation matrix is Transform * BoneCurrent.
        mode = "fbx_bone_space_transform_current_bone"
        world_to_max = mesh_world_to_max
        unweighted_source_matrix = mesh_world
        deformation_matrices: list[list[float]] = []
        for binding in bindings:
            deformation_matrices.append(
                _multiply_row_major_matrices(
                    binding["transform"],
                    binding["bone_world"],
                )
            )
        if len(deformation_matrices) != len(bindings):
            context["meshes"][mesh_key] = {
                "status": "non_invertible_transform_link",
                "mesh_name": mesh_name,
                "mesh_model_id": _int_or_default(model_id_text, 0),
            }
            continue

        source_count = len(source_positions)
        accumulated = [[0.0] * 16 for _ in range(source_count)]
        weight_sums = [0.0] * source_count
        for binding, deformation in zip(bindings, deformation_matrices):
            for source_index, weight in zip(binding["indexes"], binding["weights"]):
                if source_index < 0 or source_index >= source_count or weight <= 0.0:
                    continue
                row = accumulated[source_index]
                for matrix_index in range(16):
                    row[matrix_index] += deformation[matrix_index] * weight
                weight_sums[source_index] += weight
        source_matrices: list[list[float]] = []
        weighted_vertex_count = 0
        for source_index, weight_sum in enumerate(weight_sums):
            if weight_sum <= 0.000000000001:
                source_matrices.append(list(unweighted_source_matrix))
                continue
            weighted_vertex_count += 1
            source_matrices.append(
                [value / weight_sum for value in accumulated[source_index]]
            )
        context["meshes"][mesh_key] = {
            "status": "binary_cluster_evaluated",
            "mode": mode,
            "source_matrices": source_matrices,
            "world_to_max_matrix": world_to_max,
            "weighted_vertex_count": weighted_vertex_count,
            "mesh_name": mesh_name,
            "mesh_model_id": _int_or_default(model_id_text, 0),
            "geometry_id": _int_or_default(
                clusters[0].get("geometry_id") if clusters else 0,
                0,
            ),
            "geometry_fingerprint": str(
                clusters[0].get("geometry_fingerprint", "") if clusters else ""
                or ""
            ),
        }
    return context


# ---------------------------------------------------------------------------
# Pure-Python UFBX substitute
# ---------------------------------------------------------------------------
#
# The normal Probe pipeline intentionally consumes a small, duck-typed subset
# of the pyufbx Scene/Node/Mesh API.  Keeping the substitute objects limited
# to that subset is important: the native UFBX lane remains untouched, while
# a missing/broken extension can still expose the exact same raw Geometry
# arrays to the existing identity, normal, UV and Skin code below.


@dataclass(slots=True)
class _BinaryFbxAxesAdapter:
    right: int = 0
    up: int = 4
    front: int = 3


@dataclass(slots=True)
class _BinaryFbxSettingsAdapter:
    axes: _BinaryFbxAxesAdapter
    unit_meters: float = 0.01
    original_unit_meters: float = 0.01
    original_axis_up: int = 4
    default_camera: str = ""
    frames_per_second: float = 30.0
    time_mode: int = 0
    time_protocol: int = 0
    snap_mode: int = 0


@dataclass(slots=True)
class _BinaryFbxMaterialAdapter:
    name: str
    shading_model: str = ""
    id: int = 0


@dataclass(slots=True)
class _BinaryFbxClusterAdapter:
    bone_name: str
    vertices: list[int]
    weights: list[float]
    transform: list[float] | None = None
    transform_link: list[float] | None = None
    name: str = ""


@dataclass(slots=True)
class _BinaryFbxSkinAdapter:
    clusters: list[_BinaryFbxClusterAdapter]
    name: str = ""


@dataclass(slots=True)
class _BinaryFbxMeshAdapter:
    name: str
    geometry_id: int
    vertex_positions: list[list[float]]
    indices: list[int]
    faces: list[tuple[int, int]]
    vertex_normals: list[list[float]]
    vertex_uvs: list[list[float]]
    uv_set_names: list[str]
    uv_values_by_set: list[list[list[float]]]
    uv_indices_by_set: list[list[int]]
    materials: list[_BinaryFbxMaterialAdapter]
    skin_deformers: list[_BinaryFbxSkinAdapter]
    face_material: list[int]
    num_uv_sets: int
    num_vertices: int
    num_indices: int
    num_faces: int
    num_triangles: int
    vertex_bitangent: list[Any] | None = None
    vertex_tangent: list[Any] | None = None
    vertex_color: list[Any] | None = None
    vertex_crease: list[Any] | None = None
    edge_crease: list[Any] | None = None
    blend_deformers: list[Any] | None = None

    def get_vertex_uvs_for_set(self, set_index: int) -> list[list[float]]:
        if set_index < 0 or set_index >= len(self.uv_values_by_set):
            return []
        return self.uv_values_by_set[set_index]

    def get_vertex_uv_indices_for_set(self, set_index: int) -> list[int]:
        if set_index < 0 or set_index >= len(self.uv_indices_by_set):
            return []
        return self.uv_indices_by_set[set_index]


@dataclass(slots=True)
class _BinaryFbxNodeAdapter:
    name: str
    attrib_type: str
    mesh: _BinaryFbxMeshAdapter | None = None
    parent: "_BinaryFbxNodeAdapter | None" = None
    children: list["_BinaryFbxNodeAdapter"] | None = None
    node_to_parent: list[float] | None = None
    node_to_world: list[float] | None = None
    geometry_to_parent: list[float] | None = None
    geometry_to_world: list[float] | None = None
    local_transform: list[float] | None = None
    world_transform: list[float] | None = None
    geometry_transform: list[float] | None = None
    bone: Any | None = None
    camera: Any | None = None
    light: Any | None = None
    visible: bool = True
    is_root: bool = False
    fbx_model_id: int = 0

    def __post_init__(self) -> None:
        if self.children is None:
            self.children = []
        if self.node_to_parent is None:
            self.node_to_parent = _binary_fbx_identity_matrix()
        if self.node_to_world is None:
            self.node_to_world = _binary_fbx_identity_matrix()
        if self.geometry_to_parent is None:
            self.geometry_to_parent = list(self.node_to_parent)
        if self.geometry_to_world is None:
            self.geometry_to_world = list(self.node_to_world)
        if self.local_transform is None:
            self.local_transform = list(self.node_to_parent)
        if self.world_transform is None:
            self.world_transform = list(self.node_to_world)
        if self.geometry_transform is None:
            self.geometry_transform = _binary_fbx_identity_matrix()


@dataclass(slots=True)
class _BinaryFbxSceneAdapter:
    nodes: list[_BinaryFbxNodeAdapter]
    meshes: list[_BinaryFbxMeshAdapter]
    bones: list[_BinaryFbxNodeAdapter]
    materials: list[_BinaryFbxMaterialAdapter]
    root_node: _BinaryFbxNodeAdapter
    settings: _BinaryFbxSettingsAdapter
    axes: _BinaryFbxAxesAdapter
    metadata: dict[str, Any]
    _codex_ufbx_runtime_mode: str = "ufbx_missed_substitute"

    def close(self) -> None:
        return None

    def find_node(self, name: str) -> _BinaryFbxNodeAdapter | None:
        for node in self.nodes:
            if node.name == name:
                return node
        return None

    def find_material(self, name: str) -> _BinaryFbxMaterialAdapter | None:
        for material in self.materials:
            if material.name == name:
                return material
        return None


def _binary_fbx_identity_matrix() -> list[float]:
    return [
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    ]


def _binary_fbx_properties70(node: _BinaryFbxNode) -> dict[str, list[Any]]:
    properties: dict[str, list[Any]] = {}
    for container in node.children:
        if container.name not in {"Properties70", "Properties60"}:
            continue
        for property_node in container.children:
            if property_node.name != "P" or not property_node.properties:
                continue
            property_name = str(property_node.properties[0] or "")
            if property_name:
                properties[property_name] = list(property_node.properties)
    return properties


def _binary_fbx_property_vector(
    properties: dict[str, list[Any]],
    name: str,
    default: tuple[float, float, float],
) -> list[float]:
    values = properties.get(name)
    if not isinstance(values, list) or len(values) < 7:
        return [float(value) for value in default]
    try:
        result = [float(values[4]), float(values[5]), float(values[6])]
    except (TypeError, ValueError, OverflowError):
        return [float(value) for value in default]
    if not all(math.isfinite(value) for value in result):
        return [float(value) for value in default]
    return result


def _binary_fbx_property_scalar(
    properties: dict[str, list[Any]],
    name: str,
    default: int | float,
) -> int | float:
    values = properties.get(name)
    if not isinstance(values, list):
        return default
    for value in reversed(values[4:] if len(values) > 4 else values):
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return value
    return default


def _binary_fbx_translation_matrix(vector: list[float]) -> list[float]:
    matrix = _binary_fbx_identity_matrix()
    matrix[12], matrix[13], matrix[14] = vector[:3]
    return matrix


def _binary_fbx_scale_matrix(vector: list[float]) -> list[float]:
    matrix = _binary_fbx_identity_matrix()
    matrix[0], matrix[5], matrix[10] = vector[:3]
    return matrix


def _binary_fbx_axis_rotation_matrix(axis: str, radians: float) -> list[float]:
    cosine = math.cos(radians)
    sine = math.sin(radians)
    matrix = _binary_fbx_identity_matrix()
    # Row-vector convention: these are the transposes of the conventional
    # column-vector elementary rotation matrices.
    if axis == "X":
        matrix[5], matrix[6], matrix[9], matrix[10] = cosine, sine, -sine, cosine
    elif axis == "Y":
        matrix[0], matrix[2], matrix[8], matrix[10] = cosine, -sine, sine, cosine
    else:
        matrix[0], matrix[1], matrix[4], matrix[5] = cosine, sine, -sine, cosine
    return matrix


def _binary_fbx_rotation_matrix(
    degrees: list[float],
    rotation_order: int,
) -> list[float]:
    orders = (
        "XYZ",
        "XZY",
        "YZX",
        "YXZ",
        "ZXY",
        "ZYX",
    )
    order = orders[rotation_order] if 0 <= rotation_order < len(orders) else "XYZ"
    angle_by_axis = {
        "X": math.radians(float(degrees[0])),
        "Y": math.radians(float(degrees[1])),
        "Z": math.radians(float(degrees[2])),
    }
    result = _binary_fbx_identity_matrix()
    for axis in order:
        result = _multiply_row_major_matrices(
            result,
            _binary_fbx_axis_rotation_matrix(axis, angle_by_axis[axis]),
        )
    return result


def _binary_fbx_model_local_matrix(model: _BinaryFbxNode) -> list[float]:
    """Build the ordinary FBX Model local matrix in row-vector form.

    Keep this fallback mathematically aligned with the standalone Generic
    rebuild path.  Pre/Post rotations are evaluated in XYZ authoring order
    (FBX does not apply ``RotationOrder`` to those fields), and each pivot is
    applied exactly once around its corresponding operation.
    """
    properties = _binary_fbx_properties70(model)
    translation = _binary_fbx_property_vector(properties, "Lcl Translation", (0.0, 0.0, 0.0))
    rotation = _binary_fbx_property_vector(properties, "Lcl Rotation", (0.0, 0.0, 0.0))
    scaling = _binary_fbx_property_vector(properties, "Lcl Scaling", (1.0, 1.0, 1.0))
    rotation_order = int(_binary_fbx_property_scalar(properties, "RotationOrder", 0) or 0)
    rotation_matrix = _binary_fbx_rotation_matrix(rotation, rotation_order)
    pre_rotation = _binary_fbx_rotation_matrix(
        _binary_fbx_property_vector(properties, "PreRotation", (0.0, 0.0, 0.0)),
        0,
    )
    post_rotation = _binary_fbx_rotation_matrix(
        _binary_fbx_property_vector(properties, "PostRotation", (0.0, 0.0, 0.0)),
        0,
    )
    inverse_post = _invert_row_major_matrix(post_rotation) or _binary_fbx_identity_matrix()
    total_rotation = _multiply_row_major_matrices(
        _multiply_row_major_matrices(inverse_post, rotation_matrix),
        pre_rotation,
    )
    scale_matrix = _binary_fbx_scale_matrix(scaling)
    rotation_offset = _binary_fbx_property_vector(properties, "RotationOffset", (0.0, 0.0, 0.0))
    rotation_pivot = _binary_fbx_property_vector(properties, "RotationPivot", (0.0, 0.0, 0.0))
    scaling_offset = _binary_fbx_property_vector(properties, "ScalingOffset", (0.0, 0.0, 0.0))
    scaling_pivot = _binary_fbx_property_vector(properties, "ScalingPivot", (0.0, 0.0, 0.0))
    # Row-vector FBX order, matching _source_model_local_parts().
    result = _binary_fbx_identity_matrix()
    for component in (
        _binary_fbx_translation_matrix([-value for value in scaling_pivot]),
        scale_matrix,
        _binary_fbx_translation_matrix(scaling_pivot),
        _binary_fbx_translation_matrix(scaling_offset),
        _binary_fbx_translation_matrix([-value for value in rotation_pivot]),
        total_rotation,
        _binary_fbx_translation_matrix(rotation_pivot),
        _binary_fbx_translation_matrix(rotation_offset),
        _binary_fbx_translation_matrix(translation),
    ):
        result = _multiply_row_major_matrices(result, component)
    return result


def _binary_fbx_axis_value(axis: Any, sign: Any, default: int) -> int:
    try:
        axis_index = int(axis)
        axis_sign = int(sign)
    except (TypeError, ValueError, OverflowError):
        return default
    if axis_index not in {0, 1, 2}:
        return default
    return (axis_index * 2) if axis_sign >= 0 else (axis_index * 2 + 1)


def _binary_fbx_scene_settings(global_settings: _BinaryFbxNode | None) -> tuple[_BinaryFbxAxesAdapter, _BinaryFbxSettingsAdapter]:
    properties = _binary_fbx_properties70(global_settings) if global_settings is not None else {}
    right = _binary_fbx_axis_value(
        _binary_fbx_property_scalar(properties, "CoordAxis", 0),
        _binary_fbx_property_scalar(properties, "CoordAxisSign", 1),
        0,
    )
    up = _binary_fbx_axis_value(
        _binary_fbx_property_scalar(properties, "UpAxis", 2),
        _binary_fbx_property_scalar(properties, "UpAxisSign", 1),
        4,
    )
    front = _binary_fbx_axis_value(
        _binary_fbx_property_scalar(properties, "FrontAxis", 1),
        _binary_fbx_property_scalar(properties, "FrontAxisSign", -1),
        3,
    )
    axes = _BinaryFbxAxesAdapter(right=right, up=up, front=front)
    try:
        unit_scale = float(_binary_fbx_property_scalar(properties, "UnitScaleFactor", 1.0))
    except (TypeError, ValueError, OverflowError):
        unit_scale = 1.0
    try:
        original_scale = float(_binary_fbx_property_scalar(properties, "OriginalUnitScaleFactor", unit_scale))
    except (TypeError, ValueError, OverflowError):
        original_scale = unit_scale
    if not math.isfinite(unit_scale) or unit_scale <= 0.0:
        unit_scale = 1.0
    if not math.isfinite(original_scale) or original_scale <= 0.0:
        original_scale = unit_scale
    settings = _BinaryFbxSettingsAdapter(
        axes=axes,
        unit_meters=unit_scale * 0.01,
        original_unit_meters=original_scale * 0.01,
        original_axis_up=int(_binary_fbx_property_scalar(properties, "OriginalUpAxis", up) or up),
        default_camera=str(_binary_fbx_property_scalar(properties, "DefaultCamera", "") or ""),
        frames_per_second=float(_binary_fbx_property_scalar(properties, "TimeMode", 30.0) or 30.0),
        time_mode=int(_binary_fbx_property_scalar(properties, "TimeMode", 0) or 0),
        time_protocol=int(_binary_fbx_property_scalar(properties, "TimeProtocol", 0) or 0),
        snap_mode=int(_binary_fbx_property_scalar(properties, "SnapOnFrameMode", 0) or 0),
    )
    return axes, settings


def _binary_fbx_model_property_text(model: _BinaryFbxNode, name: str) -> str:
    values = _binary_fbx_properties70(model).get(name)
    if not isinstance(values, list):
        return ""
    for value in reversed(values[4:] if len(values) > 4 else values):
        if isinstance(value, str):
            return value
    return ""


def _binary_fbx_collect_connections(
    connections_root: _BinaryFbxNode | None,
) -> tuple[dict[int, list[int]], dict[int, list[int]]]:
    parents_by_child: dict[int, list[int]] = {}
    children_by_parent: dict[int, list[int]] = {}
    if connections_root is None:
        return parents_by_child, children_by_parent
    for connection in connections_root.children:
        values = connection.properties
        if connection.name != "C" or len(values) < 3 or values[0] != "OO":
            continue
        if not isinstance(values[1], int) or not isinstance(values[2], int):
            continue
        child_id, parent_id = int(values[1]), int(values[2])
        parents_by_child.setdefault(child_id, []).append(parent_id)
        children_by_parent.setdefault(parent_id, []).append(child_id)
    return parents_by_child, children_by_parent


def _binary_fbx_build_mesh_adapter(
    geometry_node: _BinaryFbxNode,
    *,
    materials: list[_BinaryFbxMaterialAdapter] | None = None,
    skin_deformers: list[_BinaryFbxSkinAdapter] | None = None,
) -> _BinaryFbxMeshAdapter:
    geometry_id = int(geometry_node.properties[0]) if geometry_node.properties and isinstance(geometry_node.properties[0], int) else 0
    geometry_name = _clean_binary_fbx_object_name(geometry_node.properties[1]) if len(geometry_node.properties) > 1 else ""
    raw_geometry: dict[str, Any] | None = None
    try:
        raw_geometry = _binary_fbx_read_corner_geometry(geometry_node)
    except Exception:
        raw_geometry = None
    raw_positions = _binary_fbx_node_child_value(geometry_node, "Vertices")
    raw_polygon_indices = _binary_fbx_node_child_value(geometry_node, "PolygonVertexIndex")
    if raw_positions is None and raw_polygon_indices is None:
        raw_positions = []
    if not isinstance(raw_positions, list) or len(raw_positions) % 3 != 0:
        raise ValueError(f"Geometry {geometry_id} has no valid Vertices array")
    positions = [
        [float(raw_positions[index]), float(raw_positions[index + 1]), float(raw_positions[index + 2])]
        for index in range(0, len(raw_positions), 3)
    ]
    # Generic MAX reconstruction intentionally keeps zero-vertex placeholder
    # Meshes.  They are valid scene records and must not enter the polygon
    # decoder, which correctly rejects missing topology for non-empty Meshes.
    if not positions:
        indices = []
        faces = []
    elif raw_geometry is not None:
        indices = [int(value) for value in raw_geometry.get("source_indices", [])]
        faces = [tuple(map(int, face)) for face in raw_geometry.get("faces", [])]
    else:
        indices, faces, _corner_faces = _binary_fbx_polygon_vertex_stream(
            raw_polygon_indices,
            position_count=len(positions),
        )

    # UFBX exposes vertex_normals as a stream aligned with polygon corners for
    # ByPolygonVertex layers.  Preserve that shape so the legacy compatibility
    # extractor remains equivalent if strict raw matching is unavailable.
    corner_normals: list[list[float]] = []
    if isinstance(raw_geometry, dict) and isinstance(raw_geometry.get("normal"), dict):
        normal = raw_geometry["normal"]
        values = normal.get("values")
        corner_indices = normal.get("corner_indices")
        if isinstance(values, list) and isinstance(corner_indices, list):
            for direct_index in corner_indices:
                try:
                    corner_normals.append([float(value) for value in values[int(direct_index)][:3]])
                except (TypeError, ValueError, IndexError, OverflowError):
                    corner_normals = []
                    break

    uv_values_by_set: list[list[list[float]]] = []
    uv_indices_by_set: list[list[int]] = []
    uv_set_names: list[str] = []
    if isinstance(raw_geometry, dict):
        for index, channel in enumerate(raw_geometry.get("uv_channels", [])):
            if not isinstance(channel, dict):
                continue
            values = channel.get("values")
            corner_indices = channel.get("corner_indices")
            if not isinstance(values, list) or not isinstance(corner_indices, list):
                continue
            try:
                uv_values_by_set.append(
                    [[float(value[0]), float(value[1])] for value in values]
                )
                uv_indices_by_set.append([int(value) for value in corner_indices])
            except (TypeError, ValueError, IndexError, OverflowError):
                continue
            uv_set_names.append(str(channel.get("name", "") or f"map{index + 1}"))

    # UFBX's vertex_uvs is the direct value table (not a corner-expanded
    # stream); this is also what the summary and normal-fallback code expect.
    primary_uv_values = [list(value) for value in uv_values_by_set[0]] if uv_values_by_set else []
    face_material: list[int] = []
    material_layer = next(
        (child for child in geometry_node.children if child.name == "LayerElementMaterial"),
        None,
    )
    if material_layer is not None:
        raw_materials = _binary_fbx_node_child_value(material_layer, "Materials")
        if isinstance(raw_materials, list):
            try:
                face_material = [int(value) for value in raw_materials]
            except (TypeError, ValueError, OverflowError):
                face_material = []
    return _BinaryFbxMeshAdapter(
        name=geometry_name,
        geometry_id=geometry_id,
        vertex_positions=positions,
        indices=indices,
        faces=faces,
        vertex_normals=corner_normals,
        vertex_uvs=primary_uv_values,
        uv_set_names=uv_set_names,
        uv_values_by_set=uv_values_by_set,
        uv_indices_by_set=uv_indices_by_set,
        materials=list(materials or []),
        skin_deformers=list(skin_deformers or []),
        face_material=face_material,
        num_uv_sets=len(uv_values_by_set),
        num_vertices=len(positions),
        num_indices=len(indices),
        num_faces=len(faces),
        num_triangles=sum(max(0, int(size) - 2) for _begin, size in faces),
    )


def _binary_fbx_build_skin_rows(
    objects_by_id: dict[int, _BinaryFbxNode],
    parents_by_child: dict[int, list[int]],
    children_by_parent: dict[int, list[int]],
) -> tuple[dict[int, list[_BinaryFbxClusterAdapter]], set[int]]:
    clusters_by_geometry: dict[int, list[_BinaryFbxClusterAdapter]] = {}
    geometries_with_skin: set[int] = set()
    for cluster_id, cluster in objects_by_id.items():
        if cluster.name != "Deformer" or len(cluster.properties) < 3 or str(cluster.properties[2]) != "Cluster":
            continue
        skin_ids = [
            parent_id
            for parent_id in parents_by_child.get(cluster_id, [])
            if objects_by_id.get(parent_id) is not None
            and objects_by_id[parent_id].name == "Deformer"
            and len(objects_by_id[parent_id].properties) >= 3
            and str(objects_by_id[parent_id].properties[2]) == "Skin"
        ]
        if len(skin_ids) != 1:
            continue
        geometry_ids = [
            parent_id
            for parent_id in parents_by_child.get(skin_ids[0], [])
            if objects_by_id.get(parent_id) is not None and objects_by_id[parent_id].name == "Geometry"
        ]
        if len(geometry_ids) != 1:
            continue
        geometry_id = int(geometry_ids[0])
        geometries_with_skin.add(geometry_id)
        bone_ids = [
            child_id
            for child_id in children_by_parent.get(cluster_id, [])
            if objects_by_id.get(child_id) is not None
            and objects_by_id[child_id].name == "Model"
            and len(objects_by_id[child_id].properties) >= 3
            and str(objects_by_id[child_id].properties[2]) == "LimbNode"
        ]
        bone_name = ""
        if bone_ids and len(objects_by_id[bone_ids[0]].properties) > 1:
            bone_name = _clean_binary_fbx_object_name(objects_by_id[bone_ids[0]].properties[1])
        indexes = _binary_fbx_node_child_value(cluster, "Indexes")
        weights = _binary_fbx_node_child_value(cluster, "Weights")
        # UFBX keeps a valid zero-influence Cluster whose Indexes/Weights
        # properties are omitted.  Preserve that empty row so the substitute
        # has the same bone-name/cluster contract; it contributes no weights.
        if indexes is None and weights is None:
            parsed_indexes: list[int] = []
            parsed_weights: list[float] = []
        else:
            if not isinstance(indexes, list) or not isinstance(weights, list) or len(indexes) != len(weights):
                continue
            try:
                parsed_indexes = [int(value) for value in indexes]
                parsed_weights = [float(value) for value in weights]
            except (TypeError, ValueError, OverflowError):
                continue
        if not bone_name or not all(math.isfinite(value) and value >= 0.0 for value in parsed_weights):
            continue
        transform = _binary_fbx_matrix(_binary_fbx_node_child_value(cluster, "Transform"))
        transform_link = _binary_fbx_matrix(_binary_fbx_node_child_value(cluster, "TransformLink"))
        clusters_by_geometry.setdefault(geometry_id, []).append(
            _BinaryFbxClusterAdapter(
                bone_name=bone_name,
                name=bone_name,
                vertices=parsed_indexes,
                weights=parsed_weights,
                transform=transform,
                transform_link=transform_link,
            )
        )
    return clusters_by_geometry, geometries_with_skin


def ufbx_missed_substitute(
    path: str | Path,
    *,
    binary_document: _BinaryFbxDocument | None = None,
) -> _BinaryFbxSceneAdapter:
    """Load a binary FBX through the local parser when UFBX is unavailable.

    This is intentionally a loader substitute, not a second export pipeline:
    all normal/UV/topology and route decisions remain in the existing Probe
    functions.  Unsupported (ASCII/corrupt) input raises a data error rather
    than fabricating a default normal or silently exporting an empty scene.
    """
    fbx_path = Path(path)
    document = binary_document or _build_binary_fbx_document(fbx_path)
    if document.path.resolve() != fbx_path.resolve():
        raise ValueError("Binary FBX document belongs to a different path")
    roots = document.roots
    objects_root = next((node for node in roots if node.name == "Objects"), None)
    if objects_root is None:
        raise ValueError("Binary FBX Objects node is missing")
    connections_root = next((node for node in roots if node.name == "Connections"), None)
    parents_by_child, children_by_parent = _binary_fbx_collect_connections(connections_root)
    objects_by_id: dict[int, _BinaryFbxNode] = {
        int(node.properties[0]): node
        for node in objects_root.children
        if node.properties and isinstance(node.properties[0], int)
    }
    geometry_nodes = {
        object_id: node
        for object_id, node in objects_by_id.items()
        if node.name == "Geometry"
        and len(node.properties) >= 3
        and str(node.properties[2]) == "Mesh"
    }
    model_nodes = [
        node
        for node in objects_root.children
        if node.name == "Model" and len(node.properties) >= 3 and isinstance(node.properties[0], int)
    ]
    material_adapters: dict[int, _BinaryFbxMaterialAdapter] = {}
    for object_id, node in objects_by_id.items():
        if node.name != "Material" or len(node.properties) < 2:
            continue
        material_adapters[object_id] = _BinaryFbxMaterialAdapter(
            name=_clean_binary_fbx_object_name(node.properties[1]),
            id=object_id,
        )
    clusters_by_geometry, geometries_with_skin = _binary_fbx_build_skin_rows(
        objects_by_id,
        parents_by_child,
        children_by_parent,
    )

    mesh_adapters: dict[int, _BinaryFbxMeshAdapter] = {}
    for geometry_id, geometry_node in geometry_nodes.items():
        connected_material_ids: list[int] = []
        for model_id, model_node in objects_by_id.items():
            if model_node.name != "Model":
                continue
            if geometry_id not in parents_by_child:
                continue
            if model_id not in parents_by_child.get(geometry_id, []):
                continue
            connected_material_ids.extend(
                child_id
                for child_id in children_by_parent.get(model_id, [])
                if child_id in material_adapters
            )
        materials = [material_adapters[mid] for mid in dict.fromkeys(connected_material_ids) if mid in material_adapters]
        skin_rows = clusters_by_geometry.get(geometry_id, [])
        skin_deformers = []
        if geometry_id in geometries_with_skin:
            skin_deformers = [_BinaryFbxSkinAdapter(clusters=list(skin_rows))]
        try:
            mesh_adapters[geometry_id] = _binary_fbx_build_mesh_adapter(
                geometry_node,
                materials=materials,
                skin_deformers=skin_deformers,
            )
        except Exception:
            # A malformed unconnected helper Geometry must not make a valid
            # scene unusable; connected malformed Geometry is reported below.
            if any(
                geometry_id in children_by_parent.get(int(model.properties[0]), [])
                for model in model_nodes
            ):
                raise

    axes, settings = _binary_fbx_scene_settings(
        next((node for node in roots if node.name == "GlobalSettings"), None)
    )
    root = _BinaryFbxNodeAdapter(
        name="",
        attrib_type="ROOT",
        is_root=True,
        fbx_model_id=0,
    )
    node_adapters: dict[int, _BinaryFbxNodeAdapter] = {}
    for model in model_nodes:
        model_id = int(model.properties[0])
        model_name = _clean_binary_fbx_object_name(model.properties[1])
        model_type = str(model.properties[2] or "")
        geometry_ids = [
            child_id
            for child_id in children_by_parent.get(model_id, [])
            if child_id in mesh_adapters
        ]
        node_adapters[model_id] = _BinaryFbxNodeAdapter(
            name=model_name,
            attrib_type=model_type,
            mesh=mesh_adapters.get(geometry_ids[0]) if geometry_ids else None,
            fbx_model_id=model_id,
            visible=True,
            node_to_parent=_binary_fbx_model_local_matrix(model),
        )
        if model_type.casefold() == "limbnode":
            node_adapters[model_id].bone = node_adapters[model_id]
    # Dataclass slots intentionally reject ad-hoc bookkeeping attributes; use
    # a local map for hierarchy assembly instead.
    parent_model_map: dict[int, int] = {}
    for model in model_nodes:
        model_id = int(model.properties[0])
        parent_model_map[model_id] = next(
            (
                int(parent_id)
                for parent_id in parents_by_child.get(model_id, [])
                if objects_by_id.get(parent_id) is not None and objects_by_id[parent_id].name == "Model"
            ),
            0,
        )
    # Resolve parent/world matrices recursively instead of relying on FBX
    # Objects order.  Exporters are allowed to serialize a child Model before
    # its parent; a single insertion-order pass would then attach an incorrect
    # world matrix to that child and to every dependent Skin calculation.
    world_cache: dict[int, list[float]] = {}
    resolving: set[int] = set()

    def resolve_world(model_id: int) -> list[float]:
        cached = world_cache.get(model_id)
        if cached is not None:
            return list(cached)
        node = node_adapters[model_id]
        local = list(node.node_to_parent or _binary_fbx_identity_matrix())
        if model_id in resolving:
            # Malformed cyclic Model links cannot be evaluated.  Preserve the
            # local transform as a deterministic, non-recursive fallback.
            world = local
        else:
            resolving.add(model_id)
            parent_id = parent_model_map.get(model_id, 0)
            if parent_id in node_adapters:
                parent_world = resolve_world(parent_id)
            else:
                parent_world = _binary_fbx_identity_matrix()
            resolving.discard(model_id)
            world = _multiply_row_major_matrices(local, parent_world)
        world_cache[model_id] = list(world)
        return list(world)

    for model_id, node in node_adapters.items():
        parent_id = parent_model_map.get(model_id, 0)
        parent = node_adapters.get(parent_id, root)
        node.parent = parent
        if parent.children is None:
            parent.children = []
        parent.children.append(node)
        node.node_to_world = resolve_world(model_id)
        node.geometry_to_parent = list(node.node_to_parent or _binary_fbx_identity_matrix())
        node.geometry_to_world = list(node.node_to_world)
        node.local_transform = list(node.node_to_parent or _binary_fbx_identity_matrix())
        node.world_transform = list(node.node_to_world)

    nodes = [root] + [node_adapters[int(model.properties[0])] for model in model_nodes]
    bones = [node for node in nodes if node is not root and node.attrib_type.casefold() == "limbnode"]
    meshes = list(mesh_adapters.values())
    materials = list(material_adapters.values())
    return _BinaryFbxSceneAdapter(
        nodes=nodes,
        meshes=meshes,
        bones=bones,
        materials=materials,
        root_node=root,
        settings=settings,
        axes=axes,
        metadata={
            "source": "binary_fbx_python_reader",
            "path": str(fbx_path),
            "version": int(document.version),
            "runtime_mode": "ufbx_missed_substitute",
        },
    )


def _apply_binary_fbx_skin_pose_channels(
    geometry: dict[str, Any],
    mesh: Any,
    instance_node: Any | None,
    skin_context: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(geometry, dict):
        return geometry
    if len(_safe_list(getattr(mesh, "skin_deformers", None))) < 1:
        geometry.setdefault("fbx_skin_pose_evaluation_status", "not_skinned")
        return geometry
    # Do not let native UFBX rows short-circuit the canonical Skin evaluator.
    # Native and substitute runtimes must consume the same Generic binary
    # Cluster data.  UFBX rows remain an input fallback only when the canonical
    # evaluator has no usable entry for this mesh.
    node_name = str(getattr(instance_node, "name", "") or "")
    mesh_pose = None
    if isinstance(skin_context, dict):
        mesh_context = skin_context.get("meshes", {})
        if isinstance(mesh_context, dict):
            direct_pose = mesh_context.get(node_name)
            if isinstance(direct_pose, dict):
                mesh_pose = direct_pose
            else:
                model_id = _int_or_default(
                    getattr(instance_node, "fbx_model_id", 0),
                    0,
                )
                if model_id > 0:
                    keyed_pose = mesh_context.get(
                        f"{node_name}\x00{model_id}"
                    )
                    if isinstance(keyed_pose, dict):
                        mesh_pose = keyed_pose
                if mesh_pose is None and node_name:
                    candidates: list[dict[str, Any]] = []
                    for key, value in mesh_context.items():
                        if not isinstance(value, dict):
                            continue
                        key_name = str(value.get("mesh_name", "") or "")
                        if key_name == "":
                            key_name = str(key).partition("\x00")[0]
                        if key_name == node_name:
                            candidates.append(value)
                    if len(candidates) == 1:
                        mesh_pose = candidates[0]
                    elif len(candidates) > 1:
                        mesh_fingerprint = _ufbx_mesh_geometry_fingerprint(mesh)
                        fingerprint_matches = [
                            value
                            for value in candidates
                            if str(value.get("geometry_fingerprint", "") or "")
                            == mesh_fingerprint
                        ]
                        if len(fingerprint_matches) == 1:
                            mesh_pose = fingerprint_matches[0]
    if not isinstance(mesh_pose, dict) or mesh_pose.get("status") != "binary_cluster_evaluated":
        status = mesh_pose.get("status") if isinstance(mesh_pose, dict) else "binary_cluster_evaluation_unavailable"
        # Weightless placeholder Skin/Cluster records are valid no-op data.
        # A real weighted Skin must never silently fall back to native UFBX
        # rows or source geometry after the Generic boundary.
        has_positive_weight = any(
            isinstance(weight, (int, float))
            and math.isfinite(float(weight))
            and float(weight) > 0.0
            for deformer in _safe_list(getattr(mesh, "skin_deformers", None))
            for cluster in _safe_list(getattr(deformer, "clusters", None))
            for weight in _safe_list(getattr(cluster, "weights", None))
        )
        if has_positive_weight:
            raise ValueError(
                f"Canonical Generic Skin evaluation unavailable for mesh {node_name!r}: {status}"
            )
        geometry["fbx_skin_pose_evaluation_status"] = "not_skinned_weightless_placeholder"
        return geometry

    source_matrices = mesh_pose.get("source_matrices")
    world_to_max = mesh_pose.get("world_to_max_matrix")
    source_indices = geometry.get("source_vertex_indices")
    positions = geometry.get("positions")
    normals = geometry.get("normals")
    source_positions = _safe_list(getattr(mesh, "vertex_positions", None))
    if (
        not isinstance(source_matrices, list)
        or not isinstance(world_to_max, list)
        or len(world_to_max) != 16
        or not isinstance(source_indices, list)
        or not isinstance(positions, list)
        or len(source_indices) != len(positions)
        or len(source_positions) != len(source_matrices)
    ):
        raise ValueError(
            f"Canonical Generic Skin geometry is unaligned for mesh {node_name!r}"
        )

    evaluated_world_positions: list[list[float]] = []
    evaluated_max_positions: list[list[float]] = []
    evaluated_world_normals: list[list[float]] = []
    evaluated_max_normals: list[list[float]] = []
    referenced_source_indices: set[int] = set()
    for export_index, source_index_value in enumerate(source_indices):
        try:
            source_index = int(source_index_value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(
                f"Canonical Generic Skin source index is invalid for mesh {node_name!r}"
            )
        if source_index < 0 or source_index >= len(source_matrices):
            raise ValueError(
                f"Canonical Generic Skin source matrix is missing for mesh {node_name!r}"
            )
        referenced_source_indices.add(source_index)
        matrix = source_matrices[source_index]
        world_position = _transform_position_row_major(positions[export_index], matrix)
        evaluated_world_positions.append(world_position)
        evaluated_max_positions.append(
            _transform_position_row_major(world_position, world_to_max)
        )
        source_normal = normals[export_index] if isinstance(normals, list) and export_index < len(normals) else _default_normal()
        world_normal = _transform_normal_row_major(source_normal, matrix)
        evaluated_world_normals.append(world_normal)
        evaluated_max_normals.append(_transform_normal_row_major(world_normal, world_to_max))

    # Blender can retain source vertices that no exported triangle references.
    # They still have verified Cluster matrices and must reach the MOD position
    # patcher, otherwise a Bones+Mesh export would leave them behind the pose.
    unreferenced_source_indices = [
        source_index
        for source_index in range(len(source_matrices))
        if source_index not in referenced_source_indices
    ]
    unreferenced_max_positions: list[list[float]] = []
    for source_index in unreferenced_source_indices:
        world_position = _transform_position_row_major(
            source_positions[source_index],
            source_matrices[source_index],
        )
        unreferenced_max_positions.append(
            _transform_position_row_major(world_position, world_to_max)
        )

    geometry["skinned_positions"] = [list(row) for row in evaluated_world_positions]
    geometry["skinned_world_positions"] = [list(row) for row in evaluated_world_positions]
    geometry["skinned_max_positions"] = [list(row) for row in evaluated_max_positions]
    geometry["skinned_normals"] = [list(row) for row in evaluated_world_normals]
    geometry["skinned_max_normals"] = [list(row) for row in evaluated_max_normals]
    geometry["binary_skin_unreferenced_source_indices"] = unreferenced_source_indices
    geometry["binary_skin_unreferenced_max_positions"] = unreferenced_max_positions
    geometry["skinned_is_local"] = False
    geometry["fbx_skin_pose_evaluation_status"] = "binary_cluster_evaluated"
    geometry["fbx_skin_pose_evaluation_schema"] = FBX_BINARY_SKIN_EVALUATION_SCHEMA
    geometry["fbx_skin_pose_evaluation_mode"] = str(mesh_pose.get("mode", ""))
    return geometry


def _infer_effective_skinned_is_local(
    raw_max_positions: Any,
    skinned_positions: Any,
    *,
    skinned_is_local_hint: bool,
    node_to_world: Any,
    raw_positions: Any = None,
) -> bool:
    # The Generic document defines the row domain once.  Do not sample values
    # and guess local/world semantics per Mesh: that made native and substitute
    # UFBX builds choose different Skin paths for identical input.
    return bool(skinned_is_local_hint)


def _build_skinned_pose_output_channels(
    skinned_positions: list[list[float]],
    skinned_normals: list[list[float]],
    *,
    skinned_is_local: bool,
    node_to_world: Any,
) -> dict[str, list[list[float]]]:
    out_skinned_world_positions: list[list[float]] = []
    out_skinned_max_positions: list[list[float]] = []
    out_skinned_max_normals: list[list[float]] = []
    prepared_transform = _prepare_row_major_transform(node_to_world)
    transformed_normals: dict[tuple[bool, float, float, float], list[float]] = {}

    row_count = max(len(skinned_positions), len(skinned_normals))
    for row_index in range(row_count):
        skinned_pos = skinned_positions[row_index] if row_index < len(skinned_positions) else _default_vec3()
        skinned_normal = skinned_normals[row_index] if row_index < len(skinned_normals) else _default_normal()
        if skinned_is_local:
            skinned_world_pos = _transform_position_row_major(skinned_pos, prepared_transform)
        else:
            skinned_world_pos = list(skinned_pos)
        normal_values = _normal_vec3_to_list(skinned_normal)
        normal_key = (
            bool(skinned_is_local),
            normal_values[0],
            normal_values[1],
            normal_values[2],
        )
        skinned_max_normal = transformed_normals.get(normal_key)
        if skinned_max_normal is None:
            if skinned_is_local:
                skinned_world_normal = _transform_normal_row_major(
                    skinned_normal,
                    prepared_transform,
                )
            else:
                skinned_world_normal = _normalize_normal_vec3(skinned_normal)
            skinned_max_normal = _fbx_world_to_max_normal(skinned_world_normal)
            transformed_normals[normal_key] = skinned_max_normal
        out_skinned_world_positions.append(list(skinned_world_pos))
        out_skinned_max_positions.append(_fbx_world_to_max_vec3(skinned_world_pos))
        out_skinned_max_normals.append(list(skinned_max_normal))
    return {
        "skinned_world_positions": out_skinned_world_positions,
        "skinned_max_positions": out_skinned_max_positions,
        "skinned_max_normals": out_skinned_max_normals,
    }


def _augment_geometry_with_skinned_pose_channels(
    geometry: dict[str, Any],
    mesh: Any,
    instance_node: Any | None = None,
) -> dict[str, Any]:
    if not isinstance(geometry, dict):
        return geometry
    if "skinned_positions" in geometry and "skinned_max_positions" in geometry and "skinned_is_local" in geometry:
        return geometry

    skinned_positions_src = _get_mesh_skinned_vec3_rows(mesh, "skinned_position", "skinned_positions")
    skinned_normals_src = _get_mesh_skinned_vec3_rows(mesh, "skinned_normal", "skinned_normals")
    skinned_is_local = _coerce_bool(getattr(mesh, "skinned_is_local", None), False)
    source_vertex_indices = geometry.get("source_vertex_indices")
    source_corner_indices = geometry.get("fbx_export_corner_indices")
    raw_positions = geometry.get("positions")
    raw_normals = geometry.get("normals")
    raw_max_positions = geometry.get("max_positions")
    node_to_world = getattr(instance_node, "node_to_world", None)
    mesh_vertex_count = int(getattr(mesh, "num_vertices", 0) or 0)
    mesh_indices = getattr(mesh, "indices", None)
    mesh_index_count = len(mesh_indices) if mesh_indices is not None else 0

    out_skinned_positions: list[list[float]] = []
    out_skinned_normals: list[list[float]] = []

    export_count = 0
    if isinstance(source_vertex_indices, list):
        export_count = len(source_vertex_indices)
    elif isinstance(raw_positions, list):
        export_count = len(raw_positions)

    for export_index in range(export_count):
        source_index = export_index
        if isinstance(source_vertex_indices, list) and export_index < len(source_vertex_indices):
            try:
                source_index = int(source_vertex_indices[export_index])
            except Exception:
                source_index = export_index
        source_corner_index = source_index
        if isinstance(source_corner_indices, list) and export_index < len(source_corner_indices):
            try:
                source_corner_index = int(source_corner_indices[export_index])
            except Exception:
                source_corner_index = source_index

        default_pos = _default_vec3()
        if isinstance(raw_positions, list) and export_index < len(raw_positions):
            default_pos = _vec3_to_list(raw_positions[export_index])
        skinned_pos = default_pos
        skinned_position_index = _resolve_vertex_attr_index(
            len(skinned_positions_src),
            position_index=source_index,
            corner_index=source_corner_index,
            vertex_count=mesh_vertex_count,
            index_count=mesh_index_count,
        )
        if skinned_position_index is not None and 0 <= skinned_position_index < len(skinned_positions_src):
            skinned_pos = _vec3_to_list(skinned_positions_src[skinned_position_index])
        out_skinned_positions.append(list(skinned_pos))

        default_normal = _default_normal()
        if isinstance(raw_normals, list) and export_index < len(raw_normals):
            default_normal = _vec3_to_list(raw_normals[export_index])
        skinned_normal = default_normal
        skinned_normal_index = _resolve_vertex_attr_index(
            len(skinned_normals_src),
            position_index=source_index,
            corner_index=source_corner_index,
            vertex_count=mesh_vertex_count,
            index_count=mesh_index_count,
        )
        if skinned_normal_index is not None and 0 <= skinned_normal_index < len(skinned_normals_src):
            skinned_normal = _vec3_to_list(skinned_normals_src[skinned_normal_index])
        out_skinned_normals.append(list(skinned_normal))

    effective_skinned_is_local = _infer_effective_skinned_is_local(
        raw_max_positions,
        out_skinned_positions,
        skinned_is_local_hint=skinned_is_local,
        node_to_world=node_to_world,
        raw_positions=raw_positions,
    )
    pose_channels = _build_skinned_pose_output_channels(
        out_skinned_positions,
        out_skinned_normals,
        skinned_is_local=effective_skinned_is_local,
        node_to_world=node_to_world,
    )

    geometry.setdefault("skinned_positions", out_skinned_positions)
    geometry.setdefault("skinned_world_positions", pose_channels["skinned_world_positions"])
    geometry.setdefault("skinned_max_positions", pose_channels["skinned_max_positions"])
    geometry.setdefault("skinned_normals", out_skinned_normals)
    geometry.setdefault("skinned_max_normals", pose_channels["skinned_max_normals"])
    geometry.setdefault("skinned_is_local", effective_skinned_is_local)
    return geometry


def _normalize_vec3(vec: Any, fallback: tuple[float, float, float] = (0.0, 0.0, 1.0)) -> list[float]:
    xyz = _vec3_to_list(vec)
    length = math.sqrt((xyz[0] * xyz[0]) + (xyz[1] * xyz[1]) + (xyz[2] * xyz[2]))
    if length <= 0.000001:
        return [round(float(fallback[0]), 6), round(float(fallback[1]), 6), round(float(fallback[2]), 6)]
    return [round(xyz[0] / length, 6), round(xyz[1] / length, 6), round(xyz[2] / length, 6)]


def _uv_key_from_value(uv: list[float]) -> tuple[float, float]:
    return (round(float(uv[0]), 6), round(float(uv[1]), 6))


def _resolve_corner_uv(
    uvs_src: Any,
    *,
    uvs_count: int,
    position_index: int,
    corner_index: int,
    vertex_count: int,
    index_count: int,
) -> tuple[list[float], Any]:
    if uvs_src is None or uvs_count <= 0:
        default_uv = _default_vec2()
        return default_uv, ("default", _uv_key_from_value(default_uv))

    if uvs_count == vertex_count and 0 <= position_index < uvs_count:
        uv = _vec2_to_list(uvs_src[position_index])
        return uv, ("source_vertex", position_index)

    if uvs_count == index_count and 0 <= corner_index < uvs_count:
        uv = _vec2_to_list(uvs_src[corner_index])
        return uv, ("uv_value", _uv_key_from_value(uv))

    attr_index = _resolve_vertex_attr_index(
        uvs_count,
        position_index=position_index,
        corner_index=corner_index,
        vertex_count=vertex_count,
        index_count=index_count,
    )
    if attr_index is not None and 0 <= attr_index < uvs_count:
        uv = _vec2_to_list(uvs_src[attr_index])
        return uv, ("uv_attr", int(attr_index))

    default_uv = _default_vec2()
    return default_uv, ("default", _uv_key_from_value(default_uv))


def _extract_mesh_uv_channels(
    mesh: Any,
    *,
    geom_indices: list[int],
    vertex_count: int,
) -> list[dict[str, Any]]:
    index_count = len(geom_indices)
    out: list[dict[str, Any]] = []
    uv_set_names = _safe_list(getattr(mesh, "uv_set_names", None))
    get_uvs_for_set = getattr(mesh, "get_vertex_uvs_for_set", None)
    get_uv_indices_for_set = getattr(mesh, "get_vertex_uv_indices_for_set", None)

    total_sets = 0
    try:
        total_sets = int(getattr(mesh, "num_uv_sets", 0) or 0)
    except Exception:
        total_sets = 0
    if total_sets <= 0 and len(uv_set_names) > 0:
        total_sets = len(uv_set_names)
    if total_sets <= 0 and callable(get_uvs_for_set):
        total_sets = max(1, len(uv_set_names))
    if total_sets <= 0 and getattr(mesh, "vertex_uvs", None) is not None:
        total_sets = 1

    for set_index in range(total_sets):
        values_src = None
        if callable(get_uvs_for_set):
            try:
                values_src = get_uvs_for_set(set_index)
            except Exception:
                values_src = None
        elif set_index == 0:
            values_src = getattr(mesh, "vertex_uvs", None)
        if values_src is None:
            continue

        values = [_vec2_to_list(value) for value in _safe_list(values_src)]
        if len(values) <= 0:
            continue

        corner_indices_src = None
        if callable(get_uv_indices_for_set):
            try:
                corner_indices_src = get_uv_indices_for_set(set_index)
            except Exception:
                corner_indices_src = None

        corner_indices: list[int] = []
        if corner_indices_src is not None:
            for raw_index in _safe_list(corner_indices_src):
                try:
                    corner_indices.append(int(raw_index))
                except Exception:
                    corner_indices.append(0)
        else:
            for corner_index, position_index in enumerate(geom_indices):
                attr_index = _resolve_vertex_attr_index(
                    len(values),
                    position_index=position_index,
                    corner_index=corner_index,
                    vertex_count=vertex_count,
                    index_count=index_count,
                )
                corner_indices.append(int(attr_index) if attr_index is not None else 0)

        if len(corner_indices) != index_count:
            continue

        channel_name = ""
        if set_index < len(uv_set_names):
            channel_name = str(uv_set_names[set_index] or "")
        if channel_name == "":
            channel_name = f"map{set_index + 1}"

        out.append(
            {
                "channel": set_index + 1,
                "name": channel_name,
                "values": values,
                "corner_indices": corner_indices,
            }
        )

    return out


def _resolve_corner_uv_from_channel(
    uv_channel: dict[str, Any] | None,
    *,
    position_index: int,
    corner_index: int,
    vertex_count: int,
    index_count: int,
) -> tuple[list[float], Any]:
    if not isinstance(uv_channel, dict):
        default_uv = _default_vec2()
        return default_uv, ("default", _uv_key_from_value(default_uv))

    values = uv_channel.get("values")
    corner_indices = uv_channel.get("corner_indices")
    if isinstance(values, list) and isinstance(corner_indices, list) and 0 <= corner_index < len(corner_indices):
        try:
            attr_index = int(corner_indices[corner_index])
        except Exception:
            attr_index = -1
        if 0 <= attr_index < len(values):
            uv = _vec2_to_list(values[attr_index])
            return uv, ("uv_attr", attr_index)

    return _resolve_corner_uv(
        values,
        uvs_count=len(values) if isinstance(values, list) else 0,
        position_index=position_index,
        corner_index=corner_index,
        vertex_count=vertex_count,
        index_count=index_count,
    )


def _build_source_vertex_normals(
    positions_count: int,
    normals_src: Any,
    indices_src: Any,
    *,
    vertex_count: int,
    index_count: int,
) -> list[list[float]]:
    """Build the PC_REHD 1.2.8-compatible per-position fallback normals.

    The old exporter selected one explicit normal for each mesh vertex. It did
    not average every polygon-corner normal sharing the same position. Exact
    corner normals are preserved separately by _extract_mesh_geometry().
    """
    out = [_default_normal() for _ in range(max(0, positions_count))]
    if normals_src is None or positions_count <= 0:
        return out

    normals_count = len(normals_src)
    if normals_count <= 0:
        return out

    if normals_count == positions_count or normals_count == vertex_count:
        limit = min(positions_count, normals_count)
        for position_index in range(limit):
            out[position_index] = _normalize_normal_vec3(normals_src[position_index], (0.0, 0.0, 1.0))
        return out

    if indices_src is None or index_count <= 0:
        return out

    assigned = [False for _ in range(positions_count)]
    for corner_index in range(index_count):
        try:
            position_index = int(indices_src[corner_index])
        except Exception:
            continue
        if position_index < 0 or position_index >= positions_count:
            continue
        if assigned[position_index]:
            continue
        normal_index = _resolve_vertex_attr_index(
            normals_count,
            position_index=position_index,
            corner_index=corner_index,
            vertex_count=vertex_count,
            index_count=index_count,
        )
        if normal_index is None or normal_index < 0 or normal_index >= normals_count:
            continue
        out[position_index] = _normalize_normal_vec3(normals_src[normal_index], (0.0, 0.0, 1.0))
        assigned[position_index] = True
    return out


def _resolve_corner_normal(
    normals_src: Any,
    source_normals: list[list[float]],
    *,
    position_index: int,
    corner_index: int,
    vertex_count: int,
    index_count: int,
) -> list[float]:
    normals_count = len(normals_src) if normals_src is not None else 0
    normal_index = _resolve_vertex_attr_index(
        normals_count,
        position_index=position_index,
        corner_index=corner_index,
        vertex_count=vertex_count,
        index_count=index_count,
    )
    if normal_index is not None and 0 <= normal_index < normals_count:
        return _normalize_normal_vec3(normals_src[normal_index], (0.0, 0.0, 1.0))
    if 0 <= position_index < len(source_normals):
        return list(source_normals[position_index])
    return _default_normal()


def _extract_mesh_geometry_from_binary_corner_layers(
    mesh: Any,
    instance_node: Any | None,
    raw_geometry: dict[str, Any],
    normal_fidelity: dict[str, Any],
    *,
    use_global_axis_domain: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Build MOD rows from FBX-authored polygon-corner normal and UV streams."""
    try:
        positions_src = getattr(mesh, "vertex_positions", None)
        indices_src = getattr(mesh, "indices", None)
        if positions_src is None or indices_src is None:
            raise ValueError("UFBX mesh has no position or index stream")
        geom_indices = [int(index) for index in _safe_list(indices_src)]
        source_indices = raw_geometry.get("source_indices")
        raw_faces = raw_geometry.get("faces")
        normal_layer = raw_geometry.get("normal")
        if not isinstance(source_indices, list) or geom_indices != source_indices:
            raise ValueError("UFBX topology does not exactly match the raw FBX Geometry")
        if not isinstance(raw_faces, list) or not isinstance(normal_layer, dict):
            raise ValueError("raw FBX Geometry has no usable corner normal layer")
        mesh_faces = [
            (int(face[0]), int(face[1]))
            for face in _safe_list(getattr(mesh, "faces", None))
        ]
        if mesh_faces != raw_faces:
            raise ValueError("UFBX polygon boundaries do not exactly match raw FBX Geometry")
        if len(positions_src) != int(raw_geometry.get("position_count", 0) or 0):
            raise ValueError("UFBX position count does not match raw FBX Geometry")

        normal_values = normal_layer.get("values")
        normal_corner_indices = normal_layer.get("corner_indices")
        if (
            not isinstance(normal_values, list)
            or not isinstance(normal_corner_indices, list)
            or len(normal_corner_indices) != len(geom_indices)
        ):
            raise ValueError("raw FBX normal layer is not aligned to polygon corners")

        raw_uv_channels = raw_geometry.get("uv_channels")
        if not isinstance(raw_uv_channels, list):
            raise ValueError("raw FBX UV channel container is invalid")
        uv_channels: list[dict[str, Any]] = []
        for channel_index, raw_channel in enumerate(raw_uv_channels):
            if not isinstance(raw_channel, dict):
                raise ValueError(f"raw FBX UV channel {channel_index + 1} is invalid")
            values = raw_channel.get("values")
            corner_indices = raw_channel.get("corner_indices")
            if (
                not isinstance(values, list)
                or not isinstance(corner_indices, list)
                or len(corner_indices) != len(geom_indices)
            ):
                raise ValueError(f"raw FBX UV channel {channel_index + 1} is not aligned to polygon corners")
            uv_channels.append(
                {
                    "channel": int(raw_channel.get("channel", channel_index + 1) or channel_index + 1),
                    "name": str(raw_channel.get("name", "") or f"map{channel_index + 1}"),
                    "values": [_vec2_to_list(value) for value in values],
                    "corner_indices": [int(value) for value in corner_indices],
                }
            )

        skinned_positions_src = _get_mesh_skinned_vec3_rows(mesh, "skinned_position", "skinned_positions")
        # Exact corner normals come from the raw FBX bytes.  Do not replace
        # them with UFBX's optional skinned normal stream.
        skinned_normals_src: list[Any] = []
        skinned_is_local = _coerce_bool(getattr(mesh, "skinned_is_local", None), False)
        vertex_count = int(getattr(mesh, "num_vertices", len(positions_src)) or len(positions_src))
        index_count = len(geom_indices)
        node_to_world = getattr(instance_node, "node_to_world", None)
        prepared_transform = _prepare_row_major_transform(node_to_world)
        node_world_matrix = _binary_fbx_matrix(_flatten_matrix4x4(node_to_world))
        # The Generic layer has already normalized the complete FBX once at
        # file scope.  Never derive an axis conversion from this Mesh matrix.
        world_to_max_matrix = _identity_matrix() if node_world_matrix is not None else None

        first_corner_by_position = [-1 for _ in range(len(positions_src))]
        for corner_index, position_index in enumerate(geom_indices):
            if first_corner_by_position[position_index] < 0:
                first_corner_by_position[position_index] = corner_index

        out_positions: list[list[float]] = []
        out_max_positions: list[list[float]] = []
        out_world_positions: list[list[float]] = []
        out_skinned_positions: list[list[float]] = []
        out_normals: list[list[float]] = []
        out_max_normals: list[list[float]] = []
        out_skinned_normals: list[list[float]] = []
        out_uvs: list[list[float]] = []
        out_face_indices: list[int] = []
        out_source_vertex_indices = list(range(len(positions_src)))
        out_source_corner_indices = list(first_corner_by_position)
        out_geom_face_indices: list[int] = []
        out_uv_channel_payloads = [
            {
                "channel": int(channel["channel"]),
                "name": str(channel["name"]),
                "values": [list(value) for value in channel["values"]],
                "corner_indices": [],
            }
            for channel in uv_channels
        ]

        # FBX normalId is only an indirect lookup into LayerElementNormal.Values;
        # it is not vertex identity. Using normalId (or a corner UV index) in a
        # vertex key expands the authored position table into polygon corners,
        # even when those IDs decode to the same vector. Keep the original FBX
        # position count/order and bind each position to its first authored
        # corner value. A real MOD seam must already have its own position index.
        for position_index, local_position_value in enumerate(positions_src):
            corner_index = first_corner_by_position[position_index]
            if corner_index >= 0:
                normal_id = int(normal_corner_indices[corner_index])
                corner_normal = _normalize_normal_vec3(normal_values[normal_id])
                primary_uv = _default_vec2()
                if uv_channels:
                    primary_direct_index = int(uv_channels[0]["corner_indices"][corner_index])
                    primary_uv = list(uv_channels[0]["values"][primary_direct_index])
            else:
                corner_normal = _default_normal()
                primary_uv = _default_vec2()

            local_position = _vec3_to_list(local_position_value)
            world_position = _transform_position_row_major(local_position, prepared_transform)
            max_position = (
                _transform_position_row_major(world_position, world_to_max_matrix)
                if world_to_max_matrix is not None
                else _fbx_world_to_max_vec3(world_position)
            )
            out_positions.append(local_position)
            out_world_positions.append(max_position)
            out_max_positions.append(max_position)
            out_normals.append(corner_normal)
            out_max_normals.append(
                _transform_normal_row_major(corner_normal, prepared_transform)
                if use_global_axis_domain
                else _fbx_authored_corner_normal_to_max(corner_normal)
            )

            skinned_position = local_position
            skinned_position_index = _resolve_vertex_attr_index(
                len(skinned_positions_src),
                position_index=position_index,
                corner_index=corner_index,
                vertex_count=vertex_count,
                index_count=index_count,
            )
            if skinned_position_index is not None and 0 <= skinned_position_index < len(skinned_positions_src):
                skinned_position = _vec3_to_list(skinned_positions_src[skinned_position_index])
            out_skinned_positions.append(list(skinned_position))
            out_skinned_normals.append(list(corner_normal))
            out_uvs.append(list(primary_uv))

        for face_begin, face_size in raw_faces:
            face_vertex_ids: list[int] = []
            face_geom_vertex_ids: list[int] = []
            face_uv_channel_ids = [[] for _ in out_uv_channel_payloads]
            for local_offset in range(int(face_size)):
                corner_index = int(face_begin) + local_offset
                position_index = int(geom_indices[corner_index])
                for channel_index, channel in enumerate(uv_channels):
                    direct_index = int(channel["corner_indices"][corner_index])
                    face_uv_channel_ids[channel_index].append(direct_index)
                face_vertex_ids.append(position_index)
                face_geom_vertex_ids.append(position_index)

            for tri_offset in range(1, len(face_vertex_ids) - 1):
                triangle_local_indices = (0, tri_offset, tri_offset + 1)
                out_face_indices.extend(face_vertex_ids[index] for index in triangle_local_indices)
                out_geom_face_indices.extend(face_geom_vertex_ids[index] for index in triangle_local_indices)
                for channel_index, channel_payload in enumerate(out_uv_channel_payloads):
                    channel_payload["corner_indices"].extend(
                        face_uv_channel_ids[channel_index][index] for index in triangle_local_indices
                    )

        effective_skinned_is_local = _infer_effective_skinned_is_local(
            out_max_positions,
            out_skinned_positions,
            skinned_is_local_hint=skinned_is_local,
            node_to_world=node_to_world,
        )
        pose_channels = _build_skinned_pose_output_channels(
            out_skinned_positions,
            out_skinned_normals,
            skinned_is_local=effective_skinned_is_local,
            node_to_world=node_to_world,
        )
        audit = dict(normal_fidelity)
        audit.update(
            {
                "output_vertex_count": len(out_positions),
                "output_corner_count": len(out_face_indices),
                "output_triangle_count": len(out_face_indices) // 3,
                "normal_split_vertex_count": 0,
                "normal_space": "fbx_authored_corner_no_mesh_node_transform",
                "position_mode": "binary_fbx_scene_global_canonical_xyz",
            }
        )
        return {
            "positions": out_positions,
            "max_positions": out_max_positions,
            "world_positions": out_world_positions,
            "skinned_positions": out_skinned_positions,
            "skinned_max_positions": pose_channels["skinned_max_positions"],
            "skinned_world_positions": pose_channels["skinned_world_positions"],
            "normals": out_normals,
            "max_normals": out_max_normals,
            "skinned_normals": out_skinned_normals,
            "skinned_max_normals": pose_channels["skinned_max_normals"],
            "skinned_is_local": effective_skinned_is_local,
            "uvs": out_uvs,
            "face_indices": out_face_indices,
            "source_vertex_indices": out_source_vertex_indices,
            "fbx_export_corner_indices": out_source_corner_indices,
            "fbx_geom_face_indices": out_geom_face_indices,
            "fbx_export_face_indices": list(out_face_indices),
            "fbx_uv_channels": out_uv_channel_payloads,
            "normal_fidelity": audit,
            "vertex_count": len(out_positions),
            "index_count": len(out_face_indices),
            "triangle_count": len(out_face_indices) // 3,
        }, audit
    except Exception as exc:
        audit = dict(normal_fidelity)
        audit.update(
            {
                "status": "fallback",
                "mode": "legacy_ufbx_compatibility",
                "reason": f"strict_corner_rebuild_failed: {type(exc).__name__}: {exc}",
            }
        )
        return None, audit


def _extract_mesh_geometry(
    mesh: Any,
    instance_node: Any | None = None,
    *,
    binary_corner_geometry: dict[str, Any] | None = None,
    normal_fidelity: dict[str, Any] | None = None,
    use_global_axis_domain: bool = False,
) -> dict[str, Any]:
    fallback_audit = (
        dict(normal_fidelity)
        if isinstance(normal_fidelity, dict)
        else _binary_fbx_normal_fidelity_audit(status="fallback", reason="binary_corner_context_missing")
    )
    if isinstance(binary_corner_geometry, dict):
        strict_geometry, strict_audit = _extract_mesh_geometry_from_binary_corner_layers(
            mesh,
            instance_node,
            binary_corner_geometry,
            fallback_audit,
            use_global_axis_domain=use_global_axis_domain,
        )
        if isinstance(strict_geometry, dict):
            return strict_geometry
        fallback_audit = strict_audit
    # The binary corner reader is the only accepted normal source.  Keep the
    # legacy UFBX/accelerator geometry route out of this fallback until a real
    # ambiguous FBX proves a safe mapping; positions and UVs still use the
    # ordinary compatibility extraction below.
    fallback_audit = _disable_ufbx_normal_sources(fallback_audit)

    positions_src = getattr(mesh, "vertex_positions", None)
    normals_src = None
    skinned_positions_src = _get_mesh_skinned_vec3_rows(mesh, "skinned_position", "skinned_positions")
    skinned_normals_src: list[Any] = []
    skinned_is_local = _coerce_bool(getattr(mesh, "skinned_is_local", None), False)
    uvs_src = getattr(mesh, "vertex_uvs", None)
    indices_src = getattr(mesh, "indices", None)
    faces = list(getattr(mesh, "faces", []) or [])

    positions_count = len(positions_src) if positions_src is not None else 0
    normals_count = len(normals_src) if normals_src is not None else 0
    skinned_positions_count = len(skinned_positions_src)
    skinned_normals_count = len(skinned_normals_src)
    uvs_count = len(uvs_src) if uvs_src is not None else 0
    index_count = len(indices_src) if indices_src is not None else 0
    vertex_count = int(getattr(mesh, "num_vertices", positions_count) or positions_count)
    geom_indices = [int(index) for index in _safe_list(indices_src)]
    uv_channels = _extract_mesh_uv_channels(
        mesh,
        geom_indices=geom_indices,
        vertex_count=vertex_count,
    )

    if positions_src is None or indices_src is None:
        return {
            "positions": [],
            "max_positions": [],
            "world_positions": [],
            "skinned_positions": [],
            "skinned_max_positions": [],
            "skinned_world_positions": [],
            "normals": [],
            "max_normals": [],
            "skinned_normals": [],
            "skinned_max_normals": [],
            "skinned_is_local": skinned_is_local,
            "uvs": [],
            "face_indices": [],
            "source_vertex_indices": [],
            "fbx_export_corner_indices": [],
            "fbx_geom_face_indices": [],
            "fbx_export_face_indices": [],
            "fbx_uv_channels": uv_channels,
            "vertex_count": vertex_count,
            "index_count": index_count,
            "triangle_count": int(getattr(mesh, "num_triangles", 0) or 0),
            "normal_fidelity": fallback_audit,
        }

    source_normals = _build_source_vertex_normals(
        positions_count,
        normals_src,
        indices_src,
        vertex_count=vertex_count,
        index_count=index_count,
    )

    out_positions: list[list[float]] = []
    out_max_positions: list[list[float]] = []
    out_world_positions: list[list[float]] = []
    out_skinned_positions: list[list[float]] = []
    out_normals: list[list[float]] = []
    out_max_normals: list[list[float]] = []
    out_skinned_normals: list[list[float]] = []
    out_uvs: list[list[float]] = []
    out_face_indices: list[int] = []
    out_source_vertex_indices: list[int] = []
    out_source_corner_indices: list[int] = []
    out_geom_face_indices: list[int] = []
    out_uv_channel_payloads = [
        {
            "channel": int(channel.get("channel", 1)),
            "name": str(channel.get("name", "") or ""),
            "values": [list(_vec2_to_list(value)) for value in channel.get("values", [])] if isinstance(channel.get("values"), list) else [],
            "corner_indices": [],
        }
        for channel in uv_channels
    ]
    vertex_map: dict[tuple[int, Any, tuple[int, int, int]], int] = {}
    node_to_world = getattr(instance_node, "node_to_world", None)
    prepared_transform = _prepare_row_major_transform(node_to_world)
    # Keep the compatibility extractor on the same canonical position chain
    # as the strict corner reader.  The node matrix is authored placement;
    # Generic's file-level normalization, not this Mesh, owns axis policy.
    node_world_matrix = _binary_fbx_matrix(_flatten_matrix4x4(node_to_world))
    world_to_max_matrix = _identity_matrix() if node_world_matrix is not None else None
    default_uv_channel = uv_channels[0] if uv_channels else None

    for face_begin, face_size in faces:
        face_vertex_ids: list[int] = []
        face_geom_vertex_ids: list[int] = []
        face_uv_channel_ids = [[] for _ in out_uv_channel_payloads]
        for local_offset in range(int(face_size)):
            corner_index = int(face_begin) + local_offset
            if not (0 <= corner_index < index_count):
                continue
            position_index = int(indices_src[corner_index])
            uv_value, uv_key = _resolve_corner_uv_from_channel(
                default_uv_channel,
                position_index=position_index,
                corner_index=corner_index,
                vertex_count=vertex_count,
                index_count=index_count,
            )
            corner_normal = _resolve_corner_normal(
                normals_src,
                source_normals,
                position_index=position_index,
                corner_index=corner_index,
                vertex_count=vertex_count,
                index_count=index_count,
            )
            face_geom_vertex_ids.append(position_index)
            for channel_index, channel_payload in enumerate(out_uv_channel_payloads):
                source_corner_indices = uv_channels[channel_index].get("corner_indices", [])
                try:
                    raw_uv_index = int(source_corner_indices[corner_index]) if corner_index < len(source_corner_indices) else 0
                except Exception:
                    raw_uv_index = 0
                face_uv_channel_ids[channel_index].append(raw_uv_index)
            # Split only when RE6 can preserve a different final normal byte triplet.
            # This retains every representable FBX hard edge without creating rows
            # for float differences that collapse to the same MOD normal.
            key = (
                position_index,
                uv_key,
                _encode_re6_normal_key_from_fbx_local(corner_normal, prepared_transform),
            )
            vertex_id = vertex_map.get(key)
            if vertex_id is None:
                vertex_id = len(out_positions)
                vertex_map[key] = vertex_id
                if 0 <= position_index < positions_count:
                    local_pos = _vec3_to_list(positions_src[position_index])
                    world_pos = _transform_position_row_major(local_pos, prepared_transform)
                    max_pos = (
                        _transform_position_row_major(world_pos, world_to_max_matrix)
                        if world_to_max_matrix is not None
                        else _fbx_world_to_max_vec3(world_pos)
                    )
                    out_positions.append(local_pos)
                    out_world_positions.append(max_pos)
                    out_max_positions.append(max_pos)
                else:
                    out_positions.append(_default_vec3())
                    out_world_positions.append(_default_vec3())
                    out_max_positions.append(_default_vec3())
                skinned_pos = out_positions[-1]
                if skinned_positions_count > 0:
                    skinned_attr_index = _resolve_vertex_attr_index(
                        skinned_positions_count,
                        position_index=position_index,
                        corner_index=corner_index,
                        vertex_count=vertex_count,
                        index_count=index_count,
                    )
                    if skinned_attr_index is not None and 0 <= skinned_attr_index < skinned_positions_count:
                        skinned_pos = _vec3_to_list(skinned_positions_src[skinned_attr_index])
                out_skinned_positions.append(list(skinned_pos))
                local_normal = corner_normal
                max_normal = _fbx_world_to_max_normal(
                    _transform_normal_row_major(local_normal, prepared_transform)
                )
                out_normals.append(local_normal)
                out_max_normals.append(max_normal)
                skinned_normal = out_normals[-1]
                if skinned_normals_count > 0:
                    skinned_normal_index = _resolve_vertex_attr_index(
                        skinned_normals_count,
                        position_index=position_index,
                        corner_index=corner_index,
                        vertex_count=vertex_count,
                        index_count=index_count,
                    )
                    if skinned_normal_index is not None and 0 <= skinned_normal_index < skinned_normals_count:
                        skinned_normal = _vec3_to_list(skinned_normals_src[skinned_normal_index])
                out_skinned_normals.append(list(skinned_normal))
                out_uvs.append(uv_value)
                out_source_vertex_indices.append(position_index)
                out_source_corner_indices.append(corner_index)
            face_vertex_ids.append(vertex_id)
        if len(face_vertex_ids) < 3 or len(face_geom_vertex_ids) < 3:
            continue
        for tri_offset in range(1, len(face_vertex_ids) - 1):
            out_face_indices.extend(
                [
                    face_vertex_ids[0],
                    face_vertex_ids[tri_offset],
                    face_vertex_ids[tri_offset + 1],
                ]
            )
            out_geom_face_indices.extend(
                [
                    face_geom_vertex_ids[0],
                    face_geom_vertex_ids[tri_offset],
                    face_geom_vertex_ids[tri_offset + 1],
                ]
            )
            for channel_index, channel_payload in enumerate(out_uv_channel_payloads):
                channel_face_ids = face_uv_channel_ids[channel_index]
                if len(channel_face_ids) < len(face_vertex_ids):
                    continue
                channel_payload["corner_indices"].extend(
                    [
                        channel_face_ids[0],
                        channel_face_ids[tri_offset],
                        channel_face_ids[tri_offset + 1],
                    ]
                )

    effective_skinned_is_local = _infer_effective_skinned_is_local(
        out_max_positions,
        out_skinned_positions,
        skinned_is_local_hint=skinned_is_local,
        node_to_world=node_to_world,
    )
    pose_channels = _build_skinned_pose_output_channels(
        out_skinned_positions,
        out_skinned_normals,
        skinned_is_local=effective_skinned_is_local,
        node_to_world=node_to_world,
    )

    geometry = {
        "positions": out_positions,
        "max_positions": out_max_positions,
        "world_positions": out_world_positions,
        "skinned_positions": out_skinned_positions,
        "skinned_max_positions": pose_channels["skinned_max_positions"],
        "skinned_world_positions": pose_channels["skinned_world_positions"],
        "normals": out_normals,
        "max_normals": out_max_normals,
        "skinned_normals": out_skinned_normals,
        "skinned_max_normals": pose_channels["skinned_max_normals"],
        "skinned_is_local": effective_skinned_is_local,
        "uvs": out_uvs,
        "face_indices": out_face_indices,
        "source_vertex_indices": out_source_vertex_indices,
        "fbx_export_corner_indices": out_source_corner_indices,
        "fbx_geom_face_indices": out_geom_face_indices,
        "fbx_export_face_indices": list(out_face_indices),
        "fbx_uv_channels": out_uv_channel_payloads,
        "vertex_count": len(out_positions),
        "index_count": len(out_face_indices),
        "triangle_count": len(out_face_indices) // 3,
        "normal_fidelity": fallback_audit,
    }
    return geometry


# ====== END CANONICAL SCENE / SKIN EXTRACTION ======
# ====== BEGIN PUBLIC PROBE HANDOFF / RECEIPTS ======

def parse_bone_id_from_name(bone_name: str | None, default_value: int | None = None) -> int | None:
    """Read the RE6 bone ID from the stable ``b_<parent>_<id>`` prefix.

    FBX importers append physical-instance suffixes such as ``_Import2``.
    Those suffixes are not part of the skeleton identity and must never alter
    the ID used by Skin influences.
    """
    name_text = str(bone_name or "").strip()
    match = re.match(r"(?i)^b_(\d+)_(\d+)", name_text)
    if match is None:
        return default_value
    try:
        parsed = int(match.group(2)) - 1
    except (TypeError, ValueError, OverflowError):
        return default_value
    return parsed if parsed >= 0 else default_value


def _enum_name_upper(value: Any) -> str:
    if value is None:
        return ""
    name = getattr(value, "name", None)
    if isinstance(name, str) and name != "":
        return name.upper()
    try:
        return str(value).upper()
    except Exception:
        return ""


def _node_looks_like_bone(node: Any) -> bool:
    if node is None or getattr(node, "mesh", None) is not None:
        return False
    if getattr(node, "bone", None) is not None:
        return True
    node_name = str(getattr(node, "name", "") or "")
    if parse_bone_id_from_name(node_name, default_value=None) is not None:
        return True
    return "BONE" in _enum_name_upper(getattr(node, "attrib_type", None))


def _scene_bone_object_is_usable(bone: Any) -> bool:
    if bone is None:
        return False
    if str(getattr(bone, "name", "") or "") != "":
        return True
    for attr_name in ("node_to_world", "geometry_to_world", "node_to_parent", "geometry_to_parent"):
        if getattr(bone, attr_name, None) is not None:
            return True
    return getattr(bone, "parent", None) is not None


def _scene_bone_objects(scene: Any) -> list[Any]:
    scene_bones_raw = _safe_list(getattr(scene, "bones", None))
    scene_bones = [bone for bone in scene_bones_raw if _scene_bone_object_is_usable(bone)]
    bone_nodes = [node for node in _safe_list(getattr(scene, "nodes", None)) if _node_looks_like_bone(node)]
    named_bone_nodes = [
        node
        for node in bone_nodes
        if parse_bone_id_from_name(str(getattr(node, "name", "") or ""), default_value=None) is not None
    ]
    if len(named_bone_nodes) > 0:
        return named_bone_nodes
    if len(scene_bones_raw) > 0:
        if len(scene_bones) == len(scene_bones_raw):
            return scene_bones
        if len(bone_nodes) >= len(scene_bones):
            return bone_nodes
        if len(scene_bones) > 0:
            return scene_bones
    return bone_nodes


def _extract_source_skin_rows(mesh: Any, source_vertex_count: int) -> tuple[list[str], list[list[int]], list[list[float]]]:
    bone_names: list[str] = []
    raw_bones_rows = [[] for _ in range(max(0, source_vertex_count))]
    raw_weight_rows = [[] for _ in range(max(0, source_vertex_count))]

    if len(_safe_list(getattr(mesh, "skin_deformers", None))) < 1:
        return bone_names, raw_bones_rows, raw_weight_rows

    primary = mesh.skin_deformers[0]
    for cluster_index, cluster in enumerate(primary.clusters):
        bone_name = str(getattr(cluster, "bone_name", "") or getattr(cluster, "name", "") or "")
        game_bone = parse_bone_id_from_name(bone_name, default_value=None)
        if game_bone is None:
            # COMPATIBILITY CONTRACT -- DO NOT RESTORE THE OLD EXCEPTION:
            # Max/FBX Skin data may contain renamed helpers, arbitrary link
            # nodes, or stale clusters whose names are not ``b_<parent>_<id>``.
            # Such a cluster has no deterministic RE6 bone ID, so discard the
            # entire cluster and continue exporting every valid RE6 cluster.
            # Raising here aborts an otherwise usable Mesh export. The policy
            # is locked by _run_skin_cluster_name_policy_regression_guard().
            continue
        if bone_name != "":
            bone_names.append(bone_name)
        vertices = _safe_list(getattr(cluster, "vertices", None))
        weights = _safe_list(getattr(cluster, "weights", None))
        row_count = min(len(vertices), len(weights))
        for row_index in range(row_count):
            try:
                source_vertex = int(vertices[row_index])
                raw_weight = float(weights[row_index])
            except Exception:
                continue
            if source_vertex < 0 or source_vertex >= source_vertex_count:
                continue
            if raw_weight <= 0.0:
                continue
            raw_bones_rows[source_vertex].append(int(game_bone))
            raw_weight_rows[source_vertex].append(float(raw_weight))
    return bone_names, raw_bones_rows, raw_weight_rows


def _expand_skin_rows_for_export(
    source_bones_rows: list[list[int]],
    source_weight_rows: list[list[float]],
    source_vertex_indices: list[int],
) -> tuple[list[list[int]], list[list[float]], int, int]:
    out_bones: list[list[int]] = []
    out_weights: list[list[float]] = []
    weighted_vertex_count = 0
    max_weights_per_vertex = 0
    for source_vertex in source_vertex_indices:
        if 0 <= source_vertex < len(source_bones_rows):
            row_bones = list(source_bones_rows[source_vertex])
            row_weights = list(source_weight_rows[source_vertex])
        else:
            row_bones = []
            row_weights = []
        out_bones.append(row_bones)
        out_weights.append(row_weights)
        row_count = min(len(row_bones), len(row_weights))
        if row_count > 0:
            weighted_vertex_count += 1
            max_weights_per_vertex = max(max_weights_per_vertex, row_count)
    return out_bones, out_weights, weighted_vertex_count, max_weights_per_vertex


def _build_skin_summary_variants(
    mesh: Any,
    *,
    export_source_vertex_indices: list[int] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    skin_deformer_count = len(mesh.skin_deformers)
    if skin_deformer_count < 1:
        empty = {
            "skin_deformer_count": 0,
            "bone_names": [],
            "max_weights_per_vertex": 0,
            "weighted_vertex_count": 0,
            "weight_rows_available": False,
        }
        return empty, dict(empty)

    source_vertex_count = int(getattr(mesh, "num_vertices", 0) or 0)
    bone_names, source_bones_rows, source_weight_rows = _extract_source_skin_rows(mesh, source_vertex_count)
    summary_source_vertex_indices = list(range(source_vertex_count))
    contract_source_vertex_indices = (
        [int(index) for index in export_source_vertex_indices]
        if isinstance(export_source_vertex_indices, list)
        else list(summary_source_vertex_indices)
    )

    def _build_summary(source_vertex_indices: list[int]) -> dict[str, Any]:
        raw_bones, raw_weights, weighted_vertex_count, max_weights_per_vertex = _expand_skin_rows_for_export(
            source_bones_rows,
            source_weight_rows,
            source_vertex_indices,
        )
        return {
            "skin_deformer_count": skin_deformer_count,
            "bone_names": bone_names,
            "max_weights_per_vertex": max_weights_per_vertex,
            "weighted_vertex_count": weighted_vertex_count,
            "weight_rows_available": weighted_vertex_count > 0,
            "raw_bones": raw_bones,
            "raw_weights": raw_weights,
        }

    return _build_summary(summary_source_vertex_indices), _build_summary(contract_source_vertex_indices)


def _extract_skin_summary(mesh: Any, *, source_vertex_indices: list[int] | None = None) -> dict[str, Any]:
    summary_skin, contract_skin = _build_skin_summary_variants(
        mesh,
        export_source_vertex_indices=source_vertex_indices,
    )
    if isinstance(source_vertex_indices, list):
        return contract_skin
    return summary_skin


def _require_scene(
    path: str | Path,
    *,
    use_generic_normalizer: bool = True,
    backend_kind: Any = "",
) -> tuple[Path, Any]:
    # ========================================================================
    # CANONICAL INPUT BOUNDARY
    # ========================================================================
    # Every real FBX is normalized in memory before any UFBX/binary scene read.
    # ``backend_kind`` is metadata only; it can select the Blender unit receipt
    # and MAX RouteHandle identity parsing, never a second geometry path.
    fbx_path = Path(path).resolve()
    # Synthetic paths are used by runtime self-tests; real files always use
    # Generic regardless of the caller's producer label or option value.  Keep
    # this invariant explicit: no caller can reopen a producer FBX through the
    # retired direct-UFBX path by passing ``use_generic_normalizer=False``.
    is_real_fbx_file = fbx_path.is_file()
    if is_real_fbx_file:
        use_generic_normalizer = True
    # Runtime self-tests and diagnostics intentionally use synthetic paths.
    # Keep their old loader behavior; real FBX requests are handled below.
    if not is_real_fbx_file:
        if ufbx is None:
            return fbx_path, ufbx_missed_substitute(fbx_path)
        try:
            return fbx_path, ufbx.load_file(str(fbx_path))
        except Exception as exc:
            details = classify_fbx_probe_exception(exc, stage="load_file")
            if not bool(details["runtime_retryable"]):
                raise
            scene = ufbx_missed_substitute(fbx_path)
            if isinstance(getattr(scene, "metadata", None), dict):
                scene.metadata["native_ufbx_failure"] = details
            return fbx_path, scene

    if use_generic_normalizer:
        try:
            document, generic_receipt = _generic_memory_document_for_path(fbx_path)
        except Exception as exc:
            if getattr(exc, "export_blocking", False):
                raise
            # MAX and Blender exports are Generic-only. Never reopen the raw
            # direct/UFBX axis path after normalization fails.
            raise RuntimeError(
                f"Generic FBX normalization failed for {backend_kind}: {exc}"
            ) from exc
        normalized_bytes = document.data
        if ufbx is not None:
            load_memory = getattr(ufbx, "load_memory", None)
            if callable(load_memory):
                try:
                    scene = load_memory(normalized_bytes)
                except Exception as exc:
                    # A Generic rebuild can be structurally valid for our
                    # binary reader but still be rejected by a particular
                    # native UFBX build. Keep the normalized binary document
                    # in the substitute path; re-reading the original here
                    # would silently bypass the MAX Generic lane and restore
                    # the producer's unnormalized transforms.
                    details = classify_fbx_probe_exception(exc, stage="load_memory")
                    scene = ufbx_missed_substitute(
                        fbx_path,
                        binary_document=document,
                    )
                    generic_receipt = dict(generic_receipt)
                    generic_receipt.update(
                        {
                            # The bytes are still the normalized document;
                            # keep the established status so downstream
                            # normal/axis receipt consumers do not discard it.
                            "status": "normalized",
                            "reason": "native_memory_parse_failed_preserved_normalized_document",
                            "native_memory_failure": details,
                        }
                    )
            else:
                scene = ufbx_missed_substitute(
                    fbx_path,
                    binary_document=document,
                )
        else:
            scene = ufbx_missed_substitute(
                fbx_path,
                binary_document=document,
            )
        if isinstance(getattr(scene, "metadata", None), dict):
            scene.metadata["generic_fbx_normalization"] = generic_receipt
        return fbx_path, scene

    if ufbx is None:
        return fbx_path, ufbx_missed_substitute(fbx_path)
    try:
        scene = ufbx.load_file(str(fbx_path))
    except Exception as exc:
        details = classify_fbx_probe_exception(exc, stage="load_file")
        if not bool(details["runtime_retryable"]):
            raise
        scene = ufbx_missed_substitute(fbx_path)
        if isinstance(getattr(scene, "metadata", None), dict):
            scene.metadata["native_ufbx_failure"] = details
        return fbx_path, scene
    return fbx_path, scene


def _summarize_scene(
    scene: Any,
    *,
    fbx_path: str,
    binary_model_queues: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    mesh_summaries: list[dict[str, Any]] = []
    for mesh, instance_node in _mesh_node_pairs(scene):
        node_name = str(instance_node.name) if instance_node is not None else ""
        mesh_name = str(mesh.name)
        binary_identity = _take_binary_fbx_mesh_model_identity(
            binary_model_queues or {},
            instance_node,
        )
        vertex_positions = mesh.vertex_positions if mesh.vertex_positions is not None else []
        vertex_uv = mesh.vertex_uvs if mesh.vertex_uvs is not None else []
        normal_values = mesh.vertex_normals if mesh.vertex_normals is not None else []
        has_positions = len(vertex_positions) > 0
        has_uv = len(vertex_uv) > 0
        has_normals = len(normal_values) > 0
        material_names = _mesh_material_names(mesh)
        skin_summary = _extract_skin_summary(mesh)

        mesh_summaries.append(
            {
                "node_name": node_name,
                "mesh_name": mesh_name,
                "match_name": normalize_match_name(node_name or mesh_name),
                "mesh_name_match": normalize_match_name(mesh_name),
                "mesh_slot_hint": infer_mesh_slot_hint(node_name) or infer_mesh_slot_hint(mesh_name),
                "lod_hint": infer_lod_hint(node_name) or infer_lod_hint(mesh_name),
                "vertex_count": int(getattr(mesh, "num_vertices", len(vertex_positions)) or len(vertex_positions)),
                "index_count": int(getattr(mesh, "num_indices", 0) or 0),
                "face_count": int(getattr(mesh, "num_faces", len(mesh.faces)) or len(mesh.faces)),
                "triangle_count": int(getattr(mesh, "num_triangles", 0) or 0),
                "material_names": material_names,
                "material_slot_count": len(material_names),
                "material_part_count": None,
                "bbox": _bbox_from_vertex_values(vertex_positions) if has_positions else BoundingBox().to_dict(),
                "position_preview": _take_preview_values(vertex_positions, 5, _vec3_to_list) if has_positions else [],
                "uv_preview": _take_preview_values(vertex_uv, 5, _vec2_to_list) if has_uv else [],
                "normal_preview": _take_preview_values(normal_values, 5, _vec3_to_list) if has_normals else [],
                "face_preview": [
                    {
                        "index_begin": int(face[0]),
                        "num_indices": int(face[1]),
                    }
                    for face in list(mesh.faces)[:5]
                ],
                **binary_identity,
                **skin_summary,
            }
        )

    material_names_seen: dict[str, dict[str, Any]] = {}
    for mesh_summary in mesh_summaries:
        for material_name in mesh_summary["material_names"]:
            if material_name not in material_names_seen:
                material_names_seen[material_name] = {"name": material_name}
    material_summaries = list(material_names_seen.values())

    bone_summaries: list[dict[str, Any]] = []
    for bone in _scene_bone_objects(scene):
        bone_name = str(getattr(bone, "name", "") or "")
        parent_node = getattr(getattr(bone, "parent", None), "name", None)
        parent_name = str(parent_node or "")
        node_to_world = (
            _max_import_matrix_from_fbx_matrix(getattr(bone, "node_to_world", None))
            or _max_import_matrix_from_fbx_matrix(getattr(bone, "geometry_to_world", None))
        )
        local_matrix = (
            _max_import_matrix_from_fbx_matrix(getattr(bone, "node_to_parent", None))
            or _max_import_matrix_from_fbx_matrix(getattr(bone, "geometry_to_parent", None))
        )
        bone_summaries.append(
            {
                "name": bone_name,
                "is_root": bool(getattr(bone, "is_root", False) or parent_name == ""),
                "parsed_bone_id": parse_bone_id_from_name(bone_name, default_value=None),
                "parent_name": parent_name,
                "parent_parsed_bone_id": parse_bone_id_from_name(parent_name, default_value=None),
                "world_matrix": node_to_world,
                "local_matrix": local_matrix,
            }
        )

    node_summaries: list[dict[str, Any]] = []
    for node in scene.nodes:
        node_name = str(node.name)
        node_summaries.append(
            {
                "name": node_name,
                "type": "mesh" if getattr(node, "mesh", None) is not None else "node",
                "material_count": len(getattr(getattr(node, "mesh", None), "materials", []) or []),
                "match_name": normalize_match_name(node_name),
                "mesh_slot_hint": infer_mesh_slot_hint(node_name),
                "lod_hint": infer_lod_hint(node_name),
            }
        )

    return {
        "fbx_path": str(fbx_path),
        "stats": {
            "mesh_count": len(mesh_summaries),
            "material_count": len(material_summaries),
            "bone_count": len(bone_summaries),
            "node_count": len(node_summaries),
            "skin_cluster_count": sum(len(mesh.get("bone_names", [])) for mesh in mesh_summaries),
        },
        "meshes": mesh_summaries,
        "materials": material_summaries,
        "bones": bone_summaries,
        "nodes": node_summaries,
    }


def summarize_fbx(path: str | Path) -> dict[str, Any]:
    # Public summary uses the exact same Generic-backed handoff as export.
    payload = probe_fbx_handoff(path, probe_mode="full")
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        raise RuntimeError("Canonical FBX Probe returned no summary")
    result = dict(summary)
    result["generic_fbx_normalization"] = dict(
        payload.get("generic_fbx_normalization", {})
    )
    result["fbx_axis_output_policy"] = "max_xyz"
    result["fbx_axis_transform_scope"] = "scene_global_once"
    result["canonical_axis_domain"] = FBX_NORMAL_AXIS_DOMAIN_CANONICAL
    result["canonical_unit_domain"] = "max_inches_numeric"
    return result


def _generic_normal_axis_domain_for_geometry(
    generic_normalization: Any,
    binary_identity: dict[str, Any] | None,
) -> str:
    """Return the canonical normal domain for a validated Generic Geometry."""
    if not isinstance(generic_normalization, dict):
        return ""
    if str(generic_normalization.get("status", "") or "").strip().lower() != "normalized":
        return ""
    normalization = generic_normalization.get("normalization")
    if not isinstance(normalization, dict):
        normalization = generic_normalization
    geometry_id = _int_or_default(
        (binary_identity or {}).get("fbx_geometry_id"),
        0,
    )
    if geometry_id <= 0:
        return ""
    by_geometry = normalization.get("normal_axis_domain_by_geometry_id")
    if isinstance(by_geometry, dict):
        domain = str(
            by_geometry.get(str(geometry_id), by_geometry.get(geometry_id, ""))
            or ""
        ).strip().lower()
        if domain:
            return FBX_NORMAL_AXIS_DOMAIN_CANONICAL
    canonical_ids = {
        _int_or_default(value, 0)
        for value in normalization.get("canonical_normal_geometry_ids", [])
        if _int_or_default(value, 0) > 0
    }
    if geometry_id in canonical_ids:
        return FBX_NORMAL_AXIS_DOMAIN_CANONICAL
    # Validated Generic output has one scene-global axis domain. A missing
    # historical per-Geometry marker cannot reactivate the legacy normal path.
    return FBX_NORMAL_AXIS_DOMAIN_CANONICAL


def _build_probe_contract_mesh_row(
    *,
    node_name: str,
    mesh_name: str,
    instance_node: Any | None,
    material_names: list[str],
    geometry: dict[str, Any],
    normal_fidelity: dict[str, Any],
    skin: dict[str, Any],
    binary_identity: dict[str, Any] | None = None,
    probe_route: str = "",
    probe_route_authority: str = "",
    probe_route_match_status: str = "",
    probe_geometry_required: bool = False,
    probe_geometry_skipped: bool = False,
    probe_skip_reason: str = "",
    probe_topology_scanned: bool = False,
    generic_normalization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the single authoritative Mesh contract row for Probe consumers.

    Both the public extraction adapter and the runtime handoff use this
    builder.  Route metadata is opt-in so the legacy extraction API keeps its
    established shape while the handoff retains Stage A/B facts.
    """
    geometry_normal_fidelity = geometry.get("normal_fidelity", normal_fidelity)
    normal_axis_domain = _generic_normal_axis_domain_for_geometry(
        generic_normalization,
        binary_identity,
    )
    if normal_axis_domain:
        geometry_normal_fidelity = dict(
            geometry_normal_fidelity
            if isinstance(geometry_normal_fidelity, dict)
            else {}
        )
        geometry_normal_fidelity["normal_axis_domain"] = normal_axis_domain
    max_position_rows = geometry.get("max_positions")
    if not isinstance(max_position_rows, list):
        max_position_rows = []
    skinned_max_position_rows = geometry.get("skinned_max_positions")
    if not isinstance(skinned_max_position_rows, list):
        skinned_max_position_rows = []
    # These are the two stable position authorities exposed to the Bridge:
    # ordinary export consumes max_positions, while bone-edit export consumes
    # skinned_max_positions.  Both are already canonical and are always
    # present, even when a header-only row has no geometry payload.
    row: dict[str, Any] = {
        "node_name": node_name,
        "mesh_name": mesh_name,
        "match_name": normalize_match_name(node_name or mesh_name),
        "mesh_name_match": normalize_match_name(mesh_name),
        "mesh_slot_hint": infer_mesh_slot_hint(node_name) or infer_mesh_slot_hint(mesh_name),
        "lod_hint": infer_lod_hint(node_name) or infer_lod_hint(mesh_name),
        "material_names": material_names,
        "positions": geometry.get("positions", []),
        "max_positions": max_position_rows,
        "skinned_positions": geometry.get("skinned_positions", []),
        "skinned_max_positions": skinned_max_position_rows,
        "skinned_world_positions": geometry.get("skinned_world_positions", []),
        "normals": geometry.get("normals", []),
        "max_normals": geometry.get("max_normals", []),
        "skinned_normals": geometry.get("skinned_normals", []),
        "skinned_max_normals": geometry.get("skinned_max_normals", []),
        "binary_skin_unreferenced_source_indices": geometry.get(
            "binary_skin_unreferenced_source_indices", []
        ),
        "binary_skin_unreferenced_max_positions": geometry.get(
            "binary_skin_unreferenced_max_positions", []
        ),
        "skinned_is_local": geometry.get("skinned_is_local", False),
        "fbx_skin_pose_evaluation_status": geometry.get(
            "fbx_skin_pose_evaluation_status", ""
        ),
        "fbx_skin_pose_evaluation_schema": geometry.get(
            "fbx_skin_pose_evaluation_schema", ""
        ),
        "fbx_skin_pose_evaluation_mode": geometry.get(
            "fbx_skin_pose_evaluation_mode", ""
        ),
        "uvs": geometry.get("uvs", []),
        "face_indices": geometry.get("face_indices", []),
        "source_vertex_indices": geometry.get("source_vertex_indices", []),
        "fbx_export_corner_indices": geometry.get("fbx_export_corner_indices", []),
        "fbx_geom_face_indices": geometry.get("fbx_geom_face_indices", []),
        "fbx_export_face_indices": geometry.get("fbx_export_face_indices", []),
        "fbx_uv_channels": geometry.get("fbx_uv_channels", []),
        "normal_fidelity": geometry_normal_fidelity,
        "vertex_count": geometry.get("vertex_count", 0),
        "triangle_count": geometry.get("triangle_count", 0),
        "position_domain": CANONICAL_AXIS_DOMAIN,
        "position_unit_domain": CANONICAL_UNIT_DOMAIN,
    }
    if normal_axis_domain:
        row["fbx_normal_axis_domain"] = normal_axis_domain
    if isinstance(skin, dict):
        row.update(skin)
    if isinstance(binary_identity, dict) and binary_identity:
        row.update(binary_identity)
    if probe_route != "" or probe_route_authority != "":
        row.update(
            {
                "fbx_probe_route": probe_route,
                "fbx_probe_route_authority": probe_route_authority,
                "fbx_probe_route_match_status": probe_route_match_status,
                "fbx_probe_geometry_required": bool(probe_geometry_required),
                "fbx_probe_geometry_skipped": bool(probe_geometry_skipped),
                "fbx_probe_skip_reason": probe_skip_reason,
                "fbx_probe_topology_scanned": bool(probe_topology_scanned),
            }
        )
    node_to_world = _flatten_matrix4x4(
        getattr(instance_node, "node_to_world", None)
    )
    if len(node_to_world) == 16:
        row["fbx_node_to_world_matrix"] = node_to_world
    return row


def _extract_scene_mesh_contracts(
    scene: Any,
    *,
    skin_context: dict[str, Any] | None = None,
    binary_model_queues: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
    binary_corner_context: dict[str, Any] | None = None,
    binary_document: _BinaryFbxDocument | None = None,
) -> list[dict[str, Any]]:
    contracts: list[dict[str, Any]] = []
    for mesh, instance_node in _mesh_node_pairs(scene):
        node_name = str(instance_node.name) if instance_node is not None else ""
        mesh_name = str(mesh.name)
        binary_identity = _take_binary_fbx_mesh_model_identity(
            binary_model_queues or {}, instance_node
        )
        binary_corner_geometry, normal_fidelity = _select_binary_fbx_corner_geometry(
            mesh,
            binary_corner_context,
            binary_identity,
        )
        geometry = _extract_mesh_geometry(
            mesh,
            instance_node,
            binary_corner_geometry=binary_corner_geometry,
            normal_fidelity=normal_fidelity,
        )
        geometry = _augment_geometry_with_skinned_pose_channels(geometry, mesh, instance_node)
        geometry = _apply_binary_fbx_skin_pose_channels(
            geometry,
            mesh,
            instance_node,
            skin_context,
        )
        material_names = _mesh_material_names(mesh)
        skin_summary = _extract_skin_summary(mesh, source_vertex_indices=geometry["source_vertex_indices"])
        contract_mesh = _build_probe_contract_mesh_row(
            node_name=node_name,
            mesh_name=mesh_name,
            instance_node=instance_node,
            material_names=material_names,
            geometry=geometry,
            normal_fidelity=normal_fidelity,
            skin=skin_summary,
            binary_identity=binary_identity,
        )
        contracts.append(contract_mesh)
    return contracts


def extract_fbx_mesh_contracts(path: str | Path) -> list[dict[str, Any]]:
    # Auxiliary callers must not bypass the canonical scene handoff.
    payload = probe_fbx_handoff(path, probe_mode="full")
    contracts = payload.get("contract_meshes")
    if not isinstance(contracts, list):
        raise RuntimeError("Canonical FBX Probe returned no mesh contracts")
    return contracts


def _build_probe_stage_a_plan(
    scene: Any,
    *,
    binary_model_queues: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
    probe_mode: str = "full",
    target_handles: set[int] | None = None,
    target_names: set[str] | None = None,
    target_slots: set[int] | None = None,
    route_hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify every Mesh once before any exact Geometry array is consumed."""
    normalized_target_handles = {
        _int_or_default(value, 0)
        for value in (target_handles or set())
        if _int_or_default(value, 0) > 0
    }
    normalized_target_names = {
        normalize_match_name(value)
        for value in (target_names or set())
        if normalize_match_name(value)
    }
    normalized_target_slots = {
        _int_or_default(value, 0)
        for value in (target_slots or set())
        if _int_or_default(value, 0) > 0
    }
    normalized_route_hints = _normalize_probe_route_hints(route_hints)
    unskinned_edit_policy = normalized_route_hints.get(
        "unskinned_mesh_edit_export", {}
    )
    allow_unskinned_geometry = bool(
        isinstance(unskinned_edit_policy, dict)
        and unskinned_edit_policy.get("authorized") is True
    )
    probe_mode_key = str(probe_mode or "full").casefold()
    route_hint_filter_active = (
        normalized_route_hints.get("status") == "ok"
        and bool(normalized_route_hints.get("rows"))
        and (
            probe_mode_key == "export_required"
            or bool(normalized_route_hints.get("explicit_route_required", False))
        )
    )
    filter_active = str(probe_mode or "full").casefold() == "export_required" and bool(
        normalized_target_handles
        or normalized_target_names
        or normalized_target_slots
        or route_hint_filter_active
    )
    entries: list[dict[str, Any]] = []
    route_hint_match_count = 0
    route_hint_ambiguous_count = 0
    route_hint_unmatched_count = 0
    identity_excluded_count = 0
    exact_geometry_ids: set[int] = set()
    exact_geometry_names: set[str] = set()
    exact_mesh_names: set[str] = set()

    for mesh, instance_node in _mesh_node_pairs(scene):
        node_name = str(instance_node.name) if instance_node is not None else ""
        mesh_name = str(mesh.name)
        binary_identity = _take_binary_fbx_mesh_model_identity(
            binary_model_queues or {},
            instance_node,
            preferred_route_handles=(
                normalized_target_handles
                if bool(normalized_route_hints.get("explicit_route_required", False))
                else None
            ),
        )
        target_mesh = _probe_stage_a_mesh_target(
            mesh,
            instance_node,
            binary_identity,
            target_handles=normalized_target_handles,
            target_names=normalized_target_names,
            target_slots=normalized_target_slots,
            filter_active=filter_active,
        )
        route_hint, route_hint_match = (
            _probe_route_hint_for_mesh(
                mesh,
                instance_node,
                binary_identity,
                normalized_route_hints,
            )
            if route_hint_filter_active
            else (None, "disabled")
        )
        if route_hint is not None:
            route_hint_match_count += 1
        elif route_hint_match.startswith("ambiguous_"):
            route_hint_ambiguous_count += 1
        elif route_hint_filter_active and route_hint_match == "unmatched":
            route_hint_unmatched_count += 1

        hint_lane = (
            str(route_hint.get("lane", "") or "").strip().lower()
            if route_hint
            else ""
        )
        identity_excluded = route_hint_match == "identity_excluded"
        if identity_excluded:
            # This is an unmarked/wrongly marked Max duplicate.  Keep it out
            # of Stage B entirely; the correctly marked sibling owns the route.
            identity_excluded_count += 1
        launcher_route_only = hint_lane in {"header", "delete"}
        if identity_excluded:
            has_skin = False
            triangle_count = 0
            topology_scanned = False
        elif launcher_route_only:
            has_skin = False
            triangle_count = 0
            topology_scanned = False
        else:
            has_skin = _probe_mesh_has_skin(mesh)
            triangle_count = _probe_mesh_triangle_count(mesh)
            topology_scanned = True

        if identity_excluded:
            exact_payload_required = False
            probe_route = ""
            probe_route_authority = "fbx_probe_identity_filter"
            probe_geometry_required = False
            probe_skip_reason = "max_unmarked_or_mismatched_route_identity"
        elif hint_lane == "header":
            exact_payload_required = False
            probe_route = "header_only"
            probe_route_authority = "launcher_bucket_receipt"
            probe_geometry_required = False
            probe_skip_reason = "launcher_header_route"
        elif hint_lane == "delete":
            exact_payload_required = False
            probe_route = "delete"
            probe_route_authority = "launcher_bucket_receipt"
            probe_geometry_required = False
            probe_skip_reason = "launcher_delete_route"
        elif hint_lane == "modify":
            probe_route = _probe_topology_route(
                has_skin,
                triangle_count,
                allow_unskinned_geometry=allow_unskinned_geometry,
            )
            probe_route_authority = "fbx_probe_topology"
            probe_geometry_required = probe_route == "fbx_geometry"
            exact_payload_required = probe_geometry_required
            probe_skip_reason = (
                "topology_route_source_geometry"
                if not exact_payload_required
                else ""
            )
        else:
            probe_route = (
                _probe_topology_route(
                    has_skin,
                    triangle_count,
                    allow_unskinned_geometry=allow_unskinned_geometry,
                )
                if target_mesh
                else ""
            )
            exact_payload_required = (
                probe_route == "fbx_geometry"
                if target_mesh
                else not filter_active
            )
            probe_route_authority = "fbx_probe_topology" if probe_route else ""
            probe_geometry_required = bool(exact_payload_required)
            probe_skip_reason = (
                "stage_a_route_discovery" if not exact_payload_required else ""
            )

        entry = {
            "mesh": mesh,
            "instance_node": instance_node,
            "node_name": node_name,
            "mesh_name": mesh_name,
            "binary_identity": binary_identity,
            "target_mesh": target_mesh,
            "route_hint": route_hint,
            "route_hint_match": route_hint_match,
            "identity_excluded": bool(identity_excluded),
            "hint_lane": hint_lane,
            "has_skin": bool(has_skin),
            "triangle_count": int(triangle_count),
            "topology_scanned": bool(topology_scanned),
            "exact_payload_required": bool(exact_payload_required),
            "probe_route": probe_route,
            "probe_route_authority": probe_route_authority,
            "probe_geometry_required": bool(probe_geometry_required),
            "probe_skip_reason": probe_skip_reason,
            "unskinned_mesh_edit_export_authorized": allow_unskinned_geometry,
        }
        entries.append(entry)
        if not exact_payload_required:
            continue
        geometry_id = _int_or_default(binary_identity.get("fbx_geometry_id"), 0)
        if geometry_id > 0:
            exact_geometry_ids.add(geometry_id)
        for value in (
            node_name,
            mesh_name,
            binary_identity.get("fbx_model_name", ""),
        ):
            name_key = normalize_match_name(value)
            if name_key:
                exact_geometry_names.add(name_key)
                exact_mesh_names.add(name_key)

    return {
        "entries": entries,
        "mode": str(probe_mode or "full"),
        "filter_active": bool(filter_active),
        "route_hint_filter_active": bool(route_hint_filter_active),
        "route_hints": normalized_route_hints,
        "unskinned_mesh_edit_export_authorized": allow_unskinned_geometry,
        "route_hint_match_count": route_hint_match_count,
        "route_hint_ambiguous_count": route_hint_ambiguous_count,
        "route_hint_unmatched_count": route_hint_unmatched_count,
        "identity_excluded_count": identity_excluded_count,
        "stage_a_mesh_count": sum(
            1 for entry in entries if not entry["exact_payload_required"]
        ),
        "stage_b_mesh_count": sum(
            1 for entry in entries if entry["exact_payload_required"]
        ),
        "exact_geometry_ids": exact_geometry_ids,
        "exact_geometry_names": exact_geometry_names,
        "exact_mesh_names": exact_mesh_names,
    }


def _probe_scene_handoff(
    scene: Any,
    *,
    fbx_path: str,
    max_snapshot: dict[str, Any] | None = None,
    skin_context: dict[str, Any] | None = None,
    binary_model_queues: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
    binary_corner_context: dict[str, Any] | None = None,
    probe_mode: str = "full",
    target_handles: set[int] | None = None,
    target_names: set[str] | None = None,
    target_slots: set[int] | None = None,
    route_hints: dict[str, Any] | None = None,
    stage_a_plan: dict[str, Any] | None = None,
    generic_normalization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # The scene has already crossed the mandatory Generic boundary.  There is
    # no producer- or caller-specific axis branch after this point.
    use_global_axis_domain = True
    mesh_summaries: list[dict[str, Any]] = []
    contract_meshes: list[dict[str, Any]] = []
    route_receipt_rows: list[dict[str, Any]] = []
    active_stage_a_plan = (
        stage_a_plan
        if isinstance(stage_a_plan, dict)
        and isinstance(stage_a_plan.get("entries"), list)
        else _build_probe_stage_a_plan(
            scene,
            binary_model_queues=binary_model_queues,
            probe_mode=probe_mode,
            target_handles=target_handles,
            target_names=target_names,
            target_slots=target_slots,
            route_hints=route_hints,
        )
    )
    normalized_route_hints = active_stage_a_plan.get("route_hints", {})
    # Stage A is the sole owner of the unskinned-geometry authorization fact.
    # Reuse that decision while writing the receipt; do not recalculate or
    # introduce a second policy gate in Stage B.
    allow_unskinned_geometry = bool(
        active_stage_a_plan.get("unskinned_mesh_edit_export_authorized", False)
    )
    route_hint_filter_active = bool(
        active_stage_a_plan.get("route_hint_filter_active", False)
    )
    stage_a_filter_active = bool(active_stage_a_plan.get("filter_active", False))
    route_hint_match_count = _int_or_default(
        active_stage_a_plan.get("route_hint_match_count"), 0
    )
    route_hint_ambiguous_count = _int_or_default(
        active_stage_a_plan.get("route_hint_ambiguous_count"), 0
    )
    route_hint_unmatched_count = _int_or_default(
        active_stage_a_plan.get("route_hint_unmatched_count"), 0
    )
    stage_a_mesh_count = _int_or_default(
        active_stage_a_plan.get("stage_a_mesh_count"), 0
    )
    stage_b_mesh_count = _int_or_default(
        active_stage_a_plan.get("stage_b_mesh_count"), 0
    )
    for entry in active_stage_a_plan.get("entries", []):
        if not isinstance(entry, dict):
            continue
        mesh = entry.get("mesh")
        instance_node = entry.get("instance_node")
        if mesh is None:
            continue
        node_name = str(entry.get("node_name", "") or "")
        mesh_name = str(entry.get("mesh_name", "") or "")
        binary_identity = (
            dict(entry.get("binary_identity", {}))
            if isinstance(entry.get("binary_identity"), dict)
            else {}
        )
        if bool(entry.get("identity_excluded", False)):
            # Do not open UFBX Geometry/Skin arrays for a duplicate Max node
            # whose FBX route marker is absent or does not match Launcher.
            mesh_summaries.append(
                {
                    "node_name": node_name,
                    "mesh_name": mesh_name,
                    "match_name": normalize_match_name(node_name or mesh_name),
                    "mesh_name_match": normalize_match_name(mesh_name),
                    "mesh_slot_hint": infer_mesh_slot_hint(node_name) or infer_mesh_slot_hint(mesh_name),
                    "lod_hint": infer_lod_hint(node_name) or infer_lod_hint(mesh_name),
                    "vertex_count": int(getattr(mesh, "num_vertices", 0) or 0),
                    "index_count": int(getattr(mesh, "num_indices", 0) or 0),
                    "face_count": int(getattr(mesh, "num_faces", 0) or 0),
                    "triangle_count": 0,
                    "material_names": [],
                    "material_slot_count": 0,
                    "material_part_count": None,
                    "bbox": BoundingBox().to_dict(),
                    "position_preview": [],
                    "uv_preview": [],
                    "normal_preview": [],
                    "face_preview": [],
                    "fbx_probe_route": "",
                    "fbx_probe_route_authority": "fbx_probe_identity_filter",
                    "fbx_probe_route_match_status": "identity_excluded",
                    "fbx_probe_geometry_required": False,
                    "fbx_probe_geometry_skipped": True,
                    "fbx_probe_skip_reason": str(
                        entry.get("probe_skip_reason", "max_unmarked_or_mismatched_route_identity")
                        or "max_unmarked_or_mismatched_route_identity"
                    ),
                    "fbx_probe_topology_scanned": False,
                    "identity_excluded": True,
                    **binary_identity,
                }
            )
            continue
        route_hint = (
            entry.get("route_hint")
            if isinstance(entry.get("route_hint"), dict)
            else None
        )
        route_hint_match = str(entry.get("route_hint_match", "disabled") or "disabled")
        hint_lane = str(entry.get("hint_lane", "") or "").strip().lower()
        has_skin = bool(entry.get("has_skin", False))
        triangle_count = _int_or_default(entry.get("triangle_count"), 0)
        topology_scanned = bool(entry.get("topology_scanned", False))
        exact_payload_required = bool(entry.get("exact_payload_required", False))
        probe_route = str(entry.get("probe_route", "") or "")
        probe_route_authority = str(entry.get("probe_route_authority", "") or "")
        probe_geometry_required = bool(entry.get("probe_geometry_required", False))
        probe_skip_reason = str(entry.get("probe_skip_reason", "") or "")
        if exact_payload_required:
            vertex_positions = _safe_list(getattr(mesh, "vertex_positions", None))
            vertex_uv = _safe_list(getattr(mesh, "vertex_uvs", None))
            normal_values = _safe_list(getattr(mesh, "vertex_normals", None))
            has_positions = len(vertex_positions) > 0
            has_uv = len(vertex_uv) > 0
            has_normals = len(normal_values) > 0
            binary_corner_geometry, normal_fidelity = _select_binary_fbx_corner_geometry(
                mesh,
                binary_corner_context,
                binary_identity,
            )
            geometry = _extract_mesh_geometry(
                mesh,
                instance_node,
                binary_corner_geometry=binary_corner_geometry,
                normal_fidelity=normal_fidelity,
                use_global_axis_domain=use_global_axis_domain,
            )
            geometry = _augment_geometry_with_skinned_pose_channels(geometry, mesh, instance_node)
            geometry = _apply_binary_fbx_skin_pose_channels(
                geometry,
                mesh,
                instance_node,
                skin_context,
            )
            summary_skin, contract_skin = _build_skin_summary_variants(
                mesh,
                export_source_vertex_indices=geometry.get("source_vertex_indices"),
            )
        else:
            vertex_positions = []
            vertex_uv = []
            normal_values = []
            has_positions = False
            has_uv = False
            has_normals = False
            normal_fidelity = _binary_fbx_normal_fidelity_audit(
                status="fallback",
                reason="stage_a_route_discovery",
            )
            geometry = _build_lightweight_mesh_geometry(
                mesh,
                normal_fidelity=normal_fidelity,
                vertex_count=int(getattr(mesh, "num_vertices", 0) or 0),
                index_count=int(getattr(mesh, "num_indices", 0) or 0),
                triangle_count=triangle_count,
            )
            summary_skin = _build_lightweight_skin_summary(mesh, has_skin=has_skin)
            contract_skin = dict(summary_skin)
        material_names = _mesh_material_names(mesh)
        vertex_count = int(
            getattr(mesh, "num_vertices", 0)
            or len(vertex_positions)
        )
        face_values = (
            _safe_list(getattr(mesh, "faces", None))
            if exact_payload_required
            else []
        )
        face_count = int(
            getattr(mesh, "num_faces", 0)
            or len(face_values)
        )
        route_receipt_rows.append(
            {
                "scene_node": node_name,
                "mesh_name": mesh_name,
                "scene_node_handle": _int_or_default(
                    (route_hint or {}).get("scene_node_handle"), 0
                ),
                "mesh_slot": _int_or_default(
                    (route_hint or {}).get("mesh_slot"),
                    _int_or_default(
                        (infer_mesh_slot_hint(node_name) or infer_mesh_slot_hint(mesh_name) or {}).get("slot"),
                        0,
                    ),
                ),
                "fbx_route_handle": _int_or_default(binary_identity.get("fbx_route_handle"), 0),
                "expected_fbx_route_handle": _int_or_default(
                    (route_hint or {}).get("fbx_route_handle"),
                    _int_or_default((route_hint or {}).get("scene_node_handle"), 0),
                ),
                "fbx_route_handle_match": (
                    _int_or_default(binary_identity.get("fbx_route_handle"), 0)
                    == _int_or_default(
                        (route_hint or {}).get("fbx_route_handle"),
                        _int_or_default((route_hint or {}).get("scene_node_handle"), 0),
                    )
                    if route_hint is not None
                    else False
                ),
                "lane": hint_lane,
                "route": probe_route,
                "authority": probe_route_authority,
                "match_status": "matched" if route_hint is not None else route_hint_match,
                "geometry_required": bool(probe_geometry_required),
                "geometry_skipped": not bool(exact_payload_required),
                "skip_reason": probe_skip_reason,
                "topology_scanned": topology_scanned,
                "fbx_has_skin": bool(has_skin),
                "fbx_vertex_count": vertex_count,
                "fbx_face_count": int(triangle_count),
                "unskinned_mesh_edit_export_authorized": allow_unskinned_geometry,
            }
        )

        mesh_summaries.append(
            {
                "node_name": node_name,
                "mesh_name": mesh_name,
                "match_name": normalize_match_name(node_name or mesh_name),
                "mesh_name_match": normalize_match_name(mesh_name),
                "mesh_slot_hint": infer_mesh_slot_hint(node_name) or infer_mesh_slot_hint(mesh_name),
                "lod_hint": infer_lod_hint(node_name) or infer_lod_hint(mesh_name),
                "vertex_count": vertex_count,
                "index_count": int(getattr(mesh, "num_indices", 0) or 0),
                "face_count": face_count,
                "triangle_count": triangle_count,
                "material_names": material_names,
                "material_slot_count": len(material_names),
                "material_part_count": None,
                "bbox": _bbox_from_vertex_values(vertex_positions) if has_positions else BoundingBox().to_dict(),
                "position_preview": _take_preview_values(vertex_positions, 5, _vec3_to_list) if has_positions else [],
                "uv_preview": _take_preview_values(vertex_uv, 5, _vec2_to_list) if has_uv else [],
                "normal_preview": _take_preview_values(normal_values, 5, _vec3_to_list) if has_normals else [],
                "face_preview": [
                    {
                        "index_begin": int(face[0]),
                        "num_indices": int(face[1]),
                    }
                    for face in face_values[:5]
                ],
                "fbx_probe_route": probe_route,
                "fbx_probe_route_authority": probe_route_authority,
                "fbx_probe_route_match_status": "matched" if route_hint is not None else route_hint_match,
                "fbx_probe_geometry_required": bool(probe_geometry_required),
                "fbx_probe_geometry_skipped": not bool(exact_payload_required),
                "fbx_probe_skip_reason": probe_skip_reason,
                "fbx_probe_topology_scanned": topology_scanned,
                "normal_fidelity": geometry.get("normal_fidelity", normal_fidelity),
                **binary_identity,
                **summary_skin,
            }
        )

        contract_mesh = _build_probe_contract_mesh_row(
            node_name=node_name,
            mesh_name=mesh_name,
            instance_node=instance_node,
            material_names=material_names,
            geometry=geometry,
            normal_fidelity=normal_fidelity,
            skin=contract_skin,
            binary_identity=binary_identity,
            probe_route=probe_route,
            probe_route_authority=probe_route_authority,
            probe_route_match_status=("matched" if route_hint is not None else route_hint_match),
            probe_geometry_required=probe_geometry_required,
            probe_geometry_skipped=not exact_payload_required,
            probe_skip_reason=probe_skip_reason,
            probe_topology_scanned=topology_scanned,
            generic_normalization=generic_normalization,
        )
        contract_meshes.append(contract_mesh)

    material_names_seen: dict[str, dict[str, Any]] = {}
    for mesh_summary in mesh_summaries:
        for material_name in mesh_summary["material_names"]:
            if material_name not in material_names_seen:
                material_names_seen[material_name] = {"name": material_name}
    material_summaries = list(material_names_seen.values())

    bone_summaries: list[dict[str, Any]] = []
    for bone in _scene_bone_objects(scene):
        bone_name = str(getattr(bone, "name", "") or "")
        parent_node = getattr(getattr(bone, "parent", None), "name", None)
        parent_name = str(parent_node or "")
        node_to_world = (
            _max_import_matrix_from_fbx_matrix(getattr(bone, "node_to_world", None))
            or _max_import_matrix_from_fbx_matrix(getattr(bone, "geometry_to_world", None))
        )
        local_matrix = (
            _max_import_matrix_from_fbx_matrix(getattr(bone, "node_to_parent", None))
            or _max_import_matrix_from_fbx_matrix(getattr(bone, "geometry_to_parent", None))
        )
        bone_summaries.append(
            {
                "name": bone_name,
                "is_root": bool(getattr(bone, "is_root", False) or parent_name == ""),
                "parsed_bone_id": parse_bone_id_from_name(bone_name, default_value=None),
                "parent_name": parent_name,
                "parent_parsed_bone_id": parse_bone_id_from_name(parent_name, default_value=None),
                "world_matrix": node_to_world,
                "local_matrix": local_matrix,
            }
        )

    node_summaries: list[dict[str, Any]] = []
    for node in scene.nodes:
        node_name = str(node.name)
        node_summaries.append(
            {
                "name": node_name,
                "type": "mesh" if getattr(node, "mesh", None) is not None else "node",
                "material_count": len(getattr(getattr(node, "mesh", None), "materials", []) or []),
                "match_name": normalize_match_name(node_name),
                "mesh_slot_hint": infer_mesh_slot_hint(node_name),
                "lod_hint": infer_lod_hint(node_name),
            }
        )

    summary = {
        "fbx_path": str(fbx_path),
        "stats": {
            "mesh_count": len(mesh_summaries),
            "material_count": len(material_summaries),
            "bone_count": len(bone_summaries),
            "node_count": len(node_summaries),
            "skin_cluster_count": sum(len(mesh.get("bone_names", [])) for mesh in mesh_summaries),
        },
        "meshes": mesh_summaries,
        "materials": material_summaries,
        "bones": bone_summaries,
        "nodes": node_summaries,
    }
    payload: dict[str, Any] = {
        "status": "ok",
        "ufbx_runtime_mode": str(
            getattr(scene, "_codex_ufbx_runtime_mode", "ufbx_native")
            or "ufbx_native"
        ),
        "summary": summary,
        "contract_meshes": contract_meshes,
        "probe_stage": {
            "mode": str(probe_mode or "full"),
            "stage_a_mesh_count": int(stage_a_mesh_count),
            "stage_b_mesh_count": int(stage_b_mesh_count),
            "target_filter_active": bool(stage_a_filter_active),
            "route_hint_status": (
                str(normalized_route_hints.get("status", "absent") or "absent")
                if route_hint_filter_active
                else "disabled_for_full_mode"
            ),
            "route_hint_count": len(normalized_route_hints.get("rows", [])) if route_hint_filter_active else 0,
            "route_hint_match_count": int(route_hint_match_count),
            "route_hint_ambiguous_count": int(route_hint_ambiguous_count),
            "route_hint_unmatched_count": int(route_hint_unmatched_count),
            "identity_excluded_count": int(
                active_stage_a_plan.get("identity_excluded_count", 0)
            ),
            "unskinned_mesh_edit_export_authorized": allow_unskinned_geometry,
        },
        "probe_route_receipt": {
            "schema": FBX_PROBE_ROUTE_RECEIPT_SCHEMA,
            "authority": "fbx_probe",
            "policy": "fbx_probe_topology_v1",
            "rows": route_receipt_rows,
        },
        "normal_fidelity": [
            {
                **mesh.get("normal_fidelity", {}),
                "node_name": str(mesh.get("node_name", "") or ""),
                "mesh_name": str(mesh.get("mesh_name", "") or ""),
            }
            for mesh in contract_meshes
            if isinstance(mesh, dict) and isinstance(mesh.get("normal_fidelity"), dict)
        ],
        "canonical_probe_schema": CANONICAL_FBX_PROBE_SCHEMA,
        "canonical_axis_domain": CANONICAL_AXIS_DOMAIN,
        "canonical_unit_domain": CANONICAL_UNIT_DOMAIN,
        "fbx_axis_output_policy": GENERIC_AXIS_OUTPUT_POLICY,
        "fbx_axis_transform_scope": GENERIC_AXIS_TRANSFORM_SCOPE,
        "bone_matrix_axis_domain": CANONICAL_AXIS_DOMAIN,
        "transform_stage": "generic_memory_to_canonical_domain",
    }
    if isinstance(max_snapshot, dict):
        payload["compare"] = compare_fbx_to_max_snapshot(summary, max_snapshot)
    return payload


def probe_fbx_handoff(
    path: str | Path,
    *,
    max_snapshot: dict[str, Any] | None = None,
    target_handles: set[int] | None = None,
    target_names: set[str] | None = None,
    target_slots: set[int] | None = None,
    probe_mode: str = "full",
    route_hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_route_hints = _normalize_probe_route_hints(route_hints)
    # ====== ONE GENERIC HANDOFF ======
    # Producer labels are retained only for the two explicit policy facts:
    # Blender's fixed unit conversion and MAX RouteHandle identity. Every
    # actual FBX, including direct/unknown callers, uses the same in-memory
    # Generic document and the same canonical parser below.
    use_generic_normalizer = True
    fbx_path, scene = _require_scene(
        path,
        use_generic_normalizer=use_generic_normalizer,
        backend_kind=normalized_route_hints.get("backend_kind"),
    )
    # The export handoff starts with a structural tree only.  Large FBX arrays
    # are decoded lazily when the Stage B target actually consumes them.
    binary_document, generic_receipt = _generic_memory_document_for_path(fbx_path)
    normalized_target_handles = {
        _int_or_default(value, 0)
        for value in (target_handles or set())
        if _int_or_default(value, 0) > 0
    }
    normalized_target_names = {
        normalize_match_name(value)
        for value in (target_names or set())
        if normalize_match_name(value)
    }
    normalized_target_slots = {
        _int_or_default(value, 0)
        for value in (target_slots or set())
        if _int_or_default(value, 0) > 0
    }
    stage_a_filter_active = (
        str(probe_mode or "full").casefold() == "export_required"
        and bool(
            normalized_target_handles
            or normalized_target_names
            or normalized_target_slots
            or normalized_route_hints.get("rows")
        )
    )
    binary_model_queues, route_model_observations = _binary_fbx_model_identity_context(
        fbx_path,
        binary_document=binary_document,
        target_handles=normalized_target_handles,
        target_names=normalized_target_names,
        target_slots=normalized_target_slots,
        target_filter_active=stage_a_filter_active,
        explicit_route_required=bool(
            normalized_route_hints.get("explicit_route_required", False)
        ),
    )
    stage_a_plan = _build_probe_stage_a_plan(
        scene,
        binary_model_queues=binary_model_queues,
        probe_mode=probe_mode,
        target_handles=normalized_target_handles,
        target_names=normalized_target_names,
        target_slots=normalized_target_slots,
        route_hints=normalized_route_hints,
    )
    # Stage A is the sole route authority.  Only its exact-geometry entries
    # are allowed to open the expensive Skin and corner-attribute readers.
    target_geometry_ids = {
        _int_or_default(value, 0)
        for value in stage_a_plan.get("exact_geometry_ids", set())
        if _int_or_default(value, 0) > 0
    }
    target_geometry_names = {
        normalize_match_name(value)
        for value in stage_a_plan.get("exact_geometry_names", set())
        if normalize_match_name(value)
    }
    target_mesh_names = {
        normalize_match_name(value)
        for value in stage_a_plan.get("exact_mesh_names", set())
        if normalize_match_name(value)
    }
    exact_target_filter_active = (
        str(probe_mode or "full").casefold() == "export_required"
    )
    skin_context = _build_binary_fbx_skin_evaluation_context(
        fbx_path,
        scene,
        binary_document=binary_document,
        target_mesh_names=target_mesh_names if exact_target_filter_active else None,
        use_global_axis_domain=(
            use_generic_normalizer
            and isinstance(generic_receipt, dict)
            and str(generic_receipt.get("status", "") or "").strip().lower()
            == "normalized"
        ),
    )
    payload = _probe_scene_handoff(
        scene,
        fbx_path=str(fbx_path),
        max_snapshot=max_snapshot,
        skin_context=skin_context,
        binary_model_queues=_clone_binary_fbx_model_identity_queues(binary_model_queues),
        binary_corner_context=_build_binary_fbx_corner_geometry_context(
            fbx_path,
            binary_document=binary_document,
            target_geometry_ids=target_geometry_ids,
            target_geometry_names=target_geometry_names,
            target_filter_active=exact_target_filter_active,
        ),
        probe_mode=probe_mode,
        target_handles=normalized_target_handles,
        target_names=normalized_target_names,
        target_slots=normalized_target_slots,
        route_hints=normalized_route_hints,
        stage_a_plan=stage_a_plan,
        generic_normalization=generic_receipt,
    )
    payload["binary_parse"] = binary_document.receipt()
    payload["status"] = "ok"
    payload["ufbx_runtime_mode"] = str(
        getattr(scene, "_codex_ufbx_runtime_mode", "ufbx_native")
        or "ufbx_native"
    )
    payload["route_models"] = route_model_observations
    normalized_route_hints = _normalize_probe_route_hints(route_hints)
    route_receipt = payload.get("probe_route_receipt")
    if (
        isinstance(route_receipt, dict)
        and str(probe_mode or "full").casefold() == "export_required"
        and normalized_route_hints.get("status") == "ok"
    ):
        represented_keys = {
            (
                _int_or_default(row.get("scene_node_handle"), 0),
                _int_or_default(row.get("mesh_slot"), 0),
                str(row.get("lane", "") or "").strip().lower(),
            )
            for row in route_receipt.get("rows", [])
            if isinstance(row, dict)
        }
        # Header/Delete rows do not need a scene Mesh in the FBX.  Preserve
        # their Launcher route as a hint-only pass-through row.
        for hint in normalized_route_hints.get("rows", []):
            if not isinstance(hint, dict):
                continue
            lane = str(hint.get("lane", "") or "").strip().lower()
            if lane not in {"header", "delete"}:
                continue
            key = (
                _int_or_default(hint.get("scene_node_handle"), 0),
                _int_or_default(hint.get("mesh_slot"), 0),
                lane,
            )
            if key in represented_keys:
                continue
            names = hint.get("names") if isinstance(hint.get("names"), list) else []
            route_receipt.setdefault("rows", []).append(
                {
                    "scene_node": str(names[0] if names else ""),
                    "mesh_name": str(names[0] if names else ""),
                    "scene_node_handle": key[0],
                    "mesh_slot": key[1],
                    "fbx_route_handle": _int_or_default(
                        hint.get("fbx_route_handle"),
                        key[0],
                    ),
                    "lane": lane,
                    "route": "header_only" if lane == "header" else "delete",
                    "authority": "launcher_bucket_receipt",
                    "match_status": "hint_only",
                    "geometry_required": False,
                    "geometry_skipped": True,
                    "skip_reason": (
                        "launcher_header_route"
                        if lane == "header"
                        else "launcher_delete_route"
                    ),
                    "topology_scanned": False,
                    "fbx_has_skin": False,
                    "fbx_vertex_count": 0,
                    "fbx_face_count": 0,
                }
            )
        payload["probe_route_receipt"] = route_receipt
    fbx_axes = _scene_axis_receipt(scene)
    fbx_units = _scene_unit_receipt(scene)
    # Producer-specific Blender scaling was retired.  Generic reconstruction
    # owns the unit boundary; this receipt is diagnostic only and never mutates
    # geometry, skin, bone, or node-matrix rows.
    fbx_model_scale_transform = {
        "schema": FBX_TRANSFORM_SCALE_SCHEMA,
        "status": "not_applied",
        "backend": str(normalized_route_hints.get("backend_kind") or "").strip().lower(),
        "source": "generic_fbx_reconstruction",
        "unit_factor": 1.0,
        "applied_once": False,
    }
    payload["fbx_axes"] = fbx_axes
    payload["fbx_units"] = fbx_units
    payload["summary"]["fbx_axes"] = fbx_axes
    payload["summary"]["fbx_units"] = fbx_units
    payload["fbx_model_scale_transform"] = fbx_model_scale_transform
    payload["summary"]["fbx_model_scale_transform"] = dict(
        fbx_model_scale_transform
    )
    transform_scale = _fbx_transform_scale_receipt(
        payload,
        fbx_units,
        backend_kind=normalized_route_hints.get("backend_kind"),
    )
    payload["fbx_transform_scale_receipt"] = transform_scale
    payload["summary"]["fbx_transform_scale_receipt"] = dict(transform_scale)
    payload["generic_fbx_normalization"] = generic_receipt
    # Publish the canonical writer contract unconditionally.  A normalized
    # receipt is mandatory above; there is no disabled/legacy axis fallback.
    payload["fbx_axis_output_policy"] = GENERIC_AXIS_OUTPUT_POLICY
    payload["fbx_axis_transform_scope"] = GENERIC_AXIS_TRANSFORM_SCOPE
    payload["canonical_probe_schema"] = CANONICAL_FBX_PROBE_SCHEMA
    payload["canonical_axis_domain"] = CANONICAL_AXIS_DOMAIN
    payload["canonical_unit_domain"] = CANONICAL_UNIT_DOMAIN
    payload["bone_matrix_axis_domain"] = CANONICAL_AXIS_DOMAIN
    payload["transform_stage"] = "generic_memory_to_canonical_domain"
    dynamic_mapping = _dynamic_mod_fbx_mapping_receipt()
    payload["dynamic_mod_fbx_mapping_receipt"] = dynamic_mapping
    payload["mod_to_canonical_matrix"] = list(
        dynamic_mapping["mod_to_canonical_matrix"]
    )
    payload["canonical_to_mod_matrix"] = list(
        dynamic_mapping["canonical_to_mod_matrix"]
    )
    return payload


def _ufbx_contract_normalize(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return {"non_finite_float": "nan"}
        if math.isinf(value):
            return {"non_finite_float": "+inf" if value > 0.0 else "-inf"}
        rounded = round(value, UFBX_BEHAVIOR_FLOAT_DECIMALS)
        return 0.0 if rounded == 0.0 else rounded
    if isinstance(value, dict):
        return {
            str(key): _ufbx_contract_normalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_ufbx_contract_normalize(item) for item in value]
    item_method = getattr(value, "item", None)
    if callable(item_method):
        try:
            return _ufbx_contract_normalize(item_method())
        except Exception:
            pass
    tolist_method = getattr(value, "tolist", None)
    if callable(tolist_method):
        try:
            return _ufbx_contract_normalize(tolist_method())
        except Exception:
            pass
    raise TypeError(f"Unsupported ufbx behavior-contract value: {type(value).__name__}")


def _ufbx_contract_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _ufbx_contract_normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _ufbx_contract_value_receipt(value: Any) -> dict[str, Any]:
    normalized = _ufbx_contract_normalize(value)
    if isinstance(normalized, (list, dict)):
        top_level_count = len(normalized)
    elif normalized is None:
        top_level_count = 0
    else:
        top_level_count = 1
    return {
        "top_level_count": top_level_count,
        "sha256": hashlib.sha256(_ufbx_contract_json_bytes(normalized)).hexdigest(),
    }


def _ufbx_contract_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


_UFBX_MESH_EXACT_CONTRACT_FIELDS = frozenset(
    {
        "node_name",
        "mesh_name",
        "match_name",
        "mesh_name_match",
        "mesh_slot_hint",
        "lod_hint",
        "material_names",
        "vertex_count",
        "triangle_count",
        "skin_deformer_count",
        "bone_names",
        "max_weights_per_vertex",
        "weighted_vertex_count",
        "weight_rows_available",
        "skinned_is_local",
    }
)


def build_ufbx_behavior_contract(path: str | Path) -> dict[str, Any]:
    """Build a compact, deterministic receipt over every FBX value used by RE6."""
    fbx_path = Path(path).resolve()
    handoff = probe_fbx_handoff(fbx_path, probe_mode="full")
    summary = dict(handoff.get("summary", {}))
    mesh_receipts: list[dict[str, Any]] = []
    for ordinal, mesh in enumerate(handoff.get("contract_meshes", [])):
        exact: dict[str, Any] = {}
        channels: dict[str, Any] = {}
        for field_name in sorted(mesh):
            field_value = mesh[field_name]
            if field_name in _UFBX_MESH_EXACT_CONTRACT_FIELDS:
                exact[field_name] = _ufbx_contract_normalize(field_value)
            else:
                channels[field_name] = _ufbx_contract_value_receipt(field_value)
        mesh_receipts.append(
            {
                "ordinal": ordinal,
                "fields": sorted(mesh),
                "exact": exact,
                "channels": channels,
            }
        )

    bone_receipts: list[dict[str, Any]] = []
    for ordinal, bone in enumerate(summary.get("bones", [])):
        identity = {
            key: _ufbx_contract_normalize(value)
            for key, value in bone.items()
            if key not in {"world_matrix", "local_matrix"}
        }
        bone_receipts.append(
            {
                "ordinal": ordinal,
                "identity": identity,
                "world_matrix": _ufbx_contract_value_receipt(bone.get("world_matrix")),
                "local_matrix": _ufbx_contract_value_receipt(bone.get("local_matrix")),
            }
        )

    contract: dict[str, Any] = {
        "schema": UFBX_BEHAVIOR_CONTRACT_SCHEMA,
        "float_decimals": UFBX_BEHAVIOR_FLOAT_DECIMALS,
        "fixture": {
            "name": fbx_path.name,
            "size": fbx_path.stat().st_size,
            "sha256": _ufbx_contract_file_sha256(fbx_path),
        },
        "ufbx": {
            "version": str(getattr(ufbx, "__version__", "") or ""),
            "patch": str(getattr(ufbx, "__codex_patch__", "") or ""),
        },
        "stats": _ufbx_contract_normalize(summary.get("stats", {})),
        "fbx_axes": _ufbx_contract_normalize(
            handoff.get("fbx_axes", summary.get("fbx_axes", {}))
        ),
        "meshes": mesh_receipts,
        "materials": _ufbx_contract_normalize(summary.get("materials", [])),
        "bones": bone_receipts,
        "nodes": _ufbx_contract_normalize(summary.get("nodes", [])),
    }
    contract["contract_sha256"] = hashlib.sha256(_ufbx_contract_json_bytes(contract)).hexdigest()
    return contract


def _build_max_mesh_index(max_snapshot: dict[str, Any]) -> tuple[dict[int, dict[str, Any]], dict[str, dict[str, Any]]]:
    slot_map: dict[int, dict[str, Any]] = {}
    name_map: dict[str, dict[str, Any]] = {}
    for mesh in max_snapshot.get("meshes", []):
        node_name = mesh.get("node_name", "")
        mesh_name = mesh.get("mesh_name", "")
        slot_hint = infer_mesh_slot_hint(node_name) or infer_mesh_slot_hint(mesh_name)
        if slot_hint is not None:
            slot_map[slot_hint["slot"]] = mesh
        keys = {
            normalize_match_name(node_name),
            normalize_match_name(mesh_name),
        }
        for key in keys:
            if key:
                name_map[key] = mesh
    return slot_map, name_map


def compare_fbx_to_max_snapshot(fbx_summary: dict[str, Any], max_snapshot: dict[str, Any]) -> dict[str, Any]:
    slot_map, name_map = _build_max_mesh_index(max_snapshot)
    matches: list[dict[str, Any]] = []
    unmatched_fbx: list[dict[str, Any]] = []

    for mesh in fbx_summary.get("meshes", []):
        mesh_slot_hint = mesh.get("mesh_slot_hint")
        matched: dict[str, Any] | None = None
        match_type = ""
        if mesh_slot_hint is not None:
            matched = slot_map.get(mesh_slot_hint["slot"])
            if matched is not None:
                match_type = "mesh_slot"
        if matched is None:
            for key in (mesh.get("match_name", ""), mesh.get("mesh_name_match", "")):
                if key and key in name_map:
                    matched = name_map[key]
                    match_type = "normalized_name"
                    break
        if matched is None:
            unmatched_fbx.append(
                {
                    "fbx_node_name": mesh.get("node_name", ""),
                    "fbx_mesh_name": mesh.get("mesh_name", ""),
                    "mesh_slot_hint": mesh_slot_hint,
                }
            )
            continue
        matches.append(
            {
                "match_type": match_type,
                "fbx_node_name": mesh.get("node_name", ""),
                "fbx_mesh_name": mesh.get("mesh_name", ""),
                "max_node_name": matched.get("node_name", ""),
                "max_mesh_name": matched.get("mesh_name", ""),
                "mesh_slot_hint": mesh_slot_hint,
                "vertex_count_equal": mesh.get("vertex_count") == matched.get("vertex_count"),
                "triangle_count_equal": mesh.get("triangle_count") == matched.get("triangle_count"),
                "material_overlap": sorted(
                    set(normalize_match_name(name) for name in mesh.get("material_names", []))
                    & set(normalize_match_name(name) for name in matched.get("material_names", []))
                ),
            }
        )

    matched_max_names = {item["max_node_name"] for item in matches}
    unmatched_max = [
        mesh
        for mesh in max_snapshot.get("meshes", [])
        if mesh.get("node_name", "") not in matched_max_names
    ]

    return {
        "match_count": len(matches),
        "unmatched_fbx_count": len(unmatched_fbx),
        "unmatched_max_count": len(unmatched_max),
        "matches": matches,
        "unmatched_fbx": unmatched_fbx,
        "unmatched_max": unmatched_max,
    }


def load_max_snapshot(path: str | Path) -> dict[str, Any]:
    data = runtime_read_json_file(path)
    if not isinstance(data, dict):
        raise ValueError("Max snapshot JSON must be an object")
    return data


def _run_skin_cluster_name_policy_regression_guard() -> dict[str, Any]:
    """Lock the non-RE6 Skin Cluster policy without affecting normal Probe runs."""

    class _Cluster:
        def __init__(self, bone_name: str, vertices: list[int], weights: list[float]) -> None:
            self.bone_name = bone_name
            self.name = bone_name
            self.vertices = vertices
            self.weights = weights

    class _Skin:
        clusters = [
            _Cluster("hair_R_01", [0], [1.0]),
            _Cluster("b_0_2", [1], [0.75]),
        ]

    class _Mesh:
        name = "skin_cluster_policy_fixture"
        skin_deformers = [_Skin()]

    bone_names, bone_rows, weight_rows = _extract_source_skin_rows(_Mesh(), 2)
    assert bone_names == ["b_0_2"]
    assert bone_rows == [[], [1]]
    assert weight_rows == [[], [0.75]]
    return {
        "status": "ok",
        "policy": "discard_unmappable_skin_clusters",
    }


def run_fbx_probe_runtime_self_test() -> dict[str, Any]:
    global ufbx, UFBX_IMPORT_ERROR, UFBX_IMPORT_FAILURE, ufbx_missed_substitute

    original_ufbx = ufbx
    original_import_error = UFBX_IMPORT_ERROR
    original_import_failure = UFBX_IMPORT_FAILURE
    original_substitute = ufbx_missed_substitute
    checks: dict[str, Any] = {
        "skin_cluster_name_policy": _run_skin_cluster_name_policy_regression_guard(),
    }
    try:
        class _SubstituteSceneFixture:
            _codex_ufbx_runtime_mode = "ufbx_missed_substitute"
            metadata: dict[str, Any] = {}

        substitute_scene = _SubstituteSceneFixture()

        def substitute_fixture(_path: str | Path, **_kwargs: Any) -> _SubstituteSceneFixture:
            return substitute_scene

        try:
            raise ModuleNotFoundError("No module named 'ufbx'")
        except ModuleNotFoundError as missing_error:
            ufbx = None
            UFBX_IMPORT_ERROR = missing_error
            UFBX_IMPORT_FAILURE = classify_fbx_probe_exception(missing_error, stage="import")
        ufbx_missed_substitute = substitute_fixture
        missing_path, missing_scene = _require_scene("missing-runtime.fbx")
        assert missing_path.name == "missing-runtime.fbx"
        assert missing_scene is substitute_scene
        missing_status = get_fbx_probe_runtime_status()
        assert missing_status["status"] == "ok"
        assert missing_status["mode"] == "ufbx_missed_substitute"
        checks["missing_module"] = missing_status

        class _BadAbiUfbx:
            @staticmethod
            def load_file(_path: str) -> Any:
                raise OSError(193, "DLL load failed: not a valid Win32 application")

        ufbx = _BadAbiUfbx()
        UFBX_IMPORT_ERROR = None
        UFBX_IMPORT_FAILURE = None
        ufbx_missed_substitute = substitute_fixture
        bad_abi_path, bad_abi_scene = _require_scene("bad-abi.fbx")
        assert bad_abi_path.name == "bad-abi.fbx"
        assert bad_abi_scene is substitute_scene
        assert substitute_scene.metadata["native_ufbx_failure"]["classification"] == "runtime_failure"
        checks["bad_abi"] = substitute_scene.metadata["native_ufbx_failure"]

        class _FbxDataError(Exception):
            pass

        parse_error = _FbxDataError("Failed to load FBX file: Unrecognized file format")

        class _BadFbxDataUfbx:
            @staticmethod
            def load_file(_path: str) -> Any:
                raise parse_error

        ufbx = _BadFbxDataUfbx()
        try:
            _require_scene("invalid-data.fbx")
        except Exception as exc:
            assert exc is parse_error
            status = classify_fbx_probe_exception(exc, stage="load_file")
            assert status["status"] == FBX_PROBE_DATA_ERROR_STATUS
            assert status["runtime_retryable"] is False
            checks["simulated_fbx_data_error"] = status
        else:
            raise AssertionError("invalid FBX data did not preserve its parser exception")
    finally:
        ufbx = original_ufbx
        UFBX_IMPORT_ERROR = original_import_error
        UFBX_IMPORT_FAILURE = original_import_failure
        ufbx_missed_substitute = original_substitute

    if original_ufbx is not None:
        try:
            _require_scene(Path(__file__))
        except Exception as exc:
            assert not isinstance(exc, FbxProbeRuntimeUnavailableError)
            status = classify_fbx_probe_exception(exc, stage="load_file")
            assert status["status"] == FBX_PROBE_DATA_ERROR_STATUS
            assert status["runtime_retryable"] is False
            checks["real_ufbx_data_error"] = status
        else:
            raise AssertionError("ufbx unexpectedly parsed the Python source as FBX data")
    else:
        checks["real_ufbx_data_error"] = {"status": "skipped", "reason": "ufbx runtime unavailable"}

    return {"status": "ok", "checks": checks}


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe an FBX file and emit mesh/bone/material summary for Max/FBX matching experiments.",
    )
    parser.add_argument("--fbx", required=True, help="Path to the FBX file to inspect.")
    parser.add_argument(
        "--max-json",
        help="Optional JSON snapshot of Max scene meshes to compare against the FBX summary.",
    )
    parser.add_argument(
        "--output",
        help="Optional output JSON path. If omitted, the summary prints to stdout.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_cli()
    args = parser.parse_args(argv)
    token = _export_gc_begin()
    try:
        summary = summarize_fbx(args.fbx)
        if args.max_json:
            max_snapshot = load_max_snapshot(args.max_json)
            summary["max_compare"] = compare_fbx_to_max_snapshot(summary, max_snapshot)

        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            runtime_write_json_file(output_path, summary, pretty=args.pretty)
        else:
            print(runtime_json_dumps_text(summary, pretty=args.pretty), flush=True)
        return 0
    finally:
        _export_gc_finish(token, cleanup=_GENERIC_FBX_MEMORY_CACHE.clear)


# ====== END PUBLIC PROBE HANDOFF / RECEIPTS ======

if __name__ == "__main__" and not globals().get("__codex_parallel_source_load__"):
    raise SystemExit(main())
