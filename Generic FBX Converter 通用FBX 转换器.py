from __future__ import annotations

import hashlib
import json
import locale
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
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
# Normal-space labels are shared with the embedded Probe normalizer.  A
# rebuilt file may contain both untouched Max Geometry and Geometry that was
# explicitly baked into the canonical XYZ basis, so the receipt is per ID.
FBX_NORMAL_AXIS_DOMAIN_CANONICAL = "canonical_xyz"
FBX_NORMAL_AXIS_DOMAIN_LEGACY = "legacy_max"
# Keep the file-level axis contract identical to the embedded Probe lane.
# The source basis is converted once during scene rebuild; downstream readers
# must not infer or apply a second per-Mesh axis rotation.
FBX_AXIS_OUTPUT_POLICY = "max_xyz"
FBX_AXIS_TRANSFORM_CONTRACT = "scene_global_once"


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
    return list(struct.unpack("<" + item_fmt * int(count), raw)), offset, kind


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
    path: str | Path,
    *,
    include_footer_id: bool = False,
) -> tuple[int, list[FbxNode]] | tuple[int, list[FbxNode], bytes | None]:
    source = Path(path).resolve()
    data = source.read_bytes()
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
        return struct.pack("<" + item_fmt * len(value), *value)
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
    return header + name + property_blob + b"".join(child_blobs)


def write_fbx(
    path: str | Path,
    version: int,
    roots: Iterable[FbxNode],
    *,
    footer_id: bytes | None = None,
) -> None:
    target = Path(path).resolve()
    roots_list = list(roots)
    if not roots_list:
        raise ValueError("Cannot write an FBX without root nodes")
    output_version = int(version) if int(version) >= 7000 else FBX_VERSION_DEFAULT
    selected_footer_id = FBX_FOOT_ID if footer_id is None else bytes(footer_id)
    if len(selected_footer_id) != 16:
        raise ValueError("FBX footer identity must contain exactly 16 bytes")
    body_parts: list[bytes] = []
    cursor = len(FBX_MAGIC) + 4
    for index, root in enumerate(roots_list):
        encoded = _encode_node(root, cursor, version=output_version, is_last=index == len(roots_list) - 1)
        body_parts.append(encoded)
        cursor += len(encoded)
    null_record = FBX_NULL_RECORD_WIDE if output_version >= 7500 else FBX_NULL_RECORD_NARROW
    body = b"".join(body_parts) + null_record
    before_footer_version = len(FBX_MAGIC) + 4 + len(body) + len(selected_footer_id) + 4
    padding = (-before_footer_version) % 16
    if padding == 0:
        padding = 16
    footer = selected_footer_id + b"\x00" * 4 + b"\x00" * padding + struct.pack("<I", output_version) + b"\x00" * 120 + FBX_FOOT_MAGIC
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", delete=False, dir=target.parent, prefix=f".{target.name}.", suffix=".tmp") as handle:
            temporary = Path(handle.name)
            handle.write(FBX_MAGIC)
            handle.write(struct.pack("<I", output_version))
            handle.write(body)
            handle.write(footer)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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


def _int_or_default(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


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


def _finite_matrix(value: Any, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 16:
        raise ValueError(f"{label} must contain 16 values")
    result = [float(item) for item in value]
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{label} contains non-finite values")
    return result


def _multiply_row_major_matrices(left: list[float], right: list[float]) -> list[float]:
    left = _finite_matrix(left, "left matrix")
    right = _finite_matrix(right, "right matrix")
    return [
        sum(left[row * 4 + item] * right[item * 4 + column] for item in range(4))
        for row in range(4)
        for column in range(4)
    ]


def _invert_row_major_matrix(matrix: list[float]) -> list[float] | None:
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


def _transform_position_row_major(row: Any, matrix: list[float]) -> list[float]:
    values = list(row)
    if len(values) < 3:
        raise ValueError("Position row must contain three values")
    matrix = _finite_matrix(matrix, "position matrix")
    x, y, z = (float(values[0]), float(values[1]), float(values[2]))
    return [
        (x * matrix[0]) + (y * matrix[4]) + (z * matrix[8]) + matrix[12],
        (x * matrix[1]) + (y * matrix[5]) + (z * matrix[9]) + matrix[13],
        (x * matrix[2]) + (y * matrix[6]) + (z * matrix[10]) + matrix[14],
    ]


def _transform_normal_row_major(row: Any, matrix: list[float]) -> list[float]:
    values = list(row)
    if len(values) < 3:
        raise ValueError("Normal row must contain three values")
    matrix = _finite_matrix(matrix, "normal matrix")
    a00, a01, a02 = matrix[0], matrix[1], matrix[2]
    a10, a11, a12 = matrix[4], matrix[5], matrix[6]
    a20, a21, a22 = matrix[8], matrix[9], matrix[10]
    c00 = (a11 * a22) - (a12 * a21)
    c01 = (a12 * a20) - (a10 * a22)
    c02 = (a10 * a21) - (a11 * a20)
    determinant = (a00 * c00) + (a01 * c01) + (a02 * c02)
    x, y, z = (float(values[0]), float(values[1]), float(values[2]))
    if abs(determinant) <= 1.0e-12:
        transformed = [
            (x * a00) + (y * a10) + (z * a20),
            (x * a01) + (y * a11) + (z * a21),
            (x * a02) + (y * a12) + (z * a22),
        ]
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
        transformed = [
            (normal[0] * x) + (normal[1] * y) + (normal[2] * z),
            (normal[3] * x) + (normal[4] * y) + (normal[5] * z),
            (normal[6] * x) + (normal[7] * y) + (normal[8] * z),
        ]
    length = math.sqrt(sum(value * value for value in transformed))
    if length <= 1.0e-12:
        return [0.0, 0.0, 1.0]
    return [value / length for value in transformed]


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


def _matrix_relative_error(left: list[float], right: list[float]) -> float:
    left_values = _finite_matrix(left, "left matrix")
    right_values = _finite_matrix(right, "right matrix")
    magnitude = max(
        1.0,
        *(abs(value) for value in left_values),
        *(abs(value) for value in right_values),
    )
    return max(
        abs(left_values[index] - right_values[index])
        for index in range(16)
    ) / magnitude


def _is_unit_rigid_axis_matrix(matrix: list[float]) -> bool:
    """Return whether a bind only describes an already-baked axis basis."""
    values = _finite_matrix(matrix, "bind matrix")
    rows = [values[offset : offset + 3] for offset in (0, 4, 8)]
    lengths = [math.sqrt(sum(component * component for component in row)) for row in rows]
    if any(abs(length - 1.0) > 1.0e-3 for length in lengths):
        return False
    if math.sqrt(sum(values[index] ** 2 for index in (12, 13, 14))) > 1.0e-3:
        return False
    normalized = [
        [component / length for component in row]
        for row, length in zip(rows, lengths)
    ]
    for left_index in range(3):
        for right_index in range(left_index + 1, 3):
            dot = sum(
                left_component * right_component
                for left_component, right_component in zip(
                    normalized[left_index], normalized[right_index]
                )
            )
            if abs(dot) > 1.0e-3:
                return False
    determinant = (
        normalized[0][0]
        * (
            normalized[1][1] * normalized[2][2]
            - normalized[1][2] * normalized[2][1]
        )
        - normalized[0][1]
        * (
            normalized[1][0] * normalized[2][2]
            - normalized[1][2] * normalized[2][0]
        )
        + normalized[0][2]
        * (
            normalized[1][0] * normalized[2][1]
            - normalized[1][1] * normalized[2][0]
        )
    )
    return abs(abs(determinant) - 1.0) <= 1.0e-3


def _classify_skin_geometry_domain(
    mesh_clusters: dict[int, list[dict[str, Any]]],
) -> str:
    """Classify the scene before any Max-specific Skin matrix is reapplied."""
    votes: list[bool] = []
    for clusters in mesh_clusters.values():
        active = [
            cluster
            for cluster in clusters
            if int(cluster.get("positive_weight_count", 0) or 0) > 0
            and cluster.get("transform") is not None
            and cluster.get("transform_link") is not None
        ]
        if not active:
            continue
        candidates = [
            _multiply_row_major_matrices(
                cluster["transform"], cluster["transform_link"]
            )
            for cluster in active
        ]
        first = candidates[0]
        if any(_matrix_relative_error(first, candidate) > 1.0e-4 for candidate in candidates[1:]):
            return "MIXED"
        votes.append(_is_unit_rigid_axis_matrix(first))
    if votes and all(votes):
        return "ALREADY_BAKED"
    if votes and not any(votes):
        return "LOCAL_BIND"
    return "MIXED"


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
        result = _multiply_row_major_matrices(
            result,
            _axis_rotation_matrix(axis, angles[axis]),
        )
    return result


def _local_trs_matrix(
    translation: list[float], rotation: list[float], scale: list[float]
) -> list[float]:
    return _multiply_row_major_matrices(
        _multiply_row_major_matrices(_scaling_matrix(scale), _rotation_matrix(rotation)),
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
    post_inverse = _invert_row_major_matrix(_rotation_matrix(post_rotation, 0))
    if post_inverse is None:
        raise ValueError("PostRotation is not invertible")
    total_rotation = _multiply_row_major_matrices(
        _multiply_row_major_matrices(post_inverse, local_rotation),
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
        matrix = _multiply_row_major_matrices(matrix, component)
    evaluated_translation = list(matrix[12:15])
    unscaled = _multiply_row_major_matrices(
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
                world = _multiply_row_major_matrices(parts["matrix"], parent_world)
                unscaled_world = _multiply_row_major_matrices(
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
                adjusted = _multiply_row_major_matrices(
                    _multiply_row_major_matrices(
                        _scaling_matrix(adjusted_scale), parts["rotation_matrix"]
                    ),
                    _translation_matrix(adjusted_translation),
                )
                adjusted_unscaled = _multiply_row_major_matrices(
                    parts["rotation_matrix"],
                    _translation_matrix(adjusted_translation),
                )
                world = _multiply_row_major_matrices(adjusted, unscaled_worlds[parent_id])
                unscaled_world = _multiply_row_major_matrices(
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
    inverse = _invert_row_major_matrix(parent_world)
    if inverse is None:
        raise ValueError(f"Model parent {parent_id} is not invertible")
    return _multiply_row_major_matrices(world, inverse)


def _v5_scene_context(
    roots: list[FbxNode], source_graph: dict[str, Any], parents_by_child: dict[int, list[int]]
) -> dict[str, Any]:
    """Derive V5 bind/domain data from the source FBX graph alone."""
    model_nodes = list(source_graph.get("models", []))
    objects_by_id = dict(source_graph.get("objects_by_id", {}))
    parent_ids = dict(source_graph.get("model_parent_ids", {}))
    worlds = _source_model_world_matrices(model_nodes, parent_ids)
    axis_conversion = _generic_axis_conversion_matrix(roots)
    target_worlds = {
        model_id: _multiply_row_major_matrices(world, axis_conversion)
        for model_id, world in worlds.items()
    }
    mesh_ids = set(int(value) for value in source_graph.get("mesh_model_ids", []))
    geometry_by_id = {
        _object_id(node): node
        for node in source_graph.get("geometries", [])
        if _object_id(node) > 0
    }
    model_geometry_ids = dict(source_graph.get("model_geometry_ids", {}))
    unit_scale = _global_unit_scale(roots)
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
        skin_ids = [
            parent_id
            for parent_id in parents_by_child.get(cluster_id, [])
            if objects_by_id.get(parent_id) is not None
            and objects_by_id[parent_id].name == "Deformer"
            and _node_type(objects_by_id[parent_id]).casefold() == "skin"
        ]
        geometry_ids: set[int] = set()
        for skin_id in skin_ids:
            geometry_ids.update(
                int(parent_id)
                for parent_id in parents_by_child.get(skin_id, [])
                if parent_id in geometry_by_id
            )
        if not geometry_ids:
            geometry_ids.update(
                int(parent_id)
                for parent_id in parents_by_child.get(cluster_id, [])
                if parent_id in geometry_by_id
            )
        mesh_model_ids: set[int] = set()
        for geometry_id in geometry_ids:
            mesh_model_ids.update(
                int(parent_id)
                for parent_id in parents_by_child.get(geometry_id, [])
                if parent_id in mesh_ids
            )
        if len(bone_ids) != 1 or not mesh_model_ids:
            continue
        raw_indexes = _child_value(cluster, "Indexes")
        raw_weights = _child_value(cluster, "Weights")
        raw_transform = _child_value(cluster, "Transform")
        raw_link = _child_value(cluster, "TransformLink")
        if raw_indexes is None and raw_weights is None:
            # Some exporters leave connected, but weightless, placeholder
            # Clusters in the scene. They have no skin influence to normalize.
            continue
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
        }
        cluster_by_id[cluster_id] = row
        for mesh_model_id in sorted(mesh_model_ids):
            mesh_clusters.setdefault(mesh_model_id, []).append(row)

    geometry_domain = _classify_skin_geometry_domain(mesh_clusters)
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
    canonical_cluster_matrices: dict[int, tuple[list[float], list[float]]] = {}
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
        if geometry_domain == "ALREADY_BAKED":
            # The Geometry is already in the common scene domain. Keep the
            # control points untouched; Cluster links are still preserved below
            # as the fixed bind pose, while Model worlds remain the current pose.
            domain_scales[mesh_model_id] = 1.0
            bind_mesh_matrices[mesh_model_id] = _identity_matrix()
            for cluster in active_clusters:
                cluster_domain_scales.setdefault(int(cluster["cluster_id"]), []).append(1.0)
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
        for cluster in active_clusters:
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
                _multiply_row_major_matrices(transform, transform_link)
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
            bind_mesh_matrices[mesh_model_id] = _multiply_row_major_matrices(
                _scale_affine_matrix(
                    first, domain, label=f"Mesh {mesh_model_id} bind matrix"
                ),
                axis_conversion,
            )

    bind_candidates_by_bone: dict[int, list[list[float]]] = {}
    for cluster_id, cluster in cluster_by_id.items():
        bone_id = int(cluster["bone_model_id"])
        has_positive_influence = int(cluster.get("positive_weight_count", 0) or 0) > 0
        # TransformLink is the source of truth for the bind pose.  Normalize it
        # into the same axis/unit domain as the rebuilt geometry, but never
        # replace it with the current bone Model world.
        link = _multiply_row_major_matrices(
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
            bind_scale,
            label=f"Bone {bone_id} canonical bind",
        )
        link = _finite_matrix(link, label=f"Bone {bone_id} canonical bind")
        inverse = _invert_row_major_matrix(link)
        if inverse is None:
            raise ValueError(f"Bone {bone_id} canonical bind is not invertible")
        canonical_cluster_matrices[int(cluster_id)] = (inverse, link)
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
            pose = _multiply_row_major_matrices(pose_candidates[0], axis_conversion)
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
                    canonical_bone_worlds[bone_id],
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
                pose_scale,
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
        "output_worlds": output_worlds,
        "domain_scales": domain_scales,
        "bind_mesh_matrices": bind_mesh_matrices,
        "mesh_clusters": mesh_clusters,
        "cluster_matrices": canonical_cluster_matrices,
        "canonical_bind_by_model": canonical_bind_by_model,
        "skinned_mesh_ids": skinned_mesh_ids,
        "unit_scale": unit_scale,
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
        "use_global_axis_domain": True,
        "source_axis_signature": _axis_signature(roots) or [],
        "axis_conversion_matrix": list(axis_conversion),
        "canonical_to_source_axis_matrix": (
            _invert_row_major_matrix(axis_conversion)
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
    return cloned


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

    output_positions: list[list[float]] = []
    output_faces: list[list[int]] = []
    corner_normals: list[list[float]] = []
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
            if position_matrix is not None:
                position = _transform_position_row_major(position, position_matrix)
            output_positions.append(position)
            cp_to_output[source_index].append(output_index)
        if normal_row is not None:
            normal = list(normal_row)
            if normal_matrix is not None:
                normal = _transform_normal_row_major(normal, normal_matrix)
            corner_normals.append(normal)
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
    except (TypeError, ValueError, OverflowError):
        translation = _model_property_vector(source, "Lcl Translation", 3, [0.0, 0.0, 0.0])
        rotation = _model_property_vector(source, "Lcl Rotation", 3, [0.0, 0.0, 0.0])
        scale = _model_property_vector(source, "Lcl Scaling", 3, [1.0, 1.0, 1.0])
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
        "LayerElementUV",
        "LayerElementColor",
        "LayerElementMaterial",
        "Layer",
    }

    def preserve_unknown_children() -> None:
        for child in source.children:
            if child.name in generated_children:
                continue
            cloned = _clone_generic_node(
                child,
                strip_max_metadata=True,
            )
            if cloned is not None:
                geometry.children.append(cloned)

    if payload.get("status") != "rebuilt":
        preserve_unknown_children()
        return geometry

    vertices = payload.get("vertices", [])
    faces = payload.get("faces", [])
    normals = payload.get("loop_normals", [])
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
    source_uv_nodes = [
        child for child in source.children if child.name == "LayerElementUV"
    ]
    uv_nodes_by_index: dict[int, FbxNode] = {}
    uv_node_order: list[tuple[int, FbxNode]] = []
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
        "LayerElementUV",
        "LayerElementColor",
        "LayerElementMaterial",
    }
    generated_bindings: dict[tuple[str, int], bool] = {
        ("LayerElementSmoothing", 0): True,
        ("LayerElementNormal", 0): isinstance(normals, list)
        and len(normals) == sum(len(face) for face in faces),
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
                cloned = _clone_generic_node(
                    source_layer_element,
                    strip_max_metadata=True,
                )
                if cloned is not None:
                    layer.children.append(cloned)
                continue
            layer_type = str(_child_value(source_layer_element, "Type") or "")
            if layer_type not in generated_layer_types:
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
            matrix = (
                canonical_matrices[0 if child_name == "Transform" else 1]
                if canonical_matrices is not None
                else _child_value(source, child_name)
            )
            values = []
            if isinstance(matrix, list):
                try:
                    values = [float(value) for value in matrix]
                except (TypeError, ValueError, OverflowError):
                    values = []
            node.add(child_name, ("d", values))
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
        matrix_node = _child_node(pose_node, "Matrix")
        if source_node is None or not source_node.properties or matrix_node is None:
            continue
        try:
            source_model_id = int(source_node.properties[0])
            matrix = [float(value) for value in (matrix_node.properties[0] if matrix_node.properties else [])]
        except (TypeError, ValueError, OverflowError):
            continue
        output_model_id = id_map.get(source_model_id)
        if canonical_bind_matrices and source_model_id in canonical_bind_matrices:
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
        return roots, {"safe_rebuilder_status": "passthrough_no_objects"}

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
    for mesh_model_id in v5_context["skinned_mesh_ids"]:
        canonical_bind_by_model[int(mesh_model_id)] = _identity_matrix()

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
    # Keep the exact per-Geometry normal/axis receipt used by the embedded
    # Probe. A scene can mix skinned Geometry baked to canonical XYZ with
    # untouched Max/legacy Geometry, so a scene-wide guess is not sufficient.
    axis_conversion = v5_context.get("axis_conversion")
    source_axes_are_canonical = (
        isinstance(axis_conversion, list)
        and _matrices_match(axis_conversion, _identity_matrix())
    )
    canonical_normal_geometry_ids: set[int] = set()
    legacy_normal_geometry_ids: set[int] = set()
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
        # An identity source basis is already canonical. Otherwise only
        # Geometry that went through an explicit bind/axis bake is canonical;
        # untouched Geometry remains in the legacy Max basis.
        baked = (
            isinstance(applied_matrix, list)
            and not _matrices_match(applied_matrix, _identity_matrix())
        )
        domain = (
            FBX_NORMAL_AXIS_DOMAIN_CANONICAL
            if source_axes_are_canonical or baked
            else FBX_NORMAL_AXIS_DOMAIN_LEGACY
        )
        normal_axis_domain_by_geometry_id[str(output_geometry_id)] = domain
        if domain == FBX_NORMAL_AXIS_DOMAIN_CANONICAL:
            canonical_normal_geometry_ids.add(output_geometry_id)
        else:
            legacy_normal_geometry_ids.add(output_geometry_id)

    emitted_geometry_ids: set[int] = set()
    geometry_cp_maps: dict[int, dict[int, list[int]]] = {}
    geometry_rebuilt_count = 0
    geometry_header_only_count = 0
    geometry_skip_reasons: list[str] = []
    skin_clusters_remapped = 0
    for source_id in sorted(mesh_model_ids):
        source_model = model_by_id[source_id]
        model_name = model_names.get(source_id, "Mesh")
        for geometry_id in model_geometry_ids.get(source_id, []):
            source_geometry = geometry_by_id.get(geometry_id)
            if source_geometry is None or geometry_id in emitted_geometry_ids:
                continue
            bind_matrix = (
                v5_context["bind_mesh_matrices"].get(source_id)
                if source_id in v5_context["skinned_mesh_ids"]
                else None
            )
            geometry_payload = _extract_geometry_semantics(
                source_geometry,
                position_matrix=bind_matrix,
                normal_matrix=bind_matrix,
            )
            record_normal_axis_domain(
                geometry_id,
                applied_matrix=bind_matrix,
            )
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
                local_matrix=_output_local_from_world(
                    source_id, output_worlds, source_graph["model_parent_ids"]
                ),
                name_override=model_name,
            )
        )
        emitted_object_ids.add(source_id)

    # Preserve valid unlinked Geometry objects as standalone Geometry records.
    for geometry_id, source_geometry in geometry_by_id.items():
        if geometry_id in emitted_geometry_ids:
            continue
        geometry_payload = _extract_geometry_semantics(source_geometry)
        record_normal_axis_domain(geometry_id, applied_matrix=None)
        generic_objects.children.append(
            _generic_geometry_node(
                source_geometry,
                geometry_output_ids[geometry_id],
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
        generic_objects.children.append(
            _generic_deformer_node(
                source,
                id_map[source_id],
                index_map=index_map,
                canonical_matrices=canonical_matrices,
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
            canonicalize_axes=_generic_axis_signature_is_valid(roots),
        ),
        "Definitions": _generic_definitions(generic_objects),
        "Objects": generic_objects,
        "Connections": generic_connections,
    }
    for name in preferred_order:
        if name in root_replacements:
            generic_roots.append(root_replacements[name])
        elif name in root_by_name:
            cloned = _clone_generic_node(root_by_name[name])
            if cloned is not None:
                generic_roots.append(cloned)
    for source_root in roots:
        if source_root.name in preferred_order or source_root.name == "Creator":
            continue
        cloned = _clone_generic_node(source_root)
        if cloned is not None:
            generic_roots.append(cloned)
    creator = next((node for node in generic_roots if node.name == "Creator"), None)
    if creator is None:
        generic_roots.append(FbxNode("Creator", ["Generic FBX Converter"], ["S"], []))
    elif creator.properties:
        creator.properties[0] = "Generic FBX Converter"
    return generic_roots, {
        "safe_rebuilder_status": "rebuilt",
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
        "legacy_normal_geometry_ids": sorted(legacy_normal_geometry_ids),
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
        # Keep the source->canonical basis used by rebuilt Model rows. The
        # downstream Writer can apply its inverse without assuming every Max
        # FBX used the same axis signature.
        "source_axis_signature": _axis_signature(roots) or [],
        "axis_conversion_matrix": list(axis_conversion),
        "canonical_to_source_axis_matrix": (
            _invert_row_major_matrix(axis_conversion)
            or _identity_matrix()
        ),
    }


def default_output_path(source: str | Path) -> Path:
    source_path = Path(source).resolve()
    suffix = source_path.suffix or ".fbx"
    return source_path.with_name(f"Generic 通用 {source_path.stem}{suffix}")


def _verify_round_trip(
    source: dict[str, Any],
    output: dict[str, Any],
    normalization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks: list[str] = []
    errors: list[str] = []
    semantic_rebuild = bool(
        normalization
        and (
            int(normalization.get("geometry_rebuilt_count", 0) or 0) > 0
            or int(normalization.get("skin_clusters_remapped", 0) or 0) > 0
        )
    )
    header_only_route = bool(
        normalization
        and int(normalization.get("geometry_skipped_count", 0) or 0) > 0
    )
    structural_rebuild = bool(
        normalization and normalization.get("safe_rebuilder_status") == "rebuilt"
    )
    other_mesh_empty_geometry_count = 0
    if normalization:
        try:
            other_mesh_empty_geometry_count = max(
                0, int(normalization.get("other_mesh_empty_geometry_count", 0) or 0)
            )
        except (TypeError, ValueError, OverflowError):
            other_mesh_empty_geometry_count = 0
    for key in (
        "geometry_count", "normal_layer_count", "uv_layer_count", "skin_deformer_count",
        "skin_cluster_count", "material_count", "texture_count", "video_count",
        "connection_count", "animation_stack_count", "animation_layer_count", "objects_child_count",
    ):
        if source.get(key) != output.get(key):
            if (
                key == "geometry_count"
                and other_mesh_empty_geometry_count
                and output.get(key)
                == source.get(key, 0) + other_mesh_empty_geometry_count
            ):
                checks.append("other_mesh_empty_geometry_added")
            elif structural_rebuild and key in {"connection_count", "objects_child_count"}:
                checks.append(f"{key}_changed_by_safe_rebuilder")
            else:
                errors.append(f"{key}:{source.get(key)}!={output.get(key)}")
        else:
            checks.append(key)
    if source.get("embedded_media_sha256") != output.get("embedded_media_sha256"):
        errors.append("embedded_media_sha256")
    else:
        checks.append("embedded_media_sha256")
    for key in ("geometry_digests", "skin_cluster_digests", "connection_digests"):
        if source.get(key) != output.get(key):
            if (semantic_rebuild and key in {"geometry_digests", "skin_cluster_digests"}) or (
                structural_rebuild and key == "connection_digests"
            ):
                checks.append(f"{key}_changed_by_semantic_rebuild")
            elif header_only_route and key == "geometry_digests":
                # HEADER ONLY intentionally drops malformed Geometry payload
                # arrays while retaining the Geometry object and graph edge.
                checks.append("geometry_digests_changed_by_header_only_route")
            elif other_mesh_empty_geometry_count and key == "geometry_digests":
                checks.append("geometry_digests_changed_by_other_mesh_placeholders")
            else:
                errors.append(key)
        else:
            checks.append(key)
    if structural_rebuild:
        checks.append("safe_rebuilder_structure")
    elif semantic_rebuild:
        checks.append("semantic_rebuild")
    elif header_only_route:
        checks.append("header_only_geometry_route")
    if int(output.get("max_metadata_count", 0) or 0) != 0:
        errors.append("max_metadata_not_removed")
    else:
        checks.append("max_metadata_removed")
    return {"status": "PASS" if not errors else "FAIL", "checks": checks, "errors": errors}


def convert_fbx(source_fbx: str | Path, output_fbx: str | Path | None = None) -> dict[str, Any]:
    source = Path(source_fbx).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Source FBX does not exist: {source}")
    output = Path(output_fbx).resolve() if output_fbx is not None else default_output_path(source)
    if output == source:
        raise ValueError("Output FBX must be different from the source")
    version, roots, source_footer_id = read_fbx(source, include_footer_id=True)
    source_stats = collect_stats(roots)
    normalization = normalize_generic_tree(roots)
    roots, safe_rebuilder = _safe_rebuild_generic_scene(roots)
    normalization.update(safe_rebuilder)
    write_fbx(output, version, roots, footer_id=source_footer_id)
    output_version, output_roots = read_fbx(output)
    output_stats = collect_stats(output_roots)
    round_trip = _verify_round_trip(source_stats, output_stats, normalization)
    if output_version != version:
        round_trip["checks"].append("fbx_version_reencoded")
    return {
        "status": "PASS" if round_trip["status"] == "PASS" else "FAIL",
        "source_path": str(source),
        "output_path": str(output),
        # Expose the same top-level receipt fields used by the embedded Probe
        # so a caller does not need to guess whether the file was converted
        # once globally or still requires per-Mesh axis handling.
        "fbx_axis_output_policy": normalization.get(
            "fbx_axis_output_policy", FBX_AXIS_OUTPUT_POLICY
        ),
        "axis_transform_contract": normalization.get(
            "axis_transform_contract", FBX_AXIS_TRANSFORM_CONTRACT
        ),
        "use_global_axis_domain": bool(
            normalization.get("use_global_axis_domain", True)
        ),
        "source": source_stats,
        "output": output_stats,
        "normalization": normalization,
        "round_trip": round_trip,
    }


# ---------------------------------------------------------------------------
# GUI

def _relaunch_without_console() -> bool:
    """Hand a file-associated GUI launch to pythonw.exe and let py.exe exit."""
    if os.name != "nt" or os.environ.get("_GENERIC_FBX_CONVERTER_PYTHONW") == "1":
        return False
    executable = Path(sys.executable)
    if executable.name.casefold() not in {"python.exe", "py.exe"}:
        return False
    pythonw = executable.with_name("pythonw.exe")
    if not pythonw.is_file():
        return False

    script = Path(__file__).resolve()
    environment = os.environ.copy()
    environment["_GENERIC_FBX_CONVERTER_PYTHONW"] = "1"
    creation_flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )
    startup_info = None
    if hasattr(subprocess, "STARTUPINFO"):
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
        startup_info.wShowWindow = 0
    try:
        subprocess.Popen(
            [str(pythonw), str(script), *sys.argv[1:]],
            cwd=str(script.parent),
            env=environment,
            close_fds=True,
            creationflags=creation_flags,
            startupinfo=startup_info,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return True


def _detach_windows_console() -> None:
    """Keep a double-clicked .py GUI from leaving a console window behind."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.FreeConsole()
    except Exception:
        pass


def _apply_windows_dark_title_bar(window: Any, dark: bool, background: str) -> None:
    """Set the native Windows caption to the same light/dark palette."""
    if os.name != "nt":
        return
    try:
        import ctypes

        top_window = window.winfo_toplevel() if hasattr(window, "winfo_toplevel") else window
        hwnd_value = int(top_window.winfo_id())
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        top_hwnd = user32.GetAncestor(ctypes.c_void_p(hwnd_value), 2)
        if top_hwnd:
            hwnd_value = int(top_hwnd)
        hwnd = ctypes.c_void_p(hwnd_value)
        dwmapi = ctypes.windll.dwmapi
        set_attribute = dwmapi.DwmSetWindowAttribute
        set_attribute.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        set_attribute.restype = ctypes.c_long
        enabled = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):
            set_attribute(
                hwnd,
                attribute,
                ctypes.byref(enabled),
                ctypes.sizeof(enabled),
            )
        red = int(background[1:3], 16)
        green = int(background[3:5], 16)
        blue = int(background[5:7], 16)
        color_ref = ctypes.c_uint32((blue << 16) | (green << 8) | red)
        set_attribute(hwnd, 35, ctypes.byref(color_ref), ctypes.sizeof(color_ref))
        text_ref = ctypes.c_uint32(0x00FFFFFF if dark else 0x0018202B)
        set_attribute(hwnd, 36, ctypes.byref(text_ref), ctypes.sizeof(text_ref))
    except Exception:
        pass

def _language() -> str:
    candidates: list[Any] = []
    try:
        candidates.append(locale.getlocale()[0])
    except Exception:
        pass
    try:
        candidates.append(locale.getdefaultlocale()[0])
    except Exception:
        pass
    text = " ".join(str(value or "") for value in candidates).casefold()
    return "zh" if text.startswith("zh") or "zh_" in text else "en"


TEXT = {
    "zh": {
        "title": "Generic FBX Converter 通用FBX 转换器",
        "update_notice": " | 发现Github Release 新版本！",
        "subtitle": "读取 FBX 的 Geometry、法线、UV 与 Skin 语义，重建为 Blender 与 3ds Max 通用场景",
        "convert": "选择 FBX 并转换",
        "dark": "深色模式",
        "language": "English",
        "ready": "准备就绪",
        "cancel": "已取消",
        "source": "源文件",
        "output": "输出文件",
        "success": "转换完成，语义重建与二进制回读验证通过",
        "failed": "转换完成，但回读验证发现差异",
        "overwrite": "输出文件已存在，是否覆盖？",
        "error": "转换失败",
        "choose": "选择 FBX 文件",
        "file_types": "FBX 文件",
    },
    "en": {
        "title": "Generic FBX Converter",
        "update_notice": " | New GitHub Release available!",
        "subtitle": "Read FBX Geometry, normals, UVs and Skin semantics into a generic Blender/3ds Max scene",
        "convert": "Choose FBX and Convert",
        "dark": "Dark mode",
        "language": "中文",
        "ready": "Ready",
        "cancel": "Cancelled",
        "source": "Source",
        "output": "Output",
        "success": "Conversion complete; semantic rebuild and binary read-back verification passed",
        "failed": "Conversion complete, but read-back verification found differences",
        "overwrite": "The output file already exists. Overwrite it?",
        "error": "Conversion failed",
        "choose": "Choose an FBX file",
        "file_types": "FBX files",
    },
}


# ---------------------------------------------------------------------------
# GITHUB UPDATE CHECK
#
# The checker is deliberately independent from the FBX reader and from the
# GUI conversion path.  It performs one best-effort lookup when the window is
# opened: GitHub's repository tree is traversed for a blob whose *filename*
# matches this script, then its Git blob hash is compared with the local file.
# No repository path is hard-coded, and every network/JSON failure is silent.

GITHUB_REPOSITORY = (
    "Criticise/PC-REHD-1.2.8-Scrpit-rewritten-in-Python-by-Code-X-GPT-5.6"
)
GITHUB_RELEASE_URL = (
    "https://github.com/Criticise/PC-REHD-1.2.8-Scrpit-rewritten-in-Python-by-Code-X-GPT-5.6"
    "/releases/tag/v1.0.0"
)
GITHUB_API_REPOSITORY_URL = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
GITHUB_UPDATE_CHECK_TIMEOUT_SECONDS = 6.0
GITHUB_UPDATE_CHECK_USER_AGENT = "Generic-FBX-Converter-update-check"


def _github_json(url: str) -> dict[str, Any] | None:
    """Read one GitHub JSON response without ever surfacing network errors."""
    request = urllib.request.Request(
        str(url),
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": GITHUB_UPDATE_CHECK_USER_AGENT,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=GITHUB_UPDATE_CHECK_TIMEOUT_SECONDS,
        ) as response:
            status = int(getattr(response, "status", 200) or 200)
            if status < 200 or status >= 300:
                return None
            # The recursive tree can be larger than a few MiB.  Read the
            # complete GitHub response so a valid tree is not treated as a
            # JSON failure merely because an arbitrary probe limit was hit.
            raw_payload = response.read()
        payload = json.loads(raw_payload.decode("utf-8", errors="replace"))
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        TypeError,
    ):
        return None
    return payload if isinstance(payload, dict) else None


def _git_blob_sha1(data: bytes) -> str:
    """Return the same content hash GitHub reports for a tree blob."""
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def _check_github_update(local_script: str | Path | None = None) -> dict[str, Any]:
    """Check the repository once; return a quiet, UI-ready result dictionary."""
    try:
        local_path = (
            Path(local_script).resolve()
            if local_script is not None
            else Path(__file__).resolve()
        )
        if not local_path.is_file() or local_path.suffix.casefold() != ".py":
            return {"available": False, "reason": "local_script_missing"}
        local_hash = _git_blob_sha1(local_path.read_bytes())

        repository = _github_json(GITHUB_API_REPOSITORY_URL)
        default_branch = (
            str(repository.get("default_branch", "") or "").strip()
            if isinstance(repository, dict)
            else ""
        )
        if not default_branch:
            return {"available": False, "reason": "default_branch_missing"}

        tree_url = (
            f"{GITHUB_API_REPOSITORY_URL}/git/trees/"
            f"{urllib.parse.quote(default_branch, safe='')}?recursive=1"
        )
        tree_payload = _github_json(tree_url)
        tree_rows = tree_payload.get("tree") if isinstance(tree_payload, dict) else None
        if not isinstance(tree_rows, list):
            return {"available": False, "reason": "tree_unavailable"}
        if bool(tree_payload.get("truncated")):
            # A truncated recursive tree is not authoritative.  Keep the
            # check advisory and silent instead of making a partial decision.
            return {"available": False, "reason": "tree_truncated"}

        local_name = local_path.name.casefold()
        candidates: list[dict[str, str]] = []
        for row in tree_rows:
            if not isinstance(row, dict) or str(row.get("type", "")) != "blob":
                continue
            remote_path = str(row.get("path", "") or "").replace("\\", "/")
            if remote_path.rsplit("/", 1)[-1].casefold() != local_name:
                continue
            remote_hash = str(row.get("sha", "") or "").strip().casefold()
            if not remote_hash:
                continue
            candidates.append({"path": remote_path, "sha": remote_hash})
        if not candidates:
            return {"available": False, "reason": "same_name_file_not_found"}

        changed = [row for row in candidates if row["sha"] != local_hash]
        if not changed:
            return {
                "available": False,
                "reason": "same_hash",
                "local_hash": local_hash,
                "matched_paths": [row["path"] for row in candidates],
            }
        return {
            "available": True,
            "local_hash": local_hash,
            "matched_paths": [row["path"] for row in candidates],
            "changed_paths": [row["path"] for row in changed],
            "release_url": GITHUB_RELEASE_URL,
        }
    except Exception:
        # Update discovery is advisory.  It must never prevent conversion or
        # make an offline/restricted machine display an error dialog.
        return {"available": False, "reason": "check_failed"}


def _start_github_update_check(
    root: Any,
    on_result: Any,
) -> None:
    """Start exactly one daemon check and marshal its result onto Tk's thread."""
    def worker() -> None:
        result = _check_github_update(Path(__file__))

        def deliver_result() -> None:
            try:
                on_result(result)
            except Exception:
                # Tk can be torn down between the network reply and this
                # callback.  Update discovery must never leak a GUI error.
                pass

        try:
            root.after(0, deliver_result)
        except Exception:
            # The user may close the window while the request is in flight.
            pass

    try:
        threading.Thread(
            target=worker,
            name="Generic-FBX-Converter-GitHub-Update",
            daemon=True,
        ).start()
    except Exception:
        pass


def _launch_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox

    state = _load_user_state()
    lang = state["language"] or _language()
    labels = TEXT[lang]
    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-alpha", 0.0)
    except tk.TclError:
        pass

    colors = {
        "light": {"bg": "#f4f6f8", "panel": "#ffffff", "fg": "#18212b", "accent": "#1769aa"},
        "dark": {"bg": "#20252b", "panel": "#2b323a", "fg": "#edf2f7", "accent": "#55a7e8"},
    }
    root.title(labels["title"])
    root.minsize(700, 430)

    outer = tk.Frame(root, padx=28, pady=24)
    outer.pack(fill="both", expand=True)
    heading_row = tk.Frame(outer)
    heading_row.pack(fill="x")
    heading = tk.Label(heading_row, text=labels["title"], anchor="w", font=("Segoe UI", 18, "bold"))
    heading.pack(side="left")
    update_badge = tk.Label(
        heading_row,
        text=labels["update_notice"],
        anchor="w",
        font=("Segoe UI", 10, "bold"),
        cursor="hand2",
        relief="flat",
        bd=0,
        highlightthickness=0,
    )
    subtitle = tk.Label(outer, text=labels["subtitle"], anchor="w", justify="left", wraplength=630, font=("Segoe UI", 10))
    subtitle.pack(fill="x", pady=(7, 18))

    controls = tk.Frame(outer)
    controls.pack(fill="x")
    dark_var = tk.BooleanVar(value=state["dark_mode"])
    convert_button = tk.Button(controls, text=labels["convert"], padx=18, pady=9, relief="flat", cursor="hand2")
    convert_button.pack(side="left")
    language_button = tk.Button(controls, text=labels["language"], padx=12, pady=7, relief="flat", cursor="hand2")
    language_button.pack(side="right", padx=(10, 0))
    dark_button = tk.Checkbutton(controls, text=labels["dark"], variable=dark_var, relief="flat")
    dark_button.pack(side="right")

    status_var = tk.StringVar(value=labels["ready"])
    status_state = {"key": "ready"}
    update_state = {"started": False, "available": False}
    status = tk.Label(outer, textvariable=status_var, anchor="w", font=("Segoe UI", 10, "bold"))
    status.pack(fill="x", pady=(18, 8))
    result_text = tk.Text(outer, height=14, wrap="word", state="disabled", relief="flat", padx=12, pady=10, font=("Consolas", 9))
    result_text.pack(fill="both", expand=True)

    def set_result(text: str) -> None:
        result_text.configure(state="normal")
        result_text.delete("1.0", "end")
        result_text.insert("1.0", text)
        result_text.configure(state="disabled")

    def set_status(key: str, suffix: str = "") -> None:
        status_state["key"] = key
        status_var.set(labels[key] + suffix)

    def save_user_state() -> None:
        _save_user_state(lang, bool(dark_var.get()))

    def refresh_window_title() -> None:
        suffix = labels["update_notice"] if update_state["available"] else ""
        root.title(labels["title"] + suffix)

    def open_release_page(_event: Any = None) -> None:
        try:
            webbrowser.open_new_tab(GITHUB_RELEASE_URL)
        except Exception:
            pass

    def apply_update_result(result: Any) -> None:
        if not isinstance(result, dict) or not bool(result.get("available")):
            return
        update_state["available"] = True
        palette = colors["dark" if dark_var.get() else "light"]
        update_badge.configure(
            text=labels["update_notice"],
            fg=palette["accent"],
            activeforeground=palette["accent"],
            activebackground=palette["bg"],
        )
        if not update_badge.winfo_ismapped():
            update_badge.pack(side="left", padx=(10, 0))
        refresh_window_title()
        root.update_idletasks()

    def start_update_check() -> None:
        if update_state["started"]:
            return
        update_state["started"] = True
        _start_github_update_check(root, apply_update_result)

    def toggle_language() -> None:
        nonlocal labels, lang
        lang = "en" if lang == "zh" else "zh"
        labels = TEXT[lang]
        refresh_window_title()
        heading.configure(text=labels["title"])
        update_badge.configure(text=labels["update_notice"])
        subtitle.configure(text=labels["subtitle"])
        convert_button.configure(text=labels["convert"])
        language_button.configure(text=labels["language"])
        dark_button.configure(text=labels["dark"])
        set_status(status_state["key"], "..." if status_state["key"] == "convert" else "")
        apply_theme()

    def apply_theme() -> None:
        palette = colors["dark" if dark_var.get() else "light"]
        root.configure(bg=palette["bg"])
        _apply_windows_dark_title_bar(root, dark_var.get(), palette["bg"])
        for widget in (outer, heading_row, controls):
            widget.configure(bg=palette["bg"])
        for widget in (heading, update_badge, subtitle, status):
            widget.configure(bg=palette["bg"], fg=palette["fg"])
        if update_state["available"]:
            update_badge.configure(fg=palette["accent"], activeforeground=palette["accent"])
        update_badge.configure(activebackground=palette["bg"])
        dark_button.configure(bg=palette["bg"], fg=palette["fg"], activebackground=palette["bg"], activeforeground=palette["fg"], selectcolor=palette["panel"])
        language_button.configure(bg=palette["panel"], fg=palette["fg"], activebackground=palette["panel"], activeforeground=palette["fg"])
        convert_button.configure(bg=palette["accent"], fg="white", activebackground=palette["accent"], activeforeground="white")
        result_text.configure(bg=palette["panel"], fg=palette["fg"], insertbackground=palette["fg"])
        save_user_state()

    def result_summary(result: dict[str, Any]) -> str:
        source = result["source"]
        output = result["output"]
        round_trip = result["round_trip"]
        rows = [
            f"{labels['source']}: {result['source_path']}",
            f"{labels['output']}: {result['output_path']}",
            "",
            f"Geometry: {source['geometry_count']} -> {output['geometry_count']}",
            f"Geometry rebuilt: {result['normalization'].get('geometry_rebuilt_count', 0)}; skipped: {result['normalization'].get('geometry_skipped_count', 0)}",
            f"Normals: {source['normal_layer_count']} -> {output['normal_layer_count']}",
            f"UV layers: {source['uv_layer_count']} -> {output['uv_layer_count']}",
            f"Skin clusters: {source['skin_cluster_count']} -> {output['skin_cluster_count']}",
            f"Skin clusters remapped: {result['normalization'].get('skin_clusters_remapped', 0)}",
            f"Materials / Textures / Videos: {source['material_count']} / {source['texture_count']} / {source['video_count']}",
            f"Connections: {source['connection_count']} -> {output['connection_count']}",
            f"Animations: {source['animation_stack_count']} stacks, {source['animation_layer_count']} layers",
            f"Max metadata removed: {result['normalization']['removed_max_metadata']}",
            "",
            f"Read-back: {round_trip['status']}",
        ]
        if round_trip["errors"]:
            rows.append("Differences: " + ", ".join(round_trip["errors"]))
        return "\n".join(rows)

    def convert_clicked() -> None:
        selected = filedialog.askopenfilename(
            parent=root,
            title=labels["choose"],
            filetypes=[(labels["file_types"], "*.fbx"), ("All files", "*.*")],
        )
        if not selected:
            set_status("cancel")
            return
        source = Path(selected)
        output = default_output_path(source)
        if output.exists() and not messagebox.askyesno(labels["title"], labels["overwrite"], parent=root):
            set_status("cancel")
            return
        convert_button.configure(state="disabled")
        set_status("convert", "...")
        root.update_idletasks()
        try:
            result = convert_fbx(source, output)
        except Exception as exc:
            set_status("error")
            messagebox.showerror(labels["error"], f"{type(exc).__name__}: {exc}", parent=root)
        else:
            set_status("success" if result["status"] == "PASS" else "failed")
            set_result(result_summary(result))
        finally:
            convert_button.configure(state="normal")

    convert_button.configure(command=convert_clicked)
    dark_button.configure(command=apply_theme)
    language_button.configure(command=toggle_language)
    update_badge.bind("<Button-1>", open_release_page)
    update_badge.pack_forget()
    root.protocol("WM_DELETE_WINDOW", lambda: (save_user_state(), root.destroy()))
    apply_theme()
    root.update_idletasks()
    width = max(700, root.winfo_reqwidth())
    height = max(430, root.winfo_reqheight())
    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    root.geometry(f"{width}x{height}+{max(0, (screen_width - width) // 2)}+{max(0, (screen_height - height) // 2)}")
    def show_window() -> None:
        root.deiconify()
        try:
            root.attributes("-alpha", 1.0)
        except tk.TclError:
            pass
        root.update_idletasks()
        apply_theme()
        start_update_check()

    root.after_idle(show_window)
    root.mainloop()











# ---------------------------------------------------------------------------
# USER STATE DATA
#
# This small block is intentionally isolated from the converter and GUI layout
# code. It is the only place that defines the persisted language/theme state.

USER_STATE_DEFAULTS: dict[str, Any] = {
    "language": None,
    "dark_mode": False,
}
USER_STATE_FILE = (
    Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    / "Generic FBX Converter"
    / "user_state.json"
)


def _load_user_state() -> dict[str, Any]:
    state = dict(USER_STATE_DEFAULTS)
    try:
        payload = json.loads(USER_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return state
    if not isinstance(payload, dict):
        return state
    language = payload.get("language")
    if language in TEXT:
        state["language"] = language
    dark_mode = payload.get("dark_mode")
    if isinstance(dark_mode, bool):
        state["dark_mode"] = dark_mode
    return state


def _save_user_state(language: str | None, dark_mode: bool) -> None:
    state = {
        "language": language if language in TEXT else None,
        "dark_mode": bool(dark_mode),
    }
    try:
        USER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        USER_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError):
        pass


if __name__ == "__main__":
    if _relaunch_without_console():
        raise SystemExit(0)
    _detach_windows_console()
    _launch_gui()
