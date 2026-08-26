from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import struct
import sys
import traceback
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
UFBX_BEHAVIOR_CONTRACT_SCHEMA = "pc-rehd-code-x-patched-ufbx-behavior-v1"
UFBX_BEHAVIOR_FLOAT_DECIMALS = 8
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
            "runtime_retryable": False,
        }
    if isinstance(UFBX_IMPORT_FAILURE, dict):
        return dict(UFBX_IMPORT_FAILURE)
    missing_error = UFBX_IMPORT_ERROR or ModuleNotFoundError("ufbx is not available")
    return classify_fbx_probe_exception(missing_error, stage="import")


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


def _restore_max_space_geometry(
    geometry: dict[str, Any],
    mesh: Any,
    instance_node: Any | None,
) -> dict[str, Any]:
    """Replace every legacy V4 Y-up derivative with the Max Z-up FBX value."""
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
    xyz = _normal_vec3_to_list(vec)
    return _normalize_normal_vec3([xyz[0], xyz[2], -xyz[1]])


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
    """Return the writer RGB key for one authored FBX polygon-corner normal."""
    game_normal = _max_normal_to_re6_game_normal(_fbx_authored_corner_normal_to_max(normal))
    return tuple(max(0, min(255, int((axis * 127.0) + 127.0))) for axis in game_normal)


def _encode_re6_normal_key_from_fbx_local(normal: Any, node_to_world: Any) -> tuple[int, int, int]:
    """Return the legacy/UFBX writer key after applying the Mesh node transform.

    AI MAINTENANCE GATE: this compatibility API is consumed by the pure-Python
    fallback and every ``codex_fbx_probe_accel`` bundle.  Keep it synchronized
    with those callers when changing FBX normal-space handling.  The strict raw
    polygon-corner path intentionally uses
    ``_encode_re6_normal_key_from_fbx_corner()`` instead.
    """
    max_normal = _fbx_world_to_max_normal(
        _transform_normal_row_major(normal, node_to_world)
    )
    game_normal = _max_normal_to_re6_game_normal(max_normal)
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


@dataclass(slots=True)
class _BinaryFbxNode:
    name: str
    properties: list[Any]
    children: list["_BinaryFbxNode"]


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
        raw_value = data[offset : offset + stored_size]
        offset += stored_size
        if not decode_array:
            return None, offset
        if encoding == 1:
            raw_value = zlib.decompress(raw_value)
        elif encoding != 0:
            raise ValueError(f"Binary FBX array has unsupported encoding {encoding}")
        item_format, item_size = _FBX_BINARY_ARRAY_LAYOUTS[property_type]
        expected_size = int(value_count) * item_size
        if len(raw_value) != expected_size:
            raise ValueError("Binary FBX array byte count does not match its element count")
        return list(struct.unpack("<" + item_format * int(value_count), raw_value)), offset
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


def _read_binary_fbx_roots(
    path: Path,
    *,
    decode_array_names: frozenset[str] | None = None,
) -> list[_BinaryFbxNode]:
    data = path.read_bytes()
    if not data.startswith(_FBX_BINARY_SIGNATURE):
        raise ValueError("FBX is not a supported binary FBX file")
    _binary_fbx_require_range(data, len(_FBX_BINARY_SIGNATURE), 4, label="version")
    version = struct.unpack_from("<I", data, len(_FBX_BINARY_SIGNATURE))[0]
    if version < 7000:
        raise ValueError(f"Binary FBX version {version} is not supported for skin evaluation")
    header_size = 25 if version >= 7500 else 13
    decode_names = decode_array_names or frozenset(
        {"Indexes", "Weights", "Transform", "TransformLink"}
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


def _binary_fbx_node_child_value(node: _BinaryFbxNode, child_name: str) -> Any:
    for child in node.children:
        if child.name == child_name and child.properties:
            return child.properties[0]
    return None


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
            result["strict_error"] = f"uv_layer_{uv_layer_index + 1}_invalid: {exc}"
            return result
    return result


def _build_binary_fbx_corner_geometry_context(path: Path) -> dict[str, Any]:
    """Build an exact Geometry fingerprint lookup for authored corner channels.

    This is advisory at the file level: a malformed Geometry must not block
    other Meshes or the export itself. Each Mesh receives its own audit record.
    """
    context: dict[str, Any] = {
        "by_fingerprint": {},
        "status": "available",
        "error": "",
    }
    try:
        roots = _read_binary_fbx_roots(
            path,
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
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if not isinstance(context, dict) or context.get("status") != "available":
        reason = str(context.get("error", "") if isinstance(context, dict) else "") or "binary_corner_reader_unavailable"
        return None, _binary_fbx_normal_fidelity_audit(status="fallback", reason=reason)
    fingerprint = _ufbx_mesh_geometry_fingerprint(mesh)
    if fingerprint == "":
        return None, _binary_fbx_normal_fidelity_audit(status="fallback", reason="ufbx_geometry_fingerprint_unavailable")
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
    geometry_fingerprints: dict[int, str] = {}
    for node in objects_root.children:
        if node.name == "Geometry" and node.properties and isinstance(node.properties[0], int):
            geometry_fingerprints[int(node.properties[0])] = _binary_fbx_geometry_fingerprint(
                _binary_fbx_node_child_value(node, "Vertices"),
                _binary_fbx_node_child_value(node, "PolygonVertexIndex"),
            )
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
            "fbx_geometry_fingerprint": "",
        }
        if route_handle > 0:
            route_models[int(model_id)] = {
                **model_identity,
                "fbx_geometry_connected": False,
            }
        if model_type.casefold() == "mesh" and model_name != "":
            models[int(model_id)] = model_identity

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
            if int(parent_id) in models and int(child_id) in geometry_fingerprints:
                models[int(parent_id)]["fbx_geometry_fingerprint"] = geometry_fingerprints[
                    int(child_id)
                ]
            if int(parent_id) in route_models and int(child_id) in geometry_fingerprints:
                route_models[int(parent_id)]["fbx_geometry_connected"] = True

    queues: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for model_id in sorted(models):
        row = models[model_id]
        key = (
            str(row["fbx_model_name"]).casefold(),
            str(row["fbx_parent_name"]).casefold(),
        )
        queues.setdefault(key, []).append(row)
    observations = [route_models[model_id] for model_id in sorted(route_models)]
    return queues, observations


def _binary_fbx_mesh_model_identity_queues(path: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    queues, _observations = _binary_fbx_model_identity_context(path)
    return queues


def extract_fbx_route_model_observations(path: str | Path) -> list[dict[str, Any]]:
    """Return route-marked FBX Model facts without making export decisions."""

    _queues, observations = _binary_fbx_model_identity_context(Path(path))
    return observations


def _take_binary_fbx_mesh_model_identity(
    queues: dict[tuple[str, str], list[dict[str, Any]]],
    instance_node: Any | None,
) -> dict[str, Any]:
    if instance_node is None:
        return {}
    node_name = str(getattr(instance_node, "name", "") or "")
    parent_name = str(getattr(getattr(instance_node, "parent", None), "name", "") or "")
    key = (node_name.casefold(), parent_name.casefold())
    rows = queues.get(key)
    geometry_fingerprint = _ufbx_mesh_geometry_fingerprint(getattr(instance_node, "mesh", None))
    if rows and len(rows) == 1:
        return dict(rows.pop(0))
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


def _world_to_max_axis_matrix(mesh_world: list[float]) -> list[float] | None:
    """Extract only the FBX axis basis from a Mesh bind matrix.

    Importer-generated FBX stores Max Z-up to FBX Y-up on the Mesh helper,
    while a Max-exported FBX normally has an identity axis basis.  The Skin
    result is world-space, so remove only that basis before handing positions
    to the Max-coordinate MOD encoder; mesh scale must remain in the result.
    """
    if len(mesh_world) != 16:
        return None
    axis = [0.0] * 16
    for row in range(3):
        row_offset = row * 4
        length = math.sqrt(
            sum(mesh_world[row_offset + column] * mesh_world[row_offset + column] for column in range(3))
        )
        if length <= 0.000000000001:
            return None
        for column in range(3):
            axis[row_offset + column] = mesh_world[row_offset + column] / length
    axis[15] = 1.0
    return _invert_row_major_matrix(axis)


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


def _build_binary_fbx_skin_clusters(path: Path) -> tuple[dict[str, list[dict[str, Any]]], str]:
    try:
        roots = _read_binary_fbx_roots(path)
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

    result: dict[str, list[dict[str, Any]]] = {}
    for cluster_id, cluster in objects_by_id.items():
        if not object_type_matches(cluster_id, "Deformer", "Cluster"):
            continue
        transform = _binary_fbx_matrix(_binary_fbx_node_child_value(cluster, "Transform"))
        transform_link = _binary_fbx_matrix(_binary_fbx_node_child_value(cluster, "TransformLink"))
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
        mesh_model_ids = [
            object_id
            for object_id in parents_by_child.get(geometry_ids[0], [])
            if object_type_matches(object_id, "Model", "Mesh")
        ]
        if len(mesh_model_ids) != 1:
            continue
        mesh_model = objects_by_id[mesh_model_ids[0]]
        bone_model = objects_by_id[bone_ids[0]]
        if len(mesh_model.properties) < 2 or len(bone_model.properties) < 2:
            continue
        mesh_name = _clean_binary_fbx_object_name(mesh_model.properties[1])
        bone_name = _clean_binary_fbx_object_name(bone_model.properties[1])
        if mesh_name == "" or bone_name == "":
            continue
        try:
            parsed_indexes = [int(value) for value in indexes]
            parsed_weights = [float(value) for value in weights]
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(value) and value >= 0.0 for value in parsed_weights):
            continue
        result.setdefault(mesh_name, []).append(
            {
                "bone_name": bone_name,
                "indexes": parsed_indexes,
                "weights": parsed_weights,
                "transform": transform,
                "transform_link": transform_link,
            }
        )
    return result, "ok"


def _build_binary_fbx_skin_evaluation_context(path: Path, scene: Any) -> dict[str, Any]:
    clusters_by_mesh, graph_status = _build_binary_fbx_skin_clusters(path)
    context: dict[str, Any] = {
        "schema": FBX_BINARY_SKIN_EVALUATION_SCHEMA,
        "status": graph_status,
        "meshes": {},
    }
    if graph_status != "ok":
        return context

    nodes_by_name = {
        str(getattr(node, "name", "") or ""): node
        for node in _safe_list(getattr(scene, "nodes", None))
        if str(getattr(node, "name", "") or "") != ""
    }
    for mesh_name, clusters in clusters_by_mesh.items():
        mesh_node = nodes_by_name.get(mesh_name)
        mesh = getattr(mesh_node, "mesh", None) if mesh_node is not None else None
        source_positions = _safe_list(getattr(mesh, "vertex_positions", None))
        mesh_world = _binary_fbx_matrix(_flatten_matrix4x4(getattr(mesh_node, "node_to_world", None)))
        mesh_world_to_max = _world_to_max_axis_matrix(mesh_world) if mesh_world is not None else None
        if mesh_node is None or mesh is None or mesh_world is None or mesh_world_to_max is None or not source_positions:
            context["meshes"][mesh_name] = {"status": "missing_mesh_node_or_geometry"}
            continue

        bindings: list[dict[str, Any]] = []
        for cluster in clusters:
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
                    "max_prebind": _multiply_row_major_matrices(transform, transform_link),
                }
            )
        if not bindings:
            context["meshes"][mesh_name] = {"status": "missing_bone_world_matrix"}
            continue

        world_to_max = mesh_world_to_max
        unweighted_source_matrix = mesh_world
        if all(_row_major_matrices_match(binding["max_prebind"], mesh_world) for binding in bindings):
            mode = "max_cluster_prebind"
            deformation_matrices = [
                _multiply_row_major_matrices(binding["transform"], binding["bone_world"])
                for binding in bindings
            ]
        elif all(_row_major_matrices_match(binding["transform"], mesh_world) for binding in bindings):
            mode = "standard_cluster_bind"
            deformation_matrices = []
            for binding in bindings:
                inverse_link = _invert_row_major_matrix(binding["transform_link"])
                if inverse_link is None:
                    deformation_matrices = []
                    break
                deformation_matrices.append(
                    _multiply_row_major_matrices(
                        _multiply_row_major_matrices(binding["transform"], inverse_link),
                        binding["bone_world"],
                    )
                )
            if not deformation_matrices:
                context["meshes"][mesh_name] = {"status": "non_invertible_cluster_link"}
                continue
        elif (
            (armature_bind := _shared_cluster_prebind(bindings)) is not None
            and (armature_world_to_max := _world_to_max_axis_matrix(armature_bind)) is not None
        ):
            # Blender writes the common Armature bind basis to Transform x
            # TransformLink while its Mesh node remains in export space.  At
            # rest Transform x TransformLink restores that basis; at the
            # current pose Transform x BoneWorld is the corresponding LBS map.
            mode = "blender_armature_cluster_bind"
            deformation_matrices = [
                _multiply_row_major_matrices(binding["transform"], binding["bone_world"])
                for binding in bindings
            ]
            world_to_max = armature_world_to_max
            unweighted_source_matrix = armature_bind
        else:
            context["meshes"][mesh_name] = {"status": "unrecognized_cluster_bind_convention"}
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
        context["meshes"][mesh_name] = {
            "status": "binary_cluster_evaluated",
            "mode": mode,
            "source_matrices": source_matrices,
            "world_to_max_matrix": world_to_max,
            "weighted_vertex_count": weighted_vertex_count,
        }
    return context


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
    if _get_mesh_skinned_vec3_rows(mesh, "skinned_position", "skinned_positions"):
        geometry["fbx_skin_pose_evaluation_status"] = "ufbx_runtime_evaluated"
        return geometry
    node_name = str(getattr(instance_node, "name", "") or "")
    mesh_pose = (
        skin_context.get("meshes", {}).get(node_name)
        if isinstance(skin_context, dict)
        else None
    )
    if not isinstance(mesh_pose, dict) or mesh_pose.get("status") != "binary_cluster_evaluated":
        status = mesh_pose.get("status") if isinstance(mesh_pose, dict) else "binary_cluster_evaluation_unavailable"
        geometry["fbx_skin_pose_evaluation_status"] = str(status)
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
        geometry["fbx_skin_pose_evaluation_status"] = "binary_cluster_evaluation_unaligned_geometry"
        return geometry

    evaluated_world_positions: list[list[float]] = []
    evaluated_max_positions: list[list[float]] = []
    evaluated_world_normals: list[list[float]] = []
    evaluated_max_normals: list[list[float]] = []
    referenced_source_indices: set[int] = set()
    for export_index, source_index_value in enumerate(source_indices):
        try:
            source_index = int(source_index_value)
        except (TypeError, ValueError, OverflowError):
            geometry["fbx_skin_pose_evaluation_status"] = "binary_cluster_evaluation_invalid_source_index"
            return geometry
        if source_index < 0 or source_index >= len(source_matrices):
            geometry["fbx_skin_pose_evaluation_status"] = "binary_cluster_evaluation_missing_source_matrix"
            return geometry
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
    if skinned_is_local_hint:
        return True
    if node_to_world is None:
        return skinned_is_local_hint
    has_max_positions = isinstance(raw_max_positions, list)
    has_raw_positions = isinstance(raw_positions, list)
    if (not has_max_positions and not has_raw_positions) or not isinstance(skinned_positions, list):
        return skinned_is_local_hint
    reference_count = len(raw_max_positions) if has_max_positions else len(raw_positions)
    row_count = min(reference_count, len(skinned_positions))
    if row_count <= 0:
        return skinned_is_local_hint

    sample_limit = min(row_count, 32)
    sample_step = max(1, row_count // sample_limit)
    local_error = 0.0
    world_error = 0.0
    used = 0
    prepared_transform = _prepare_row_major_transform(node_to_world)
    for row_index in range(0, row_count, sample_step):
        raw_max = (
            raw_max_positions[row_index]
            if has_max_positions
            else _transform_position_row_major(raw_positions[row_index], prepared_transform)
        )
        skinned_pos = skinned_positions[row_index]
        local_candidate = _fbx_world_to_max_vec3(
            _transform_position_row_major(skinned_pos, prepared_transform)
        )
        world_candidate = _fbx_world_to_max_vec3(skinned_pos)
        local_error += _distance_sq_vec3(local_candidate, raw_max)
        world_error += _distance_sq_vec3(world_candidate, raw_max)
        used += 1
        if used >= sample_limit:
            break
    if used <= 0:
        return skinned_is_local_hint

    # Some ufbx builds expose posed rows as object-local even when the flag says otherwise.
    # Compare both interpretations against the raw mesh's transformed max-space rows and
    # keep the one that stays in the same coordinate regime.
    if local_error < world_error:
        return True
    return skinned_is_local_hint


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
        world_to_max_matrix = (
            _world_to_max_axis_matrix(node_world_matrix)
            if node_world_matrix is not None
            else None
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
                "channel": int(channel["channel"]),
                "name": str(channel["name"]),
                "values": [list(value) for value in channel["values"]],
                "corner_indices": [],
            }
            for channel in uv_channels
        ]
        vertex_map: dict[tuple[int, tuple[tuple[float, float], ...], tuple[int, int, int]], int] = {}

        for face_begin, face_size in raw_faces:
            face_vertex_ids: list[int] = []
            face_geom_vertex_ids: list[int] = []
            face_uv_channel_ids = [[] for _ in out_uv_channel_payloads]
            for local_offset in range(int(face_size)):
                corner_index = int(face_begin) + local_offset
                position_index = int(geom_indices[corner_index])
                normal_direct_index = int(normal_corner_indices[corner_index])
                corner_normal = _normalize_normal_vec3(normal_values[normal_direct_index])
                corner_uvs: list[list[float]] = []
                uv_key_parts: list[tuple[float, float]] = []
                for channel_index, channel in enumerate(uv_channels):
                    direct_index = int(channel["corner_indices"][corner_index])
                    uv = list(channel["values"][direct_index])
                    corner_uvs.append(uv)
                    uv_key_parts.append(_uv_key_from_value(uv))
                    face_uv_channel_ids[channel_index].append(direct_index)
                primary_uv = corner_uvs[0] if corner_uvs else _default_vec2()
                authored_max_normal = _fbx_authored_corner_normal_to_max(corner_normal)
                normal_key = _encode_re6_normal_key_from_fbx_corner(authored_max_normal)
                key = (position_index, tuple(uv_key_parts), normal_key)
                vertex_id = vertex_map.get(key)
                if vertex_id is None:
                    vertex_id = len(out_positions)
                    vertex_map[key] = vertex_id
                    local_position = _vec3_to_list(positions_src[position_index])
                    world_position = _transform_position_row_major(local_position, prepared_transform)
                    # Blender's Mesh node carries its export axis basis and
                    # object scale. The MOD position writer needs the restored
                    # Max-space row rather than a raw local or FBX-world row.
                    max_position = (
                        _transform_position_row_major(world_position, world_to_max_matrix)
                        if world_to_max_matrix is not None
                        else _fbx_world_to_max_vec3(world_position)
                    )
                    out_positions.append(local_position)
                    out_world_positions.append(max_position)
                    out_max_positions.append(max_position)
                    out_normals.append(corner_normal)
                    out_max_normals.append(authored_max_normal)
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
                    skinned_normal = corner_normal
                    skinned_normal_index = _resolve_vertex_attr_index(
                        len(skinned_normals_src),
                        position_index=position_index,
                        corner_index=corner_index,
                        vertex_count=vertex_count,
                        index_count=index_count,
                    )
                    if skinned_normal_index is not None and 0 <= skinned_normal_index < len(skinned_normals_src):
                        skinned_normal = _vec3_to_list(skinned_normals_src[skinned_normal_index])
                    out_skinned_normals.append(list(skinned_normal))
                    out_uvs.append(list(primary_uv))
                    out_source_vertex_indices.append(position_index)
                    out_source_corner_indices.append(corner_index)
                face_vertex_ids.append(vertex_id)
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
                "normal_split_vertex_count": len(out_positions),
                "normal_space": "fbx_authored_corner_no_mesh_node_transform",
                "position_mode": "binary_fbx_node_axis_to_max",
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
                    max_pos = _fbx_world_to_max_vec3(world_pos)
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
        if bone_name != "":
            bone_names.append(bone_name)
        game_bone = parse_bone_id_from_name(bone_name, default_value=None)
        if game_bone is None:
            mesh_name = str(getattr(mesh, "name", "") or "<unnamed mesh>")
            raise ValueError(
                "FBX Skin cluster cannot be mapped to an RE6 bone ID: "
                f"mesh={mesh_name}; cluster={cluster_index}; bone={bone_name or '<unnamed>'}"
            )
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


def _require_scene(path: str | Path) -> tuple[Path, Any]:
    if ufbx is None:
        runtime_error = FbxProbeRuntimeUnavailableError(get_fbx_probe_runtime_status())
        if UFBX_IMPORT_ERROR is not None:
            raise runtime_error from UFBX_IMPORT_ERROR
        raise runtime_error
    fbx_path = Path(path)
    try:
        scene = ufbx.load_file(str(fbx_path))
    except Exception as exc:
        details = classify_fbx_probe_exception(exc, stage="load_file")
        if bool(details["runtime_retryable"]):
            raise FbxProbeRuntimeUnavailableError(details) from exc
        raise
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
            binary_model_queues or {}, instance_node
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
    fbx_path, scene = _require_scene(path)
    summary = _summarize_scene(
        scene,
        fbx_path=str(fbx_path),
        binary_model_queues=_binary_fbx_mesh_model_identity_queues(fbx_path),
    )
    summary["fbx_axes"] = _scene_axis_receipt(scene)
    return summary


def _extract_scene_mesh_contracts(
    scene: Any,
    *,
    skin_context: dict[str, Any] | None = None,
    binary_model_queues: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
    binary_corner_context: dict[str, Any] | None = None,
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
        node_to_world = _flatten_matrix4x4(getattr(instance_node, "node_to_world", None))
        contract_mesh = {
            "node_name": node_name,
            "mesh_name": mesh_name,
            "match_name": normalize_match_name(node_name or mesh_name),
            "mesh_name_match": normalize_match_name(mesh_name),
            "mesh_slot_hint": infer_mesh_slot_hint(node_name) or infer_mesh_slot_hint(mesh_name),
            "lod_hint": infer_lod_hint(node_name) or infer_lod_hint(mesh_name),
            "material_names": material_names,
            "positions": geometry["positions"],
            # Strict corner geometry has already restored the FBX node's
            # Blender axis basis. This explicit Max-space table prevents the
            # writer from serializing tiny FBX-local position rows. Legacy
            # extraction deliberately retains its established position route.
            **(
                {"max_positions": geometry["max_positions"]}
                if isinstance(geometry.get("normal_fidelity"), dict)
                and str(geometry["normal_fidelity"].get("status", "") or "") == "exact"
                else {}
            ),
            "skinned_positions": geometry["skinned_positions"],
            "skinned_max_positions": geometry["skinned_max_positions"],
            "skinned_world_positions": geometry["skinned_world_positions"],
            "normals": geometry["normals"],
            "max_normals": geometry["max_normals"],
            "skinned_normals": geometry["skinned_normals"],
            "skinned_max_normals": geometry["skinned_max_normals"],
            "binary_skin_unreferenced_source_indices": geometry.get(
                "binary_skin_unreferenced_source_indices", []
            ),
            "binary_skin_unreferenced_max_positions": geometry.get(
                "binary_skin_unreferenced_max_positions", []
            ),
            "skinned_is_local": geometry["skinned_is_local"],
            "fbx_skin_pose_evaluation_status": geometry.get("fbx_skin_pose_evaluation_status", ""),
            "fbx_skin_pose_evaluation_schema": geometry.get("fbx_skin_pose_evaluation_schema", ""),
            "fbx_skin_pose_evaluation_mode": geometry.get("fbx_skin_pose_evaluation_mode", ""),
            "uvs": geometry["uvs"],
            "face_indices": geometry["face_indices"],
            "source_vertex_indices": geometry["source_vertex_indices"],
            "fbx_export_corner_indices": geometry["fbx_export_corner_indices"],
            "fbx_geom_face_indices": geometry["fbx_geom_face_indices"],
            "fbx_export_face_indices": geometry["fbx_export_face_indices"],
            "fbx_uv_channels": geometry["fbx_uv_channels"],
            "normal_fidelity": geometry.get("normal_fidelity", normal_fidelity),
            "vertex_count": geometry["vertex_count"],
            "triangle_count": geometry["triangle_count"],
            **skin_summary,
        }
        if binary_identity:
            contract_mesh.update(binary_identity)
        if len(node_to_world) == 16:
            contract_mesh["fbx_node_to_world_matrix"] = node_to_world
        contracts.append(contract_mesh)
    return contracts


def extract_fbx_mesh_contracts(path: str | Path) -> list[dict[str, Any]]:
    fbx_path, scene = _require_scene(path)
    skin_context = _build_binary_fbx_skin_evaluation_context(fbx_path, scene)
    return _extract_scene_mesh_contracts(
        scene,
        skin_context=skin_context,
        binary_model_queues=_binary_fbx_mesh_model_identity_queues(fbx_path),
        binary_corner_context=_build_binary_fbx_corner_geometry_context(fbx_path),
    )


def _probe_scene_handoff(
    scene: Any,
    *,
    fbx_path: str,
    max_snapshot: dict[str, Any] | None = None,
    skin_context: dict[str, Any] | None = None,
    binary_model_queues: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
    binary_corner_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mesh_summaries: list[dict[str, Any]] = []
    contract_meshes: list[dict[str, Any]] = []
    for mesh, instance_node in _mesh_node_pairs(scene):
        node_name = str(instance_node.name) if instance_node is not None else ""
        mesh_name = str(mesh.name)
        binary_identity = _take_binary_fbx_mesh_model_identity(
            binary_model_queues or {}, instance_node
        )
        vertex_positions = mesh.vertex_positions if mesh.vertex_positions is not None else []
        vertex_uv = mesh.vertex_uvs if mesh.vertex_uvs is not None else []
        normal_values = mesh.vertex_normals if mesh.vertex_normals is not None else []
        has_positions = len(vertex_positions) > 0
        has_uv = len(vertex_uv) > 0
        has_normals = len(normal_values) > 0
        binary_corner_geometry, normal_fidelity = _select_binary_fbx_corner_geometry(
            mesh,
            binary_corner_context,
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
        summary_skin, contract_skin = _build_skin_summary_variants(
            mesh,
            export_source_vertex_indices=geometry.get("source_vertex_indices"),
        )

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
                "normal_fidelity": geometry.get("normal_fidelity", normal_fidelity),
                **binary_identity,
                **summary_skin,
            }
        )

        node_to_world = _flatten_matrix4x4(getattr(instance_node, "node_to_world", None))
        contract_mesh = {
            "node_name": node_name,
            "mesh_name": mesh_name,
            "match_name": normalize_match_name(node_name or mesh_name),
            "mesh_name_match": normalize_match_name(mesh_name),
            "mesh_slot_hint": infer_mesh_slot_hint(node_name) or infer_mesh_slot_hint(mesh_name),
            "lod_hint": infer_lod_hint(node_name) or infer_lod_hint(mesh_name),
            "material_names": material_names,
            "positions": geometry["positions"],
            # probe_fbx_handoff() is the runtime bridge path. Keep this in
            # lockstep with extract_fbx_mesh_contracts(): exact corner rows
            # need the already axis-restored Max-space position table.
            **(
                {"max_positions": geometry["max_positions"]}
                if isinstance(geometry.get("normal_fidelity"), dict)
                and str(geometry["normal_fidelity"].get("status", "") or "") == "exact"
                else {}
            ),
            "skinned_positions": geometry["skinned_positions"],
            "skinned_max_positions": geometry["skinned_max_positions"],
            "skinned_world_positions": geometry["skinned_world_positions"],
            "normals": geometry["normals"],
            "max_normals": geometry["max_normals"],
            "skinned_normals": geometry["skinned_normals"],
            "skinned_max_normals": geometry["skinned_max_normals"],
            "binary_skin_unreferenced_source_indices": geometry.get(
                "binary_skin_unreferenced_source_indices", []
            ),
            "binary_skin_unreferenced_max_positions": geometry.get(
                "binary_skin_unreferenced_max_positions", []
            ),
            "skinned_is_local": geometry["skinned_is_local"],
            "fbx_skin_pose_evaluation_status": geometry.get("fbx_skin_pose_evaluation_status", ""),
            "fbx_skin_pose_evaluation_schema": geometry.get("fbx_skin_pose_evaluation_schema", ""),
            "fbx_skin_pose_evaluation_mode": geometry.get("fbx_skin_pose_evaluation_mode", ""),
            "uvs": geometry["uvs"],
            "face_indices": geometry["face_indices"],
            "source_vertex_indices": geometry["source_vertex_indices"],
            "fbx_export_corner_indices": geometry["fbx_export_corner_indices"],
            "fbx_export_face_indices": geometry["fbx_export_face_indices"],
            "fbx_geom_face_indices": geometry["fbx_geom_face_indices"],
            "fbx_uv_channels": geometry["fbx_uv_channels"],
            "normal_fidelity": geometry.get("normal_fidelity", normal_fidelity),
            "vertex_count": geometry["vertex_count"],
            "triangle_count": geometry["triangle_count"],
            **binary_identity,
            **contract_skin,
        }
        if len(node_to_world) == 16:
            contract_mesh["fbx_node_to_world_matrix"] = node_to_world
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
        "summary": summary,
        "contract_meshes": contract_meshes,
        "normal_fidelity": [
            {
                **mesh.get("normal_fidelity", {}),
                "node_name": str(mesh.get("node_name", "") or ""),
                "mesh_name": str(mesh.get("mesh_name", "") or ""),
            }
            for mesh in contract_meshes
            if isinstance(mesh, dict) and isinstance(mesh.get("normal_fidelity"), dict)
        ],
    }
    if isinstance(max_snapshot, dict):
        payload["compare"] = compare_fbx_to_max_snapshot(summary, max_snapshot)
    return payload


def probe_fbx_handoff(path: str | Path, *, max_snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    fbx_path, scene = _require_scene(path)
    skin_context = _build_binary_fbx_skin_evaluation_context(fbx_path, scene)
    binary_model_queues, route_model_observations = _binary_fbx_model_identity_context(
        fbx_path
    )
    payload = _probe_scene_handoff(
        scene,
        fbx_path=str(fbx_path),
        max_snapshot=max_snapshot,
        skin_context=skin_context,
        binary_model_queues=binary_model_queues,
        binary_corner_context=_build_binary_fbx_corner_geometry_context(fbx_path),
    )
    payload["route_models"] = route_model_observations
    fbx_axes = _scene_axis_receipt(scene)
    payload["fbx_axes"] = fbx_axes
    payload["summary"]["fbx_axes"] = fbx_axes
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
    fbx_path, scene = _require_scene(path)
    handoff = _probe_scene_handoff(scene, fbx_path=str(fbx_path))
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
        "fbx_axes": _ufbx_contract_normalize(_scene_axis_receipt(scene)),
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


def run_fbx_probe_runtime_self_test() -> dict[str, Any]:
    global ufbx, UFBX_IMPORT_ERROR, UFBX_IMPORT_FAILURE

    original_ufbx = ufbx
    original_import_error = UFBX_IMPORT_ERROR
    original_import_failure = UFBX_IMPORT_FAILURE
    checks: dict[str, Any] = {}
    try:
        try:
            raise ModuleNotFoundError("No module named 'ufbx'")
        except ModuleNotFoundError as missing_error:
            ufbx = None
            UFBX_IMPORT_ERROR = missing_error
            UFBX_IMPORT_FAILURE = classify_fbx_probe_exception(missing_error, stage="import")
        try:
            _require_scene("missing-runtime.fbx")
        except FbxProbeRuntimeUnavailableError as exc:
            assert isinstance(exc, ImportError)
            assert exc.status == FBX_PROBE_RUNTIME_UNAVAILABLE_STATUS
            assert exc.bridge_status == FBX_PROBE_BRIDGE_RETRY_STATUS
            assert exc.runtime_retryable is True
            assert exc.to_status_dict()["exception_type"].endswith("ModuleNotFoundError")
            assert exc.traceback_summary
            checks["missing_module"] = exc.to_status_dict()
        else:
            raise AssertionError("missing ufbx did not raise a runtime-unavailable error")

        class _BadAbiUfbx:
            @staticmethod
            def load_file(_path: str) -> Any:
                raise OSError(193, "DLL load failed: not a valid Win32 application")

        ufbx = _BadAbiUfbx()
        UFBX_IMPORT_ERROR = None
        UFBX_IMPORT_FAILURE = None
        try:
            _require_scene("bad-abi.fbx")
        except FbxProbeRuntimeUnavailableError as exc:
            status = exc.to_status_dict()
            assert status["classification"] == "runtime_failure"
            assert status["runtime_retryable"] is True
            assert status["bridge_status"] == FBX_PROBE_BRIDGE_RETRY_STATUS
            assert status["exception_type"].endswith("OSError")
            assert "load_file" in status["traceback_summary"]
            checks["bad_abi"] = status
        else:
            raise AssertionError("bad ufbx ABI did not raise a runtime-unavailable error")

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

    summary = summarize_fbx(args.fbx)
    if args.max_json:
        max_snapshot = load_max_snapshot(args.max_json)
        summary["max_compare"] = compare_fbx_to_max_snapshot(summary, max_snapshot)

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_write_json_file(output_path, summary, pretty=args.pretty)
    else:
        print(runtime_json_dumps_text(summary, pretty=args.pretty))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
