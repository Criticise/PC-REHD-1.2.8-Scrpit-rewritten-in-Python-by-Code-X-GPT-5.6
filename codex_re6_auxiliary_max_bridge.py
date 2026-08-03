from __future__ import annotations

import time
import uuid
from typing import Any


AUXILIARY_GEOMETRY_CONTRACT = "pc-rehd-sbc-adr-ems-geometry-v1"
AUXILIARY_GEOMETRY_REVISION = 1
AUXILIARY_OPERATION_RECEIPT_SCHEMA = "pc-rehd-code-x-operation-receipt-v1"
AUXILIARY_FAILURE_DOMAIN = "auxiliary"
SUPPORTED_AUXILIARY_KINDS = frozenset({"sbc", "adr", "ems"})


def _point3(value: Any) -> list[float]:
    return [float(value.x), float(value.y), float(value.z)]


def read_auxiliary_mesh_geometry(
    rt: Any,
    node: Any,
    *,
    kind: str,
    role: str,
) -> dict[str, Any]:
    """Read geometry only for the isolated SBC/ADR/EMS compatibility route."""
    normalized_kind = str(kind or "").strip().casefold()
    normalized_role = str(role or "").strip().casefold()
    if normalized_kind not in SUPPORTED_AUXILIARY_KINDS:
        raise ValueError(f"Unsupported auxiliary geometry kind: {kind!r}")
    if not normalized_role:
        raise ValueError("Auxiliary geometry role is required")

    converted = None
    try:
        converted = rt.snapshotAsMesh(node)
        vertex_count = int(rt.getNumVerts(converted))
        face_count = int(rt.getNumFaces(converted))
        try:
            object_transform = node.objectTransform
        except Exception:
            object_transform = node.transform

        vertices: list[list[float]] = []
        for index in range(1, vertex_count + 1):
            point = rt.getVert(converted, index)
            try:
                point = point * object_transform
            except Exception:
                pass
            vertices.append(_point3(point))

        faces: list[list[int]] = []
        for index in range(1, face_count + 1):
            face = rt.getFace(converted, index)
            faces.append([int(face.x) - 1, int(face.y) - 1, int(face.z) - 1])

        return {
            "contract": AUXILIARY_GEOMETRY_CONTRACT,
            "revision": AUXILIARY_GEOMETRY_REVISION,
            "kind": normalized_kind,
            "role": normalized_role,
            "space": "max_world",
            "vertices": vertices,
            "faces": faces,
        }
    finally:
        # Drop the Python wrapper reference only. Forcing MaxScript GC while
        # snapshotAsMesh wrappers are being released can invalidate native
        # objects still owned by pymxs.
        converted = None


def _auxiliary_failure_classification(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, ValueError):
        return {
            "status_code": "AUX_INVALID_REQUEST",
            "retryable": False,
            "degraded": False,
            "recovery_action": "fix_auxiliary_request",
        }
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return {
            "status_code": "AUX_RUNTIME_UNAVAILABLE",
            "retryable": True,
            "degraded": True,
            "recovery_action": "retry_auxiliary_once",
        }
    if isinstance(exc, (AttributeError, RuntimeError, TypeError)):
        return {
            "status_code": "AUX_MAX_API_CONTRACT_FAILED",
            "retryable": True,
            "degraded": True,
            "recovery_action": "reload_max_agent_then_retry_auxiliary",
        }
    return {
        "status_code": "AUX_UNEXPECTED_FAILURE",
        "retryable": False,
        "degraded": True,
        "recovery_action": "isolate_auxiliary_domain",
    }


def _auxiliary_operation_receipt(
    *,
    correlation_id: str,
    status: str,
    status_code: str,
    retryable: bool,
    degraded: bool,
    recovery_action: str,
    duration_ms: float,
    error: Exception | None = None,
) -> dict[str, Any]:
    return {
        "schema": AUXILIARY_OPERATION_RECEIPT_SCHEMA,
        "module": "codex_re6_auxiliary_max_bridge",
        "operation": "read_auxiliary_mesh_geometry",
        "failure_domain": AUXILIARY_FAILURE_DOMAIN,
        "isolation_scope": "auxiliary_only",
        "status": str(status).upper(),
        "status_code": str(status_code),
        "retryable": bool(retryable),
        "degraded": bool(degraded),
        "recovery_action": str(recovery_action),
        "contract_revision": AUXILIARY_GEOMETRY_REVISION,
        "correlation_id": str(correlation_id),
        "duration_ms": max(0.0, round(float(duration_ms), 3)),
        "error_type": type(error).__name__ if error is not None else "",
        "detail": str(error) if error is not None else "",
    }


def run_auxiliary_geometry_operation(
    rt: Any,
    node: Any,
    *,
    kind: str,
    role: str,
    correlation_id: str = "",
) -> dict[str, Any]:
    """Return a local AUX result envelope without poisoning unrelated domains."""
    operation_id = str(correlation_id or "").strip() or uuid.uuid4().hex
    started = time.perf_counter()
    try:
        result = read_auxiliary_mesh_geometry(rt, node, kind=kind, role=role)
    except Exception as exc:
        classification = _auxiliary_failure_classification(exc)
        return {
            "ok": False,
            "result": None,
            "receipt": _auxiliary_operation_receipt(
                correlation_id=operation_id,
                status="FAIL",
                duration_ms=(time.perf_counter() - started) * 1000.0,
                error=exc,
                **classification,
            ),
        }
    return {
        "ok": True,
        "result": result,
        "receipt": _auxiliary_operation_receipt(
            correlation_id=operation_id,
            status="PASS",
            status_code="AUX_OK",
            retryable=False,
            degraded=False,
            recovery_action="none",
            duration_ms=(time.perf_counter() - started) * 1000.0,
        ),
    }


def get_auxiliary_geometry_contract_report() -> dict[str, Any]:
    return {
        "status": "PASS",
        "contract": AUXILIARY_GEOMETRY_CONTRACT,
        "revision": AUXILIARY_GEOMETRY_REVISION,
        "operation_receipt_schema": AUXILIARY_OPERATION_RECEIPT_SCHEMA,
        "failure_domain": AUXILIARY_FAILURE_DOMAIN,
        "independent_failure_domain": True,
        "blocks_unrelated_operations": False,
        "timeout_owner": "launcher_max_agent",
        "supported_kinds": sorted(SUPPORTED_AUXILIARY_KINDS),
        "mod_pipeline_geometry_access": False,
    }


__all__ = [
    "AUXILIARY_GEOMETRY_CONTRACT",
    "AUXILIARY_GEOMETRY_REVISION",
    "AUXILIARY_OPERATION_RECEIPT_SCHEMA",
    "AUXILIARY_FAILURE_DOMAIN",
    "SUPPORTED_AUXILIARY_KINDS",
    "get_auxiliary_geometry_contract_report",
    "read_auxiliary_mesh_geometry",
    "run_auxiliary_geometry_operation",
]
