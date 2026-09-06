from __future__ import annotations

"""RE6/REHD MOD import contract and FBX builder.

This module is intentionally self-contained. It parses a source MOD into the
scene contract consumed directly by the Python Launcher, writes an explicit
normal route receipt, and emits a standard FBX 7.4
binary file without requiring Blender, Noesis, Assimp, or the Autodesk SDK.

AI MAINTENANCE GATE
===================
The parser layouts below preserve the verified RE6 import behavior.
When changing one of these layouts, update all three places in the same change:
1. this module's FVF_LAYOUTS and import regression fixtures;
2. codex_python_runtime_bootstrap.py module contract and clean-import gate;
3. the release copy / package verification workflow.

The regression guard also locks V4 scene-contract details discovered by real
Max 2026 comparison: FBX XYZ is composed as Rz*Ry*Rx, Mesh smoothing groups use
the 1-based face ordinal, positive Skin lanes below 1e-4 survive, and Max's
exported Cluster Transform is derived from the Max-space bone bind matrix.
Do not simplify any of those rules without a new Python/Max scene comparison.

``include_normals=True`` embeds MOD normals in FBX mesh geometry; ``False``
emits an FBX with no normal layer. The Launcher defaults this switch on and
calls the Python API directly. Other callers
must pass ``False`` to request the legacy PC-REHD 1.2.8 no-normal import.
"""

import argparse
import array
import base64
import dataclasses
import hashlib
import json
import math
import os
import re
import struct
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from codex_python_runtime_bootstrap import (
    runtime_json_dumps_text,
    runtime_write_json_file,
)
from codex_re6_scene_compatibility import describe_import_skin_compatibility

# Runtime installation and dependency repair belong to Launcher startup health,
# never to a user-triggered import. This module is pure-Python on its hot path;
# importing it must not acquire the runtime install lock or launch a bootstrap.


# ============================================================================
# BEGIN RE6 MOD IMPORT PUBLIC CONTRACT
#
# The following revision is consumed by the bootstrap clean-import gate.  Do
# not increase it merely to bypass a failing regression.  A revision change
# requires a real parser/route/FBX contract change and new regression proof.
# ============================================================================
IMPORT_MODULE_CONTRACT_REVISION = 12
IMPORT_ROUTE_SCHEMA = "codex-re6-mod-import-route-v1"
IMPORT_MANIFEST_SCHEMA = "codex-re6-mod-import-manifest-v1"
BLENDER_SCENE_DATA_SCHEMA = "pc-rehd-code-x-blender-scene-data-v1"
BLENDER_SCENE_DATA_REVISION = 1
FBX_BINARY_VERSION = 7400
# MOD coordinates and the matrices below are authored in the Max scene's
# native inch basis.  Declare that basis in FBX metadata so Max and Blender
# apply the same 2.54 cm-per-unit conversion; geometry and Skin arrays stay
# numerically untouched.
FBX_UNIT_METERS = 0.0254
FBX_UNIT_SCALE_FACTOR = FBX_UNIT_METERS / 0.01
FBX_CREATOR = "Codex RE6 MOD Import FBX Builder"
FBX_MAX_BITMAP_COMPATIBILITY_POLICY = "DiffuseColor"
FBX_NORMAL_PROFILE_MAX = "max"
FBX_NORMAL_PROFILE_BLENDER_SAFE = "blender_safe"
FBX_NORMAL_PROFILES = frozenset(
    {FBX_NORMAL_PROFILE_MAX, FBX_NORMAL_PROFILE_BLENDER_SAFE}
)

MOD_HEADER_STRUCT = struct.Struct("<HHHIIIIIIIIIIIII")
MESH_HEADER_STRUCT = struct.Struct("<HHBHBBBBBIIIIIIBBBBHHI")
BONE_INFO_STRUCT = struct.Struct("<BBBB5f")
MATRIX4X4_STRUCT = struct.Struct("<16f")
BONE_MAP_ROW_STRUCT = struct.Struct("<8I")
TRIANGLE_STRUCT = struct.Struct("<3H")
_UINT16_STRUCT = struct.Struct("<H")
_UINT16X2_STRUCT = struct.Struct("<2H")
_INT16_STRUCT = struct.Struct("<h")
_INT16X3_STRUCT = struct.Struct("<hhh")
_FLOAT32_STRUCT = struct.Struct("<f")
_FLOAT32X3_STRUCT = struct.Struct("<fff")
_BYTE4_STRUCT = struct.Struct("<4B")
_BYTE8_STRUCT = struct.Struct("<8B")
_READ_STRUCT_CACHE: dict[str, struct.Struct] = {
    item.format: item
    for item in (
        MOD_HEADER_STRUCT,
        MESH_HEADER_STRUCT,
        BONE_INFO_STRUCT,
        MATRIX4X4_STRUCT,
        BONE_MAP_ROW_STRUCT,
        TRIANGLE_STRUCT,
        _UINT16_STRUCT,
        struct.Struct("<HH"),
        _INT16X3_STRUCT,
        _FLOAT32_STRUCT,
        _FLOAT32X3_STRUCT,
        _INT16_STRUCT,
        _UINT16X2_STRUCT,
        _BYTE4_STRUCT,
        _BYTE8_STRUCT,
        struct.Struct("<4f"),
        struct.Struct("<5I"),
        struct.Struct("<16f"),
    )
}
_FBX_ARRAY_FORMAT_BY_KIND = {
    "b": "B",
    "i": "i",
    "l": "q",
    "f": "f",
    "d": "d",
}
MOD_HEADER_PREFIX_SIZE = 4 + 2 + MOD_HEADER_STRUCT.size
BONE_INFO_SIZE = BONE_INFO_STRUCT.size
BONE_MATRIX_SIZE = MATRIX4X4_STRUCT.size
BONE_MTP_SIZE = 256
MOD_FULL_SCAN_SCHEMA = "re6-mod-full-scan-v1"
MOD_FULL_SCAN_HEXDUMP_BYTES_PER_ROW = 16
EPSILON = 1.0e-10

LOD_GROUP_IDS = (0, 1, 2, 3, 4, 5, 6, 249, 252, 254, 255)

FIX_PROCESSING_MODE_CODEX = "codex"
FIX_PROCESSING_MODE_LEGACY_128 = "legacy_128"
FIX_PROCESSING_MODES = frozenset(
    {FIX_PROCESSING_MODE_CODEX, FIX_PROCESSING_MODE_LEGACY_128}
)

# MRL material binding is deliberately opt-in.  The normal MOD import route
# remains geometry-only unless a caller supplies an MRL to the new API below.
MRL_TEXTURE_SOURCE_MODES = frozenset({"dds", "tex"})
MRL_TEXTURE_TRAILING_SUFFIX_RE = re.compile(r"(?:\.(?:tex|dds))+$", re.IGNORECASE)
MRL_TEXTURE_SEARCH_DIRECTORY_NAMES = frozenset(
    {"model", "models", "texture", "textures", "dds", "tex"}
)
MRL_TEXTURE_SEARCH_MAX_ANCESTORS = 10
MRL_TEXTURE_SEARCH_MAX_FILES = 50000
MRL_BASE_COLOR_SHADERS = frozenset({"tbasemap", "talbedomap", "basemap", "diffuse"})
MRL_BASE_COLOR_RESOURCE_TOKENS = frozenset(
    {"bm", "basemap", "albedo", "albedomap", "diffuse", "basecolor", "base_color"}
)
MRL_SHADER_SLOTS = {
    44852: "tbasemap", 24362: "tthinmap", 839791: "talbedomap",
    60699: "tspecularmap", 698096: "tlightmap", 92554: "ttransparencymap",
    98767: "tspecularblendmap", 117546: "thairshiftmap", 123937: "tocclusionmap",
    140896: "tnormalmap", 171811: "tgrassalbedomap", 214004: "tspheremap",
    299850: "tvtxdisplacement", 359452: "talbedoblend2map", 376460: "tguibasemap",
    412739: "tenvmap", 437118: "tindirectmap", 481875: "tdetailnormalmap",
    501998: "tmaskmap", 557413: "tdetailmaskmap", 870036: "tdetailnormalmap2",
    972478: "tnormalblendmap", 973115: "temissionmap", 986046: "tbuilderbasemap",
    1045950: "talbedoblendmap", 14784: "tvtxdispmask",
}


def _normalize_fix_processing_mode(value: Any) -> str:
    normalized = str(value or FIX_PROCESSING_MODE_CODEX).strip().casefold()
    if normalized in {"legacy", "legacy128", "1.2.8", "128"}:
        normalized = FIX_PROCESSING_MODE_LEGACY_128
    elif normalized in {"codex", "code_x", "codex_mode"}:
        normalized = FIX_PROCESSING_MODE_CODEX
    if normalized not in FIX_PROCESSING_MODES:
        raise ValueError(
            "Unsupported fix processing mode: "
            f"{value!r}; expected 'codex' or 'legacy_128'"
        )
    return normalized


def _normalize_fbx_normal_profile(value: Any) -> str:
    """Keep Max's established output separate from Blender-safe loop normals."""

    normalized = str(value or FBX_NORMAL_PROFILE_MAX).strip().casefold()
    aliases = {
        "max_explicit": FBX_NORMAL_PROFILE_MAX,
        "max": FBX_NORMAL_PROFILE_MAX,
        "blender": FBX_NORMAL_PROFILE_BLENDER_SAFE,
        "blender_safe": FBX_NORMAL_PROFILE_BLENDER_SAFE,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in FBX_NORMAL_PROFILES:
        raise ValueError(
            "Unsupported FBX normal profile: "
            f"{value!r}; expected 'max' or 'blender_safe'"
        )
    return normalized


@dataclass(frozen=True)
class VertexLayout:
    """One deployed V4 parser branch, expressed as byte offsets."""

    parser_fvf: int
    read_stride: int
    position_kind: str
    uv_kind: str | None = None
    uv_offset: int | None = None
    uv2_kind: str | None = None
    uv2_offset: int | None = None
    normal_offset: int | None = None
    tangent_offset: int | None = None
    tangent_header_stride: int | None = None
    skin_kind: str | None = None
    create_scene_skin: bool = True


def _layouts(*fvfs: int, **kwargs: Any) -> dict[int, VertexLayout]:
    return {fvf: VertexLayout(parser_fvf=fvf, **kwargs) for fvf in fvfs}


# ============================================================================
# BEGIN V4 FVF PARSER CONTRACT
#
# This table starts from the deployed V4 import switch, with skin fields locked
# to the current writer's binary layouts. ``read_stride`` is the actual writer
# record size; header-stride normal lookup remains independent below.
# ============================================================================
FVF_LAYOUTS: dict[int, VertexLayout] = {}
FVF_LAYOUTS.update(_layouts(0xB0983013, 0xB0983014, read_stride=12, position_kind="short", uv_kind="half", uv_offset=8, skin_kind="single_6"))
FVF_LAYOUTS.update(_layouts(0xDB7DA014, 0xB6681034, read_stride=16, position_kind="short", uv_kind="half", uv_offset=12, normal_offset=8))
FVF_LAYOUTS.update(_layouts(0x0CB68015, 0x0CB68016, read_stride=20, position_kind="short", uv_kind="half", uv_offset=16, normal_offset=8, tangent_offset=12, tangent_header_stride=20, skin_kind="single_6", create_scene_skin=False))
FVF_LAYOUTS.update(_layouts(0xA8FAB018, 0xA8FAB019, read_stride=20, position_kind="short", uv_kind="half", uv_offset=16, normal_offset=8, tangent_offset=12, tangent_header_stride=20, skin_kind="single_6"))
FVF_LAYOUTS.update(_layouts(0xA7D7D036, read_stride=20, position_kind="float", uv_kind="u16", uv_offset=16, normal_offset=12))
FVF_LAYOUTS.update(_layouts(0xC31F201C, 0xC31F201D, read_stride=24, position_kind="short", uv_kind="half", uv_offset=16, normal_offset=8, tangent_offset=12, tangent_header_stride=24, skin_kind="two_c31"))
FVF_LAYOUTS.update(_layouts(0xCBF6C01A, read_stride=24, position_kind="short", uv_kind="half", uv_offset=16, normal_offset=8, tangent_offset=12, tangent_header_stride=24, skin_kind="single_u16_6"))
FVF_LAYOUTS.update(_layouts(0x207D6037, read_stride=24, position_kind="float", uv_kind="half", uv_offset=16, normal_offset=12))
FVF_LAYOUTS.update(_layouts(0xD1A47038, read_stride=24, position_kind="float", uv_kind="half", uv_offset=16, uv2_kind="half", uv2_offset=20, normal_offset=12))
FVF_LAYOUTS.update(_layouts(0xD8297028, read_stride=24, position_kind="float", uv_kind="half", uv_offset=20, normal_offset=12))
FVF_LAYOUTS.update(_layouts(0xC66FA03A, read_stride=24, position_kind="float", uv_kind="half", uv_offset=16, uv2_kind="half", uv2_offset=20, normal_offset=12))
FVF_LAYOUTS.update(_layouts(0x667B1019, read_stride=24, position_kind="short", uv_kind="half", uv_offset=16, uv2_kind="half", uv2_offset=20, normal_offset=8, skin_kind="single_u16_6", create_scene_skin=False))
FVF_LAYOUTS.update(_layouts(0x14D40020, 0x14D40021, read_stride=28, position_kind="short", uv_kind="half", uv_offset=20, normal_offset=8, tangent_offset=12, tangent_header_stride=28, skin_kind="legacy4"))
FVF_LAYOUTS.update(_layouts(0x5E7F202C, read_stride=28, position_kind="float", uv_kind="half", uv_offset=20, uv2_kind="half", uv2_offset=24, normal_offset=12))
FVF_LAYOUTS.update(_layouts(0x2BE814D4, 0x7CD414D4, read_stride=28, position_kind="short", uv_kind="zero"))
FVF_LAYOUTS.update(_layouts(0xA320C016, 0xA320C017, read_stride=28, position_kind="short", uv_kind="half", uv_offset=20, uv2_kind="half", uv2_offset=24, normal_offset=8))
FVF_LAYOUTS.update(_layouts(0x49B4F029, read_stride=28, position_kind="float", uv_kind="half", uv_offset=20, uv2_kind="half", uv2_offset=24, normal_offset=12, tangent_offset=16, tangent_header_stride=28))
FVF_LAYOUTS.update(_layouts(0x0D9E801D, read_stride=28, position_kind="short", uv_kind="zero", normal_offset=8, skin_kind="two_u16_20", create_scene_skin=False))
FVF_LAYOUTS.update(_layouts(0xA013501E, read_stride=28, position_kind="short", uv_kind="half", uv_offset=16, normal_offset=8, tangent_offset=12, tangent_header_stride=28, skin_kind="two_20"))
FVF_LAYOUTS.update(_layouts(0xD877801B, read_stride=32, position_kind="short", uv_kind="half", uv_offset=16, uv2_kind="half", uv2_offset=24, normal_offset=8, tangent_offset=12, tangent_header_stride=32, skin_kind="single_6"))
FVF_LAYOUTS.update(_layouts(0x747D1031, 0x9399C033, 0x12553032, read_stride=32, position_kind="float", uv_kind="half", uv_offset=20, uv2_kind="half", uv2_offset=24, normal_offset=12))
FVF_LAYOUTS.update(_layouts(0xB86DE02A, 0x926FD02E, read_stride=32, position_kind="float", uv_kind="half", uv_offset=20, uv2_kind="half", uv2_offset=24, normal_offset=12, tangent_offset=16, tangent_header_stride=32))
FVF_LAYOUTS.update(_layouts(0xA14E003C, read_stride=32, position_kind="short", uv_kind="zero"))
FVF_LAYOUTS.update(_layouts(0xDA55A021, read_stride=28, position_kind="short", uv_kind="half", uv_offset=20, normal_offset=8, skin_kind="legacy4"))
FVF_LAYOUTS.update(_layouts(0x77D87022, read_stride=32, position_kind="short", uv_kind="half", uv_offset=20, normal_offset=8, skin_kind="legacy4"))
FVF_LAYOUTS.update(_layouts(0xB392101F, read_stride=36, position_kind="short", uv_kind="half", uv_offset=16, uv2_kind="half", uv2_offset=20, normal_offset=8))
FVF_LAYOUTS.update(_layouts(0xBB424024, 0xBB424025, read_stride=36, position_kind="short", uv_kind="half", uv_offset=24, normal_offset=8, tangent_offset=32, tangent_header_stride=36, skin_kind="top4_bb"))
FVF_LAYOUTS.update(_layouts(0x63B6C02F, read_stride=36, position_kind="float", uv_kind="half", uv_offset=20, uv2_kind="half", uv2_offset=12, normal_offset=16))
FVF_LAYOUTS.update(_layouts(0x64593023, read_stride=40, position_kind="short", uv_kind="half", uv_offset=20, uv2_kind="half", uv2_offset=32, normal_offset=8, tangent_offset=12, tangent_header_stride=40, skin_kind="legacy4"))
FVF_LAYOUTS.update(_layouts(0x75C3E025, read_stride=40, position_kind="short", uv_kind="half", uv_offset=24, uv2_kind="half", uv2_offset=36, normal_offset=8, tangent_offset=32, tangent_header_stride=40, skin_kind="legacy8_half", create_scene_skin=False))
FVF_LAYOUTS.update(_layouts(0xD84E3026, read_stride=40, position_kind="short", uv_kind="half", uv_offset=24, normal_offset=8, tangent_offset=32, tangent_header_stride=40, skin_kind="legacy8_half", create_scene_skin=False))
FVF_LAYOUTS.update(_layouts(0xCBCF7027, read_stride=48, position_kind="short", uv_kind="half", uv_offset=24, uv2_kind="half", uv2_offset=40, normal_offset=8, tangent_offset=32, tangent_header_stride=48, skin_kind="top4_cbc"))
FVF_LAYOUTS.update(_layouts(0x2F55C03D, read_stride=64, position_kind="short", uv_kind="half", uv_offset=20, normal_offset=8, tangent_offset=12, tangent_header_stride=64, skin_kind="legacy4"))
# END V4 FVF PARSER CONTRACT
# ============================================================================


@dataclass
class ModHeader:
    magic: str
    mod_ver: int
    bone_count: int
    mesh_count: int
    mat_count: int
    vert_count: int
    triangle_count: int
    vertex_ids: int
    vertex_buffer_size: int
    padding: int
    bone_map_count: int
    ptr_bone: int
    ptr_bone_map: int
    ptr_mat_id: int
    ptr_mesh: int
    ptr_vertex: int
    ptr_triangle: int
    end_size: int


@dataclass
class MeshHeader:
    meshtype: int
    vert_count: int
    unk01: int
    mat_id: int
    lod_level: int
    unk04: int
    vert_flag: int
    vert_stride: int
    unk05: int
    vert_start: int
    vert_base: int
    fvf_info: int
    face_start: int
    face_count: int
    face_base: int
    bonemapindex: int
    weightmaps: int
    unk07: int
    unk08: int
    min_index: int
    max_index: int
    unk09: int


@dataclass
class BoneRecord:
    slot: int
    anim_map_id: int
    parent_byte: int
    child: int
    local_lookup: int
    trans: list[float]
    uk2: float
    uk3: float
    source_local_matrix: list[float]
    max_world_matrix: list[float]
    max_local_matrix: list[float]
    parent_slot: int | None
    name: str


@dataclass
class ParsedMesh:
    physical_slot: int
    display_slot: int
    header: MeshHeader
    source_fvf: int
    parser_fvf: int
    layout: VertexLayout
    positions: list[list[float]]
    uv1: list[list[float] | None]
    uv2: list[list[float] | None]
    max_normals: list[list[float] | None]
    game_normals: list[list[float] | None]
    raw_tangents: list[list[int] | None]
    raw_skin_bones: list[list[int] | None]
    raw_skin_weights: list[list[float] | None]
    fbx_skin_bones: list[list[int] | None]
    fbx_skin_weights: list[list[float] | None]
    skin_bone_limit: int | None
    faces: list[list[int]]
    invalid_face_count: int
    parent_name: str
    node_name: str


def _read_int(data: bytes, offset: int, fmt: str, label: str) -> tuple[Any, ...]:
    unpacker = _READ_STRUCT_CACHE.get(fmt)
    if unpacker is None:
        unpacker = struct.Struct(fmt)
        _READ_STRUCT_CACHE[fmt] = unpacker
    if offset < 0 or offset + unpacker.size > len(data):
        raise ValueError(f"MOD truncated while reading {label} at 0x{offset:X}")
    return unpacker.unpack_from(data, offset)


def _read_slice(data: bytes, offset: int, size: int, label: str) -> bytes:
    if offset < 0 or size < 0 or offset + size > len(data):
        raise ValueError(f"MOD truncated while reading {label} at 0x{offset:X}")
    return data[offset : offset + size]


def _finite_number(value: float, default: float = 0.0) -> float:
    result = float(value)
    return result if math.isfinite(result) else default


def _require_finite_number(value: float, label: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"MOD contains a non-finite {label}: {value!r}")
    return result


def _require_finite_values(values: Sequence[float], label: str) -> list[float]:
    return [_require_finite_number(value, f"{label}[{index}]") for index, value in enumerate(values)]


def _safe_unit(vec: Sequence[float], fallback: Sequence[float] = (0.0, 0.0, 1.0)) -> list[float]:
    xyz = [_finite_number(value) for value in vec[:3]]
    length = math.sqrt(sum(value * value for value in xyz))
    if length <= EPSILON:
        return [float(value) for value in fallback[:3]]
    return [value / length for value in xyz]


def _short_position_to_max(record: bytes) -> list[float]:
    x, y, z = _INT16X3_STRUCT.unpack_from(record, 0)
    return [x / 32767.0, -z / 32767.0, y / 32767.0]


def _float_position_to_max(record: bytes) -> list[float]:
    x, y, z = _FLOAT32X3_STRUCT.unpack_from(record, 0)
    x_value, y_value, z_value = _require_finite_values((x, y, z), "float position")
    return [x_value, -z_value, y_value]


def _decode_v4_float16_raw(raw: int) -> float:
    negative = bool(raw & 0x8000)
    exponent = (raw >> 10) & 0x1F
    fraction = raw & 0x03FF
    if exponent == 0:
        value = fraction * (2.0 ** -24)
    elif exponent == 31:
        # V4 intentionally turns every half Inf/NaN payload into finite max.
        value = 65504.0
    else:
        value = (1.0 + fraction / 1024.0) * (2.0 ** (exponent - 15))
    return -value if negative else value


_V4_FLOAT16_TABLE = tuple(_decode_v4_float16_raw(raw) for raw in range(1 << 16))


def _read_v4_float16(record: bytes, offset: int) -> float:
    """Decode half exactly as V4 ReadFloat16, including exponent-31 saturation."""
    raw = int(_UINT16_STRUCT.unpack_from(record, offset)[0])
    return _V4_FLOAT16_TABLE[raw]


def _read_half_uv(record: bytes, offset: int) -> list[float]:
    raw_u, raw_v = _UINT16X2_STRUCT.unpack_from(record, offset)
    u = _V4_FLOAT16_TABLE[raw_u]
    v = _V4_FLOAT16_TABLE[raw_v]
    return [u, 1.0 - v, 0.0]


def _read_u16_uv(record: bytes, offset: int) -> list[float]:
    u, v = _UINT16X2_STRUCT.unpack_from(record, offset)
    return [u / 65535.0, v / 65535.0, 0.0]


def _decode_packed_normal(record: bytes, offset: int) -> tuple[list[float], list[float]]:
    r, g, b, _a = _BYTE4_STRUCT.unpack_from(record, offset)
    decoded = [max(-1.0, min(1.0, (value - 127.0) / 127.0)) for value in (r, g, b)]
    game = _safe_unit(decoded)
    return [game[0], -game[2], game[1]], game


def _decode_header_stride_normal(
    data: bytes,
    vertex_start: int,
    vertex_index: int,
    header_stride: int,
    normal_offset: int | None,
    label: str,
) -> tuple[list[float] | None, list[float] | None]:
    if normal_offset is None or header_stride < normal_offset + 4:
        return None, None
    record_offset = vertex_start + vertex_index * header_stride
    if record_offset < 0 or record_offset + header_stride > len(data):
        raise ValueError(f"MOD truncated while reading {label} at 0x{record_offset:X}")
    return _decode_packed_normal(data, record_offset + normal_offset)


def _sanitize_face_one_based(raw: Sequence[int], vert_start: int, vert_count: int) -> tuple[list[int], bool]:
    base = vert_start - 1
    first = int(raw[0]) - base
    second = int(raw[1]) - base
    third = int(raw[2]) - base
    first = 1 if first < 0 else first
    second = 1 if second < 0 else second
    third = 1 if third < 0 else third
    invalid = (
        first < 1
        or first > vert_count
        or second < 1
        or second > vert_count
        or third < 1
        or third > vert_count
    )
    return ([1, 1, 1] if invalid else [first, second, third]), invalid


SKIN_KIND_MAX_INFLUENCES = {
    "single_6": 1,
    "single_u16_6": 1,
    "single_7": 1,
    "two_20": 2,
    "two_c31": 2,
    "two_u16_20": 2,
    "legacy4": 4,
    "legacy8_half": 8,
    "top4_bb": 4,
    "top4_cbc": 4,
}
VALID_SCENE_SKIN_BONE_LIMITS = frozenset({1, 2, 4, 8})


def _skin_bone_limit_for_layout(layout: VertexLayout) -> int:
    skin_kind = str(layout.skin_kind or "")
    try:
        limit = int(SKIN_KIND_MAX_INFLUENCES[skin_kind])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Scene Skin layout has no declared influence capacity: {skin_kind or '<none>'}") from exc
    if not layout.create_scene_skin or limit not in VALID_SCENE_SKIN_BONE_LIMITS:
        raise ValueError(f"Scene Skin layout has an invalid influence capacity: {skin_kind}={limit}")
    return limit


def _normalise_skin_pairs(
    bones: Sequence[int],
    weights: Sequence[float],
    *,
    max_influences: int | None = None,
    merge_duplicate_weights: bool = True,
) -> tuple[list[int], list[float]]:
    """Resolve duplicate global bones, apply a stable capacity, and normalize.

    Palette aliases can make separate source lanes resolve to the same global
    Bone ID. Genuine source aliases are aggregated by default. Legacy4 parser
    rows are different: V4 repeats the first Bone and its weight only to satisfy
    ReplaceVertexWeights, so those filler lanes must be ignored instead of
    added a second time. Positive lanes below 1e-4 remain real influences.
    Source order is retained unless a future row exceeds its FVF capacity;
    TopN ties are then resolved by the first source lane.
    """
    if max_influences is not None and max_influences <= 0:
        raise ValueError(f"Skin influence capacity must be positive, got {max_influences}")
    if not merge_duplicate_weights:
        ordered: list[int] = []
        seen: set[int] = set()
        positive_weights: list[float] = []
        for bone, weight in zip(bones, weights):
            bone_id = int(bone)
            weight_value = _finite_number(weight)
            if bone_id <= 0 or weight_value <= 0.0 or bone_id in seen:
                continue
            seen.add(bone_id)
            ordered.append(bone_id)
            positive_weights.append(weight_value)
        if max_influences is not None and len(ordered) > max_influences:
            selected = sorted(
                range(len(ordered)),
                key=lambda index: (-positive_weights[index], index),
            )[:max_influences]
            ordered = [ordered[index] for index in selected]
            positive_weights = [positive_weights[index] for index in selected]
        total = sum(positive_weights)
        if total <= EPSILON:
            return [], []
        normalized = [weight / total for weight in positive_weights]
        return ordered, [
            _FLOAT32_STRUCT.unpack(_FLOAT32_STRUCT.pack(weight))[0]
            for weight in normalized
        ]

    ordered = []
    first_slots: dict[int, int] = {}
    totals: dict[int, float] = {}
    for source_slot, (bone, weight) in enumerate(zip(bones, weights)):
        bone_id = int(bone)
        weight_value = _finite_number(weight)
        if bone_id <= 0 or weight_value <= 0.0:
            continue
        if bone_id in totals:
            if merge_duplicate_weights:
                totals[bone_id] += weight_value
            continue
        ordered.append(bone_id)
        first_slots[bone_id] = source_slot
        totals[bone_id] = weight_value
    if max_influences is not None:
        if len(ordered) > max_influences:
            ordered = sorted(
                ordered,
                key=lambda bone_id: (-totals[bone_id], first_slots[bone_id]),
            )[:max_influences]
    total = sum(totals[bone] for bone in ordered)
    if total <= EPSILON:
        return [], []
    normalized = [totals[bone] / total for bone in ordered]
    return ordered, [
        _FLOAT32_STRUCT.unpack(_FLOAT32_STRUCT.pack(weight))[0]
        for weight in normalized
    ]


def _v4_legacy_four_slots(bones: list[int], weights: list[float]) -> tuple[list[int], list[float]]:
    """Mirror the deployed parser's unique-list then duplicate-filler behavior."""
    b1, b2, b3, b4 = bones[:4]
    w1, w2, w3, w4 = weights[:4]
    unique_bones = [b1]
    unique_weights = [w1]
    for bone, weight in ((b2, w2), (b3, w3), (b4, w4)):
        if bone not in unique_bones:
            unique_bones.append(bone)
            unique_weights.append(weight)
    if len(unique_bones) == 1:
        return [b1, b1, b1, b1], [w1, w1, w1, w1]
    if len(unique_bones) == 2:
        return [b1, unique_bones[1], b1, b1], [w1, unique_weights[1], w1, w1]
    if len(unique_bones) == 3:
        return [b1, unique_bones[1], unique_bones[2], b1], [w1, unique_weights[1], unique_weights[2], w1]
    return [b1, unique_bones[1], unique_bones[2], b4], [w1, unique_weights[1], unique_weights[2], unique_weights[3]]


def _v4_top_four_slots(
    bones: list[int],
    weights: list[float],
    *,
    zero_fallback_bone: int | None = None,
) -> tuple[list[int], list[float]]:
    """Aggregate duplicate bones, then select Top4 with source-slot tie order."""
    ordered_bones: list[int] = []
    first_slots: dict[int, int] = {}
    totals: dict[int, float] = {}
    for source_slot, (bone, weight) in enumerate(zip(bones, weights)):
        bone_id = int(bone)
        weight_value = _finite_number(weight)
        if bone_id <= 0 or weight_value <= 0.0:
            continue
        if bone_id not in totals:
            ordered_bones.append(bone_id)
            first_slots[bone_id] = source_slot
            totals[bone_id] = 0.0
        totals[bone_id] += weight_value

    if not ordered_bones:
        if zero_fallback_bone is None or int(zero_fallback_bone) <= 0:
            raise ValueError("RE6 Top4 skin row has no positive weight; refusing to invent a bone influence")
        fallback = int(zero_fallback_bone)
        return [fallback, fallback, fallback, fallback], [1.0, 0.0, 0.0, 0.0]

    selected_bones = sorted(
        ordered_bones,
        key=lambda bone_id: (-totals[bone_id], first_slots[bone_id]),
    )[:4]
    selected_weights = [totals[bone_id] for bone_id in selected_bones]
    total = sum(selected_weights)
    selected_weights = [value / total for value in selected_weights]
    while len(selected_bones) < 4:
        selected_bones.append(selected_bones[0])
        selected_weights.append(0.0)
    return selected_bones, selected_weights


def _decode_top4_source_slots(record: bytes, skin_kind: str) -> tuple[list[int], list[float]]:
    w1 = _INT16_STRUCT.unpack_from(record, 6)[0] / 32767.0
    w2, w3, w4, w5 = [
        value / 255.0
        for value in _BYTE4_STRUCT.unpack_from(record, 12)
    ]
    bones = [value + 1 for value in _BYTE8_STRUCT.unpack_from(record, 16)]
    w6, w7 = [value / 32767.0 for value in _UINT16X2_STRUCT.unpack_from(record, 28)]
    if skin_kind == "top4_bb":
        return bones, [w1, w2, w3, w4, w5, w6, w7, 0.0]
    if skin_kind == "top4_cbc":
        w8 = 1.0 - (w1 + w2 + w3 + w4 + w5 + w6 + w7)
        return bones, [w1, w2, w3, w4, w5, w6, w7, w8]
    raise ValueError(f"Unsupported RE6 Top4 skin layout: {skin_kind}")


def _select_top4_source_slots(
    bones: list[int],
    weights: list[float],
    skin_kind: str,
) -> tuple[list[int], list[float]]:
    if skin_kind == "top4_bb" and all(value == 0.0 for value in weights):
        return _v4_top_four_slots(bones, weights, zero_fallback_bone=bones[7])
    return _v4_top_four_slots(bones, weights)


def _decode_skin_row(record: bytes, skin_kind: str | None) -> tuple[list[int] | None, list[float] | None]:
    if skin_kind is None:
        return None, None
    if skin_kind in {"top4_bb", "top4_cbc"}:
        bones, weights = _decode_top4_source_slots(record, skin_kind)
        return _select_top4_source_slots(bones, weights, skin_kind)
    if skin_kind == "single_6":
        return [record[6] + 1], [1.0]
    if skin_kind == "single_u16_6":
        return [_UINT16_STRUCT.unpack_from(record, 6)[0] + 1], [1.0]
    if skin_kind == "single_7":
        return [record[7] + 1], [1.0]
    if skin_kind in {"two_20", "two_c31"}:
        w1 = _INT16_STRUCT.unpack_from(record, 6)[0] / 32767.0
        raw_b1, raw_b2 = _UINT16X2_STRUCT.unpack_from(record, 20)
        b1 = int(_V4_FLOAT16_TABLE[raw_b1]) + 1
        b2 = int(_V4_FLOAT16_TABLE[raw_b2]) + 1
        return [b1, b2], [w1, 1.0 - w1]
    if skin_kind == "two_u16_20":
        w1 = _INT16_STRUCT.unpack_from(record, 6)[0] / 32767.0
        b1, b2 = [value + 1 for value in _UINT16X2_STRUCT.unpack_from(record, 20)]
        return [b1, b2], [w1, 1.0 - w1]
    if skin_kind == "legacy4":
        w1 = _INT16_STRUCT.unpack_from(record, 6)[0] / 32767.0
        bones = [value + 1 for value in _BYTE4_STRUCT.unpack_from(record, 16)]
        raw_w2, raw_w3 = _UINT16X2_STRUCT.unpack_from(record, 24)
        w2 = _V4_FLOAT16_TABLE[raw_w2]
        w3 = _V4_FLOAT16_TABLE[raw_w3]
        weights = [w1, w2, w3, 1.0 - (w1 + w2 + w3)]
        total = sum(weights)
        if abs(total) > EPSILON:
            weights = [value / total for value in weights]
        return _v4_legacy_four_slots(bones, weights)
    if skin_kind == "legacy8_half":
        w1 = _INT16_STRUCT.unpack_from(record, 6)[0] / 32767.0
        w2, w3, w4, w5 = [value / 255.0 for value in _BYTE4_STRUCT.unpack_from(record, 12)]
        bones = [value + 1 for value in _BYTE8_STRUCT.unpack_from(record, 16)]
        raw_w6, raw_w7 = _UINT16X2_STRUCT.unpack_from(record, 28)
        w6 = _V4_FLOAT16_TABLE[raw_w6]
        w7 = _V4_FLOAT16_TABLE[raw_w7]
        w8 = 1.0 - (w1 + w2 + w3 + w4 + w5 + w6 + w7)
        return bones, [w1, w2, w3, w4, w5, w6, w7, w8]
    raise ValueError(f"Unsupported RE6 skin layout: {skin_kind}")


def _parse_header(data: bytes) -> ModHeader:
    if len(data) < MOD_HEADER_PREFIX_SIZE or data[:4] != b"MOD\x00":
        raise ValueError("Not a MOD file")
    mod_ver = _read_int(data, 4, "<H", "MOD version")[0]
    values = _read_int(data, 6, MOD_HEADER_STRUCT.format, "MOD header")
    return ModHeader("MOD", mod_ver, *[int(value) for value in values])


def _parse_mesh_headers(data: bytes, header: ModHeader) -> list[MeshHeader]:
    result: list[MeshHeader] = []
    for mesh_index in range(header.mesh_count):
        offset = header.ptr_mesh + mesh_index * MESH_HEADER_STRUCT.size
        values = _read_int(data, offset, MESH_HEADER_STRUCT.format, f"MeshHeader {mesh_index + 1}")
        result.append(MeshHeader(*[int(value) for value in values]))
    return result


def _mod_topology_error(
    error_code: str,
    message: str,
    *,
    diagnostic: dict[str, Any],
) -> ValueError:
    """Build a machine-readable, source-offset-specific MOD topology error."""
    payload = {
        "schema": "re6-mod-topology-diagnostic-v1",
        "status": "ERROR",
        "error_code": str(error_code),
        **diagnostic,
    }
    error = ValueError(f"{error_code}: {message}")
    error.diagnostic = payload  # type: ignore[attr-defined]
    return error


def _validate_mod_triangle_topology(
    data: bytes,
    header: ModHeader,
    mesh_headers: Sequence[MeshHeader],
) -> dict[str, Any]:
    """Validate the deployed MOD indexed-triangle byte contract.

    MeshHeader.face_count stores index/corner count, not triangle count.  The
    importer only supports the established 3-index triangle stream for now;
    this gate prevents malformed input from being silently truncated by an
    integer division before any vertex or face bytes are consumed.
    """
    file_size = len(data)
    triangle_start = int(header.ptr_triangle)
    mesh_header_start = int(header.ptr_mesh)
    declared_index_count = int(header.triangle_count)
    if triangle_start < 0 or triangle_start > file_size:
        raise _mod_topology_error(
            "triangle_pointer_out_of_range",
            (
                f"triangle buffer starts at 0x{triangle_start:X}, outside"
                f" the {file_size}-byte MOD"
            ),
            diagnostic={
                "mesh_slot": 1 if mesh_headers else None,
                "mesh_header_offset": mesh_header_start if mesh_headers else None,
                "triangle_start": triangle_start,
                "triangle_offset": triangle_start,
                "triangle_end": triangle_start,
                "triangle_bytes": 0,
                "remaining_bytes": max(0, file_size - triangle_start),
                "file_size": file_size,
                "declared_triangle_count": declared_index_count,
                "mesh_face_count_total": 0,
            },
        )

    cumulative_indices = 0
    face_counts: list[int] = []
    for mesh_slot, mesh_header in enumerate(mesh_headers, start=1):
        face_count = int(mesh_header.face_count)
        mesh_header_offset = mesh_header_start + (mesh_slot - 1) * MESH_HEADER_STRUCT.size
        mesh_triangle_start = triangle_start + cumulative_indices * 2
        if face_count % 3 != 0:
            raise _mod_topology_error(
                "face_count_not_multiple_of_3",
                (
                    f"Mesh {mesh_slot} at MeshHeader 0x{mesh_header_offset:X}"
                    f" declares face_count={face_count}; indexed triangle "
                    f"streams require a multiple of 3"
                ),
                diagnostic={
                    "mesh_slot": mesh_slot,
                    "face_count": face_count,
                    "mesh_header_offset": mesh_header_offset,
                    "triangle_start": mesh_triangle_start,
                    "triangle_offset": mesh_triangle_start,
                    "triangle_end": mesh_triangle_start + max(0, face_count) * 2,
                    "triangle_bytes": max(0, face_count) * 2,
                    "remaining_bytes": max(0, file_size - mesh_triangle_start),
                    "file_size": file_size,
                    "declared_triangle_count": declared_index_count,
                    "mesh_face_count_total": sum(face_counts) + face_count,
                },
            )
        face_counts.append(face_count)
        cumulative_indices += face_count

    total_indices = sum(face_counts)
    if total_indices != declared_index_count:
        first_header_offset = mesh_header_start if mesh_headers else None
        raise _mod_topology_error(
            "triangle_count_mismatch",
            (
                f"MOD header triangle_count={declared_index_count} does not"
                f" match Mesh face_count total={total_indices}"
            ),
            diagnostic={
                "mesh_slot": 1 if mesh_headers else None,
                "mesh_header_offset": first_header_offset,
                "triangle_start": triangle_start,
                "triangle_offset": triangle_start,
                "triangle_end": triangle_start + total_indices * 2,
                "triangle_bytes": total_indices * 2,
                "remaining_bytes": max(0, file_size - triangle_start),
                "file_size": file_size,
                "declared_triangle_count": declared_index_count,
                "mesh_face_count_total": total_indices,
            },
        )

    expected_triangle_bytes = total_indices * 2
    expected_triangle_end = triangle_start + expected_triangle_bytes
    if expected_triangle_end > file_size:
        cumulative_indices = 0
        for mesh_slot, mesh_header in enumerate(mesh_headers, start=1):
            mesh_header_offset = mesh_header_start + (mesh_slot - 1) * MESH_HEADER_STRUCT.size
            mesh_triangle_start = triangle_start + cumulative_indices * 2
            mesh_triangle_end = mesh_triangle_start + int(mesh_header.face_count) * 2
            if mesh_triangle_end > file_size:
                raise _mod_topology_error(
                    "triangle_data_truncated",
                    (
                        f"Mesh {mesh_slot} triangle data 0x{mesh_triangle_start:X}"
                        f"..0x{mesh_triangle_end:X} exceeds the {file_size}-byte MOD"
                    ),
                    diagnostic={
                        "mesh_slot": mesh_slot,
                        "face_count": int(mesh_header.face_count),
                        "mesh_header_offset": mesh_header_offset,
                        "triangle_start": mesh_triangle_start,
                        "triangle_offset": mesh_triangle_start,
                        "triangle_end": mesh_triangle_end,
                        "triangle_bytes": int(mesh_header.face_count) * 2,
                        "remaining_bytes": max(0, file_size - mesh_triangle_start),
                        "file_size": file_size,
                        "declared_triangle_count": declared_index_count,
                        "mesh_face_count_total": total_indices,
                    },
                )
            cumulative_indices += int(mesh_header.face_count)
        # The per-Mesh loop above should identify every truncation. Keep this
        # final branch defensive for an empty or concurrently changed header.
        raise _mod_topology_error(
            "triangle_data_truncated",
            f"triangle buffer ends at 0x{expected_triangle_end:X}, beyond file size {file_size}",
            diagnostic={
                "mesh_slot": None,
                "mesh_header_offset": None,
                "triangle_start": triangle_start,
                "triangle_offset": triangle_start,
                "triangle_end": expected_triangle_end,
                "triangle_bytes": expected_triangle_bytes,
                "remaining_bytes": max(0, file_size - triangle_start),
                "file_size": file_size,
                "declared_triangle_count": declared_index_count,
                "mesh_face_count_total": total_indices,
            },
        )

    return {
        "schema": "re6-mod-topology-diagnostic-v1",
        "status": "OK",
        "error_code": "",
        "mesh_count": len(mesh_headers),
        "declared_triangle_count": declared_index_count,
        "mesh_face_count_total": total_indices,
        "triangle_start": triangle_start,
        "triangle_end": expected_triangle_end,
        "triangle_bytes": expected_triangle_bytes,
        "remaining_bytes": max(0, file_size - expected_triangle_end),
        "file_size": file_size,
    }


def _triangle_count_from_face_count(face_count: int, *, strict: bool) -> int:
    """Convert an index/corner count to triangles after an explicit gate."""
    index_count = int(face_count)
    triangle_count, remainder = divmod(index_count, 3)
    if strict and remainder:
        raise ValueError(
            f"MOD face_count={index_count} is not divisible by 3; topology must be validated first"
        )
    return max(0, triangle_count)


def _parse_bone_map_rows(data: bytes, header: ModHeader) -> list[list[int]]:
    if header.bone_map_count <= 0:
        return []
    if header.ptr_bone_map <= 0:
        raise ValueError("MOD declares bone-map rows but ptr_bone_map is null")
    table_size = header.bone_map_count * BONE_MAP_ROW_STRUCT.size
    table = _read_slice(data, header.ptr_bone_map, table_size, "bone-map table")
    return [
        list(BONE_MAP_ROW_STRUCT.unpack_from(table, row_index * BONE_MAP_ROW_STRUCT.size))
        for row_index in range(header.bone_map_count)
    ]


def _mesh_bone_palette(mesh_header: MeshHeader, bone_map_rows: Sequence[Sequence[int]]) -> list[int] | None:
    if mesh_header.bonemapindex <= 0:
        return None
    if mesh_header.weightmaps <= 0:
        raise ValueError(
            f"Mesh bonemapindex {mesh_header.bonemapindex} requires at least one weight-map row"
        )
    row_start = mesh_header.bonemapindex - 1
    row_end = row_start + mesh_header.weightmaps
    if row_start < 0 or row_end > len(bone_map_rows):
        raise ValueError(
            f"Mesh bone palette rows {row_start + 1}..{row_end} exceed the {len(bone_map_rows)} MOD bone-map rows"
        )
    return [int(value) for row in bone_map_rows[row_start:row_end] for value in row]


def _resolve_mesh_skin_bones(
    bones: list[int] | None,
    palette: Sequence[int] | None,
    *,
    bone_count: int,
    label: str,
) -> list[int] | None:
    if bones is None or palette is None:
        return bones
    resolved: list[int] = []
    for encoded_bone in bones:
        local_slot = int(encoded_bone) - 1
        if local_slot < 0 or local_slot >= len(palette):
            raise ValueError(
                f"{label} local bone slot {local_slot} exceeds mesh palette range 0..{len(palette) - 1}"
            )
        global_bone = int(palette[local_slot])
        if global_bone < 0 or global_bone >= bone_count:
            raise ValueError(
                f"{label} palette slot {local_slot} resolves to invalid global bone {global_bone} for {bone_count} bones"
            )
        resolved.append(global_bone + 1)
    return resolved


def _read_bounds_and_preamble(data: bytes) -> tuple[list[list[float]], list[int]]:
    offset = MOD_HEADER_PREFIX_SIZE
    bounds: list[list[float]] = []
    for bound_index in range(3):
        values = _read_int(data, offset + bound_index * 16, "<4f", "bound")
        bounds.append(_require_finite_values(values, f"bound {bound_index}"))
    preamble = [int(value) for value in _read_int(data, offset + 48, "<5I", "MOD import preamble")]
    return bounds, preamble


def _read_matrix_max(data: bytes, offset: int, label: str) -> list[float]:
    values = _require_finite_values(_read_int(data, offset, "<16f", label), label)
    # This is the deployed V4 ReadMatrix behavior: orientation rows stay in
    # file order while only the translation gets the RE6 -> Max axis remap.
    return [
        values[0], values[1], values[2], values[3],
        values[4], values[5], values[6], values[7],
        values[8], values[9], values[10], values[11],
        -values[12], values[14], -values[13], values[15],
    ]


def _identity_matrix() -> list[float]:
    return [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]


# Max's imported scene is Z-up.  The Autodesk Max FBX exporter writes a
# Y-up file while retaining the original Z-up declaration in metadata.  This
# is the row-vector conversion visible in the golden Max FBX: [x,y,z] becomes
# [x,z,-y].  Applying it to root world matrices and root helpers lets ufbx and
# Max see the same scene without rotating child local transforms twice.
MAX_TO_FBX_YUP_MATRIX = [
    1.0, 0.0, 0.0, 0.0,
    0.0, 0.0, -1.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
]


def _matrix_multiply(left: Sequence[float], right: Sequence[float]) -> list[float]:
    return [
        sum(float(left[row * 4 + item]) * float(right[item * 4 + column]) for item in range(4))
        for row in range(4)
        for column in range(4)
    ]


def _matrix_inverse(matrix: Sequence[float]) -> list[float]:
    work = [[float(matrix[row * 4 + column]) for column in range(4)] + [1.0 if row == column else 0.0 for column in range(4)] for row in range(4)]
    for pivot_column in range(4):
        pivot_row = max(range(pivot_column, 4), key=lambda row: abs(work[row][pivot_column]))
        if abs(work[pivot_row][pivot_column]) <= EPSILON:
            raise ValueError("Non-invertible MOD bone transform")
        work[pivot_column], work[pivot_row] = work[pivot_row], work[pivot_column]
        pivot = work[pivot_column][pivot_column]
        work[pivot_column] = [value / pivot for value in work[pivot_column]]
        for row in range(4):
            if row == pivot_column:
                continue
            factor = work[row][pivot_column]
            work[row] = [value - factor * pivot_value for value, pivot_value in zip(work[row], work[pivot_column])]
    return [work[row][column] for row in range(4) for column in range(4, 8)]


def _matrix_scale(matrix: Sequence[float]) -> list[float]:
    return [math.sqrt(sum(float(matrix[row * 4 + column]) ** 2 for column in range(3))) for row in range(3)]


def _matrix_without_scale(matrix: Sequence[float]) -> list[float]:
    result = list(matrix)
    for row, scale in enumerate(_matrix_scale(matrix)):
        if scale <= EPSILON:
            continue
        for column in range(3):
            result[row * 4 + column] /= scale
    return result


def _try_read_cdxm_display_map(data: bytes, map_offset: int, expected_mesh_count: int) -> list[int] | None:
    if map_offset < 0 or map_offset > len(data) - 8 or data[map_offset : map_offset + 4] != b"CDXM":
        return None
    version, count = _read_int(data, map_offset + 4, "<HH", "CDXM header")
    end = map_offset + 8 + int(count) * 2
    if version != 1 or count <= 0 or count > 0xFFFF or end > len(data):
        return None
    values = [int(value) for value in _read_int(data, map_offset + 8, f"<{count}H", "CDXM display map")]
    return values[:expected_mesh_count] if expected_mesh_count > 0 else values


def _find_cdxm_display_map(data: bytes, header: ModHeader) -> list[int]:
    if len(data) < 8:
        return []
    scan_start = int(header.end_size)
    if scan_start < 0:
        scan_start = 0
    if scan_start > len(data):
        scan_start = 0

    direct = _try_read_cdxm_display_map(data, scan_start, header.mesh_count)
    if direct is not None:
        return direct
    tail_expected_start = len(data) - (8 + max(0, header.mesh_count) * 2)
    if tail_expected_start != scan_start:
        direct = _try_read_cdxm_display_map(data, tail_expected_start, header.mesh_count)
        if direct is not None:
            return direct

    bounded_start = max(scan_start, len(data) - (65542 + 256))
    for position in range(bounded_start, len(data) - 7):
        if data[position : position + 4] != b"CDXM":
            continue
        candidate = _try_read_cdxm_display_map(data, position, header.mesh_count)
        if candidate is not None:
            return candidate
    return []


def _display_slot_from_map(display_map: Sequence[int], physical_slot: int) -> int:
    if 1 <= physical_slot <= len(display_map):
        mapped = int(display_map[physical_slot - 1])
        if mapped > 0:
            return mapped
    return physical_slot


def _parser_fvf(source_fvf: int, *, fix_lp2: bool, fix_dmc: bool) -> int:
    value = int(source_fvf)
    if fix_lp2:
        value += 7
    if fix_dmc:
        value -= 1
    return value


def _default_layout(parser_fvf: int, header_stride: int) -> VertexLayout:
    if header_stride < 8:
        raise ValueError(f"Unsupported default parser stride {header_stride} for FVF 0x{parser_fvf:08X}")
    return VertexLayout(
        parser_fvf=parser_fvf,
        read_stride=header_stride,
        position_kind="short",
        uv_kind="half",
        uv_offset=header_stride - 8,
    )


def _mesh_name(
    display_slot: int,
    source_fvf: int,
    header: MeshHeader,
    *,
    fix_processing_mode: str = FIX_PROCESSING_MODE_CODEX,
    parser_fvf: int | None = None,
    blender_compact_mesh_names: bool = False,
) -> str:
    if blender_compact_mesh_names:
        raise ValueError(
            "Blender compact Mesh names are retired; use complete RE6 Mesh Header names"
        )
    display_text = str(display_slot)
    display_text = display_text.zfill(3)
    name_fvf = int(source_fvf)
    if _normalize_fix_processing_mode(fix_processing_mode) == FIX_PROCESSING_MODE_LEGACY_128:
        name_fvf = int(parser_fvf if parser_fvf is not None else source_fvf)
    return (
        f"Mesh_{display_text}_{name_fvf:08X}_LODx{header.lod_level}_"
        f"MatID:{header.mat_id}_Group:{header.unk01}_DisplayMode:{header.unk05}_Type:{header.meshtype}"
    )


def _mesh_parent_name(header: MeshHeader, has_skin_rows: bool, bone_count: int) -> str:
    if header.lod_level == 0:
        return "LodGroup_0"
    if has_skin_rows and bone_count > 0 and header.lod_level in LOD_GROUP_IDS:
        return f"LodGroup_{header.lod_level}"
    return "OtherMesh"


def _resolve_bone_parent_slot(index: int, parent_byte: int, bone_count: int) -> int | None:
    parent = int(parent_byte)
    if parent == 255:
        return None
    if index == 0:
        raise ValueError(f"Root bone slot 1 has invalid parent byte {parent}; expected 255")
    if parent < 0 or parent >= bone_count:
        raise ValueError(
            f"Bone slot {index + 1} has out-of-range parent byte {parent} for {bone_count} bones"
        )
    if parent == index:
        raise ValueError(f"Bone slot {index + 1} cannot parent itself")
    return parent + 1


def _validate_bone_parent_cycles(records: Sequence[BoneRecord]) -> None:
    by_slot = {record.slot: record for record in records}
    for record in records:
        seen = {record.slot}
        parent_slot = record.parent_slot
        while parent_slot is not None:
            if parent_slot in seen:
                raise ValueError(f"Bone hierarchy cycle detected while resolving slot {record.slot}")
            seen.add(parent_slot)
            parent = by_slot.get(parent_slot)
            if parent is None:
                raise ValueError(f"Bone slot {record.slot} references missing parent slot {parent_slot}")
            parent_slot = parent.parent_slot


def _parse_bones(data: bytes, header: ModHeader) -> tuple[list[BoneRecord], list[int], list[float]]:
    if header.bone_count <= 0:
        return [], [], [1.0, 1.0, 1.0]
    bone_info_end = header.ptr_bone + header.bone_count * BONE_INFO_SIZE
    local_start = bone_info_end
    world_start = local_start + header.bone_count * BONE_MATRIX_SIZE
    mtp_start = world_start + header.bone_count * BONE_MATRIX_SIZE
    _read_slice(data, header.ptr_bone, header.bone_count * BONE_INFO_SIZE, "BoneInfo table")
    _read_slice(data, local_start, header.bone_count * BONE_MATRIX_SIZE, "bone local matrix table")
    _read_slice(data, world_start, header.bone_count * BONE_MATRIX_SIZE, "bone world matrix table")
    mtp_bytes = _read_slice(data, mtp_start, BONE_MTP_SIZE, "bone MTP table")
    source_local = [_read_matrix_max(data, local_start + index * BONE_MATRIX_SIZE, "bone local matrix") for index in range(header.bone_count)]
    world = [_read_matrix_max(data, world_start + index * BONE_MATRIX_SIZE, "bone world matrix") for index in range(header.bone_count)]
    root_scale = _matrix_scale(world[0])
    normalized_world = [list(world[0])] + [_matrix_without_scale(matrix) for matrix in world[1:]]
    records: list[BoneRecord] = []
    for index in range(header.bone_count):
        info_offset = header.ptr_bone + index * BONE_INFO_SIZE
        identifier, parent, child, local_lookup, tx, ty, tz, uk2, uk3 = _read_int(data, info_offset, BONE_INFO_STRUCT.format, "BoneInfo")
        parent_slot = _resolve_bone_parent_slot(index, int(parent), header.bone_count)
        trans_values = _require_finite_values((tx, ty, tz), f"BoneInfo {index + 1} translation")
        uk_values = _require_finite_values((uk2, uk3), f"BoneInfo {index + 1} auxiliary floats")
        world_matrix = normalized_world[index]
        if parent_slot is None:
            local_matrix = list(world_matrix)
        else:
            local_matrix = _matrix_multiply(world_matrix, _matrix_inverse(normalized_world[parent_slot - 1]))
        records.append(
            BoneRecord(
                slot=index + 1,
                anim_map_id=int(identifier),
                parent_byte=int(parent),
                child=int(child),
                local_lookup=int(local_lookup),
                trans=trans_values,
                uk2=uk_values[0],
                uk3=uk_values[1],
                source_local_matrix=source_local[index],
                max_world_matrix=world_matrix,
                max_local_matrix=local_matrix,
                parent_slot=parent_slot,
                name=f"b_{int(parent) + 1}_{index + 1}",
            )
        )
    _validate_bone_parent_cycles(records)
    return records, [byte + 1 for byte in mtp_bytes], root_scale


def _parse_meshes(
    data: bytes,
    header: ModHeader,
    mesh_headers: list[MeshHeader],
    *,
    root_scale: list[float],
    fix_lp2: bool,
    fix_dmc: bool,
    fix_processing_mode: str = FIX_PROCESSING_MODE_CODEX,
    include_normals: bool = True,
    blender_compact_mesh_names: bool = False,
    mesh_start_index: int = 0,
    mesh_end_index: int | None = None,
    display_map: Sequence[int] | None = None,
    validate_topology: bool = False,
) -> tuple[list[ParsedMesh], list[int]]:
    normalized_fix_mode = _normalize_fix_processing_mode(fix_processing_mode)
    start_index = int(mesh_start_index)
    end_index = len(mesh_headers) if mesh_end_index is None else int(mesh_end_index)
    if start_index < 0 or end_index < start_index or end_index > len(mesh_headers):
        raise ValueError(
            f"Invalid Mesh parse range {start_index}..{end_index} for "
            f"{len(mesh_headers)} Mesh headers"
        )
    if display_map is None:
        display_map = _find_cdxm_display_map(data, header)
    else:
        display_map = [int(value) for value in display_map]
    if validate_topology:
        _validate_mod_triangle_topology(data, header, mesh_headers)
    bone_map_rows = _parse_bone_map_rows(data, header)
    face_cursor = int(header.ptr_triangle) + sum(
        _triangle_count_from_face_count(int(item.face_count), strict=validate_topology) * 6
        for item in mesh_headers[:start_index]
    )
    parsed: list[ParsedMesh] = []
    source_view = memoryview(data)
    for mesh_index, mesh_header in enumerate(
        mesh_headers[start_index:end_index], start=start_index + 1
    ):
        parser_fvf = _parser_fvf(mesh_header.fvf_info, fix_lp2=fix_lp2, fix_dmc=fix_dmc)
        layout = FVF_LAYOUTS.get(parser_fvf) or _default_layout(parser_fvf, mesh_header.vert_stride)
        bone_palette = _mesh_bone_palette(mesh_header, bone_map_rows) if layout.skin_kind is not None else None
        vertex_start = header.ptr_vertex + mesh_header.vert_base + mesh_header.vert_start * mesh_header.vert_stride
        required_bytes = mesh_header.vert_count * layout.read_stride
        vertex_buffer = _read_slice(source_view, vertex_start, required_bytes, f"Mesh {mesh_index} vertex buffer")
        vertex_records = struct.Struct(f"<{layout.read_stride}s").iter_unpack(vertex_buffer)
        position_reader = _short_position_to_max if layout.position_kind == "short" else _float_position_to_max
        max_influences = SKIN_KIND_MAX_INFLUENCES.get(layout.skin_kind)
        positions: list[list[float]] = []
        uv1_rows: list[list[float] | None] = []
        uv2_rows: list[list[float] | None] = []
        max_normals: list[list[float] | None] = []
        game_normals: list[list[float] | None] = []
        tangents: list[list[int] | None] = []
        raw_bones: list[list[int] | None] = []
        raw_weights: list[list[float] | None] = []
        fbx_bones: list[list[int] | None] = []
        fbx_weights: list[list[float] | None] = []
        for vertex_index, (record,) in enumerate(vertex_records):
            positions.append(position_reader(record))
            if layout.uv_kind == "half" and layout.uv_offset is not None:
                uv1_rows.append(_read_half_uv(record, layout.uv_offset))
            elif layout.uv_kind == "u16" and layout.uv_offset is not None:
                uv1_rows.append(_read_u16_uv(record, layout.uv_offset))
            elif layout.uv_kind == "zero":
                uv1_rows.append([0.0, 0.0, 0.0])
            else:
                uv1_rows.append(None)
            if layout.uv2_kind == "half" and layout.uv2_offset is not None:
                uv2_rows.append(_read_half_uv(record, layout.uv2_offset))
            elif layout.uv2_kind == "u16" and layout.uv2_offset is not None:
                uv2_rows.append(_read_u16_uv(record, layout.uv2_offset))
            else:
                uv2_rows.append(None)
            max_normal, game_normal = _decode_header_stride_normal(
                data,
                vertex_start,
                vertex_index,
                mesh_header.vert_stride,
                layout.normal_offset,
                f"Mesh {mesh_index} header-stride vertex {vertex_index}",
            )
            if max_normal is not None and game_normal is not None:
                max_normals.append(max_normal)
                game_normals.append(game_normal)
            else:
                max_normals.append(None)
                game_normals.append(None)
            if layout.tangent_offset is not None and (layout.tangent_header_stride is None or mesh_header.vert_stride == layout.tangent_header_stride):
                header_record_offset = vertex_start + vertex_index * mesh_header.vert_stride
                if (
                    header_record_offset < 0
                    or header_record_offset + mesh_header.vert_stride > len(data)
                ):
                    raise ValueError(
                        "MOD truncated while reading "
                        f"Mesh {mesh_index} tangent vertex {vertex_index} "
                        f"at 0x{header_record_offset:X}"
                    )
                tangents.append(
                    list(
                        _read_int(
                            data,
                            header_record_offset + layout.tangent_offset,
                            "<4B",
                            "packed tangent",
                        )
                    )
                )
            else:
                tangents.append(None)
            if layout.skin_kind in {"top4_bb", "top4_cbc"}:
                bones, weights = _decode_top4_source_slots(record, layout.skin_kind)
                bones = _resolve_mesh_skin_bones(
                    bones,
                    bone_palette,
                    bone_count=header.bone_count,
                    label=f"Mesh {mesh_index} vertex {vertex_index}",
                )
                bones, weights = _select_top4_source_slots(bones, weights, layout.skin_kind)
            else:
                bones, weights = _decode_skin_row(record, layout.skin_kind)
                bones = _resolve_mesh_skin_bones(
                    bones,
                    bone_palette,
                    bone_count=header.bone_count,
                    label=f"Mesh {mesh_index} vertex {vertex_index}",
                )
            raw_bones.append(bones)
            raw_weights.append(weights)
            if bones is None or weights is None or not layout.create_scene_skin:
                fbx_bones.append(None)
                fbx_weights.append(None)
            else:
                semantic_bones, semantic_weights = _normalise_skin_pairs(
                    bones,
                    weights,
                    max_influences=max_influences,
                    merge_duplicate_weights=layout.skin_kind != "legacy4",
                )
                fbx_bones.append(semantic_bones)
                fbx_weights.append(semantic_weights)
        faces: list[list[int]] = []
        invalid_faces = 0
        triangle_count = _triangle_count_from_face_count(
            int(mesh_header.face_count),
            strict=validate_topology,
        )
        face_end = face_cursor + triangle_count * TRIANGLE_STRUCT.size
        if face_cursor >= 0 and face_end <= len(data):
            triangle_rows = TRIANGLE_STRUCT.iter_unpack(source_view[face_cursor:face_end])
        else:
            # Preserve the original first-invalid-face diagnostic on truncated data.
            face_start = face_cursor
            triangle_rows = (
                _read_int(data, face_start + index * TRIANGLE_STRUCT.size, "<3H", f"Mesh {mesh_index} face {index}")
                for index in range(triangle_count)
            )
        for raw in triangle_rows:
            face_cursor += 6
            one_based, invalid_face = _sanitize_face_one_based(raw, mesh_header.vert_start, mesh_header.vert_count)
            if invalid_face:
                invalid_faces += 1
            faces.append([value - 1 for value in one_based])
        display_slot = _display_slot_from_map(display_map, mesh_index)
        has_skin_rows = bool(
            layout.create_scene_skin
            and raw_bones
            and raw_bones[0] is not None
            and header.bone_count > 0
        )
        parsed.append(
            ParsedMesh(
                physical_slot=mesh_index,
                display_slot=display_slot,
                header=mesh_header,
                source_fvf=mesh_header.fvf_info,
                parser_fvf=parser_fvf,
                layout=layout,
                positions=positions,
                uv1=uv1_rows,
                uv2=uv2_rows,
                max_normals=max_normals if include_normals else max_normals,
                game_normals=game_normals,
                raw_tangents=tangents,
                raw_skin_bones=raw_bones,
                raw_skin_weights=raw_weights,
                fbx_skin_bones=fbx_bones,
                fbx_skin_weights=fbx_weights,
                skin_bone_limit=_skin_bone_limit_for_layout(layout) if has_skin_rows else None,
                faces=faces,
                invalid_face_count=invalid_faces,
                parent_name=_mesh_parent_name(mesh_header, has_skin_rows, header.bone_count),
                node_name=_mesh_name(
                    display_slot,
                    mesh_header.fvf_info,
                    mesh_header,
                    fix_processing_mode=normalized_fix_mode,
                    parser_fvf=parser_fvf,
                    blender_compact_mesh_names=blender_compact_mesh_names,
                ),
            )
        )
    return parsed, display_map


def _as_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        # Parser arrays already contain only JSON-native scalar/list values.
        # dataclasses.asdict() deep-copied every vertex array before this
        # function recursively walked it again, doubling work for no change in
        # the route contract.  Only nested dataclass fields need conversion.
        return {
            item.name: _as_jsonable(getattr(value, item.name))
            for item in dataclasses.fields(value)
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _as_jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_as_jsonable(item) for item in value]
    if isinstance(value, list):
        return value
    return value


def _mod_full_scan_text(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (int, str)):
        return str(value) if isinstance(value, int) else json.dumps(value, ensure_ascii=False)
    return json.dumps(value, ensure_ascii=False, allow_nan=True, separators=(",", ":"))


def _mod_full_scan_hex(value: int) -> str:
    return f"0x{max(0, int(value)):X}"


def _mod_full_scan_section(
    region: str,
    offset: int,
    size: int,
    file_size: int,
) -> dict[str, Any]:
    start = int(offset)
    requested = max(0, int(size))
    end = start + requested
    available = max(0, min(max(0, int(file_size)) - max(0, start), requested))
    return {
        "region": str(region),
        "offset": start,
        "offset_hex": _mod_full_scan_hex(start),
        "requested_bytes": requested,
        "end_offset": end,
        "end_offset_hex": _mod_full_scan_hex(end),
        "available_bytes": available,
    }


def _mod_full_scan_record_gap(
    scan: dict[str, Any],
    region: str,
    exc: Exception,
    *,
    offset: int | None = None,
    requested_bytes: int | None = None,
    available_bytes: int | None = None,
) -> None:
    row: dict[str, Any] = {
        "region": str(region),
        "exception_type": type(exc).__name__,
        "exception_text": str(exc),
    }
    if offset is not None:
        row["offset"] = int(offset)
        row["offset_hex"] = _mod_full_scan_hex(int(offset))
    if requested_bytes is not None:
        row["requested_bytes"] = max(0, int(requested_bytes))
    if available_bytes is not None:
        row["available_bytes"] = max(0, int(available_bytes))
    scan["unrecorded_or_partial_regions"].append(row)


def _mod_full_scan_rows(
    data: bytes,
    *,
    region: str,
    offset: int,
    row_count: int,
    row_struct: struct.Struct,
    scan: dict[str, Any],
) -> list[list[Any]]:
    start = int(offset)
    requested_rows = max(0, int(row_count))
    row_size = int(row_struct.size)
    available_bytes = max(0, len(data) - max(0, start))
    readable_rows = min(requested_rows, available_bytes // row_size)
    rows: list[list[Any]] = []
    try:
        for index in range(readable_rows):
            rows.append(
                [
                    value.item() if hasattr(value, "item") else value
                    for value in row_struct.unpack_from(data, start + index * row_size)
                ]
            )
    except Exception as exc:
        _mod_full_scan_record_gap(
            scan,
            region,
            exc,
            offset=start,
            requested_bytes=requested_rows * row_size,
            available_bytes=available_bytes,
        )
        return rows
    if readable_rows < requested_rows:
        _mod_full_scan_record_gap(
            scan,
            region,
            ValueError(
                f"recorded {readable_rows} of {requested_rows} rows from the declared range"
            ),
            offset=start,
            requested_bytes=requested_rows * row_size,
            available_bytes=available_bytes,
        )
    return rows


def _mod_full_scan_summary(scan: dict[str, Any]) -> dict[str, int]:
    meshes = [row for row in scan.get("meshes", []) if isinstance(row, dict)]
    return {
        "mesh_header_count": len(scan.get("mesh_headers", [])),
        "mesh_payload_count": len(meshes),
        "bone_count": len(scan.get("bones", [])),
        "material_id_count": len(scan.get("material_ids", [])),
        "bone_map_row_count": len(scan.get("bone_map_rows", [])),
        "vertex_row_count": sum(
            len(row.get("positions", []))
            for row in meshes
            if isinstance(row.get("positions"), list)
        ),
        "face_row_count": sum(
            len(row.get("faces", []))
            for row in meshes
            if isinstance(row.get("faces"), list)
        ),
        "topology_diagnostic_count": len(
            scan.get("topology_diagnostics", [])
        ),
        "unrecorded_or_partial_region_count": len(
            scan.get("unrecorded_or_partial_regions", [])
        ),
    }


def scan_mod_file_observables(mod_path: str | Path) -> dict[str, Any]:
    """Read every available MOD field without assigning a validity verdict.

    The scanner retains source binary bytes for a complete hexdump, and records
    a named partial-region row whenever a specific reader cannot continue.
    This lets support reports retain useful sections after a later Mesh, Bone,
    or table stops decoding.
    """
    source = _windows_lexical_full_path(mod_path)
    data = source.read_bytes()
    scan: dict[str, Any] = {
        "schema": MOD_FULL_SCAN_SCHEMA,
        "source": {
            "path": str(source),
            "name": source.name,
            "suffix": source.suffix,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        "raw_header_hex": data[:MOD_HEADER_PREFIX_SIZE].hex(" "),
        "section_ranges": [
            _mod_full_scan_section(
                "mod_header", 0, MOD_HEADER_PREFIX_SIZE, len(data)
            ),
            _mod_full_scan_section(
                "bounds_and_import_preamble",
                MOD_HEADER_PREFIX_SIZE,
                68,
                len(data),
            ),
        ],
        "header": {},
        "bounds": [],
        "import_preamble": {},
        "mesh_headers": [],
        "material_ids": [],
        "bone_map_rows": [],
        "cdxm_display_map": [],
        "bones": [],
        "mtp": [],
        "root_mesh_scale": [],
        "meshes": [],
        "topology_diagnostics": [],
        "unrecorded_or_partial_regions": [],
        "raw_binary": data,
    }
    try:
        header = _parse_header(data)
    except Exception as exc:
        _mod_full_scan_record_gap(
            scan,
            "mod_header",
            exc,
            offset=0,
            requested_bytes=MOD_HEADER_PREFIX_SIZE,
            available_bytes=len(data),
        )
        scan["summary"] = _mod_full_scan_summary(scan)
        return scan

    scan["header"] = _as_jsonable(header)
    bone_local_offset = int(header.ptr_bone) + int(header.bone_count) * BONE_INFO_SIZE
    bone_world_offset = bone_local_offset + int(header.bone_count) * BONE_MATRIX_SIZE
    bone_mtp_offset = bone_world_offset + int(header.bone_count) * BONE_MATRIX_SIZE
    scan["section_ranges"].extend(
        (
            _mod_full_scan_section(
                "bone_info_table",
                int(header.ptr_bone),
                int(header.bone_count) * BONE_INFO_SIZE,
                len(data),
            ),
            _mod_full_scan_section(
                "bone_local_matrix_table",
                bone_local_offset,
                int(header.bone_count) * BONE_MATRIX_SIZE,
                len(data),
            ),
            _mod_full_scan_section(
                "bone_world_matrix_table",
                bone_world_offset,
                int(header.bone_count) * BONE_MATRIX_SIZE,
                len(data),
            ),
            _mod_full_scan_section(
                "bone_mtp_table",
                bone_mtp_offset,
                BONE_MTP_SIZE if int(header.bone_count) > 0 else 0,
                len(data),
            ),
            _mod_full_scan_section(
                "bone_map_table",
                int(header.ptr_bone_map),
                int(header.bone_map_count) * BONE_MAP_ROW_STRUCT.size,
                len(data),
            ),
            _mod_full_scan_section(
                "material_id_table",
                int(header.ptr_mat_id),
                int(header.mat_count) * 4,
                len(data),
            ),
            _mod_full_scan_section(
                "mesh_header_table",
                int(header.ptr_mesh),
                int(header.mesh_count) * MESH_HEADER_STRUCT.size,
                len(data),
            ),
            _mod_full_scan_section(
                "vertex_buffer",
                int(header.ptr_vertex),
                int(header.vertex_buffer_size),
                len(data),
            ),
            _mod_full_scan_section(
                "triangle_buffer",
                int(header.ptr_triangle),
                int(header.triangle_count) * 2,
                len(data),
            ),
            _mod_full_scan_section(
                "declared_end_size", 0, int(header.end_size), len(data)
            ),
        )
    )

    try:
        bounds, preamble = _read_bounds_and_preamble(data)
        scan["bounds"] = bounds
        scan["import_preamble"] = {
            "lodzero": int(preamble[0]),
            "lodone": int(preamble[1]),
            "ldc": int(preamble[2]),
            "pad": int(preamble[3]),
            "entrycount": int(preamble[4]),
        }
    except Exception as exc:
        _mod_full_scan_record_gap(
            scan,
            "bounds_and_import_preamble",
            exc,
            offset=MOD_HEADER_PREFIX_SIZE,
            requested_bytes=68,
            available_bytes=max(0, len(data) - MOD_HEADER_PREFIX_SIZE),
        )

    mesh_headers: list[MeshHeader] = []
    try:
        mesh_headers = _parse_mesh_headers(data, header)
        scan["mesh_headers"] = [_as_jsonable(item) for item in mesh_headers]
    except Exception as exc:
        _mod_full_scan_record_gap(
            scan,
            "mesh_headers",
            exc,
            offset=int(header.ptr_mesh),
            requested_bytes=int(header.mesh_count) * MESH_HEADER_STRUCT.size,
            available_bytes=max(0, len(data) - int(header.ptr_mesh)),
        )

    try:
        scan["topology"] = _validate_mod_triangle_topology(
            data, header, mesh_headers
        )
    except Exception as exc:
        diagnostic = getattr(exc, "diagnostic", None)
        if isinstance(diagnostic, dict):
            scan["topology_diagnostics"].append(dict(diagnostic))
            _mod_full_scan_record_gap(
                scan,
                "triangle_topology",
                exc,
                offset=int(
                    diagnostic.get("triangle_offset", header.ptr_triangle)
                ),
                requested_bytes=int(diagnostic.get("triangle_bytes", 0) or 0),
                available_bytes=int(
                    diagnostic.get("remaining_bytes", 0) or 0
                ),
            )
        else:
            _mod_full_scan_record_gap(scan, "triangle_topology", exc)

    material_rows = _mod_full_scan_rows(
        data,
        region="material_ids",
        offset=int(header.ptr_mat_id),
        row_count=int(header.mat_count),
        row_struct=struct.Struct("<I"),
        scan=scan,
    )
    scan["material_ids"] = [
        {
            "material_slot": index,
            "value": int(values[0]),
            "value_hex": f"0x{int(values[0]):08X}",
        }
        for index, values in enumerate(material_rows, start=1)
    ]

    bone_map_rows = _mod_full_scan_rows(
        data,
        region="bone_map_rows",
        offset=int(header.ptr_bone_map),
        row_count=int(header.bone_map_count),
        row_struct=BONE_MAP_ROW_STRUCT,
        scan=scan,
    )
    scan["bone_map_rows"] = [
        {"row": index, "slots": [int(value) for value in values]}
        for index, values in enumerate(bone_map_rows, start=1)
    ]

    display_map: list[int] = []
    try:
        display_map = _find_cdxm_display_map(data, header)
        scan["cdxm_display_map"] = [int(value) for value in display_map]
    except Exception as exc:
        _mod_full_scan_record_gap(scan, "cdxm_display_map", exc)

    root_scale = [1.0, 1.0, 1.0]
    try:
        bones, mtp, root_scale = _parse_bones(data, header)
        scan["bones"] = [_as_jsonable(item) for item in bones]
        scan["mtp"] = [int(value) for value in mtp]
        scan["root_mesh_scale"] = [float(value) for value in root_scale]
    except Exception as exc:
        _mod_full_scan_record_gap(
            scan,
            "bones_and_mtp",
            exc,
            offset=int(header.ptr_bone),
            requested_bytes=(
                int(header.bone_count)
                * (BONE_INFO_SIZE + BONE_MATRIX_SIZE * 2)
                + (BONE_MTP_SIZE if int(header.bone_count) > 0 else 0)
            ),
            available_bytes=max(0, len(data) - int(header.ptr_bone)),
        )

    for mesh_index, mesh_header in enumerate(mesh_headers, start=1):
        try:
            rows, _display_map = _parse_meshes(
                data,
                header,
                mesh_headers,
                root_scale=root_scale,
                fix_lp2=False,
                fix_dmc=False,
                include_normals=True,
                mesh_start_index=mesh_index - 1,
                mesh_end_index=mesh_index,
                display_map=display_map,
            )
            if rows:
                scan["meshes"].append(_as_jsonable(rows[0]))
        except Exception as exc:
            vertex_offset = (
                int(header.ptr_vertex)
                + int(mesh_header.vert_base)
                + int(mesh_header.vert_start) * int(mesh_header.vert_stride)
            )
            _mod_full_scan_record_gap(
                scan,
                f"mesh_{mesh_index:04d}_observable_payload",
                exc,
                offset=vertex_offset,
                requested_bytes=int(mesh_header.vert_count)
                * max(0, int(mesh_header.vert_stride)),
                available_bytes=max(0, len(data) - vertex_offset),
            )

    scan["summary"] = _mod_full_scan_summary(scan)
    return scan


def _write_mod_full_scan_mapping(
    handle: Any,
    values: dict[str, Any],
    *,
    prefix: str = "",
) -> None:
    for key, value in values.items():
        name = str(key).upper()
        if isinstance(value, dict):
            handle.write(f"{prefix}{name}_BEGIN\n")
            _write_mod_full_scan_mapping(handle, value, prefix=prefix + "  ")
            handle.write(f"{prefix}{name}_END\n")
        else:
            handle.write(f"{prefix}{name}={_mod_full_scan_text(value)}\n")


def _write_mod_full_scan_rows(
    handle: Any,
    label: str,
    values: Any,
) -> None:
    rows = values if isinstance(values, list) else []
    for index, value in enumerate(rows):
        handle.write(f"{label}[{index:06d}]={_mod_full_scan_text(value)}\n")


def _write_mod_full_scan_mesh(handle: Any, mesh: dict[str, Any], index: int) -> None:
    mesh_label = f"MESH[{index:04d}]"
    handle.write(f"[{mesh_label}]\n")
    sequence_fields = {
        "positions",
        "uv1",
        "uv2",
        "max_normals",
        "game_normals",
        "raw_tangents",
        "raw_skin_bones",
        "raw_skin_weights",
        "fbx_skin_bones",
        "fbx_skin_weights",
        "faces",
    }
    scalar_values = {
        str(key): value
        for key, value in mesh.items()
        if str(key) not in sequence_fields
    }
    _write_mod_full_scan_mapping(handle, scalar_values)
    for field in (
        "positions",
        "uv1",
        "uv2",
        "max_normals",
        "game_normals",
        "raw_tangents",
        "raw_skin_bones",
        "raw_skin_weights",
        "fbx_skin_bones",
        "fbx_skin_weights",
        "faces",
    ):
        handle.write(f"[{mesh_label}.{field.upper()}]\n")
        _write_mod_full_scan_rows(handle, field.upper(), mesh.get(field, []))


def _write_mod_full_scan_hexdump(handle: Any, data: bytes) -> None:
    handle.write("[RAW_BINARY_HEXDUMP]\n")
    for offset in range(0, len(data), MOD_FULL_SCAN_HEXDUMP_BYTES_PER_ROW):
        row = data[offset : offset + MOD_FULL_SCAN_HEXDUMP_BYTES_PER_ROW]
        hex_text = row.hex(" ").upper()
        ascii_text = "".join(
            chr(value) if 32 <= value <= 126 else "." for value in row
        )
        handle.write(f"OFFSET={offset:08X} HEX={hex_text} ASCII={ascii_text}\n")


def write_mod_full_scan_report(
    scan: dict[str, Any], output_path: str | Path
) -> Path:
    """Write the complete English, AI-readable TXT report without cleanup."""
    output = _windows_lexical_full_path(output_path)
    raw_binary = scan.get("raw_binary", b"")
    if not isinstance(raw_binary, bytes):
        raise TypeError("MOD full scan report requires raw binary bytes")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("RE6_MOD_FULL_SCAN_REPORT\n")
        handle.write(f"REPORT_SCHEMA={MOD_FULL_SCAN_SCHEMA}\n")
        handle.write("DATA_POLICY=RECORD_ONLY_NO_VALIDITY_VERDICT\n")
        handle.write(
            "NOTE=All readable decoded fields and a complete binary hexdump are included. "
            "Unreadable regions are listed without a validity verdict.\n\n"
        )
        handle.write("[SOURCE]\n")
        _write_mod_full_scan_mapping(handle, dict(scan.get("source", {})))
        handle.write(f"RAW_HEADER_HEX={scan.get('raw_header_hex', '')}\n\n")
        handle.write("[SUMMARY]\n")
        _write_mod_full_scan_mapping(handle, dict(scan.get("summary", {})))
        handle.write("\n[MOD_HEADER]\n")
        _write_mod_full_scan_mapping(handle, dict(scan.get("header", {})))
        handle.write("\n[BOUNDS]\n")
        _write_mod_full_scan_rows(handle, "BOUND", scan.get("bounds", []))
        handle.write("\n[IMPORT_PREAMBLE]\n")
        _write_mod_full_scan_mapping(handle, dict(scan.get("import_preamble", {})))
        handle.write("\n[SECTION_RANGES]\n")
        for index, row in enumerate(scan.get("section_ranges", []), start=1):
            handle.write(f"SECTION[{index:03d}]\n")
            _write_mod_full_scan_mapping(handle, dict(row))
        handle.write("\n[MATERIAL_ID_TABLE]\n")
        for row in scan.get("material_ids", []):
            _write_mod_full_scan_mapping(handle, dict(row))
        handle.write("\n[BONE_MAP_TABLE]\n")
        for row in scan.get("bone_map_rows", []):
            _write_mod_full_scan_mapping(handle, dict(row))
        handle.write("\n[CDXM_DISPLAY_MAP]\n")
        _write_mod_full_scan_rows(handle, "DISPLAY_SLOT", scan.get("cdxm_display_map", []))
        handle.write("\n[MTP]\n")
        _write_mod_full_scan_rows(handle, "MTP", scan.get("mtp", []))
        handle.write("\n[ROOT_MESH_SCALE]\n")
        _write_mod_full_scan_rows(handle, "SCALE", scan.get("root_mesh_scale", []))
        handle.write("\n[BONES]\n")
        for index, row in enumerate(scan.get("bones", []), start=1):
            handle.write(f"BONE[{index:04d}]\n")
            _write_mod_full_scan_mapping(handle, dict(row))
        handle.write("\n[MESH_HEADERS]\n")
        for index, row in enumerate(scan.get("mesh_headers", []), start=1):
            handle.write(f"MESH_HEADER[{index:04d}]\n")
            _write_mod_full_scan_mapping(handle, dict(row))
        handle.write("\n[TOPOLOGY_DIAGNOSTICS]\n")
        for index, row in enumerate(
            scan.get("topology_diagnostics", []), start=1
        ):
            handle.write(f"TOPOLOGY[{index:04d}]\n")
            _write_mod_full_scan_mapping(handle, dict(row))
        handle.write("\n[MESHES]\n")
        for index, row in enumerate(scan.get("meshes", []), start=1):
            _write_mod_full_scan_mesh(handle, dict(row), index)
        handle.write("\n[UNRECORDED_OR_PARTIAL_REGIONS]\n")
        for index, row in enumerate(
            scan.get("unrecorded_or_partial_regions", []), start=1
        ):
            handle.write(f"REGION_RECORD[{index:04d}]\n")
            _write_mod_full_scan_mapping(handle, dict(row))
        handle.write("\n")
        _write_mod_full_scan_hexdump(handle, raw_binary)
    return output


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _windows_lexical_full_path(value: str | os.PathLike[str]) -> Path:
    """Match .NET Path.GetFullPath without resolving junctions or symlinks."""
    return Path(os.path.abspath(os.fspath(value)))


def _normalize_mrl_texture_source_mode(value: Any) -> str:
    mode = str(value or "").strip().casefold()
    if mode not in MRL_TEXTURE_SOURCE_MODES:
        raise ValueError(f"Unsupported MRL texture source mode: {value!r}")
    return mode


def _mrl_texture_name_identity(value: Any) -> str:
    raw = str(value or "").replace("\\", "/").strip().lstrip("~/")
    leaf = raw.rsplit("/", 1)[-1].strip().lstrip("~")
    return MRL_TEXTURE_TRAILING_SUFFIX_RE.sub("", leaf).strip().casefold()


def _mrl_texture_resource_parts(value: Any) -> tuple[str, ...]:
    return tuple(
        part.strip()
        for part in str(value or "").replace("\\", "/").split("/")
        if part.strip() and part.strip() not in {".", ".."}
    )


def _mrl_texture_source_mode(path: Path) -> str:
    suffix = path.suffix.casefold().lstrip(".")
    return suffix if suffix in MRL_TEXTURE_SOURCE_MODES else ""


def _mrl_texture_search_directories(
    mrl_path: Path,
    resource_names: Iterable[str],
    *,
    related_paths: Iterable[str | Path] = (),
    texture_roots: Iterable[str | Path] = (),
) -> tuple[Path, ...]:
    """Build the same bounded MRL-centred lookup set used by the Launcher."""
    seeds: list[Path] = [mrl_path.parent]
    for raw in (*related_paths, *texture_roots):
        try:
            candidate = _windows_lexical_full_path(raw)
        except (OSError, TypeError, ValueError):
            continue
        if candidate.is_file():
            candidate = candidate.parent
        if candidate.is_dir():
            seeds.append(candidate)

    anchors: list[Path] = []
    seen_anchors: set[str] = set()
    for seed in seeds:
        current = seed
        for _ in range(MRL_TEXTURE_SEARCH_MAX_ANCESTORS + 1):
            key = os.path.normcase(str(current)).casefold()
            if key not in seen_anchors:
                seen_anchors.add(key)
                anchors.append(current)
            parent = current.parent
            if parent == current:
                break
            current = parent

    exact: list[Path] = []
    nearby: list[Path] = []
    seen: set[str] = set()

    def add(directory: Path, destination: list[Path]) -> None:
        try:
            resolved = directory.resolve(strict=False)
        except (OSError, RuntimeError):
            return
        if not resolved.is_dir():
            return
        key = os.path.normcase(str(resolved)).casefold()
        if key not in seen:
            seen.add(key)
            destination.append(resolved)

    for seed in seeds:
        add(seed, exact)

    relative_directories: list[Path] = []
    for resource in resource_names:
        parts = _mrl_texture_resource_parts(resource)
        if len(parts) >= 2:
            relative = Path(*parts[:-1])
            if relative not in relative_directories:
                relative_directories.append(relative)

    resource_directories: list[Path] = []
    for anchor in anchors:
        for relative in relative_directories:
            add(anchor / relative, resource_directories)
        if anchor.name.casefold() in MRL_TEXTURE_SEARCH_DIRECTORY_NAMES:
            add(anchor, nearby)
        try:
            children = tuple(anchor.iterdir())
        except OSError:
            children = ()
        for child in children:
            if not child.is_dir():
                continue
            child_name = child.name.casefold()
            if child_name in MRL_TEXTURE_SEARCH_DIRECTORY_NAMES:
                add(child, nearby)
            if child_name in {"files", "data", "model", "models", "texture", "textures", "dds", "tex"}:
                try:
                    packages = tuple(child.iterdir())
                except OSError:
                    packages = ()
                for relative in relative_directories:
                    add(child / relative, resource_directories)
                    for package in packages:
                        if package.is_dir():
                            add(package / relative, resource_directories)

    for directory in reversed(resource_directories):
        if directory not in exact:
            exact.insert(0, directory)
    return tuple(exact + nearby)


def _iter_mrl_texture_files(roots: Iterable[Path]) -> Iterable[Path]:
    yielded = 0
    visited: set[str] = set()
    pending = [(root, 0) for root in roots]
    while pending and yielded < MRL_TEXTURE_SEARCH_MAX_FILES:
        directory, depth = pending.pop(0)
        try:
            resolved = directory.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        key = os.path.normcase(str(resolved)).casefold()
        if key in visited:
            continue
        visited.add(key)
        try:
            entries = tuple(resolved.iterdir())
        except OSError:
            continue
        for candidate in entries:
            if candidate.is_file():
                yield candidate
                yielded += 1
                if yielded >= MRL_TEXTURE_SEARCH_MAX_FILES:
                    return
            elif candidate.is_dir() and not candidate.is_symlink():
                pending.append((candidate, depth + 1))


def _mrl_texture_candidate_rank(mrl_path: Path, resource: str, candidate: Path) -> tuple[int, int, int, str]:
    resource_parts = [part.casefold() for part in _mrl_texture_resource_parts(resource)]
    if resource_parts:
        resource_parts[-1] = _mrl_texture_name_identity(resource_parts[-1])
    candidate_parts = [part.casefold() for part in candidate.parts]
    if candidate_parts:
        candidate_parts[-1] = _mrl_texture_name_identity(candidate_parts[-1])
    shared_suffix = 0
    for expected, actual in zip(reversed(resource_parts), reversed(candidate_parts)):
        if expected != actual:
            break
        shared_suffix += 1
    try:
        relative_depth = len(candidate.relative_to(mrl_path.parent.resolve()).parts)
    except ValueError:
        relative_depth = len(candidate.parts)
    return (-shared_suffix, relative_depth, len(candidate.name), str(candidate).casefold())


def _decode_re6_tex_for_embedded_fbx(source: Path, decode_directory: Path) -> Path:
    try:
        import codex_re6_tex_decode
    except Exception as exc:
        raise RuntimeError(f"RE6 TEX decoder is unavailable: {exc}") from exc
    try:
        kind, parsed = codex_re6_tex_decode.parse_texture_file(source.read_bytes())
    except Exception as exc:
        raise ValueError(f"Unable to parse RE6 TEX texture {source}: {exc}") from exc
    if kind != "tex":
        raise ValueError(f"Embedded FBX only accepts TEX input here, got {kind!r}: {source}")
    decode_directory.mkdir(parents=True, exist_ok=True)
    output = decode_directory / f"{_sha256_file(source)}.dds"
    if not output.is_file():
        output.write_bytes(codex_re6_tex_decode.convert_to_dds(parsed))
    if output.read_bytes()[:4] != b"DDS ":
        raise RuntimeError(f"RE6 TEX decoder did not produce a DDS payload: {source}")
    return output


def _mrl_base_color_binding_is_valid(shader: str, resource: str) -> bool:
    normalized_shader = str(shader or "").strip().casefold()
    if normalized_shader:
        return normalized_shader in MRL_BASE_COLOR_SHADERS
    identity = _mrl_texture_name_identity(resource)
    tokens = {token for token in re.split(r"[^a-z0-9]+", identity) if token}
    return bool(tokens & MRL_BASE_COLOR_RESOURCE_TOKENS) or identity.endswith(
        ("_bm", "_basemap", "_albedo", "_diffuse", "_basecolor")
    )


def _build_mrl_embedded_texture_plan(
    mod_path: str | Path,
    mrl_path: str | Path,
    *,
    texture_mode: str,
    decode_directory: Path,
    texture_roots: Iterable[str | Path] = (),
) -> dict[str, Any]:
    """Resolve only MRL BM/Base Color bindings for embedded-FBX export."""
    source_mod = _windows_lexical_full_path(mod_path)
    source_mrl = _windows_lexical_full_path(mrl_path)
    if not source_mod.is_file():
        raise FileNotFoundError(f"Source MOD does not exist: {source_mod}")
    if not source_mrl.is_file():
        raise FileNotFoundError(f"Source MRL does not exist: {source_mrl}")
    mode = _normalize_mrl_texture_source_mode(texture_mode)
    mod_data = source_mod.read_bytes()
    mod_header = _parse_header(mod_data)
    if int(mod_header.mod_ver) != 211:
        raise ValueError(f"MRL exact bind supports MOD version 211, got {mod_header.mod_ver}")
    mrl_data = source_mrl.read_bytes()
    if len(mrl_data) < 28 or mrl_data[:3] != b"MRL":
        raise ValueError("It is not a supported MRL file")
    version, material_count, texture_count, shader_version, textures_offset, materials_offset = struct.unpack_from(
        "<6I", mrl_data, 4
    )
    texture_rows: list[dict[str, Any]] = []
    for index in range(int(texture_count)):
        offset = int(textures_offset) + index * 76
        row = _read_slice(mrl_data, offset, 76, f"MRL texture {index}")
        _type_hash, _unknown_a, _unknown_b = struct.unpack_from("<III", row, 0)
        resource = row[12:76].split(b"\0", 1)[0].decode("utf-8", "replace")
        texture_rows.append({"resource": resource})
    resource_names = tuple(str(row["resource"]) for row in texture_rows)
    search_directories = _mrl_texture_search_directories(
        source_mrl,
        resource_names,
        related_paths=(source_mod,),
        texture_roots=texture_roots,
    )
    requested_identities = {_mrl_texture_name_identity(resource) for resource in resource_names}
    texture_index: dict[str, list[Path]] = {}
    for candidate in _iter_mrl_texture_files(search_directories):
        if _mrl_texture_source_mode(candidate) != mode:
            continue
        identity = _mrl_texture_name_identity(candidate.name)
        if identity and identity in requested_identities:
            texture_index.setdefault(identity, []).append(candidate.resolve())

    def resolve_texture(resource: str) -> tuple[Path | None, Path | None]:
        identity = _mrl_texture_name_identity(resource)
        for candidate in sorted(
            texture_index.get(identity, ()),
            key=lambda item: _mrl_texture_candidate_rank(source_mrl, resource, item),
        ):
            if mode == "dds":
                return candidate, candidate
            return candidate, _decode_re6_tex_for_embedded_fbx(candidate, decode_directory)
        return None, None

    material_bindings: dict[int, dict[str, Any]] = {}
    texture_resolution: dict[int, dict[str, Any]] = {}
    for material_index in range(int(material_count)):
        offset = int(materials_offset) + material_index * 60
        row = _read_slice(mrl_data, offset, 60, f"MRL material {material_index}")
        values = struct.unpack_from("<8I4f3I", row, 0)
        material_hash = int(values[1])
        resource_count = int(values[6]) & 0xFFF
        command_offset = int(values[13])
        for resource_index in range(resource_count):
            command = _read_slice(
                mrl_data,
                command_offset + resource_index * 12,
                12,
                f"MRL material command {material_index}:{resource_index}",
            )
            header_a, texture_reference, shader_object = struct.unpack_from("<III", command, 0)
            if (header_a & 0xF) != 3:
                continue
            texture_index_value = int(texture_reference) - 1
            if texture_index_value < 0 or texture_index_value >= len(texture_rows):
                continue
            resource = str(texture_rows[texture_index_value]["resource"])
            shader = MRL_SHADER_SLOTS.get(int(shader_object) // 4096, "")
            if not _mrl_base_color_binding_is_valid(shader, resource):
                continue
            source_texture, image = resolve_texture(resource)
            if source_texture is None or image is None or not image.is_file():
                continue
            image_bytes = image.read_bytes()
            if image_bytes[:4] != b"DDS ":
                raise ValueError(f"MRL texture did not resolve to DDS media: {image}")
            image_sha256 = hashlib.sha256(image_bytes).hexdigest()
            binding = {
                "shader": shader or "tbasemap",
                "resource": resource,
                "source_texture_path": str(source_texture),
                "image_path": str(image),
                "image_sha256": image_sha256,
                "image_size": len(image_bytes),
            }
            material_bindings.setdefault(material_hash, binding)
            texture_resolution[texture_index_value] = {
                "resource": resource,
                "identity": _mrl_texture_name_identity(resource),
                "source_texture_path": str(source_texture),
                "image_path": str(image),
                "image_sha256": image_sha256,
                "image_size": len(image_bytes),
                "resolved": True,
            }
            break

    material_hashes = list(
        struct.unpack_from(f"<{int(mod_header.mat_count)}I", mod_data, int(mod_header.ptr_mat_id))
    ) if mod_header.mat_count else []
    bindings_by_mesh_slot: dict[int, dict[str, Any]] = {}
    for mesh_index in range(int(mod_header.mesh_count)):
        packed = struct.unpack_from("<I", mod_data, int(mod_header.ptr_mesh) + mesh_index * 48 + 4)[0]
        source_material_index = (int(packed) >> 12) & 0xFFF
        material_hash = int(material_hashes[source_material_index]) if source_material_index < len(material_hashes) else 0
        binding = material_bindings.get(material_hash)
        if binding is not None:
            bindings_by_mesh_slot[mesh_index + 1] = {
                **binding,
                "physical_slot": mesh_index + 1,
                "source_material_index": source_material_index,
                "material_hash": material_hash,
            }

    source_paths = {row["source_texture_path"] for row in bindings_by_mesh_slot.values()}
    decoded_paths = {
        row["image_path"]
        for row in bindings_by_mesh_slot.values()
        if os.path.normcase(row["image_path"]) != os.path.normcase(row["source_texture_path"])
    }
    return {
        "schema": "codex-re6-mrl-embedded-fbx-v1",
        "mrl_path": str(source_mrl),
        "mod_path": str(source_mod),
        "texture_mode": mode,
        "version": int(version),
        "shader_version": int(shader_version),
        "source_mesh_count": int(mod_header.mesh_count),
        "source_material_count": int(mod_header.mat_count),
        "mrl_material_count": int(material_count),
        "mrl_texture_count": int(texture_count),
        "texture_search_strategy": "mrl_centered_bounded_relative_name_scan",
        "texture_search_roots": [str(path) for path in search_directories[:32]],
        "texture_resolution": list(texture_resolution.values()),
        "bindings_by_mesh_slot": bindings_by_mesh_slot,
        "resolved_texture_source_count": len(source_paths),
        "temporary_decoded_texture_count": len(decoded_paths),
    }


def _request_contract_identity(
    source_path: str,
    source_sha256: str,
    *,
    include_normals: bool,
    fix_lp2: bool,
    fix_dmc: bool,
    fix_processing_mode: str = FIX_PROCESSING_MODE_CODEX,
    import_name_plan_sha256: str = "",
    blender_compact_mesh_names: bool = False,
) -> dict[str, Any]:
    normalized_fix_mode = _normalize_fix_processing_mode(fix_processing_mode)
    identity = {
        "revision": IMPORT_MODULE_CONTRACT_REVISION,
        "source_path": str(source_path),
        "source_sha256": str(source_sha256),
        "include_normals": bool(include_normals),
        "fix_lp2": bool(fix_lp2),
        "fix_dmc": bool(fix_dmc),
        "fix_processing_mode": normalized_fix_mode,
    }
    normalized_plan_sha = str(import_name_plan_sha256 or "").strip().lower()
    if normalized_plan_sha:
        if not re.fullmatch(r"[0-9a-f]{64}", normalized_plan_sha):
            raise ValueError("Import name-plan SHA-256 is invalid")
        identity["import_name_plan_sha256"] = normalized_plan_sha
    if blender_compact_mesh_names:
        identity["blender_compact_mesh_names"] = True
    return identity


def _request_contract_digest(identity: dict[str, Any]) -> str:
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _normalize_reserved_scene_names(values: Iterable[str] | None) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        raise TypeError("reserved_scene_names must be an iterable of complete names")
    names: dict[str, str] = {}
    for value in values:
        name = str(value or "")
        if not name:
            continue
        names.setdefault(name.casefold(), name)
    return [names[key] for key in sorted(names)]


def _apply_reserved_scene_name_plan(
    scene: dict[str, Any],
    reserved_scene_names: Iterable[str] | None,
) -> dict[str, Any]:
    """Allocate final FBX node names before Max sees the import."""
    reserved_names = _normalize_reserved_scene_names(reserved_scene_names)
    reserved_folded = {name.casefold() for name in reserved_names}
    helpers = dict(scene.get("scene_helpers", {}))
    bounds_names = [str(value or "") for value in helpers.get("bounds", [])]
    lod_group_names = [str(value or "") for value in helpers.get("lod_groups", [])]
    other_mesh_name = str(helpers.get("other_mesh", "OtherMesh") or "OtherMesh")
    bones = [row for row in scene.get("bones", []) if isinstance(row, dict)]
    meshes = [row for row in scene.get("meshes", []) if isinstance(row, dict)]

    targets: list[dict[str, Any]] = []
    for index, name in enumerate(bounds_names):
        targets.append({"kind": "bound", "role": "helper", "index": index, "source_name": name})
    for index, bone in enumerate(bones):
        targets.append(
            {
                "kind": "bone",
                "role": "bone",
                "index": index,
                "slot": int(bone.get("slot", 0) or 0),
                "source_name": str(bone.get("name", "") or ""),
            }
        )
    for index, name in enumerate(lod_group_names):
        targets.append({"kind": "lod_group", "role": "helper", "index": index, "source_name": name})
    targets.append(
        {"kind": "other_mesh", "role": "helper", "index": 0, "source_name": other_mesh_name}
    )
    for index, mesh in enumerate(meshes):
        targets.append(
            {
                "kind": "mesh",
                "role": "mesh",
                "index": index,
                "physical_slot": int(mesh.get("physical_slot", 0) or 0),
                "source_name": str(mesh.get("node_name", "") or ""),
            }
        )

    conflict_targets = [
        row for row in targets if row["source_name"].casefold() in reserved_folded
    ]
    conflict_ids = {id(row) for row in conflict_targets}
    used_names = set(reserved_folded)
    used_names.update(
        row["source_name"].casefold()
        for row in targets
        if id(row) not in conflict_ids and row["source_name"]
    )
    source_counts: dict[str, int] = {}
    for row in conflict_targets:
        folded = row["source_name"].casefold()
        source_counts[folded] = source_counts.get(folded, 0) + 1

    import_number = 0
    final_by_target: dict[int, str] = {}
    if conflict_targets:
        for candidate_number in range(2, 10000):
            candidate_names: set[str] = set()
            candidate_plan: dict[int, str] = {}
            source_ordinals: dict[str, int] = {}
            for row in conflict_targets:
                source_name = row["source_name"]
                folded = source_name.casefold()
                source_ordinals[folded] = source_ordinals.get(folded, 0) + 1
                ordinal_suffix = (
                    f"_{source_ordinals[folded]}" if source_counts.get(folded, 0) > 1 else ""
                )
                final_name = f"{source_name}_Import{candidate_number}{ordinal_suffix}"
                final_folded = final_name.casefold()
                if final_folded in used_names or final_folded in candidate_names:
                    break
                candidate_names.add(final_folded)
                candidate_plan[id(row)] = final_name
            else:
                import_number = candidate_number
                final_by_target = candidate_plan
                break
        if not final_by_target:
            raise RuntimeError("Unable to allocate a deterministic unique import-name suffix")

    entries: list[dict[str, Any]] = []
    helper_renames: dict[str, str] = {}
    for row in targets:
        source_name = row["source_name"]
        final_name = final_by_target.get(id(row), source_name)
        entry = dict(row)
        entry["final_name"] = final_name
        entry["changed"] = final_name != source_name
        entries.append(entry)
        kind = row["kind"]
        index = int(row["index"])
        if kind == "bound":
            bounds_names[index] = final_name
            helper_renames[source_name.casefold()] = final_name
        elif kind == "bone":
            bones[index]["name"] = final_name
        elif kind == "lod_group":
            lod_group_names[index] = final_name
            helper_renames[source_name.casefold()] = final_name
        elif kind == "other_mesh":
            other_mesh_name = final_name
            helper_renames[source_name.casefold()] = final_name
        elif kind == "mesh":
            meshes[index]["node_name"] = final_name

    for mesh in meshes:
        parent_name = str(mesh.get("parent_name", "") or "")
        mesh["parent_name"] = helper_renames.get(parent_name.casefold(), parent_name)
    helpers["bounds"] = bounds_names
    helpers["lod_groups"] = lod_group_names
    helpers["other_mesh"] = other_mesh_name
    scene["scene_helpers"] = helpers

    max_post_import = dict(scene.get("max_post_import", {}))
    bounds_contract = dict(max_post_import.get("bounds", {}))
    if bounds_contract:
        renamed_bounds: dict[str, Any] = {}
        for entry in entries:
            if entry.get("role") != "helper" or int(entry.get("index", -1)) >= len(bounds_names):
                continue
            source_name = str(entry.get("source_name", "") or "")
            if source_name in bounds_contract:
                renamed_bounds[str(entry["final_name"])] = bounds_contract[source_name]
        for name, value in bounds_contract.items():
            if name.casefold() not in helper_renames:
                renamed_bounds.setdefault(name, value)
        max_post_import["bounds"] = renamed_bounds
        scene["max_post_import"] = max_post_import

    plan_identity = [
        {
            "role": str(row.get("role", "") or ""),
            "source_name": str(row.get("source_name", "") or ""),
            "final_name": str(row.get("final_name", "") or ""),
            "slot": int(row.get("slot", row.get("physical_slot", 0)) or 0),
        }
        for row in entries
    ]
    plan_sha256 = hashlib.sha256(
        json.dumps(
            plan_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    reserved_sha256 = hashlib.sha256(
        json.dumps(
            sorted(reserved_folded),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    plan = {
        "schema": "pc-rehd-import-name-plan-v1",
        "authority": "importer_pre_fbx_scene_name_plan",
        "case_sensitive": False,
        "reserved_scene_name_count": len(reserved_names),
        "reserved_scene_names_sha256": reserved_sha256,
        "import_number": import_number,
        "renamed_count": len(final_by_target),
        "plan_sha256": plan_sha256,
        "entries": entries,
    }
    scene["import_name_plan"] = plan
    return plan


def build_import_scene(
    mod_path: str | Path,
    *,
    include_normals: bool = True,
    fix_lp2: bool = False,
    fix_dmc: bool = False,
    fix_processing_mode: str = FIX_PROCESSING_MODE_CODEX,
    reserved_scene_names: Iterable[str] | None = None,
    blender_compact_mesh_names: bool = False,
    bug_control: Any | None = None,
) -> dict[str, Any]:
    """Parse one MOD into the common route/manifest scene model.

    This is intentionally an all-or-nothing parse: no caller receives a
    half-populated scene model.  It differs from legacy V4's direct scene
    creation, which could leave bounds/bones or partial Meshes after a failure.
    """
    if bug_control is not None:
        bug_control.advance("source_parse")
    if blender_compact_mesh_names:
        raise ValueError(
            "Blender compact Mesh names are retired; use complete RE6 Mesh Header names"
        )
    source = _windows_lexical_full_path(mod_path)
    normalized_fix_mode = _normalize_fix_processing_mode(fix_processing_mode)
    data = source.read_bytes()
    header = _parse_header(data)
    bounds, preamble = _read_bounds_and_preamble(data)
    mesh_headers = _parse_mesh_headers(data, header)
    # Gate the complete indexed-triangle region before parsing bones or Mesh
    # payloads.  A malformed face_count must never be silently truncated.
    _validate_mod_triangle_topology(data, header, mesh_headers)
    bones, mtp, mesh_scale = _parse_bones(data, header)
    meshes, display_map = _parse_meshes(
        data,
        header,
        mesh_headers,
        root_scale=mesh_scale,
        fix_lp2=fix_lp2,
        fix_dmc=fix_dmc,
        fix_processing_mode=normalized_fix_mode,
        include_normals=include_normals,
        blender_compact_mesh_names=bool(blender_compact_mesh_names),
        validate_topology=False,
    )
    source_hash = hashlib.sha256(data).hexdigest()
    scene = {
        "schema": IMPORT_MANIFEST_SCHEMA,
        "contract_id": "",
        "request_contract_sha256": "",
        "source": {
            "path": str(source),
            "sha256": source_hash,
            "size": len(data),
            "name": source.name,
        },
        "options": {
            "include_normals": bool(include_normals),
            "fix_lp2": bool(fix_lp2),
            "fix_dmc": bool(fix_dmc),
            "fix_processing_mode": normalized_fix_mode,
            "blender_compact_mesh_names": bool(blender_compact_mesh_names),
        },
        "header": _as_jsonable(header),
        "bounds": bounds,
        "import_preamble": {
            "lodzero": preamble[0],
            "lodone": preamble[1],
            "ldc": preamble[2],
            "pad": preamble[3],
            "entrycount": preamble[4],
        },
        "root_mesh_scale": mesh_scale,
        "mtp": mtp,
        "cdxm_display_map": display_map,
        "bones": [_as_jsonable(bone) for bone in bones],
        "meshes": [_as_jsonable(mesh) for mesh in meshes],
        "scene_helpers": {
            "lod_groups": [f"LodGroup_{value}" for value in LOD_GROUP_IDS],
            "other_mesh": "OtherMesh",
            "bounds": ["BoundSphere", "BoundBoxMin", "BoundBoxMax"],
        },
        "max_post_import": {
            "bones": {
                "class": "Dummy",
                "size": 1.5,
                "show_links": True,
                "show_links_only": True,
            },
            "lod_helpers": {
                "class": "Dummy",
                "size": 10.0,
            },
            "bounds": {
                "BoundSphere": {
                    "class": "Sphere",
                    "radius": bounds[0][3],
                    "position": bounds[0][:3],
                    "frozen": True,
                    "hidden": True,
                },
                "BoundBoxMin": {
                    "class": "Point",
                    "size": 10.0,
                    "position": bounds[1][:3],
                    "frozen": True,
                    "hidden": True,
                },
                "BoundBoxMax": {
                    "class": "Point",
                    "size": 10.0,
                    "position": bounds[2][:3],
                    "frozen": True,
                    "hidden": True,
                },
            },
        },
    }
    name_plan = _apply_reserved_scene_name_plan(scene, reserved_scene_names)
    # The deterministic _ImportN suffix is part of the visible scene name.
    # Blender compact Mesh names retain it so repeated imports use the same
    # exporter routing and collision handling as legacy Mesh_### names.
    request_contract = _request_contract_identity(
        str(source),
        source_hash,
        include_normals=include_normals,
        fix_lp2=fix_lp2,
        fix_dmc=fix_dmc,
        fix_processing_mode=normalized_fix_mode,
        import_name_plan_sha256=(
            str(name_plan["plan_sha256"])
            if int(name_plan.get("renamed_count", 0) or 0) > 0
            else ""
        ),
        blender_compact_mesh_names=bool(blender_compact_mesh_names),
    )
    request_contract_sha256 = _request_contract_digest(request_contract)
    scene["request_contract_sha256"] = request_contract_sha256
    scene["contract_id"] = "re6mod-" + request_contract_sha256[:24]
    return scene


def _blender_scene_data_json_bytes(value: Any) -> bytes:
    """Return the deterministic JSON representation used on the Blender wire."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _blender_scene_mesh_receipt(mesh: dict[str, Any]) -> dict[str, Any]:
    """Keep a source-topology witness beside every Blender Scene Data Mesh.

    Blender's FBX importer is allowed to discard degenerate triangles.  The
    direct Scene Data route is explicitly not: this receipt gives the BPY
    agent and the Writer a physical-slot keyed way to reject any loss before a
    MOD write happens.
    """
    faces = mesh.get("faces", [])
    positions = mesh.get("positions", [])
    topology = {
        "physical_slot": int(mesh.get("physical_slot", 0) or 0),
        "vertex_count": len(positions) if isinstance(positions, list) else 0,
        "triangle_count": len(faces) if isinstance(faces, list) else 0,
        "faces": faces if isinstance(faces, list) else [],
    }
    geometry = {
        "topology": topology,
        "positions": positions if isinstance(positions, list) else [],
        "uv1": mesh.get("uv1", []) if isinstance(mesh.get("uv1"), list) else [],
        "uv2": mesh.get("uv2", []) if isinstance(mesh.get("uv2"), list) else [],
        "max_normals": (
            mesh.get("max_normals", [])
            if isinstance(mesh.get("max_normals"), list)
            else []
        ),
        "fbx_skin_bones": (
            mesh.get("fbx_skin_bones", [])
            if isinstance(mesh.get("fbx_skin_bones"), list)
            else []
        ),
        "fbx_skin_weights": (
            mesh.get("fbx_skin_weights", [])
            if isinstance(mesh.get("fbx_skin_weights"), list)
            else []
        ),
    }
    return {
        "schema": "pc-rehd-code-x-blender-source-topology-v1",
        "vertex_count": topology["vertex_count"],
        "triangle_count": topology["triangle_count"],
        "topology_sha256": hashlib.sha256(
            _blender_scene_data_json_bytes(topology)
        ).hexdigest(),
        "geometry_sha256": hashlib.sha256(
            _blender_scene_data_json_bytes(geometry)
        ).hexdigest(),
    }


def build_blender_scene_data(
    mod_path: str | Path,
    *,
    include_normals: bool = True,
    fix_lp2: bool = False,
    fix_dmc: bool = False,
    fix_processing_mode: str = FIX_PROCESSING_MODE_CODEX,
    reserved_scene_names: Iterable[str] | None = None,
    blender_compact_mesh_names: bool = False,
    mrl_path: str | Path | None = None,
    texture_mode: str = "dds",
    texture_roots: Iterable[str | Path] = (),
    decode_directory: str | Path | None = None,
    bug_control: Any | None = None,
) -> dict[str, Any]:
    """Build the direct MOD-to-Blender Scene Data contract.

    This public route deliberately does not create an FBX.  The existing
    Importer remains the authority for MOD parsing, physical slot identity,
    FVF, hierarchy, skin and normal rows.  The caller may supply a temporary
    decode directory for TEX-backed display textures; those paths are included
    only until the BPY agent has packed the images into Blender.
    """
    if blender_compact_mesh_names:
        raise ValueError(
            "Blender compact Mesh names are retired; use complete RE6 Mesh Header names"
        )
    scene = build_import_scene(
        mod_path,
        include_normals=include_normals,
        fix_lp2=fix_lp2,
        fix_dmc=fix_dmc,
        fix_processing_mode=fix_processing_mode,
        reserved_scene_names=reserved_scene_names,
        blender_compact_mesh_names=bool(blender_compact_mesh_names),
        bug_control=bug_control,
    )
    meshes: list[dict[str, Any]] = []
    for raw_mesh in scene.get("meshes", []):
        if not isinstance(raw_mesh, dict):
            continue
        mesh = dict(raw_mesh)
        physical_slot = int(mesh.get("physical_slot", 0) or 0)
        if physical_slot <= 0:
            raise ValueError("Blender Scene Data Mesh has no physical slot")
        mesh["blender_source_receipt"] = _blender_scene_mesh_receipt(mesh)
        meshes.append(mesh)

    texture_import: dict[str, Any] = {
        "requested": bool(mrl_path),
        "status": "not_requested" if not mrl_path else "skipped",
        "schema": "",
        "binding_count": 0,
        "temporary_paths": [],
        "error": "",
    }
    if mrl_path:
        if bug_control is not None:
            bug_control.advance("texture_plan")
        try:
            normalized_mode = _normalize_mrl_texture_source_mode(texture_mode)
            if normalized_mode == "tex" and decode_directory is None:
                raise ValueError(
                    "Blender TEX display import requires a caller-owned temporary decode directory"
                )
            texture_plan = _build_mrl_embedded_texture_plan(
                mod_path,
                mrl_path,
                texture_mode=normalized_mode,
                decode_directory=(
                    _windows_lexical_full_path(decode_directory)
                    if decode_directory is not None
                    else _windows_lexical_full_path(Path(mrl_path).parent)
                ),
                texture_roots=texture_roots,
            )
            temporary_paths = sorted(
                {
                    str(binding.get("image_path", "") or "")
                    for binding in dict(
                        texture_plan.get("bindings_by_mesh_slot", {})
                    ).values()
                    if isinstance(binding, dict)
                    and str(binding.get("image_path", "") or "")
                    and os.path.normcase(str(binding.get("image_path", "") or ""))
                    != os.path.normcase(
                        str(binding.get("source_texture_path", "") or "")
                    )
                }
            )
            texture_import = {
                "requested": True,
                "status": "resolved",
                "schema": str(texture_plan.get("schema", "") or ""),
                "mrl_path": str(texture_plan.get("mrl_path", "") or ""),
                "texture_mode": str(texture_plan.get("texture_mode", "") or ""),
                "binding_count": len(
                    [
                        value
                        for value in dict(
                            texture_plan.get("bindings_by_mesh_slot", {})
                        ).values()
                        if isinstance(value, dict)
                    ]
                ),
                "bindings_by_mesh_slot": dict(
                    texture_plan.get("bindings_by_mesh_slot", {})
                ),
                "temporary_paths": temporary_paths,
                "error": "",
            }
        except Exception as exc:
            # Display textures must never block a valid geometry import.
            texture_import["error"] = f"{type(exc).__name__}: {exc}"

    identity = {
        "schema": BLENDER_SCENE_DATA_SCHEMA,
        "revision": BLENDER_SCENE_DATA_REVISION,
        "source_contract_id": str(scene.get("contract_id", "") or ""),
        "source_request_contract_sha256": str(
            scene.get("request_contract_sha256", "") or ""
        ),
        "source_sha256": str(scene.get("source", {}).get("sha256", "") or ""),
        "physical_slots": [
            {
                "slot": int(mesh["physical_slot"]),
                "topology_sha256": str(
                    mesh["blender_source_receipt"]["topology_sha256"]
                ),
            }
            for mesh in meshes
        ],
    }
    identity_sha256 = hashlib.sha256(
        _blender_scene_data_json_bytes(identity)
    ).hexdigest()
    root_mesh_scale = scene.get("root_mesh_scale", [])
    if isinstance(root_mesh_scale, (list, tuple)):
        normalized_root_mesh_scale = [float(value) for value in root_mesh_scale[:3]]
    else:
        normalized_root_mesh_scale = [float(root_mesh_scale or 1.0)] * 3
    while len(normalized_root_mesh_scale) < 3:
        normalized_root_mesh_scale.append(1.0)

    scene_data: dict[str, Any] = {
        "schema": BLENDER_SCENE_DATA_SCHEMA,
        "revision": BLENDER_SCENE_DATA_REVISION,
        "scene_data_id": "re6-blender-" + identity_sha256[:24],
        "source_contract_id": str(scene.get("contract_id", "") or ""),
        "source_request_contract_sha256": str(
            scene.get("request_contract_sha256", "") or ""
        ),
        "source": dict(scene.get("source", {})),
        "options": dict(scene.get("options", {})),
        "root_mesh_scale": normalized_root_mesh_scale,
        "bounds": list(scene.get("bounds", [])),
        "mtp": list(scene.get("mtp", [])),
        "scene_helpers": dict(scene.get("scene_helpers", {})),
        "import_name_plan": dict(scene.get("import_name_plan", {})),
        "bones": list(scene.get("bones", [])),
        "meshes": meshes,
        "texture_import": texture_import,
        "transport": {
            "authority": "codex_re6_mod_import_fbx.build_import_scene",
            "geometry_carrier": "blender_scene_data_direct",
            "slot_key": "PC_REHD_PHYSICAL_SLOT",
            "parent_key": "PC_REHD_PARENT_NAME",
            "material_policy": "import_display_only_excluded_from_mod_export",
        },
    }
    scene_data["contract_sha256"] = hashlib.sha256(
        _blender_scene_data_json_bytes(scene_data)
    ).hexdigest()
    return scene_data


def build_normal_route_table(scene: dict[str, Any]) -> dict[str, Any]:
    """Produce the persistent normal/tangent audit sidecar.

    The current V4 imports embedded FBX normals and does not rebuild an
    ``Edit_Normals`` modifier from this JSON.  The sidecar remains mandatory so
    source normal/tangent truth is retained for diagnostics and future tools.
    """
    route_meshes: list[dict[str, Any]] = []
    for mesh in scene.get("meshes", []):
        normals = mesh.get("max_normals", [])
        route_meshes.append(
            {
                "physical_slot": mesh["physical_slot"],
                "display_slot": mesh["display_slot"],
                "node_name": mesh["node_name"],
                "source_fvf": mesh["source_fvf"],
                "parser_fvf": mesh["parser_fvf"],
                "skin_bone_limit": mesh.get("skin_bone_limit"),
                "vertex_count": len(mesh.get("positions", [])),
                "face_count": len(mesh.get("faces", [])),
                "normal_layout_supported": any(value is not None for value in normals),
                "max_normals": normals,
                "game_normals": mesh.get("game_normals", []),
                "raw_tangents": mesh.get("raw_tangents", []),
                "topology": {
                    "faces": mesh.get("faces", []),
                    "invalid_face_count": mesh.get("invalid_face_count", 0),
                },
                "identity": {
                    "mat_id": mesh["header"]["mat_id"],
                    "lod_level": mesh["header"]["lod_level"],
                    "group": mesh["header"]["unk01"],
                    "display_mode": mesh["header"]["unk05"],
                    "mesh_type": mesh["header"]["meshtype"],
                },
            }
        )
    return {
        "schema": IMPORT_ROUTE_SCHEMA,
        "contract_id": scene["contract_id"],
        "request_contract_sha256": scene["request_contract_sha256"],
        "source": dict(scene["source"]),
        "options": {
            "fix_lp2": scene["options"]["fix_lp2"],
            "fix_dmc": scene["options"]["fix_dmc"],
            "fix_processing_mode": _normalize_fix_processing_mode(
                scene["options"].get(
                    "fix_processing_mode",
                    FIX_PROCESSING_MODE_CODEX,
                )
            ),
        },
        "max_post_import": scene.get("max_post_import", {}),
        "meshes": route_meshes,
    }


# END RE6 MOD IMPORT PUBLIC CONTRACT
# ============================================================================


# ============================================================================
# BEGIN STANDARD-LIBRARY FBX 7.4 BINARY ENCODER
#
# FBX is a documented tagged-binary tree.  The encoder below is deliberately
# small and local instead of importing Blender's GPL exporter or adding a new
# package dependency.  It covers exactly the objects this importer needs:
# Model, Geometry, Null/LimbNode attribute, Skin/Cluster, BindPose and the
# normal/UV layers that Max consumes.
# ============================================================================
FBX_MAGIC = b"Kaydara FBX Binary  \x00\x1a\x00"
FBX_NULL_RECORD = b"\x00" * 13
# Binary FBX CRC fields are coupled.  This fixed timestamp/FileId/FooterId
# triple is the same compatibility workaround used by Blender's FBX writer.
FBX_CREATION_TIME = "1970-01-01 10:00:00:000"
FBX_FILE_ID = bytes.fromhex("28b32aebb624ccc2bfc8b02aa92bfcf1")
FBX_FOOT_ID = bytes.fromhex("fabcab09d0c8d466b176fb831cf7267e")
FBX_FOOT_MAGIC = bytes.fromhex("f85a8c6adef5d97eece90ce3758f290b")
FBX_ALWAYS_BLOCK_SENTINEL = {b"AnimationStack", b"AnimationLayer"}


@dataclass
class _FbxNode:
    name: bytes
    props: list[tuple[str, Any]] = field(default_factory=list)
    children: list["_FbxNode"] = field(default_factory=list)

    def add(self, name: str | bytes, *props: tuple[str, Any]) -> "_FbxNode":
        child = _FbxNode(name.encode("utf-8") if isinstance(name, str) else name, list(props))
        self.children.append(child)
        return child


def _fbx_scalar(kind: str, value: Any) -> bytes:
    if kind == "C":
        return bytes((1 if value else 0,))
    if kind == "Y":
        return struct.pack("<h", int(value))
    if kind == "I":
        return struct.pack("<i", int(value))
    if kind == "L":
        return struct.pack("<q", int(value))
    if kind == "F":
        return struct.pack("<f", float(value))
    if kind == "D":
        return struct.pack("<d", float(value))
    if kind == "S":
        encoded = str(value).encode("utf-8")
        return struct.pack("<I", len(encoded)) + encoded
    if kind == "R":
        encoded = bytes(value)
        return struct.pack("<I", len(encoded)) + encoded
    raise ValueError(f"Unsupported FBX scalar property {kind!r}")


def _fbx_array(kind: str, values: Iterable[Any]) -> bytes:
    normalized = values if isinstance(values, list) else list(values)
    if kind not in _FBX_ARRAY_FORMAT_BY_KIND:
        raise ValueError(f"Unsupported FBX array property {kind!r}")
    numeric_format = _FBX_ARRAY_FORMAT_BY_KIND[kind]
    if kind == "b":
        raw = bytes(int(value) & 0xFF for value in normalized)
    elif normalized:
        raw = struct.pack(f"<{len(normalized)}{numeric_format}", *normalized)
    else:
        raw = b""
    encoding = 1 if len(raw) > 128 else 0
    payload = zlib.compress(raw, 1) if encoding else raw
    return struct.pack("<III", len(normalized), encoding, len(payload)) + payload


def _fbx_property_blob(props: Sequence[tuple[str, Any]]) -> bytes:
    chunks: list[bytes] = []
    for kind, value in props:
        if kind in {"b", "i", "l", "f", "d"}:
            chunks.append(kind.encode("ascii") + _fbx_array(kind, value))
        else:
            chunks.append(kind.encode("ascii") + _fbx_scalar(kind, value))
    return b"".join(chunks)


def _append_fbx_node_chunks(
    node: _FbxNode,
    start_offset: int,
    chunks: list[bytes],
    *,
    is_last: bool,
) -> int:
    property_blob = _fbx_property_blob(node.props)
    body_offset = start_offset + 13 + len(node.name) + len(property_blob)
    header_index = len(chunks)
    chunks.append(b"")
    chunks.append(property_blob)
    cursor = body_offset
    for child_index, child in enumerate(node.children):
        cursor = _append_fbx_node_chunks(
            child, cursor, chunks, is_last=child_index == len(node.children) - 1
        )
    if node.children or (not node.props and not is_last) or node.name in FBX_ALWAYS_BLOCK_SENTINEL:
        chunks.append(FBX_NULL_RECORD)
        cursor += len(FBX_NULL_RECORD)
    chunks[header_index] = struct.pack("<III", cursor, len(node.props), len(property_blob)) + bytes((len(node.name),)) + node.name
    return cursor


def _encode_fbx_node(node: _FbxNode, start_offset: int, *, is_last: bool) -> bytes:
    chunks: list[bytes] = []
    _append_fbx_node_chunks(node, start_offset, chunks, is_last=is_last)
    return b"".join(chunks)


def _write_fbx_binary(path: Path, roots: Sequence[_FbxNode]) -> None:
    body_parts: list[bytes] = []
    cursor = len(FBX_MAGIC) + 4
    for root_index, root in enumerate(roots):
        cursor = _append_fbx_node_chunks(
            root, cursor, body_parts, is_last=root_index == len(roots) - 1
        )
    body_parts.append(FBX_NULL_RECORD)
    before_footer_version = cursor + len(FBX_NULL_RECORD) + len(FBX_FOOT_ID) + 4
    padding = (-before_footer_version) % 16
    if padding == 0:
        padding = 16
    footer = FBX_FOOT_ID + b"\x00" * 4 + b"\x00" * padding + struct.pack("<I", FBX_BINARY_VERSION) + b"\x00" * 120 + FBX_FOOT_MAGIC
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("wb", delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp") as handle:
            temp_path = Path(handle.name)
            handle.write(FBX_MAGIC)
            handle.write(struct.pack("<I", FBX_BINARY_VERSION))
            handle.writelines(body_parts)
            handle.write(footer)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


def _fbx_class_name(name: str, class_name: str) -> str:
    return name + "\x00\x01" + class_name


def _fbx_properties70(owner: _FbxNode) -> _FbxNode:
    return owner.add("Properties70")


def _fbx_property70(
    properties: _FbxNode,
    name: str,
    kind: str,
    subtype: str,
    flags: str,
    values: Sequence[Any],
) -> None:
    props: list[tuple[str, Any]] = [("S", name), ("S", kind), ("S", subtype), ("S", flags)]
    for value in values:
        props.append(("D", float(value)) if isinstance(value, float) else ("I", int(value)) if isinstance(value, int) else ("S", str(value)))
    properties.add("P", *props)


def _fbx_max_user_property_blob(custom_properties: dict[str, str | int | float] | None) -> str:
    """Encode Max user properties in the FBX field Autodesk actually restores."""
    lines: list[str] = []
    for key, value in (custom_properties or {}).items():
        clean_key = str(key).replace("\r", " ").replace("\n", " ")
        clean_value = str(value).replace("\r", " ").replace("\n", " ")
        lines.append(f"{clean_key} = {clean_value}\r\n")
    return "".join(lines)


def _format_max_user_float(value: float) -> str:
    """Serialize one Max float compactly while retaining float32 round-trip precision."""
    return format(_finite_number(float(value)), ".9g")


def _encode_max_matrix_user_property(matrix: Sequence[float]) -> str:
    """Encode the 12 affine Matrix3 values that MaxScript can restore exactly."""
    if len(matrix) < 16:
        raise ValueError("Max world matrix user property requires 16 source values")
    affine_indexes = (0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 13, 14)
    return ",".join(_format_max_user_float(float(matrix[index])) for index in affine_indexes)


def _fbx_add_model_properties(
    model: _FbxNode,
    *,
    translation: Sequence[float],
    rotation: Sequence[float],
    scale: Sequence[float],
    pre_rotation: Sequence[float] | None = None,
    custom_properties: dict[str, str | int | float] | None = None,
    hidden: bool = False,
    frozen: bool = False,
) -> None:
    props = _fbx_properties70(model)
    _fbx_property70(props, "Lcl Translation", "Lcl Translation", "", "A", [float(value) for value in translation])
    _fbx_property70(props, "Lcl Rotation", "Lcl Rotation", "", "A", [float(value) for value in rotation])
    _fbx_property70(props, "Lcl Scaling", "Lcl Scaling", "", "A", [float(value) for value in scale])
    if pre_rotation is not None:
        _fbx_property70(props, "PreRotation", "Vector3D", "Vector", "", [float(value) for value in pre_rotation])
        _fbx_property70(props, "RotationActive", "bool", "", "", [1])
    # Max's FBX exporter writes RSrs inheritance for every V4 scene node.  It
    # is observable when the root bone carries the MOD scale and must not fall
    # back to the FBX default inheritance mode.
    _fbx_property70(props, "InheritType", "enum", "", "", [1])
    _fbx_property70(props, "ScalingMax", "Vector3D", "Vector", "", [0.0, 0.0, 0.0])
    _fbx_property70(props, "DefaultAttributeIndex", "int", "Integer", "", [0])
    if hidden:
        _fbx_property70(props, "Show", "bool", "", "", [0])
    if frozen:
        _fbx_property70(props, "Freeze", "bool", "", "", [1])
    user_property_blob = _fbx_max_user_property_blob(custom_properties)
    if user_property_blob:
        _fbx_property70(props, "UDP3DSMAX", "KString", "", "U", [user_property_blob])


def _matrix_to_fbx_array(max_row_matrix: Sequence[float]) -> list[float]:
    """Return FBX column-major values equivalent to a Max row-vector matrix.

    FBX stores column-major arrays.  The equivalent column-vector matrix is
    the transpose of Max's row-vector matrix; flattening that by columns gives
    the original Max row order, so returning the flat row order is correct.
    """
    return [float(value) for value in max_row_matrix]


def _orthonormalize_rotation_rows(rows: Sequence[Sequence[float]]) -> list[list[float]]:
    """Remove float32 axis drift without accepting a genuinely sheared basis."""
    first = [float(value) for value in rows[0][:3]]
    first_length = math.sqrt(sum(value * value for value in first))
    if first_length <= EPSILON:
        raise ValueError("Degenerate MOD bone transform X axis")
    x_axis = [value / first_length for value in first]

    second = [float(value) for value in rows[1][:3]]
    projection = sum(value * axis for value, axis in zip(second, x_axis))
    second = [value - projection * axis for value, axis in zip(second, x_axis)]
    second_length = math.sqrt(sum(value * value for value in second))
    if second_length <= EPSILON:
        raise ValueError("Degenerate MOD bone transform Y axis")
    y_axis = [value / second_length for value in second]
    z_axis = [
        x_axis[1] * y_axis[2] - x_axis[2] * y_axis[1],
        x_axis[2] * y_axis[0] - x_axis[0] * y_axis[2],
        x_axis[0] * y_axis[1] - x_axis[1] * y_axis[0],
    ]
    z_length = math.sqrt(sum(value * value for value in z_axis))
    if z_length <= EPSILON:
        raise ValueError("Degenerate MOD bone transform Z axis")
    z_axis = [value / z_length for value in z_axis]
    return [x_axis, y_axis, z_axis]


def _matrix_to_local_trs(matrix: Sequence[float]) -> tuple[list[float], list[float], list[float]]:
    """Decompose one Max row-vector affine matrix into FBX XYZ properties."""
    row_scale = _matrix_scale(matrix)
    rotation_row = [[float(matrix[row * 4 + column]) / (row_scale[row] if row_scale[row] > EPSILON else 1.0) for column in range(3)] for row in range(3)]
    determinant = (
        rotation_row[0][0] * (rotation_row[1][1] * rotation_row[2][2] - rotation_row[1][2] * rotation_row[2][1])
        - rotation_row[0][1] * (rotation_row[1][0] * rotation_row[2][2] - rotation_row[1][2] * rotation_row[2][0])
        + rotation_row[0][2] * (rotation_row[1][0] * rotation_row[2][1] - rotation_row[1][1] * rotation_row[2][0])
    )
    scale = list(row_scale)
    if determinant < 0.0:
        # Preserve reflections deterministically on X before repairing the
        # small loss of orthogonality introduced by float32 bone matrices.
        scale[0] = -scale[0]
        rotation_row[0] = [-value for value in rotation_row[0]]
    rotation_row = _orthonormalize_rotation_rows(rotation_row)
    # Convert row-vector basis rows into the equivalent conventional column
    # rotation matrix before extracting FBX's XYZ Euler representation.
    r00, r01, r02 = rotation_row[0][0], rotation_row[1][0], rotation_row[2][0]
    r10, r11, r12 = rotation_row[0][1], rotation_row[1][1], rotation_row[2][1]
    r20, r21, r22 = rotation_row[0][2], rotation_row[1][2], rotation_row[2][2]
    # FBX RotationOrder=XYZ composes a conventional column-vector matrix as
    # Rz * Ry * Rx.  The previous Rx * Ry * Rz extraction only failed on
    # rotated branches, making most bones look correct while moving their
    # descendants far away in Max.
    sy = max(-1.0, min(1.0, -r20))
    y = math.asin(sy)
    if abs(sy) < 0.999999:
        x = math.atan2(r21, r22)
        z = math.atan2(r10, r00)
    else:
        x = math.atan2(-r12, r11)
        z = 0.0
    rotation = [math.degrees(x), math.degrees(y), math.degrees(z)]
    translation = [float(matrix[12]), float(matrix[13]), float(matrix[14])]
    return translation, rotation, scale


def _local_trs_to_max_row_matrix(
    translation: Sequence[float],
    rotation: Sequence[float],
    scale: Sequence[float],
) -> list[float]:
    """Recompose FBX XYZ properties as the equivalent Max row matrix."""
    x, y, z = [math.radians(float(value)) for value in rotation[:3]]
    cx, sx = math.cos(x), math.sin(x)
    cy, sy = math.cos(y), math.sin(y)
    cz, sz = math.cos(z), math.sin(z)
    rotation_column = (
        (cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx),
        (sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx),
        (-sy, cy * sx, cy * cx),
    )
    sx_value, sy_value, sz_value = [float(value) for value in scale[:3]]
    tx, ty, tz = [float(value) for value in translation[:3]]
    return [
        rotation_column[0][0] * sx_value,
        rotation_column[1][0] * sx_value,
        rotation_column[2][0] * sx_value,
        0.0,
        rotation_column[0][1] * sy_value,
        rotation_column[1][1] * sy_value,
        rotation_column[2][1] * sy_value,
        0.0,
        rotation_column[0][2] * sz_value,
        rotation_column[1][2] * sz_value,
        rotation_column[2][2] * sz_value,
        0.0,
        tx,
        ty,
        tz,
        1.0,
    ]


def _require_local_trs_round_trip(matrix: Sequence[float], *, label: str) -> tuple[list[float], list[float], list[float]]:
    """Reject a matrix that FBX XYZ properties cannot reproduce faithfully."""
    translation, rotation, scale = _matrix_to_local_trs(matrix)
    rebuilt = _local_trs_to_max_row_matrix(translation, rotation, scale)
    delta = max(abs(float(source) - rebuilt_value) for source, rebuilt_value in zip(matrix, rebuilt))
    if delta > 1.0e-5:
        raise ValueError(f"FBX XYZ TRS round-trip mismatch for {label}: max delta {delta:.9g}")
    return translation, rotation, scale


def _scale_matrix(scale: Sequence[float]) -> list[float]:
    return [float(scale[0]), 0.0, 0.0, 0.0, 0.0, float(scale[1]), 0.0, 0.0, 0.0, 0.0, float(scale[2]), 0.0, 0.0, 0.0, 0.0, 1.0]


def _translation_matrix(translation: Sequence[float]) -> list[float]:
    matrix = _identity_matrix()
    matrix[12:15] = [float(value) for value in translation[:3]]
    return matrix


def _fbx_polygon_indices(faces: Sequence[Sequence[int]]) -> list[int]:
    values: list[int] = []
    for face in faces:
        face_size = len(face)
        if face_size < 3:
            continue
        if face_size == 3:
            values.extend((int(face[0]), int(face[1]), -int(face[2]) - 1))
            continue
        for index, vertex in enumerate(face):
            value = int(vertex)
            values.append(-value - 1 if index == face_size - 1 else value)
    return values


def _build_bound_sphere_geometry(radius: float) -> tuple[list[list[float]], list[list[int]]]:
    """Match the default V4 Max Sphere: 16 segments, 8 vertical divisions."""
    f32 = lambda value: struct.unpack("<f", struct.pack("<f", float(value)))[0]
    r = f32(abs(_finite_number(radius)))
    vertices: list[list[float]] = [[0.0, 0.0, r]]
    segments = 16
    rings = 8
    for ring in range(1, rings):
        phi = f32(math.pi * ring / rings)
        ring_radius = f32(r * f32(math.sin(phi)))
        z = f32(r * f32(math.cos(phi)))
        for segment in range(segments):
            theta = f32((math.pi * 0.5) + math.tau * segment / segments)
            vertices.append(
                [
                    f32(ring_radius * f32(math.cos(theta))),
                    f32(ring_radius * f32(math.sin(theta))),
                    z,
                ]
            )
    bottom_index = len(vertices)
    vertices.append([0.0, 0.0, -r])
    faces: list[list[int]] = []
    first_ring = 1
    for segment in range(segments):
        next_segment = (segment + 1) % segments
        faces.append([0, first_ring + segment, first_ring + next_segment])
    for ring in range(rings - 2):
        current = 1 + ring * segments
        following = current + segments
        for segment in range(segments):
            next_segment = (segment + 1) % segments
            faces.append([current + segment, following + segment, following + next_segment, current + next_segment])
    last_ring = bottom_index - segments
    for segment in range(segments):
        next_segment = (segment + 1) % segments
        faces.append([bottom_index, last_ring + next_segment, last_ring + segment])
    return vertices, faces


class _FbxIdAllocator:
    def __init__(self) -> None:
        self._next = 100000
        self._values: dict[str, int] = {}

    def get(self, key: str) -> int:
        if key not in self._values:
            self._values[key] = self._next
            self._next += 1
        return self._values[key]


def _fbx_unit_normal(value: Sequence[float] | None) -> list[float] | None:
    if value is None or len(value) < 3:
        return None
    try:
        vector = [float(axis) for axis in value[:3]]
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(axis) for axis in vector):
        return None
    length = math.sqrt(sum(axis * axis for axis in vector))
    if length <= EPSILON:
        return None
    return [axis / length for axis in vector]


def _fbx_face_unit_normal(
    vertices: Sequence[Sequence[float]], face: Sequence[int]
) -> list[float] | None:
    if len(face) < 3:
        return None
    try:
        first = vertices[int(face[0])]
        second = vertices[int(face[1])]
        third = vertices[int(face[2])]
        left = [float(second[index]) - float(first[index]) for index in range(3)]
        right = [float(third[index]) - float(first[index]) for index in range(3)]
    except (IndexError, TypeError, ValueError, OverflowError):
        return None
    return _fbx_unit_normal(
        [
            left[1] * right[2] - left[2] * right[1],
            left[2] * right[0] - left[0] * right[2],
            left[0] * right[1] - left[1] * right[0],
        ]
    )


def _fbx_blender_safe_loop_normals(
    vertices: Sequence[Sequence[float]],
    faces: Sequence[Sequence[int]],
    normals: Sequence[Sequence[float] | None] | None,
) -> list[list[float]] | None:
    """Return outward per-loop normals or omit a Mesh that Blender cannot map safely.

    Blender 5.1 drops zero-area source triangles while importing FBX.  A direct
    normal layer on that Mesh can no longer be trusted, so this profile leaves
    its display normals to Blender.  On healthy Meshes, only source vectors
    facing away from their own triangle are reversed; the Max profile remains
    byte-for-byte unchanged.
    """

    if not normals or len(normals) != len(vertices):
        return None
    source_normals = [_fbx_unit_normal(value) for value in normals]
    if any(value is None for value in source_normals):
        return None
    values: list[list[float]] = []
    for face in faces:
        face_normal = _fbx_face_unit_normal(vertices, face)
        if face_normal is None:
            return None
        for vertex in face:
            try:
                source = source_normals[int(vertex)]
            except (IndexError, TypeError, ValueError):
                return None
            if source is None:
                return None
            alignment = sum(source[index] * face_normal[index] for index in range(3))
            if alignment < -EPSILON:
                values.append([-axis for axis in source])
            elif alignment <= EPSILON:
                values.append(list(face_normal))
            else:
                values.append(list(source))
    return values


def _fbx_blender_renderable_faces(
    vertices: Sequence[Sequence[float]], faces: Sequence[Sequence[int]]
) -> list[list[int]]:
    """Drop only faces Blender would discard before it consumes direct normals."""

    return [
        [int(vertex) for vertex in face]
        for face in faces
        if _fbx_face_unit_normal(vertices, face) is not None
    ]


def _fbx_add_geometry(
    objects: _FbxNode,
    *,
    object_id: int,
    name: str,
    vertices: Sequence[Sequence[float]],
    faces: Sequence[Sequence[int]],
    uv1: Sequence[Sequence[float] | None] | None,
    normals: Sequence[Sequence[float] | None] | None,
    loop_normals: Sequence[Sequence[float]] | None = None,
    smooth: bool,
    material_index: int | None = None,
) -> None:
    geometry = objects.add("Geometry", ("L", object_id), ("S", _fbx_class_name(name, "Geometry")), ("S", "Mesh"))
    geometry.add("GeometryVersion", ("I", 124))
    geometry.add("Vertices", ("d", [axis for vertex in vertices for axis in vertex[:3]]))
    geometry.add("PolygonVertexIndex", ("i", _fbx_polygon_indices(faces)))
    smoothing = geometry.add("LayerElementSmoothing", ("I", 0))
    smoothing.add("Version", ("I", 102))
    smoothing.add("Name", ("S", ""))
    smoothing.add("MappingInformationType", ("S", "ByPolygon"))
    smoothing.add("ReferenceInformationType", ("S", "Direct"))
    # Max's ``mesh vertices: faces:`` constructor assigns the 1-based face
    # ordinal as the smoothing-group bit field.  Preserve that observable V4
    # scene contract for MOD meshes; the generated Bounds sphere stays smooth.
    smoothing.add("Smoothing", ("i", [1 if smooth else index for index, _face in enumerate(faces, start=1)]))
    normal_values: list[float] = []
    if loop_normals is not None:
        expected_loop_count = sum(len(face) for face in faces)
        if len(loop_normals) != expected_loop_count:
            raise ValueError(
                f"FBX geometry {name!r} loop-normal count does not match its faces"
            )
        for normal in loop_normals:
            normalized = _fbx_unit_normal(normal)
            if normalized is None:
                raise ValueError(f"FBX geometry {name!r} has an invalid loop normal")
            normal_values.extend(normalized)
    elif bool(normals) and all(value is not None for value in normals):
        normal_values = [
            axis
            for face in faces
            for vertex in face
            for axis in ((normals[int(vertex)] if normals is not None else None) or [0.0, 0.0, 1.0])
        ]
    has_normals = bool(normal_values)
    if has_normals:
        normal_layer = geometry.add("LayerElementNormal", ("I", 0))
        normal_layer.add("Version", ("I", 101))
        normal_layer.add("Name", ("S", ""))
        normal_layer.add("MappingInformationType", ("S", "ByPolygonVertex"))
        normal_layer.add("ReferenceInformationType", ("S", "Direct"))
        normal_layer.add("Normals", ("d", normal_values))
    if uv1 is not None and len(uv1) != len(vertices):
        raise ValueError(f"FBX geometry {name!r} UV1 row count does not match its vertex count")
    # Every non-empty MOD Mesh receives Map1.  V4's no-UV FVF branches create
    # zero TVerts/TVFaces, so an all-zero or all-None row set must not suppress
    # the FBX UV layer.  Generated helper geometry passes uv1=None explicitly.
    has_uv = bool(vertices) and uv1 is not None
    if has_uv:
        uv_layer = geometry.add("LayerElementUV", ("I", 0))
        uv_layer.add("Version", ("I", 101))
        uv_layer.add("Name", ("S", "map1"))
        uv_layer.add("MappingInformationType", ("S", "ByPolygonVertex"))
        uv_layer.add("ReferenceInformationType", ("S", "IndexToDirect"))
        uv_values: list[float] = []
        for uv in uv1 or []:
            uv_values.extend((uv or [0.0, 0.0, 0.0])[:2])
        uv_layer.add("UV", ("d", uv_values))
        uv_layer.add("UVIndex", ("i", [int(vertex) for face in faces for vertex in face]))
    if material_index is not None:
        material_layer = geometry.add("LayerElementMaterial", ("I", 0))
        material_layer.add("Version", ("I", 101))
        material_layer.add("Name", ("S", ""))
        material_layer.add("MappingInformationType", ("S", "AllSame"))
        material_layer.add("ReferenceInformationType", ("S", "IndexToDirect"))
        material_layer.add("Materials", ("i", [int(material_index)]))
    layer = geometry.add("Layer", ("I", 0))
    layer.add("Version", ("I", 100))
    element = layer.add("LayerElement")
    element.add("Type", ("S", "LayerElementSmoothing"))
    element.add("TypedIndex", ("I", 0))
    if has_normals:
        element = layer.add("LayerElement")
        element.add("Type", ("S", "LayerElementNormal"))
        element.add("TypedIndex", ("I", 0))
    if has_uv:
        element = layer.add("LayerElement")
        element.add("Type", ("S", "LayerElementUV"))
        element.add("TypedIndex", ("I", 0))
    if material_index is not None:
        element = layer.add("LayerElement")
        element.add("Type", ("S", "LayerElementMaterial"))
        element.add("TypedIndex", ("I", 0))


def _fbx_add_model(
    objects: _FbxNode,
    *,
    object_id: int,
    name: str,
    model_type: str,
    matrix: Sequence[float] | None = None,
    translation: Sequence[float] | None = None,
    rotation: Sequence[float] | None = None,
    scale: Sequence[float] | None = None,
    custom_properties: dict[str, str | int | float] | None = None,
    use_pre_rotation: bool = False,
    hidden: bool = False,
    frozen: bool = False,
) -> None:
    model = objects.add("Model", ("L", object_id), ("S", _fbx_class_name(name, "Model")), ("S", model_type))
    model.add("Version", ("I", 232))
    if matrix is not None:
        local_translation, local_rotation, local_scale = _require_local_trs_round_trip(matrix, label=name)
    else:
        local_translation = list(translation or (0.0, 0.0, 0.0))
        local_rotation = list(rotation or (0.0, 0.0, 0.0))
        local_scale = list(scale or (1.0, 1.0, 1.0))
    pre_rotation = list(local_rotation) if use_pre_rotation else None
    if use_pre_rotation:
        local_rotation = [0.0, 0.0, 0.0]
    _fbx_add_model_properties(
        model,
        translation=local_translation,
        rotation=local_rotation,
        scale=local_scale,
        pre_rotation=pre_rotation,
        custom_properties=custom_properties,
        hidden=hidden,
        frozen=frozen,
    )
    model.add("Shading", ("C", True))
    model.add("Culling", ("S", "CullingOff"))


def _fbx_add_null_attribute(objects: _FbxNode, *, object_id: int, name: str) -> None:
    attribute = objects.add("NodeAttribute", ("L", object_id), ("S", _fbx_class_name(name, "NodeAttribute")), ("S", "Null"))
    attribute.add("TypeFlags", ("S", "Null"))


def _fbx_add_limb_attribute(objects: _FbxNode, *, object_id: int, name: str) -> None:
    attribute = objects.add("NodeAttribute", ("L", object_id), ("S", _fbx_class_name(name, "NodeAttribute")), ("S", "LimbNode"))
    attribute.add("TypeFlags", ("S", "Skeleton"))
    props = _fbx_properties70(attribute)
    _fbx_property70(props, "Size", "double", "Number", "", [1.5])


def _fbx_connection(connections: _FbxNode, child_id: int, parent_id: int) -> None:
    connections.add("C", ("S", "OO"), ("L", child_id), ("L", parent_id))


def _fbx_property_connection(
    connections: _FbxNode,
    child_id: int,
    parent_id: int,
    property_name: str,
) -> None:
    connections.add(
        "C",
        ("S", "OP"),
        ("L", child_id),
        ("L", parent_id),
        ("S", property_name),
    )


def _fbx_add_embedded_base_color_bundle(
    objects: _FbxNode,
    *,
    material_id: int,
    texture_id: int,
    video_id: int,
    mesh_name: str,
    media_name: str,
    relative_filename: str,
    embedded_content: bytes | None,
    emit_media: bool,
) -> str:
    """Add the StandardMaterial graph verified from the exported TEST V2 FBX.

    The FBX importer recognizes this ordinary ``phong`` Material as a
    3ds Max StandardMaterial.  Keeping the bitmap on the standard
    ``DiffuseColor`` property is what makes the texture arrive in Max's
    ``diffuseMap`` slot without a MAX Script material conversion pass.
    """
    material_name = f"MRLStruct_{mesh_name}_001"
    material = objects.add(
        "Material",
        ("L", material_id),
        ("S", _fbx_class_name(material_name, "Material")),
        ("S", ""),
    )
    material.add("Version", ("I", 102))
    material.add("ShadingModel", ("S", "phong"))
    material.add("MultiLayer", ("I", 0))
    material_props = _fbx_properties70(material)
    _fbx_property70(material_props, "ShadingModel", "KString", "", "", ["phong"])
    _fbx_property70(material_props, "AmbientColor", "Color", "", "A", [0.8, 0.8, 0.8])
    _fbx_property70(material_props, "DiffuseColor", "Color", "", "A", [0.8, 0.8, 0.8])
    _fbx_property70(material_props, "TransparentColor", "Color", "", "A", [1.0, 1.0, 1.0])
    _fbx_property70(material_props, "SpecularColor", "Color", "", "A", [0.9, 0.9, 0.9])
    _fbx_property70(material_props, "SpecularFactor", "Number", "", "A", [0.0])
    _fbx_property70(material_props, "ShininessExponent", "Number", "", "A", [1.0717734098434448])
    _fbx_property70(material_props, "Emissive", "Vector3D", "Vector", "", [0.0, 0.0, 0.0])
    _fbx_property70(material_props, "Ambient", "Vector3D", "Vector", "", [0.8, 0.8, 0.8])
    _fbx_property70(material_props, "Diffuse", "Vector3D", "Vector", "", [0.8, 0.8, 0.8])
    _fbx_property70(material_props, "Specular", "Vector3D", "Vector", "", [0.0, 0.0, 0.0])
    _fbx_property70(material_props, "Shininess", "double", "Number", "", [1.0717734098434448])
    _fbx_property70(material_props, "Opacity", "double", "Number", "", [1.0])
    _fbx_property70(material_props, "Reflectivity", "double", "Number", "", [0.0])

    if emit_media:
        # A Video owns the embedded DDS bytes. Reusing a new, empty Video for
        # every Mesh leaves most material slots pointing at blank media in Max.
        # Share one uniquely named Texture/Video pair per exact media payload.
        texture_name = media_name
        texture = objects.add(
            "Texture",
            ("L", texture_id),
            ("S", _fbx_class_name(texture_name, "Texture")),
            ("S", ""),
        )
        texture.add("Type", ("S", "TextureVideoClip"))
        texture.add("Version", ("I", 202))
        texture.add("TextureName", ("S", _fbx_class_name(texture_name, "Texture")))
        texture.add("Media", ("S", _fbx_class_name(texture_name, "Video")))
        texture.add("FileName", ("S", relative_filename))
        texture.add("RelativeFilename", ("S", relative_filename))
        texture.add("ModelUVTranslation", ("D", 0.0), ("D", 0.0))
        texture.add("ModelUVScaling", ("D", 1.0), ("D", 1.0))
        texture.add("Texture_Alpha_Source", ("S", "Alpha_Black"))
        texture.add("Cropping", ("I", 0), ("I", 0), ("I", 0), ("I", 0))
        texture_props = _fbx_properties70(texture)
        _fbx_property70(texture_props, "UVSet", "KString", "", "", ["UVChannel_1"])
        _fbx_property70(texture_props, "UseMaterial", "bool", "", "", [1])

        video = objects.add(
            "Video",
            ("L", video_id),
            ("S", _fbx_class_name(texture_name, "Video")),
            ("S", "Clip"),
        )
        video.add("Type", ("S", "Clip"))
        video_props = _fbx_properties70(video)
        _fbx_property70(video_props, "Path", "KString", "XRefUrl", "", [relative_filename])
        _fbx_property70(video_props, "RelPath", "KString", "XRefUrl", "", [relative_filename])
        video.add("UseMipMap", ("I", 0))
        video.add("Filename", ("S", relative_filename))
        video.add("RelativeFilename", ("S", relative_filename))
        if embedded_content is None:
            raise ValueError("new embedded FBX media is missing DDS content")
        video.add("Content", ("R", embedded_content))
    return material_name


def _fbx_model_reachability_report(roots: Sequence[_FbxNode]) -> dict[str, Any]:
    """Prove that every FBX Model is reachable from the scene root.

    ufbx deliberately exposes disconnected FBX objects, while Autodesk Max
    imports only the object graph rooted at Model parent id 0.  A file can
    therefore pass a generic parser smoke test yet import as an empty scene.
    """
    objects = next((root for root in roots if root.name == b"Objects"), None)
    connections = next((root for root in roots if root.name == b"Connections"), None)
    if objects is None or connections is None:
        raise ValueError("FBX roots must contain Objects and Connections")

    model_ids = {
        int(node.props[0][1])
        for node in objects.children
        if node.name == b"Model" and node.props and node.props[0][0] == "L"
    }
    children_by_parent: dict[int, list[int]] = {}
    for node in connections.children:
        if node.name != b"C" or len(node.props) < 3:
            continue
        if node.props[0] != ("S", "OO"):
            continue
        child_id = int(node.props[1][1])
        parent_id = int(node.props[2][1])
        children_by_parent.setdefault(parent_id, []).append(child_id)

    reachable_ids = {0}
    pending = [0]
    while pending:
        parent_id = pending.pop()
        for child_id in children_by_parent.get(parent_id, []):
            if child_id not in reachable_ids:
                reachable_ids.add(child_id)
                pending.append(child_id)

    unreachable_models = sorted(model_ids - reachable_ids)
    root_model_ids = sorted(model_ids.intersection(children_by_parent.get(0, [])))
    return {
        "model_count": len(model_ids),
        "root_model_count": len(root_model_ids),
        "reachable_model_count": len(model_ids) - len(unreachable_models),
        "unreachable_model_count": len(unreachable_models),
        "unreachable_model_ids": unreachable_models,
    }


def _require_all_fbx_models_reachable(roots: Sequence[_FbxNode]) -> dict[str, Any]:
    report = _fbx_model_reachability_report(roots)
    if report["unreachable_model_count"]:
        raise RuntimeError(
            "FBX contains Models disconnected from scene root: "
            f"{report['unreachable_model_count']} of {report['model_count']}"
        )
    if report["model_count"] and not report["root_model_count"]:
        raise RuntimeError("FBX contains Models but no Model is connected to scene root")
    return report


def _scene_mesh_has_skin(mesh: dict[str, Any], bone_count: int) -> bool:
    rows = mesh.get("fbx_skin_bones", [])
    return bone_count > 0 and bool(rows) and rows[0] is not None


def _scene_mesh_skin_bone_limit(mesh: dict[str, Any], bone_count: int) -> int | None:
    has_skin = _scene_mesh_has_skin(mesh, bone_count)
    raw_limit = mesh.get("skin_bone_limit")
    mesh_label = str(mesh.get("node_name", f"Mesh slot {mesh.get('physical_slot', '?')}"))
    if not has_skin:
        if raw_limit is not None:
            raise ValueError(f"{mesh_label}: static Mesh must not declare a Skin bone limit")
        return None
    if isinstance(raw_limit, bool):
        raise ValueError(f"{mesh_label}: Skin bone limit must be an integer declaration")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{mesh_label}: skinned Mesh is missing its Skin bone-limit declaration") from exc
    if raw_limit != limit or limit not in VALID_SCENE_SKIN_BONE_LIMITS:
        raise ValueError(f"{mesh_label}: invalid Skin bone-limit declaration {raw_limit!r}")
    layout = mesh.get("layout")
    if isinstance(layout, dict) and layout.get("skin_kind"):
        expected = SKIN_KIND_MAX_INFLUENCES.get(str(layout["skin_kind"]))
        if expected is None or int(expected) != limit:
            raise ValueError(
                f"{mesh_label}: Skin bone-limit declaration {limit} does not match "
                f"parser skin kind {layout['skin_kind']!r}"
            )
    return limit


def _collect_mesh_cluster_rows(
    mesh: dict[str, Any],
    bone_count: int,
) -> tuple[list[int], dict[int, tuple[list[int], list[float]]]]:
    order: list[int] = []
    per_bone: dict[int, tuple[list[int], list[float]]] = {}
    # V4 builds the Skin bone palette from parser rows before Skin drops tiny
    # final lanes.  Preserve those empty-but-used clusters; otherwise Max sees
    # a different modifier bone list even when all visible weights match.
    for bones, weights in zip(mesh.get("raw_skin_bones", []), mesh.get("raw_skin_weights", [])):
        if bones is None or weights is None:
            continue
        for bone, weight in zip(bones, weights):
            bone_id = int(bone)
            weight_value = float(weight)
            if not math.isfinite(weight_value):
                weight_value = 0.0
            if bone_id <= 0 or bone_id > bone_count or weight_value <= 0.0:
                continue
            if bone_id not in per_bone:
                order.append(bone_id)
                per_bone[bone_id] = ([], [])
    for vertex_index, (bones, weights) in enumerate(zip(mesh.get("fbx_skin_bones", []), mesh.get("fbx_skin_weights", []))):
        if bones is None or weights is None:
            continue
        legal_pairs: list[tuple[int, float]] = []
        ignored_positive_weight = False
        for bone, weight in zip(bones, weights):
            bone_id = int(bone)
            weight_value = float(weight)
            if not math.isfinite(weight_value):
                weight_value = 0.0
            if bone_id <= 0 or weight_value <= 0.0:
                continue
            if bone_id > bone_count:
                ignored_positive_weight = True
                continue
            legal_pairs.append((bone_id, weight_value))
        legal_weight_total = sum(weight for _bone, weight in legal_pairs)
        weight_scale = (
            1.0 / legal_weight_total
            if ignored_positive_weight and legal_weight_total > EPSILON
            else 1.0
        )
        for bone_id, weight_value in legal_pairs:
            if bone_id not in per_bone:
                order.append(bone_id)
                per_bone[bone_id] = ([], [])
            indexes, values = per_bone[bone_id]
            indexes.append(vertex_index)
            values.append(weight_value * weight_scale)
    if not order and _scene_mesh_has_skin(mesh, bone_count):
        order = list(range(1, bone_count + 1))
        per_bone = {bone_id: ([], []) for bone_id in order}
    return order, per_bone


def describe_mesh_skin_compatibility(
    mesh: dict[str, Any],
    bone_count: int,
) -> dict[str, Any]:
    """Compatibility-module facade retained for the established importer API."""

    return describe_import_skin_compatibility(mesh, bone_count)


def _build_fbx_roots(
    scene: dict[str, Any],
    *,
    include_normals: bool,
    route_file_name: str,
    mrl_bindings: dict[int, dict[str, Any]] | None = None,
    normal_profile: str = FBX_NORMAL_PROFILE_MAX,
) -> list[_FbxNode]:
    normalized_normal_profile = _normalize_fbx_normal_profile(normal_profile)
    allocator = _FbxIdAllocator()
    header_ext = _FbxNode(b"FBXHeaderExtension")
    header_ext.add("FBXHeaderVersion", ("I", 1003))
    header_ext.add("FBXVersion", ("I", FBX_BINARY_VERSION))
    header_ext.add("EncryptionType", ("I", 0))
    header_ext.add("Creator", ("S", FBX_CREATOR))
    file_id = _FbxNode(b"FileId", [("R", FBX_FILE_ID)])
    creation_time = _FbxNode(b"CreationTime", [("S", FBX_CREATION_TIME)])
    global_settings = _FbxNode(b"GlobalSettings")
    global_settings.add("Version", ("I", 1000))
    global_props = _fbx_properties70(global_settings)
    # Match the coordinate metadata emitted by Max FBX 2020.3.9: the file is
    # Y-up, while OriginalUpAxis records the source Max Z-up scene.
    _fbx_property70(global_props, "UpAxis", "int", "Integer", "", [1])
    _fbx_property70(global_props, "UpAxisSign", "int", "Integer", "", [1])
    _fbx_property70(global_props, "FrontAxis", "int", "Integer", "", [2])
    _fbx_property70(global_props, "FrontAxisSign", "int", "Integer", "", [1])
    _fbx_property70(global_props, "CoordAxis", "int", "Integer", "", [0])
    _fbx_property70(global_props, "CoordAxisSign", "int", "Integer", "", [1])
    _fbx_property70(global_props, "OriginalUpAxis", "int", "Integer", "", [2])
    _fbx_property70(global_props, "OriginalUpAxisSign", "int", "Integer", "", [1])
    _fbx_property70(
        global_props,
        "UnitScaleFactor",
        "double",
        "Number",
        "",
        [FBX_UNIT_SCALE_FACTOR],
    )
    _fbx_property70(
        global_props,
        "OriginalUnitScaleFactor",
        "double",
        "Number",
        "",
        [FBX_UNIT_SCALE_FACTOR],
    )

    documents = _FbxNode(b"Documents")
    documents.add("Count", ("I", 1))
    document_id = allocator.get("document:scene")
    document = documents.add("Document", ("L", document_id), ("S", _fbx_class_name("Scene", "Document")), ("S", "Scene"))
    document.add("Properties70")
    document.add("RootNode", ("L", 0))

    objects = _FbxNode(b"Objects")
    connections = _FbxNode(b"Connections")
    object_counts = {
        "Model": 0,
        "Geometry": 0,
        "NodeAttribute": 0,
        "Deformer": 0,
        "Pose": 0,
        "Material": 0,
        "Texture": 0,
        "Video": 0,
    }
    bone_count = len(scene.get("bones", []))
    mesh_cluster_contracts: dict[int, tuple[list[int], dict[int, tuple[list[int], list[float]]]]] = {}
    mesh_skin_bone_limits: dict[int, int] = {}
    deforming_bone_slots: set[int] = set()
    for mesh in scene.get("meshes", []):
        skin_bone_limit = _scene_mesh_skin_bone_limit(mesh, bone_count)
        if skin_bone_limit is None:
            continue
        slot = int(mesh["physical_slot"])
        cluster_contract = _collect_mesh_cluster_rows(mesh, bone_count)
        mesh_cluster_contracts[slot] = cluster_contract
        mesh_skin_bone_limits[slot] = skin_bone_limit
        deforming_bone_slots.update(cluster_contract[0])

    def add_model(key: str, name: str, model_type: str, **kwargs: Any) -> int:
        object_id = allocator.get("model:" + key)
        _fbx_add_model(objects, object_id=object_id, name=name, model_type=model_type, **kwargs)
        object_counts["Model"] += 1
        return object_id

    def add_null(key: str, name: str, *, connect_to_root: bool = False, **kwargs: Any) -> int:
        model_id = add_model(key, name, "Null", **kwargs)
        if connect_to_root:
            _fbx_connection(connections, model_id, 0)
        return model_id

    # Bounds helpers are top-level in V4 and deliberately use raw MOD bounds
    # coordinates rather than Mesh/Bone axis remaps.
    bounds = scene.get("bounds", [[0.0, 0.0, 0.0, 0.0]] * 3)
    bound_names = [
        str(value or "")
        for value in scene.get("scene_helpers", {}).get(
            "bounds", ["BoundSphere", "BoundBoxMin", "BoundBoxMax"]
        )
    ]
    if len(bound_names) != 3 or any(not name for name in bound_names):
        raise ValueError("Import scene must declare exactly three non-empty Bounds node names")
    bound_sphere_name, bound_box_min_name, bound_box_max_name = bound_names
    sphere_vertices, sphere_faces = _build_bound_sphere_geometry(bounds[0][3] if len(bounds[0]) > 3 else 0.0)
    sphere_geometry_id = allocator.get("geometry:boundsphere")
    _fbx_add_geometry(objects, object_id=sphere_geometry_id, name=bound_sphere_name, vertices=sphere_vertices, faces=sphere_faces, uv1=None, normals=None, smooth=True)
    object_counts["Geometry"] += 1
    sphere_model_id = add_model(
        "boundsphere",
        bound_sphere_name,
        "Mesh",
        matrix=_matrix_multiply(_translation_matrix(bounds[0][:3]), MAX_TO_FBX_YUP_MATRIX),
        use_pre_rotation=True,
        custom_properties={
            "CodexRe6BoundRadius": _format_max_user_float(bounds[0][3] if len(bounds[0]) > 3 else 0.0),
            "CodexRe6ImportContractId": scene["contract_id"],
            "CodexRe6RequestSha256": scene["request_contract_sha256"],
        },
        hidden=True,
        frozen=True,
    )
    _fbx_connection(connections, sphere_geometry_id, sphere_model_id)
    _fbx_connection(connections, sphere_model_id, 0)
    add_null(
        "boundboxmin",
        bound_box_min_name,
        connect_to_root=True,
        matrix=_matrix_multiply(_translation_matrix(bounds[1][:3]), MAX_TO_FBX_YUP_MATRIX),
        use_pre_rotation=True,
        hidden=True,
        frozen=True,
        custom_properties={
            "CodexRe6ImportContractId": scene["contract_id"],
            "CodexRe6RequestSha256": scene["request_contract_sha256"],
        },
    )
    add_null(
        "boundboxmax",
        bound_box_max_name,
        connect_to_root=True,
        matrix=_matrix_multiply(_translation_matrix(bounds[2][:3]), MAX_TO_FBX_YUP_MATRIX),
        use_pre_rotation=True,
        hidden=True,
        frozen=True,
        custom_properties={
            "CodexRe6ImportContractId": scene["contract_id"],
            "CodexRe6RequestSha256": scene["request_contract_sha256"],
        },
    )

    bone_model_ids: dict[int, int] = {}
    bone_max_world_matrices: dict[int, list[float]] = {}
    bone_world_matrices: dict[int, list[float]] = {}
    for bone in scene.get("bones", []):
        slot = int(bone["slot"])
        bone_matrix = [float(value) for value in bone["max_local_matrix"]]
        is_root_bone = not bone.get("parent_slot")
        if is_root_bone:
            bone_matrix = _matrix_multiply(bone_matrix, MAX_TO_FBX_YUP_MATRIX)
        is_deforming_bone = slot in deforming_bone_slots
        model_id = add_model(
            f"bone:{slot}",
            bone["name"],
            "LimbNode" if is_deforming_bone else "Null",
            matrix=bone_matrix,
            use_pre_rotation=is_root_bone,
            custom_properties={
                "CodexV4ExportBoneSlot": slot - 1,
                "CodexV4AnimMapId": int(bone["anim_map_id"]),
                "CodexRe6ImportContractId": scene["contract_id"],
                "CodexRe6RequestSha256": scene["request_contract_sha256"],
            },
        )
        if is_deforming_bone:
            attribute_id = allocator.get(f"attr:limb:{slot}")
            _fbx_add_limb_attribute(objects, object_id=attribute_id, name=bone["name"])
            _fbx_connection(connections, attribute_id, model_id)
            object_counts["NodeAttribute"] += 1
        bone_model_ids[slot] = model_id
        bone_max_world_matrices[slot] = [float(value) for value in bone["max_world_matrix"]]
        bone_world_matrices[slot] = _matrix_multiply(
            bone_max_world_matrices[slot],
            MAX_TO_FBX_YUP_MATRIX,
        )
    for bone in scene.get("bones", []):
        parent_slot = bone.get("parent_slot")
        _fbx_connection(connections, bone_model_ids[int(bone["slot"])], bone_model_ids.get(int(parent_slot), 0) if parent_slot else 0)

    helper_model_ids: dict[str, int] = {}
    for helper_name in scene.get("scene_helpers", {}).get("lod_groups", []):
        helper_model_ids[helper_name] = add_null(
            "helper:" + helper_name,
            helper_name,
            connect_to_root=True,
            matrix=MAX_TO_FBX_YUP_MATRIX,
            use_pre_rotation=True,
            custom_properties={
                "CodexRe6ImportContractId": scene["contract_id"],
                "CodexRe6RequestSha256": scene["request_contract_sha256"],
            },
        )
    other_mesh_name = scene.get("scene_helpers", {}).get("other_mesh", "OtherMesh")
    helper_model_ids[other_mesh_name] = add_null(
        "helper:" + other_mesh_name,
        other_mesh_name,
        connect_to_root=True,
        matrix=MAX_TO_FBX_YUP_MATRIX,
        use_pre_rotation=True,
        custom_properties={
            "CodexRe6ImportContractId": scene["contract_id"],
            "CodexRe6RequestSha256": scene["request_contract_sha256"],
        },
    )

    normalized_mrl_bindings: dict[int, dict[str, Any]] = {}
    for raw_slot, raw_binding in dict(mrl_bindings or {}).items():
        try:
            slot = int(raw_slot)
        except (TypeError, ValueError):
            continue
        if slot > 0 and isinstance(raw_binding, dict):
            normalized_mrl_bindings[slot] = dict(raw_binding)
    embedded_media_by_sha256: dict[str, bytes] = {}
    for binding in normalized_mrl_bindings.values():
        image_path = _windows_lexical_full_path(str(binding.get("image_path", "") or ""))
        if not image_path.is_file():
            raise FileNotFoundError(f"MRL embedded texture image does not exist: {image_path}")
        content = image_path.read_bytes()
        if content[:4] != b"DDS ":
            raise ValueError(f"MRL embedded texture is not DDS media: {image_path}")
        actual_sha256 = hashlib.sha256(content).hexdigest()
        expected_sha256 = str(binding.get("image_sha256", "") or "").casefold()
        if expected_sha256 and actual_sha256 != expected_sha256:
            raise ValueError(
                "MRL embedded texture changed after binding plan: "
                f"{image_path} expected={expected_sha256} actual={actual_sha256}"
            )
        binding["image_sha256"] = actual_sha256
        binding["image_size"] = len(content)
        embedded_media_by_sha256.setdefault(actual_sha256, content)

    root_mesh_scale = [float(value) for value in scene.get("root_mesh_scale", [1.0, 1.0, 1.0])]
    mesh_bind_matrices: dict[int, list[float]] = {}
    mesh_model_ids: dict[int, int] = {}
    mesh_geometry_ids: dict[int, int] = {}
    embedded_media_nodes: dict[str, tuple[int, int, str, str]] = {}
    for mesh in scene.get("meshes", []):
        slot = int(mesh["physical_slot"])
        mrl_binding = normalized_mrl_bindings.get(slot)
        geometry_id = allocator.get(f"geometry:mesh:{slot}")
        mesh_geometry_ids[slot] = geometry_id
        mesh_faces = mesh["faces"]
        normal_rows = mesh.get("max_normals") if include_normals else None
        loop_normal_rows = None
        if include_normals and normalized_normal_profile == FBX_NORMAL_PROFILE_BLENDER_SAFE:
            # Blender already removes these compatibility faces. Omitting them
            # before writing keeps its direct normal layer index-aligned while
            # CodexRe6SourceFaceCount preserves the original MOD fact.
            mesh_faces = _fbx_blender_renderable_faces(
                mesh.get("positions", []), mesh_faces
            )
            loop_normal_rows = _fbx_blender_safe_loop_normals(
                mesh.get("positions", []),
                mesh_faces,
                normal_rows,
            )
            normal_rows = None
        _fbx_add_geometry(
            objects,
            object_id=geometry_id,
            name=mesh["node_name"],
            vertices=mesh["positions"],
            faces=mesh_faces,
            uv1=mesh.get("uv1"),
            normals=normal_rows,
            loop_normals=loop_normal_rows,
            smooth=False,
            material_index=0 if mrl_binding is not None else None,
        )
        object_counts["Geometry"] += 1
        source_name = str(scene["source"].get("name", "") or "").replace('"', "'")
        mesh_user_properties: dict[str, str | int | float] = {
            "CodexV4ImportedMeshIndex": slot,
            "CodexV4DisplayMeshIndex": int(mesh["display_slot"]),
            # Blender's FBX importer shortens long Object names. Keep the
            # complete RE6 Header in a user property so BPY never has to
            # reconstruct Type/DisplayMode from Blender's display alias.
            "CodexRe6FullMeshHeader": f'"{str(mesh["node_name"]).replace(chr(34), chr(39))}"',
            # Blender's FBX reader can discard zero-area compatibility faces.
            # Preserve the source fact so Blender exports can keep this Mesh
            # header-only instead of treating Blender's reduced topology as
            # editable RE6 geometry.
            "CodexRe6SourceFaceCount": int(len(mesh.get("faces", []))),
            "CodexRe6SourceInvalidFaceCount": int(mesh.get("invalid_face_count", 0) or 0),
            "CodexRe6ImportContractId": scene["contract_id"],
            "CodexRe6RequestSha256": scene["request_contract_sha256"],
            "CodexRe6ImportSourceSha256": scene["source"]["sha256"],
            "CodexRe6ImportSourceName": f'"{source_name}"',
            "CodexRe6NormalRouteFile": route_file_name,
            # UDP3DSMAX evaluates unquoted 0x values while importing.  Max
            # turns low values into decimal Integers and high-bit values
            # into precision-losing scientific-notation Floats.  Preserve
            # the exact 32-bit FVF as a quoted user-property string.
            "CodexRe6SourceFVF": f'"0x{int(mesh["source_fvf"]):08X}"',
            "CodexRe6ParserFVF": f'"0x{int(mesh["parser_fvf"]):08X}"',
            "CodexRe6FixProcessingMode": f'"{_normalize_fix_processing_mode(scene.get("options", {}).get("fix_processing_mode", FIX_PROCESSING_MODE_CODEX))}"',
        }
        if slot in mesh_skin_bone_limits:
            mesh_user_properties["CodexRe6SkinBoneLimit"] = mesh_skin_bone_limits[slot]
        model_id = add_model(
            f"mesh:{slot}",
            mesh["node_name"],
            "Mesh",
            scale=root_mesh_scale,
            custom_properties=mesh_user_properties,
        )
        mesh_model_ids[slot] = model_id
        mesh_bind_matrices[slot] = _matrix_multiply(_scale_matrix(root_mesh_scale), MAX_TO_FBX_YUP_MATRIX)
        _fbx_connection(connections, geometry_id, model_id)
        _fbx_connection(connections, model_id, helper_model_ids.get(mesh["parent_name"], 0))
        if mrl_binding is not None:
            image_sha256 = str(mrl_binding["image_sha256"])
            media_content = embedded_media_by_sha256[image_sha256]
            material_id = allocator.get(f"material:mesh:{slot}")
            media_node = embedded_media_nodes.get(image_sha256)
            media_created = media_node is None
            if media_node is None:
                # FBX Video is the media owner. Use stable, unique IDs and
                # names for a real shared media graph instead of emitting one
                # blank duplicate Video per Mesh that happens to use the same
                # DDS.
                media_name = f"MRL_{image_sha256}"
                relative_filename = f"embedded_media/{image_sha256}.dds"
                media_node = (
                    allocator.get(f"texture:media:{image_sha256}"),
                    allocator.get(f"video:media:{image_sha256}"),
                    media_name,
                    relative_filename,
                )
                embedded_media_nodes[image_sha256] = media_node
            texture_id, video_id, media_name, relative_filename = media_node
            _fbx_add_embedded_base_color_bundle(
                objects,
                material_id=material_id,
                texture_id=texture_id,
                video_id=video_id,
                mesh_name=str(mesh["node_name"]),
                media_name=media_name,
                relative_filename=relative_filename,
                embedded_content=media_content if media_created else None,
                emit_media=media_created,
            )
            _fbx_connection(connections, material_id, model_id)
            _fbx_property_connection(connections, texture_id, material_id, "DiffuseColor")
            if media_created:
                _fbx_connection(connections, video_id, texture_id)
            object_counts["Material"] += 1
            if media_created:
                object_counts["Texture"] += 1
                object_counts["Video"] += 1

    # Max's own export writes one scene BindPose, not one Pose per Mesh.  The
    # golden FBX omitted two non-deforming bone ancestors and every skinned
    # Mesh, which is why Max reports both "nodes parents" and "initial pose"
    # warnings when that file is re-imported.  Keep the single-pose structure
    # but include every skeleton node, each skinned Mesh and its helper parent.
    skinned_mesh_slots = sorted(mesh_cluster_contracts)
    if skinned_mesh_slots:
        pose_helper_names = sorted(
            {
                str(mesh["parent_name"])
                for mesh in scene.get("meshes", [])
                if int(mesh["physical_slot"]) in mesh_cluster_contracts
            }
        )
        pose_node_count = len(bone_model_ids) + len(skinned_mesh_slots) + len(pose_helper_names)
        pose_id = allocator.get("pose:scene")
        pose = objects.add("Pose", ("L", pose_id), ("S", _fbx_class_name("BIND_POSES", "Pose")), ("S", "BindPose"))
        pose.add("Type", ("S", "BindPose"))
        pose.add("Version", ("I", 100))
        pose.add("NbPoseNodes", ("I", pose_node_count))
        for bone_slot in sorted(bone_model_ids):
            bone_pose = pose.add("PoseNode")
            bone_pose.add("Node", ("L", bone_model_ids[bone_slot]))
            bone_pose.add("Matrix", ("d", _matrix_to_fbx_array(bone_world_matrices[bone_slot])))
        for helper_name in pose_helper_names:
            helper_pose = pose.add("PoseNode")
            helper_pose.add("Node", ("L", helper_model_ids[helper_name]))
            helper_pose.add("Matrix", ("d", _matrix_to_fbx_array(MAX_TO_FBX_YUP_MATRIX)))
        for slot in skinned_mesh_slots:
            mesh_pose = pose.add("PoseNode")
            mesh_pose.add("Node", ("L", mesh_model_ids[slot]))
            mesh_pose.add("Matrix", ("d", _matrix_to_fbx_array(mesh_bind_matrices[slot])))
        object_counts["Pose"] += 1

    root_mesh_scale_matrix = _scale_matrix(root_mesh_scale)
    for mesh in scene.get("meshes", []):
        slot = int(mesh["physical_slot"])
        cluster_contract = mesh_cluster_contracts.get(slot)
        if cluster_contract is None:
            continue
        cluster_order, clusters = cluster_contract
        skin_id = allocator.get(f"skin:mesh:{slot}")
        skin = objects.add("Deformer", ("L", skin_id), ("S", _fbx_class_name(mesh["node_name"], "Deformer")), ("S", "Skin"))
        skin.add("Version", ("I", 101))
        skin.add("Link_DeformAcuracy", ("D", 50.0))
        _fbx_connection(connections, skin_id, mesh_geometry_ids[slot])
        object_counts["Deformer"] += 1
        for bone_slot in cluster_order:
            indexes, weights = clusters[bone_slot]
            cluster_id = allocator.get(f"cluster:mesh:{slot}:bone:{bone_slot}")
            cluster = objects.add("Deformer", ("L", cluster_id), ("S", _fbx_class_name(f"{mesh['node_name']}_{bone_slot}", "SubDeformer")), ("S", "Cluster"))
            cluster.add("Version", ("I", 100))
            cluster.add("UserData", ("S", ""), ("S", ""))
            cluster.add("Indexes", ("i", indexes))
            cluster.add("Weights", ("d", weights))
            # This is the matrix emitted by Max 2026 for the V4 scene.  It is
            # deliberately Max-space and bone-specific; using the Mesh world
            # matrix here bends the model as soon as Max evaluates the Skin.
            cluster_transform = _matrix_multiply(
                root_mesh_scale_matrix,
                _matrix_inverse(bone_max_world_matrices[bone_slot]),
            )
            cluster.add("Transform", ("d", _matrix_to_fbx_array(cluster_transform)))
            cluster.add("TransformLink", ("d", _matrix_to_fbx_array(bone_world_matrices[bone_slot])))
            _fbx_connection(connections, cluster_id, skin_id)
            _fbx_connection(connections, bone_model_ids[bone_slot], cluster_id)
            object_counts["Deformer"] += 1

    definitions = _FbxNode(b"Definitions")
    definitions.add("Version", ("I", 100))
    definitions.add("Count", ("I", sum(object_counts.values())))
    for object_type, count in object_counts.items():
        if count:
            definition = definitions.add("ObjectType", ("S", object_type))
            definition.add("Count", ("I", count))
    takes = _FbxNode(b"Takes")
    takes.add("Current", ("S", ""))
    return [header_ext, file_id, creation_time, global_settings, documents, definitions, objects, connections, takes]


def write_import_fbx(
    scene: dict[str, Any],
    output_path: str | Path,
    *,
    include_normals: bool = True,
    route_file_name: str = "",
    mrl_bindings: dict[int, dict[str, Any]] | None = None,
    normal_profile: str = FBX_NORMAL_PROFILE_MAX,
    bug_control: Any | None = None,
) -> Path:
    """Write an FBX 7.4 scene with Max or Blender-safe explicit normals."""
    if bug_control is not None:
        bug_control.advance("fbx_build")
    target = _windows_lexical_full_path(output_path)
    roots = _build_fbx_roots(
        scene,
        include_normals=include_normals,
        route_file_name=route_file_name,
        mrl_bindings=mrl_bindings,
        normal_profile=normal_profile,
    )
    _require_all_fbx_models_reachable(roots)
    _write_fbx_binary(target, roots)
    return target


# END STANDARD-LIBRARY FBX 7.4 BINARY ENCODER
# ============================================================================


# ============================================================================
# BEGIN IMPORT ARTIFACT TRANSACTION AND SELF-TEST
#
# Every successful FBX build commits a mandatory normal/tangent audit sidecar.
# Current V4 does not use it to construct Edit_Normals or repair modifiers.
# ============================================================================
def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _atomic_json_write(path: Path, value: Any, *, pretty: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    runtime_write_json_file(path, value, pretty=pretty)


def default_normal_route_path(output_fbx: str | Path) -> Path:
    target = _windows_lexical_full_path(output_fbx)
    return target.with_suffix(target.suffix + ".codex_re6_normal_route.json")


def default_manifest_path(output_fbx: str | Path) -> Path:
    target = _windows_lexical_full_path(output_fbx)
    return target.with_suffix(target.suffix + ".codex_re6_import_manifest.json")


def _build_manifest_receipt(
    scene: dict[str, Any],
    *,
    output_fbx: Path,
    route_path: Path,
    route_sha256: str,
    route_file_sha256: str,
    include_normals: bool,
    mrl_texture_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    receipt = {
        **scene,
        "artifacts": {
            "fbx_path": str(output_fbx),
            "fbx_sha256": _sha256_file(output_fbx),
            "normal_route_path": str(route_path),
            "normal_route_sha256": route_sha256,
            "normal_route_file_sha256": route_file_sha256,
        },
        "fbx_policy": {
            "binary_version": FBX_BINARY_VERSION,
            "unit_meters": FBX_UNIT_METERS,
            "embedded_explicit_normals": bool(include_normals),
            "normal_route_always_emitted": True,
            "tangents_embedded": False,
            "uv_channels_embedded": [1],
        },
    }
    if mrl_texture_plan is not None:
        bindings = [
            {
                "physical_slot": int(slot),
                "shader": str(binding.get("shader", "") or ""),
                "resource": str(binding.get("resource", "") or ""),
                "source_texture_path": str(binding.get("source_texture_path", "") or ""),
                "image_sha256": str(binding.get("image_sha256", "") or ""),
                "image_size": int(binding.get("image_size", 0) or 0),
                "embedded_relative_filename": (
                    "embedded_media/"
                    + str(binding.get("image_sha256", "") or "")
                    + ".dds"
                ),
            }
            for slot, binding in sorted(
                dict(mrl_texture_plan.get("bindings_by_mesh_slot", {})).items(),
                key=lambda item: int(item[0]),
            )
            if isinstance(binding, dict)
        ]
        receipt["mrl_embedded_base_color"] = {
            "schema": str(mrl_texture_plan.get("schema", "") or ""),
            "mrl_path": str(mrl_texture_plan.get("mrl_path", "") or ""),
            "texture_mode": str(mrl_texture_plan.get("texture_mode", "") or ""),
            "source_mesh_count": int(mrl_texture_plan.get("source_mesh_count", 0) or 0),
            "binding_count": len(bindings),
            "unique_embedded_media_count": len(
                {entry["image_sha256"] for entry in bindings if entry["image_sha256"]}
            ),
            "bindings": bindings,
        }
        receipt["fbx_policy"]["mrl_base_color_materials_embedded"] = True
        receipt["fbx_policy"]["mrl_binding_count"] = len(bindings)
    return receipt


def _normal_route_identity_payload(route: dict[str, Any]) -> dict[str, Any]:
    """Strip machine-local artifact paths before hashing the route contract."""
    identity = dict(route)
    identity.pop("artifacts", None)
    identity.pop("route_payload_sha256", None)
    return identity


def _artifact_path_identity(path: Path) -> str:
    """Return the case-insensitive lexical identity used by the artifact guard."""
    return os.path.normcase(os.path.normpath(str(path))).casefold()


def _raise_artifact_path_collision(artifacts: dict[str, Path]) -> None:
    identities: dict[str, list[str]] = {}
    for name, path in artifacts.items():
        identities.setdefault(_artifact_path_identity(path), []).append(name)
    collisions = [names for names in identities.values() if len(names) > 1]
    if not collisions:
        return
    collision_names = [name for names in collisions for name in names]
    collision_text = "; ".join(
        "/".join(names) + "=" + str(artifacts[names[0]])
        for names in collisions
    )
    diagnostic = {
        "schema": "re6-import-artifact-path-diagnostic-v1",
        "status": "ERROR",
        "error_code": "artifact_path_collision",
        "artifact_path_collision": True,
        "colliding_artifacts": collision_names,
        "paths": {name: str(path) for name, path in artifacts.items()},
    }
    error = ValueError(
        f"artifact_path_collision: normalized artifact paths must be distinct ({collision_text})"
    )
    error.diagnostic = diagnostic  # type: ignore[attr-defined]
    raise error


def _build_import_artifacts_impl(
    mod_path: str | Path,
    output_fbx: str | Path,
    *,
    normal_route_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
    include_normals: bool = True,
    fix_lp2: bool = False,
    fix_dmc: bool = False,
    fix_processing_mode: str = FIX_PROCESSING_MODE_CODEX,
    reserved_scene_names: Iterable[str] | None = None,
    blender_compact_mesh_names: bool = False,
    mrl_path: str | Path | None = None,
    texture_mode: str = "dds",
    texture_roots: Iterable[str | Path] = (),
    pretty_json: bool = False,
    bug_control: Any | None = None,
) -> dict[str, Any]:
    """Atomically build the FBX, mandatory normal route and full manifest."""
    output = _windows_lexical_full_path(output_fbx)
    route_path = _windows_lexical_full_path(normal_route_path) if normal_route_path else default_normal_route_path(output)
    manifest = _windows_lexical_full_path(manifest_path) if manifest_path else default_manifest_path(output)
    _raise_artifact_path_collision(
        {
            "output_fbx": output,
            "normal_route": route_path,
            "manifest": manifest,
        }
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    scene = build_import_scene(
        mod_path,
        include_normals=include_normals,
        fix_lp2=fix_lp2,
        fix_dmc=fix_dmc,
        fix_processing_mode=fix_processing_mode,
        reserved_scene_names=reserved_scene_names,
        blender_compact_mesh_names=bool(blender_compact_mesh_names),
        bug_control=bug_control,
    )
    mrl_texture_plan: dict[str, Any] | None = None
    mrl_bindings: dict[int, dict[str, Any]] | None = None
    decode_workspace: tempfile.TemporaryDirectory[str] | None = None
    if mrl_path is not None:
        if bug_control is not None:
            bug_control.advance("texture_plan")
        # Keep transient TEX-to-DDS media beside the generated FBX.  Launcher
        # supplies its active log directory as output.parent, never %TEMP%.
        decode_workspace = tempfile.TemporaryDirectory(
            prefix="codex_re6_mrl_fbx_",
            dir=str(output.parent),
        )
        try:
            mrl_texture_plan = _build_mrl_embedded_texture_plan(
                mod_path,
                mrl_path,
                texture_mode=texture_mode,
                decode_directory=Path(decode_workspace.name),
                texture_roots=texture_roots,
            )
            mrl_bindings = {
                int(slot): dict(binding)
                for slot, binding in dict(
                    mrl_texture_plan.get("bindings_by_mesh_slot", {})
                ).items()
                if isinstance(binding, dict)
            }
            if not mrl_bindings:
                raise ValueError(
                    "MRL did not resolve any Base Color DDS/TEX binding for this MOD"
                )
        except Exception:
            decode_workspace.cleanup()
            raise
    if bug_control is not None:
        bug_control.advance("artifact_route")
    route = build_normal_route_table(scene)
    route.update(
        {
            "artifacts": {
                "fbx_path": str(output),
                "route_path": str(route_path),
                "manifest_path": str(manifest),
            },
            "fbx_embedded_explicit_normals": bool(include_normals),
        }
    )
    route_without_checksum = _canonical_json_bytes(_normal_route_identity_payload(route))
    route_sha256 = hashlib.sha256(route_without_checksum).hexdigest()
    route["route_payload_sha256"] = route_sha256

    # The FBX contains only the stable route file name and contract ID.  Large
    # normal arrays remain in the sidecar instead of bloating Max user props.
    try:
        write_import_fbx(
            scene,
            output,
            include_normals=include_normals,
            route_file_name=route_path.name,
            mrl_bindings=mrl_bindings,
            bug_control=bug_control,
        )
    finally:
        if decode_workspace is not None:
            decode_workspace.cleanup()
    if bug_control is not None:
        bug_control.advance("artifact_sidecars")
    _atomic_json_write(route_path, route, pretty=pretty_json)
    route_file_sha256 = _sha256_file(route_path)
    receipt = _build_manifest_receipt(
        scene,
        output_fbx=output,
        route_path=route_path,
        route_sha256=route_sha256,
        route_file_sha256=route_file_sha256,
        include_normals=include_normals,
        mrl_texture_plan=mrl_texture_plan,
    )
    _atomic_json_write(manifest, receipt, pretty=pretty_json)
    manifest_sha256 = _sha256_file(manifest)
    return {
        "status": "OK",
        "contract_revision": IMPORT_MODULE_CONTRACT_REVISION,
        "contract_id": scene["contract_id"],
        "request_contract_sha256": scene["request_contract_sha256"],
        "source_mod": str(_windows_lexical_full_path(mod_path)),
        "source_sha256": scene["source"]["sha256"],
        "output_fbx": str(output),
        "normal_route": str(route_path),
        "manifest": str(manifest),
        "embedded_explicit_normals": bool(include_normals),
        "mesh_count": len(scene.get("meshes", [])),
        "bone_count": len(scene.get("bones", [])),
        "fbx_sha256": _sha256_file(output),
        "route_payload_sha256": route_sha256,
        "normal_route_file_sha256": route_file_sha256,
        "manifest_sha256": manifest_sha256,
        "fix_lp2": bool(fix_lp2),
        "fix_dmc": bool(fix_dmc),
        "fix_processing_mode": scene["options"]["fix_processing_mode"],
        "import_name_plan": dict(scene.get("import_name_plan", {})),
        "mrl_embedded_base_color": (
            {
                "mrl_path": str(mrl_texture_plan.get("mrl_path", "") or ""),
                "texture_mode": str(mrl_texture_plan.get("texture_mode", "") or ""),
                "binding_count": len(mrl_bindings or {}),
                "unique_embedded_media_count": len(
                    {
                        str(binding.get("image_sha256", "") or "")
                        for binding in (mrl_bindings or {}).values()
                        if str(binding.get("image_sha256", "") or "")
                    }
                ),
            }
            if mrl_texture_plan is not None
            else None
        ),
    }


def build_import_artifacts(
    mod_path: str | Path,
    output_fbx: str | Path,
    *,
    normal_route_path: str | Path | None = None,
    manifest_path: str | Path | None = None,
    include_normals: bool = True,
    fix_lp2: bool = False,
    fix_dmc: bool = False,
    fix_processing_mode: str = FIX_PROCESSING_MODE_CODEX,
    reserved_scene_names: Iterable[str] | None = None,
    blender_compact_mesh_names: bool = False,
    mrl_path: str | Path | None = None,
    texture_mode: str = "dds",
    texture_roots: Iterable[str | Path] = (),
    pretty_json: bool = False,
) -> dict[str, Any]:
    """Build the import artifacts; Launcher owns operation-wide BUG control."""
    return _build_import_artifacts_impl(
        mod_path,
        output_fbx,
        normal_route_path=normal_route_path,
        manifest_path=manifest_path,
        include_normals=include_normals,
        fix_lp2=fix_lp2,
        fix_dmc=fix_dmc,
        fix_processing_mode=fix_processing_mode,
        reserved_scene_names=reserved_scene_names,
        blender_compact_mesh_names=blender_compact_mesh_names,
        mrl_path=mrl_path,
        texture_mode=texture_mode,
        texture_roots=texture_roots,
        pretty_json=pretty_json,
        bug_control=None,
    )


def verify_fbx_artifact(output_fbx: str | Path, expected: dict[str, Any]) -> dict[str, Any]:
    """Optionally verify a generated FBX through the existing ufbx probe."""
    try:
        import codex_fbx_probe

        summary = codex_fbx_probe.summarize_fbx(output_fbx)
    except Exception as exc:
        return {
            "status": "UNAVAILABLE",
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    stats = summary.get("stats", {})
    expected_meshes = int(expected.get("mesh_count", 0)) + 1  # BoundSphere is geometry.
    expected_bones = int(expected.get("bone_count", 0))
    errors: list[str] = []
    if int(stats.get("mesh_count", -1)) != expected_meshes:
        errors.append(f"mesh_count expected {expected_meshes}, got {stats.get('mesh_count')}")
    if int(stats.get("bone_count", -1)) != expected_bones:
        errors.append(f"bone_count expected {expected_bones}, got {stats.get('bone_count')}")
    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "stats": stats,
    }


def _guard_fbx_object_name(node: _FbxNode) -> str:
    if len(node.props) < 2 or node.props[1][0] != "S":
        return ""
    return str(node.props[1][1]).split("\x00", 1)[0]


def _guard_fbx_objects_and_edges(
    roots: Sequence[_FbxNode],
) -> tuple[dict[int, _FbxNode], set[tuple[int, int]]]:
    objects = next(root for root in roots if root.name == b"Objects")
    connections = next(root for root in roots if root.name == b"Connections")
    by_id = {
        int(node.props[0][1]): node
        for node in objects.children
        if node.props and node.props[0][0] == "L"
    }
    edges = {
        (int(node.props[1][1]), int(node.props[2][1]))
        for node in connections.children
        if node.name == b"C" and len(node.props) >= 3 and node.props[0] == ("S", "OO")
    }
    return by_id, edges


def _guard_fbx_model_translation(roots: Sequence[_FbxNode], model_name: str) -> list[float]:
    by_id, _edges = _guard_fbx_objects_and_edges(roots)
    model = next(
        node
        for node in by_id.values()
        if node.name == b"Model" and _guard_fbx_object_name(node) == model_name
    )
    properties = next(child for child in model.children if child.name == b"Properties70")
    row = next(
        child
        for child in properties.children
        if child.name == b"P" and child.props and child.props[0] == ("S", "Lcl Translation")
    )
    return [float(value[1]) for value in row.props[-3:]]


def _guard_require_fbx_mesh_route(
    roots: Sequence[_FbxNode],
    *,
    node_name: str,
    parent_name: str,
    physical_slot: int,
    display_slot: int,
) -> None:
    by_id, edges = _guard_fbx_objects_and_edges(roots)
    named = {
        _guard_fbx_object_name(node): object_id
        for object_id, node in by_id.items()
        if node.name == b"Model"
    }
    mesh_id = named.get(node_name)
    parent_id = named.get(parent_name)
    if mesh_id is None or parent_id is None or (mesh_id, parent_id) not in edges:
        raise AssertionError("parsed Mesh name/parent did not reach the final FBX graph")
    geometry_id = next(
        (
            object_id
            for object_id, node in by_id.items()
            if node.name == b"Geometry" and _guard_fbx_object_name(node) == node_name
        ),
        None,
    )
    if geometry_id is None or (geometry_id, mesh_id) not in edges:
        raise AssertionError("parsed Mesh geometry is not connected to its final FBX Model")
    model = by_id[mesh_id]
    properties = next(child for child in model.children if child.name == b"Properties70")
    udp = next(
        (
            str(row.props[-1][1])
            for row in properties.children
            if row.name == b"P" and row.props and row.props[0] == ("S", "UDP3DSMAX")
        ),
        "",
    )
    required = (
        f"CodexV4ImportedMeshIndex = {physical_slot}\r\n",
        f"CodexV4DisplayMeshIndex = {display_slot}\r\n",
    )
    if any(value not in udp for value in required):
        raise AssertionError("physical/display Mesh slots did not reach final FBX user properties")


def _guard_require_fbx_skin_graph(
    roots: Sequence[_FbxNode],
    *,
    mesh_name: str,
    bone_parents: dict[str, str | None],
    clusters: dict[str, dict[str, Sequence[float] | Sequence[int]]],
) -> None:
    by_id, edges = _guard_fbx_objects_and_edges(roots)
    named_models = {
        _guard_fbx_object_name(node): object_id
        for object_id, node in by_id.items()
        if node.name == b"Model"
    }
    for bone_name, parent_name in bone_parents.items():
        bone_id = named_models.get(bone_name)
        parent_id = 0 if parent_name is None else named_models.get(parent_name)
        if bone_id is None or parent_id is None or (bone_id, parent_id) not in edges:
            raise AssertionError(f"FBX bone hierarchy edge mismatch for {bone_name}")

    geometry_id = next(
        (
            object_id
            for object_id, node in by_id.items()
            if node.name == b"Geometry" and _guard_fbx_object_name(node) == mesh_name
        ),
        None,
    )
    skin_ids = [
        object_id
        for object_id, node in by_id.items()
        if node.name == b"Deformer" and len(node.props) > 2 and node.props[2] == ("S", "Skin")
    ]
    cluster_ids = {
        object_id
        for object_id, node in by_id.items()
        if node.name == b"Deformer" and len(node.props) > 2 and node.props[2] == ("S", "Cluster")
    }
    if geometry_id is None or len(skin_ids) != 1 or (skin_ids[0], geometry_id) not in edges:
        raise AssertionError("FBX Skin is not connected to the expected Geometry")
    skin_id = skin_ids[0]
    if len(cluster_ids) != len(clusters):
        raise AssertionError("FBX Cluster count does not match the expected deforming-bone set")

    for bone_name, expected in clusters.items():
        bone_id = named_models.get(bone_name)
        matches = [cluster_id for cluster_id in cluster_ids if bone_id is not None and (bone_id, cluster_id) in edges]
        if bone_id is None or len(matches) != 1 or (matches[0], skin_id) not in edges:
            raise AssertionError(f"FBX Bone/Cluster/Skin connection mismatch for {bone_name}")
        cluster = by_id[matches[0]]
        children = {child.name: child for child in cluster.children}
        for field_name, kind in ((b"Indexes", "i"), (b"Weights", "d"), (b"Transform", "d"), (b"TransformLink", "d")):
            actual_node = children.get(field_name)
            expected_values = list(expected[field_name.decode("ascii")])
            if actual_node is None or not actual_node.props or actual_node.props[0][0] != kind:
                raise AssertionError(f"FBX Cluster {bone_name} lost {field_name.decode('ascii')}")
            actual_values = list(actual_node.props[0][1])
            if len(actual_values) != len(expected_values) or any(
                abs(float(actual) - float(wanted)) > 1.0e-9
                for actual, wanted in zip(actual_values, expected_values)
            ):
                raise AssertionError(f"FBX Cluster {bone_name} {field_name.decode('ascii')} mismatch")


def _guard_build_mesh_route_mod(*, display_slot: int = 37) -> bytes:
    if display_slot < 1 or display_slot > 0xFFFF:
        raise ValueError("Importer guard display slot must fit the MOD uint16 CDXM field")
    bone_ptr = MOD_HEADER_PREFIX_SIZE + 48 + 20
    mesh_ptr = bone_ptr + BONE_INFO_SIZE + (2 * BONE_MATRIX_SIZE) + BONE_MTP_SIZE
    vertex_ptr = mesh_ptr + MESH_HEADER_STRUCT.size
    triangle_ptr = vertex_ptr + 3 * 20
    end_size = triangle_ptr + 6
    data = bytearray(end_size + 10)
    data[:4] = b"MOD\x00"
    struct.pack_into("<H", data, 4, 210)
    MOD_HEADER_STRUCT.pack_into(
        data, 6,
        1, 1, 0,
        3, 3, 0, 60, 0, 0,
        bone_ptr, 0, 0, mesh_ptr, vertex_ptr, triangle_ptr, end_size,
    )
    struct.pack_into(
        "<4f4f4f5I", data, MOD_HEADER_PREFIX_SIZE,
        2.0, 3.0, 4.0, 1.25,
        -5.0, 6.0, -7.0, 0.0,
        8.0, -9.0, 10.0, 0.0,
        0, 0, 0, 0, 1,
    )
    BONE_INFO_STRUCT.pack_into(data, bone_ptr, 9, 255, 255, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
    MATRIX4X4_STRUCT.pack_into(data, bone_ptr + BONE_INFO_SIZE, *_identity_matrix())
    MATRIX4X4_STRUCT.pack_into(data, bone_ptr + BONE_INFO_SIZE + BONE_MATRIX_SIZE, *_identity_matrix())
    data[bone_ptr + BONE_INFO_SIZE + 2 * BONE_MATRIX_SIZE : mesh_ptr] = bytes(range(256))
    mesh_header = MeshHeader(
        meshtype=7, vert_count=3, unk01=13, mat_id=42, lod_level=249,
        unk04=11, vert_flag=1, vert_stride=20, unk05=17,
        vert_start=0, vert_base=0, fvf_info=0x0CB68015,
        face_start=0, face_count=3, face_base=0,
        bonemapindex=0, weightmaps=0, unk07=19, unk08=23,
        min_index=0, max_index=2, unk09=0x12345678,
    )
    MESH_HEADER_STRUCT.pack_into(data, mesh_ptr, *dataclasses.astuple(mesh_header))
    packed_normal = (200, 90, 160, 255)
    for vertex_index, position in enumerate(((0, 0, 0), (32767, 0, 0), (0, 32767, 0))):
        struct.pack_into(
            "<hhhBB4B4B2e", data, vertex_ptr + vertex_index * 20,
            *position, 0, 0, *packed_normal, 127, 127, 254, 255, 0.0, 0.0,
        )
    struct.pack_into("<3H", data, triangle_ptr, 0, 1, 2)
    struct.pack_into("<4sHHH", data, end_size, b"CDXM", 1, 1, display_slot)
    return bytes(data)


def _guard_require_all_normal_fvfs_reach_fbx() -> int:
    normal_layouts = sorted(
        (fvf, layout) for fvf, layout in FVF_LAYOUTS.items() if layout.normal_offset is not None
    )
    vertex_bytes = sum(layout.read_stride for _fvf, layout in normal_layouts)
    triangle_ptr = vertex_bytes
    data = bytearray(vertex_bytes + 6 * len(normal_layouts))
    headers: list[MeshHeader] = []
    cursor = 0
    packed_normal = (200, 90, 160, 255)
    for physical_slot, (fvf, layout) in enumerate(normal_layouts, start=1):
        if layout.position_kind == "float":
            struct.pack_into("<fff", data, cursor, 0.125, -0.25, 0.5)
        else:
            struct.pack_into("<hhh", data, cursor, 1000, -2000, 3000)
        normal_offset = cursor + int(layout.normal_offset)
        data[normal_offset : normal_offset + 4] = bytes(packed_normal)
        headers.append(
            MeshHeader(
                meshtype=1, vert_count=1, unk01=2, mat_id=physical_slot,
                lod_level=0, unk04=0, vert_flag=0, vert_stride=layout.read_stride,
                unk05=3, vert_start=0, vert_base=cursor, fvf_info=fvf,
                face_start=0, face_count=3, face_base=0,
                bonemapindex=0, weightmaps=0, unk07=0, unk08=0,
                min_index=0, max_index=0, unk09=0,
            )
        )
        cursor += layout.read_stride
    for index in range(len(normal_layouts)):
        struct.pack_into("<3H", data, triangle_ptr + index * 6, 0, 0, 0)
    header = ModHeader(
        magic="MOD", mod_ver=210, bone_count=255, mesh_count=len(headers), mat_count=0,
        vert_count=len(headers), triangle_count=len(headers), vertex_ids=0,
        vertex_buffer_size=vertex_bytes, padding=0, bone_map_count=0,
        ptr_bone=0, ptr_bone_map=0, ptr_mat_id=0, ptr_mesh=0,
        ptr_vertex=0, ptr_triangle=triangle_ptr, end_size=len(data),
    )
    meshes, _display_map = _parse_meshes(
        bytes(data), header, headers, root_scale=[1.0, 1.0, 1.0],
        fix_lp2=False, fix_dmc=False, include_normals=True,
    )
    scene = {
        "contract_id": "re6mod-all-normal-fvf-fixture",
        "request_contract_sha256": "4" * 64,
        "source": {"sha256": "5" * 64},
        "bounds": [[0.0, 0.0, 0.0, 1.0], [-1.0, -1.0, -1.0, 0.0], [1.0, 1.0, 1.0, 0.0]],
        "bones": [],
        "root_mesh_scale": [1.0, 1.0, 1.0],
        "scene_helpers": {"lod_groups": ["LodGroup_0"], "other_mesh": "OtherMesh"},
        "meshes": [_as_jsonable(mesh) for mesh in meshes],
    }
    # This fixture exercises normal-layer reachability only. Its synthetic MOD
    # advertises bones so every Skin-capable FVF can be decoded, but it does not
    # build a matching FBX skeleton; keep that unrelated Skin contract out.
    for scene_mesh in scene["meshes"]:
        scene_mesh["fbx_skin_bones"] = []
        scene_mesh["fbx_skin_weights"] = []
        scene_mesh["skin_bone_limit"] = None
    roots = _build_fbx_roots(scene, include_normals=True, route_file_name="normal-fvf-route.json")
    objects = next(root for root in roots if root.name == b"Objects")
    geometry_by_name = {
        _guard_fbx_object_name(node): node
        for node in objects.children
        if node.name == b"Geometry"
    }
    expected_max_normal, _expected_game_normal = _decode_packed_normal(bytes(packed_normal), 0)
    for mesh in meshes:
        geometry = geometry_by_name.get(mesh.node_name)
        if geometry is None:
            raise AssertionError(f"normal FVF 0x{mesh.source_fvf:08X} lost final FBX Geometry")
        layer = next((child for child in geometry.children if child.name == b"LayerElementNormal"), None)
        values_node = next((child for child in (layer.children if layer else []) if child.name == b"Normals"), None)
        values = list(values_node.props[0][1]) if values_node and values_node.props else []
        if len(values) < 3 or any(
            abs(float(actual) - float(expected)) > 1.0e-12
            for actual, expected in zip(values[:3], expected_max_normal)
        ):
            raise AssertionError(f"normal FVF 0x{mesh.source_fvf:08X} did not reach final FBX values")
    return len(normal_layouts)


def run_import_maintenance_regression_suite() -> dict[str, Any]:
    """Run the strict importer regression suite only for explicit maintenance."""
    global IMPORT_MODULE_REGRESSION_STATUS
    checks: dict[str, Any] = {}
    try:
        if len(FVF_LAYOUTS) != 45:
            raise AssertionError(f"expected 45 unique V4 FVF layouts, got {len(FVF_LAYOUTS)}")
        if FVF_LAYOUTS[0x5E7F202C].position_kind != "float":
            raise AssertionError("unreachable duplicate 5E7F202C parser branch replaced the authoritative branch")
        if FVF_LAYOUTS[0xDA55A021].read_stride != 28:
            raise AssertionError("DA55A021 must consume the writer's actual 28-byte record")
        if FVF_LAYOUTS[0x77D87022].read_stride != 32:
            raise AssertionError("77D87022 must consume the writer's actual 32-byte record")
        if FVF_LAYOUTS[0xB392101F].read_stride != 36:
            raise AssertionError("B392101F must preserve V4's actual 36-byte parser consumption")
        zero_uv_fvfs = (0x2BE814D4, 0x7CD414D4, 0x0D9E801D, 0xA14E003C)
        if any(FVF_LAYOUTS[fvf].uv_kind != "zero" for fvf in zero_uv_fvfs):
            raise AssertionError("V4 no-UV FVF branches must synthesize zero-valued Map1 rows")
        blender_normal_vertices = [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
        blender_normal_faces = [[0, 1, 2], [0, 2, 3], [0, 0, 0]]
        blender_normal_source = [
            [0.0, 0.0, 1.0],
            [0.0, 0.0, 1.0],
            [0.0, 0.0, -1.0],
            [0.0, 1.0, 0.0],
        ]
        blender_renderable_faces = _fbx_blender_renderable_faces(
            blender_normal_vertices, blender_normal_faces
        )
        if blender_renderable_faces != [[0, 1, 2], [0, 2, 3]]:
            raise AssertionError("Blender-safe profile did not remove only zero-area faces")
        blender_safe_normals = _fbx_blender_safe_loop_normals(
            blender_normal_vertices,
            blender_renderable_faces,
            blender_normal_source,
        )
        if blender_safe_normals is None or len(blender_safe_normals) != 6:
            raise AssertionError("Blender-safe profile did not produce one normal per face corner")
        for face, offset in zip(blender_renderable_faces, (0, 3)):
            face_normal = _fbx_face_unit_normal(blender_normal_vertices, face)
            if face_normal is None or any(
                sum(
                    blender_safe_normals[offset + index][axis] * face_normal[axis]
                    for axis in range(3)
                ) <= 0.0
                for index in range(len(face))
            ):
                raise AssertionError("Blender-safe profile retained an inward loop normal")
        if _normalize_fbx_normal_profile("blender") != FBX_NORMAL_PROFILE_BLENDER_SAFE:
            raise AssertionError("Blender-safe normal-profile alias was rejected")
        checks["blender_safe_normal_profile"] = (
            "filter-zero-area+outward-per-loop+max-default"
        )

        fixture_header = ModHeader(
            magic="MOD",
            mod_ver=0,
            bone_count=0,
            mesh_count=1,
            mat_count=0,
            vert_count=2,
            triangle_count=0,
            vertex_ids=0,
            vertex_buffer_size=64,
            padding=0,
            bone_map_count=0,
            ptr_bone=0,
            ptr_bone_map=0,
            ptr_mat_id=0,
            ptr_mesh=0,
            ptr_vertex=16,
            ptr_triangle=96,
            end_size=96,
        )
        da55_mesh_header = MeshHeader(
            meshtype=0,
            vert_count=2,
            unk01=0,
            mat_id=0,
            lod_level=0,
            unk04=0,
            vert_flag=0,
            vert_stride=28,
            unk05=0,
            vert_start=0,
            vert_base=0,
            fvf_info=0xDA55A021,
            face_start=0,
            face_count=0,
            face_base=0,
            bonemapindex=0,
            weightmaps=0,
            unk07=0,
            unk08=0,
            min_index=0,
            max_index=1,
            unk09=0,
        )
        da55_data = bytearray(96)
        struct.pack_into(
            "<hhhh4B4B4B2e2e",
            da55_data,
            16,
            0,
            0,
            0,
            16384,
            127,
            127,
            254,
            255,
            1,
            2,
            3,
            4,
            0,
            1,
            0,
            2,
            0.25,
            0.75,
            0.25,
            0.125,
        )
        struct.pack_into(
            "<hhhh4B4B4B2e2e",
            da55_data,
            44,
            16384,
            -8192,
            4096,
            8192,
            254,
            127,
            127,
            255,
            5,
            6,
            7,
            8,
            4,
            5,
            6,
            7,
            0.5,
            0.25,
            0.375,
            0.125,
        )
        da55_meshes, _da55_display_map = _parse_meshes(
            bytes(da55_data),
            fixture_header,
            [da55_mesh_header],
            root_scale=[1.0, 1.0, 1.0],
            fix_lp2=False,
            fix_dmc=False,
            include_normals=True,
        )
        da55_mesh = da55_meshes[0]
        expected_da55_position = [16384.0 / 32767.0, -4096.0 / 32767.0, -8192.0 / 32767.0]
        if max(abs(actual - expected) for actual, expected in zip(da55_mesh.positions[1], expected_da55_position)) > 1.0e-12:
            raise AssertionError("DA55A021 second vertex did not start at the writer's 28-byte record boundary")
        if da55_mesh.game_normals[1] is None or da55_mesh.game_normals[1][0] < 0.999999:
            raise AssertionError("DA55A021 normals must remain located through the MeshHeader stride")
        if da55_mesh.raw_skin_bones[1] != [5, 6, 7, 8]:
            raise AssertionError("DA55A021 writer bone bytes were not decoded from offsets 16..19")
        expected_da55_weights = [8192.0 / 32767.0, 0.375, 0.125]
        if max(
            abs(actual - expected)
            for actual, expected in zip(da55_mesh.raw_skin_weights[1][:3], expected_da55_weights)
        ) > 1.0e-7:
            raise AssertionError("DA55A021 weights must decode as short/32767 plus two IEEE half lanes")
        if da55_mesh.uv2 != [None, None]:
            raise AssertionError("DA55A021 writer records do not contain the historical parser's extra UV2 lane")
        da55_raw_bones = da55_mesh.raw_skin_bones[0]
        da55_raw_weights = da55_mesh.raw_skin_weights[0]
        da55_expected_totals: dict[int, float] = {}
        da55_expected_order: list[int] = []
        for bone, weight in zip(da55_raw_bones, da55_raw_weights):
            if weight <= 0.0:
                continue
            if bone in da55_expected_totals:
                continue
            da55_expected_order.append(bone)
            da55_expected_totals[bone] = weight
        da55_expected_sum = sum(da55_expected_totals.values())
        da55_expected_weights = [
            da55_expected_totals[bone] / da55_expected_sum for bone in da55_expected_order
        ]
        if da55_mesh.fbx_skin_bones[0] != da55_expected_order or max(
            abs(actual - expected)
            for actual, expected in zip(da55_mesh.fbx_skin_weights[0], da55_expected_weights)
        ) > 1.0e-7:
            raise AssertionError("DA55 production parse treated V4 duplicate filler Bones as extra weight")

        legacy4_filler_bones, legacy4_filler_weights = _normalise_skin_pairs(
            [2, 3, 51, 2],
            [0.6707052828, 0.1614990234, 0.1677246094, 0.6707052828],
            max_influences=4,
            merge_duplicate_weights=False,
        )
        if legacy4_filler_bones != [2, 3, 51] or max(
            abs(actual - expected)
            for actual, expected in zip(
                legacy4_filler_weights,
                (0.6707529628, 0.1615105043, 0.1677365328),
            )
        ) > 1.0e-7:
            raise AssertionError("Legacy4 V4 filler Bone changed the first influence weight")

        expected_skin_kinds = {
            0x0CB68015: "single_6",
            0xD877801B: "single_6",
            0xCBF6C01A: "single_u16_6",
            0x667B1019: "single_u16_6",
            0x0D9E801D: "two_u16_20",
            0xDA55A021: "legacy4",
            0x77D87022: "legacy4",
            0x75C3E025: "legacy8_half",
            0xD84E3026: "legacy8_half",
        }
        if any(FVF_LAYOUTS[fvf].skin_kind != kind for fvf, kind in expected_skin_kinds.items()):
            raise AssertionError("writer-defined Skin FVF layouts lost their authoritative decoder kinds")
        v4_non_scene_skin_fvfs = (
            0x0CB68015,
            0x0CB68016,
            0x667B1019,
            0x0D9E801D,
            0x75C3E025,
            0xD84E3026,
        )
        if (
            any(FVF_LAYOUTS[fvf].create_scene_skin for fvf in v4_non_scene_skin_fvfs)
            or not FVF_LAYOUTS[0xA8FAB018].create_scene_skin
            or not FVF_LAYOUTS[0x14D40020].create_scene_skin
        ):
            raise AssertionError("V4 non-Skin vertex fields were promoted to scene Skin semantics")
        if (
            FVF_LAYOUTS[0x75C3E025].uv_offset != 24
            or FVF_LAYOUTS[0x75C3E025].uv2_offset != 36
            or FVF_LAYOUTS[0xD84E3026].uv_offset != 24
            or FVF_LAYOUTS[0xD84E3026].uv2_offset is not None
        ):
            raise AssertionError("legacy8 UV fields no longer match the 40-byte writer records")

        palette_header = dataclasses.replace(
            fixture_header,
            bone_count=16,
            vert_count=1,
            vertex_buffer_size=20,
            bone_map_count=1,
            ptr_bone_map=64,
            ptr_triangle=96,
            end_size=96,
        )
        palette_mesh_header = dataclasses.replace(
            da55_mesh_header,
            vert_count=1,
            vert_stride=20,
            fvf_info=0x0CB68015,
            bonemapindex=1,
            weightmaps=1,
            max_index=0,
        )
        palette_data = bytearray(96)
        palette_data[22] = 2
        palette_data[24:28] = bytes((127, 127, 254, 255))
        BONE_MAP_ROW_STRUCT.pack_into(palette_data, 64, 9, 8, 7, 6, 5, 4, 3, 2)
        palette_meshes, _palette_display_map = _parse_meshes(
            bytes(palette_data),
            palette_header,
            [palette_mesh_header],
            root_scale=[1.0, 1.0, 1.0],
            fix_lp2=False,
            fix_dmc=False,
            include_normals=True,
        )
        if (
            palette_meshes[0].raw_skin_bones != [[8]]
            or palette_meshes[0].fbx_skin_bones != [None]
            or palette_meshes[0].parent_name != "LodGroup_0"
        ):
            raise AssertionError(
                "0CB static LOD 0 must remain under LodGroup_0 without creating a scene Skin"
            )
        direct_meshes, _direct_display_map = _parse_meshes(
            bytes(palette_data),
            palette_header,
            [dataclasses.replace(palette_mesh_header, bonemapindex=0, weightmaps=0)],
            root_scale=[1.0, 1.0, 1.0],
            fix_lp2=False,
            fix_dmc=False,
            include_normals=False,
        )
        if direct_meshes[0].raw_skin_bones != [[3]]:
            raise AssertionError("bonemapindex=0 must retain direct global bone-byte semantics")

        alias_header = dataclasses.replace(
            palette_header,
            vertex_buffer_size=48,
            ptr_bone_map=96,
            ptr_triangle=128,
            end_size=128,
        )
        alias_mesh_header = dataclasses.replace(
            palette_mesh_header,
            vert_stride=48,
            fvf_info=0xCBCF7027,
        )
        alias_data = bytearray(128)
        struct.pack_into("<h", alias_data, 22, 6553)
        alias_data[28:32] = bytes((51, 48, 46, 43))
        alias_data[32:40] = bytes(range(8))
        struct.pack_into("<2H", alias_data, 44, 1966, 0)
        BONE_MAP_ROW_STRUCT.pack_into(alias_data, 96, 7, 7, 8, 9, 10, 11, 12, 13)
        alias_meshes, _alias_display_map = _parse_meshes(
            bytes(alias_data),
            alias_header,
            [alias_mesh_header],
            root_scale=[1.0, 1.0, 1.0],
            fix_lp2=False,
            fix_dmc=False,
            include_normals=False,
        )
        if alias_meshes[0].fbx_skin_bones != [[8, 9, 10, 11]]:
            raise AssertionError("Top4 must resolve palette aliases before duplicate-bone aggregation")

        invalid_local_data = bytearray(palette_data)
        invalid_local_data[22] = 8
        try:
            _parse_meshes(
                bytes(invalid_local_data),
                palette_header,
                [palette_mesh_header],
                root_scale=[1.0, 1.0, 1.0],
                fix_lp2=False,
                fix_dmc=False,
                include_normals=False,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("out-of-range mesh-local bone slot did not fail closed")
        invalid_global_data = bytearray(palette_data)
        BONE_MAP_ROW_STRUCT.pack_into(invalid_global_data, 64, 9, 8, 16, 6, 5, 4, 3, 2)
        try:
            _parse_meshes(
                bytes(invalid_global_data),
                palette_header,
                [palette_mesh_header],
                root_scale=[1.0, 1.0, 1.0],
                fix_lp2=False,
                fix_dmc=False,
                include_normals=False,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("palette entry outside the global bone table did not fail closed")
        try:
            _parse_bone_map_rows(bytes(palette_data), dataclasses.replace(palette_header, ptr_bone_map=0))
        except ValueError:
            pass
        else:
            raise AssertionError("non-empty bone-map table with a null pointer did not fail closed")

        single_0cb = bytearray(20)
        single_0cb[6] = 4
        if _decode_skin_row(bytes(single_0cb), FVF_LAYOUTS[0x0CB68015].skin_kind) != ([5], [1.0]):
            raise AssertionError("0CB static bone byte decoder regression")
        single_667 = bytearray(24)
        struct.pack_into("<H", single_667, 6, 5)
        if _decode_skin_row(bytes(single_667), FVF_LAYOUTS[0x667B1019].skin_kind) != ([6], [1.0]):
            raise AssertionError("667 ushort lane is an authoritative one-bone Skin field")
        single_cbf6 = bytearray(24)
        struct.pack_into("<H", single_cbf6, 6, 5)
        if _decode_skin_row(bytes(single_cbf6), FVF_LAYOUTS[0xCBF6C01A].skin_kind) != ([6], [1.0]):
            raise AssertionError("CBF6 u16 lane is an authoritative one-bone Skin field")
        dual_0d9e = bytearray(28)
        struct.pack_into("<h2x", dual_0d9e, 6, 0)
        struct.pack_into("<2H", dual_0d9e, 20, 2, 3)
        if _decode_skin_row(bytes(dual_0d9e), FVF_LAYOUTS[0x0D9E801D].skin_kind) != ([3, 4], [0.0, 1.0]):
            raise AssertionError("0D9E ushort lanes and implicit second weight lost Skin semantics")

        legacy8_record = bytearray(40)
        struct.pack_into("<h", legacy8_record, 6, 3277)
        legacy8_record[12:16] = bytes((20, 21, 22, 23))
        legacy8_record[16:24] = bytes(range(8))
        struct.pack_into("<e", legacy8_record, 28, 0.1)
        struct.pack_into("<e", legacy8_record, 30, 0.1)
        legacy8_bones, legacy8_weights = _decode_skin_row(
            bytes(legacy8_record),
            FVF_LAYOUTS[0x75C3E025].skin_kind,
        )
        legacy8_semantic_bones, legacy8_semantic_weights = _normalise_skin_pairs(
            legacy8_bones,
            legacy8_weights,
            max_influences=SKIN_KIND_MAX_INFLUENCES["legacy8_half"],
        )
        if legacy8_bones != list(range(1, 9)) or len(legacy8_weights) != 8:
            raise AssertionError("75C3/D84E writer records must decode all eight bone and weight slots")
        if legacy8_semantic_bones != list(range(1, 9)) or abs(sum(legacy8_semantic_weights) - 1.0) > 1.0e-6:
            raise AssertionError("legacy8 Skin rows lost an influence or failed normalization")
        duplicate_legacy8_bones = [1, 2, 1, 3, 4, 5, 6, 7]
        duplicate_legacy8_weights = [0.15, 0.1, 0.25, 0.1, 0.1, 0.1, 0.1, 0.1]
        merged_legacy8_bones, merged_legacy8_weights = _normalise_skin_pairs(
            duplicate_legacy8_bones,
            duplicate_legacy8_weights,
            max_influences=SKIN_KIND_MAX_INFLUENCES["legacy8_half"],
        )
        if merged_legacy8_bones != [1, 2, 3, 4, 5, 6, 7] or max(
            abs(actual - expected)
            for actual, expected in zip(merged_legacy8_weights, (0.4, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1))
        ) > 1.0e-7:
            raise AssertionError("75C3/D84E duplicate global Bone weights were not aggregated")

        c31_same_bone = bytearray(24)
        struct.pack_into("<e", c31_same_bone, 20, 3.0)
        struct.pack_into("<e", c31_same_bone, 22, 3.0)
        c31_bones, c31_weights = _decode_skin_row(bytes(c31_same_bone), "two_c31")
        c31_semantic_bones, c31_semantic_weights = _normalise_skin_pairs(
            c31_bones,
            c31_weights,
            max_influences=SKIN_KIND_MAX_INFLUENCES["two_c31"],
        )
        if c31_semantic_bones != [4] or c31_semantic_weights != [1.0]:
            raise AssertionError("C31 same-bone w1=0 must retain the implicit full second influence")

        clamped_max_normal, clamped_game_normal = _decode_packed_normal(bytes((255, 254, 127, 255)), 0)
        expected_clamped = math.sqrt(0.5)
        if (
            max(abs(actual - expected) for actual, expected in zip(clamped_game_normal, (expected_clamped, expected_clamped, 0.0)))
            > 1.0e-12
            or max(abs(actual - expected) for actual, expected in zip(clamped_max_normal, (expected_clamped, 0.0, expected_clamped)))
            > 1.0e-12
        ):
            raise AssertionError("packed normals must clamp each signed component before normalization")

        degenerate_face, degenerate_invalid = _sanitize_face_one_based([0, 1, 2], 1, 3)
        if degenerate_face != [1, 1, 1] or not degenerate_invalid:
            raise AssertionError("an out-of-range face must collapse to V4's fully degenerate [1,1,1] face")
        repaired_face, repaired_invalid = _sanitize_face_one_based([-1, 2, 3], 1, 3)
        if repaired_face != [1, 2, 3] or repaired_invalid:
            raise AssertionError("V4 negative-index repair must preserve the other two face corners")

        zero_uv_header = dataclasses.replace(
            fixture_header,
            vert_count=3,
            triangle_count=1,
            vertex_buffer_size=84,
            ptr_triangle=132,
            end_size=150,
        )
        zero_uv_mesh_header = dataclasses.replace(
            da55_mesh_header,
            vert_count=3,
            vert_stride=28,
            vert_start=1,
            fvf_info=0x2BE814D4,
            face_count=3,
            max_index=2,
        )
        zero_uv_data = bytearray(150)
        struct.pack_into("<hhh", zero_uv_data, 44, 0, 0, 0)
        struct.pack_into("<hhh", zero_uv_data, 72, 32767, 0, 0)
        struct.pack_into("<hhh", zero_uv_data, 100, 0, 32767, 0)
        struct.pack_into("<3H", zero_uv_data, 132, 0, 1, 2)
        zero_uv_meshes, _zero_uv_display_map = _parse_meshes(
            bytes(zero_uv_data),
            zero_uv_header,
            [zero_uv_mesh_header],
            root_scale=[1.0, 1.0, 1.0],
            fix_lp2=False,
            fix_dmc=False,
            include_normals=False,
        )
        zero_uv_mesh = zero_uv_meshes[0]
        if zero_uv_mesh.uv1 != [[0.0, 0.0, 0.0]] * 3:
            raise AssertionError("no-UV FVF parsing must emit one zero Map1 row per vertex")
        if zero_uv_mesh.faces != [[0, 0, 0]] or zero_uv_mesh.invalid_face_count != 1:
            raise AssertionError("invalid MOD triangles must remain as whole-face degenerates after zero-basing")
        zero_uv_objects = _FbxNode(b"Objects")
        _fbx_add_geometry(
            zero_uv_objects,
            object_id=1,
            name="ZeroUvFixture",
            vertices=zero_uv_mesh.positions,
            faces=zero_uv_mesh.faces,
            uv1=zero_uv_mesh.uv1,
            normals=None,
            smooth=False,
        )
        zero_uv_geometry = next(child for child in zero_uv_objects.children if child.name == b"Geometry")
        zero_uv_layer = next(child for child in zero_uv_geometry.children if child.name == b"LayerElementUV")
        zero_uv_values = next(child for child in zero_uv_layer.children if child.name == b"UV")
        zero_uv_indexes = next(child for child in zero_uv_layer.children if child.name == b"UVIndex")
        if zero_uv_values.props != [("d", [0.0] * 6)] or zero_uv_indexes.props != [("i", [0, 0, 0])]:
            raise AssertionError("all-zero MOD Map1 must survive as an indexed FBX LayerElementUV")

        cdxm_header = dataclasses.replace(fixture_header, mesh_count=3, end_size=16)
        cdxm_data = bytearray(64)
        struct.pack_into("<4sHH2H", cdxm_data, 16, b"CDXM", 1, 2, 7, 0)
        struct.pack_into("<4sHH3H", cdxm_data, 36, b"CDXM", 1, 3, 9, 8, 6)
        cdxm_map = _find_cdxm_display_map(bytes(cdxm_data), cdxm_header)
        if cdxm_map != [7, 0] or [_display_slot_from_map(cdxm_map, slot) for slot in (1, 2, 3)] != [7, 2, 3]:
            raise AssertionError("CDXM must accept the first partial map and fall back per missing/zero slot")
        zero_mesh_cdxm = _find_cdxm_display_map(
            bytes(cdxm_data),
            dataclasses.replace(cdxm_header, mesh_count=0),
        )
        if zero_mesh_cdxm != [7, 0]:
            raise AssertionError("zero-Mesh MOD metadata must retain V4's CDXM scan behavior")
        truncated_cdxm = (b"\x00" * 8) + b"CDXM"
        if _try_read_cdxm_display_map(truncated_cdxm, 8, 3) is not None:
            raise AssertionError("a header-only terminal CDXM marker must be ignored")
        if _find_cdxm_display_map(truncated_cdxm, dataclasses.replace(cdxm_header, end_size=8)):
            raise AssertionError("a truncated terminal CDXM marker must not produce a display map")

        if _parser_fvf(0x14D40019, fix_lp2=True, fix_dmc=False) != 0x14D40020:
            raise AssertionError("LP2 parser dispatch must add seven without changing source FVF identity")
        if _parser_fvf(0x14D40021, fix_lp2=False, fix_dmc=True) != 0x14D40020:
            raise AssertionError("DMC parser dispatch must subtract one without changing source FVF identity")
        if _parser_fvf(0x14D4001A, fix_lp2=True, fix_dmc=True) != 0x14D40020:
            raise AssertionError("combined parser dispatch must apply LP2 +7 then DMC -1")

        top4_bones, top4_weights = _v4_top_four_slots(
            list(range(1, 9)),
            [0.1, 0.4, 0.3, 0.2, 0.0, 0.0, 0.0, 0.0],
        )
        if top4_bones != [2, 3, 4, 1] or max(abs(a - b) for a, b in zip(top4_weights, (0.4, 0.3, 0.2, 0.1))) > 1.0e-12:
            raise AssertionError("V4 Top4 skin selection must remain in descending weight order")
        duplicate_top4_bones, duplicate_top4_weights = _v4_top_four_slots(
            [1, 1, 2, 3, 4, 5, 6, 7],
            [0.2, 0.2, 0.19, 0.18, 0.17, 0.06, 0.0, 0.0],
        )
        expected_duplicate_weights = [value / 0.94 for value in (0.4, 0.19, 0.18, 0.17)]
        if duplicate_top4_bones != [1, 2, 3, 4] or max(
            abs(actual - expected)
            for actual, expected in zip(duplicate_top4_weights, expected_duplicate_weights)
        ) > 1.0e-12:
            raise AssertionError("Top4 must aggregate duplicate bones before strongest-four selection")
        tied_top4_bones, _tied_top4_weights = _v4_top_four_slots(
            [1, 2, 3, 4, 5],
            [0.3, 0.2, 0.1, 0.1, 0.1],
        )
        if tied_top4_bones != [1, 2, 3, 4]:
            raise AssertionError("Top4 equal weights must retain deterministic first-source-slot order")
        try:
            _v4_top_four_slots(list(range(1, 9)), [0.0] * 8)
        except ValueError:
            pass
        else:
            raise AssertionError("generic Top4 zero rows must not acquire the BB-only fallback")
        zero_bb_record = bytearray(36)
        zero_bb_record[16:24] = bytes(range(8))
        zero_bb_bones, zero_bb_weights = _decode_skin_row(bytes(zero_bb_record), "top4_bb")
        zero_bb_semantic_bones, zero_bb_semantic_weights = _normalise_skin_pairs(
            zero_bb_bones,
            zero_bb_weights,
            max_influences=SKIN_KIND_MAX_INFLUENCES["top4_bb"],
        )
        if zero_bb_semantic_bones != [8] or zero_bb_semantic_weights != [1.0]:
            raise AssertionError("BB all-zero source row must use the writer's eighth-slot exact-byte fallback")
        nonzero_bb_record = bytearray(zero_bb_record)
        nonzero_bb_record[12] = 1
        nonzero_bb_bones, nonzero_bb_weights = _decode_skin_row(bytes(nonzero_bb_record), "top4_bb")
        nonzero_bb_semantic_bones, nonzero_bb_semantic_weights = _normalise_skin_pairs(
            nonzero_bb_bones,
            nonzero_bb_weights,
            max_influences=SKIN_KIND_MAX_INFLUENCES["top4_bb"],
        )
        if nonzero_bb_semantic_bones != [2] or nonzero_bb_semantic_weights != [1.0]:
            raise AssertionError("BB all-zero fallback contaminated an ordinary nonzero Top4 row")

        compatibility_fixture = {
            "physical_slot": 290,
            "node_name": "Mesh_290_14D40020_LODx0",
            "raw_skin_bones": [[1, 28, 2, 256], [27, 0]],
            "raw_skin_weights": [[0.5, 0.0, 0.25, 0.25], [0.1, 0.9]],
            "fbx_skin_bones": [[1, 28, 2, 256], [27, 0]],
            "fbx_skin_weights": [[0.5, 0.0, 0.25, 0.25], [0.1, 0.9]],
        }
        compatibility_report = describe_mesh_skin_compatibility(
            compatibility_fixture,
            27,
        )
        _compatibility_order, compatibility_clusters = _collect_mesh_cluster_rows(
            compatibility_fixture,
            27,
        )
        if (
            compatibility_report["status"] != "WARN"
            or compatibility_report["non_blocking"] is not True
            or compatibility_report["valid_skin_bone_slots"] != [1, 2, 27]
            or compatibility_report["ignored_out_of_range_reference_count"] != 2
            or compatibility_report[
                "ignored_out_of_range_positive_weight_reference_count"
            ]
            != 1
            or compatibility_report["affected_vertex_count"] != 1
            or compatibility_report["renormalized_vertex_count"] != 1
            or compatibility_report["fully_unbound_vertex_count"] != 0
            or compatibility_report["ignored_source_bone_slots"] != [28, 256]
        ):
            raise AssertionError(
                "out-of-range source Skin slots must be ignored with a non-blocking warning"
            )
        if (
            compatibility_clusters[1][0] != [0]
            or compatibility_clusters[2][0] != [0]
            or abs(compatibility_clusters[1][1][0] - (2.0 / 3.0)) > 1.0e-6
            or abs(compatibility_clusters[2][1][0] - (1.0 / 3.0)) > 1.0e-6
        ):
            raise AssertionError(
                "legal Skin weights must be renormalized after impossible source slots are removed"
            )
        all_invalid_report = describe_mesh_skin_compatibility(
            {
                "raw_skin_bones": [[28]],
                "raw_skin_weights": [[1.0]],
                "fbx_skin_bones": [[28]],
                "fbx_skin_weights": [[1.0]],
            },
            2,
        )
        if (
            all_invalid_report["valid_skin_bone_slots"] != [1, 2]
            or all_invalid_report["ignored_out_of_range_reference_count"] != 1
            or all_invalid_report["fully_unbound_vertex_count"] != 1
        ):
            raise AssertionError(
                "a Skin row with no legal source slot must retain the writer's empty-cluster fallback"
            )
        checks["skin_slot_compatibility"] = {
            "policy": compatibility_report["policy"],
            "non_blocking": True,
            "ignored_fixture_slots": compatibility_report[
                "ignored_source_bone_slots"
            ],
        }
        try:
            _float_position_to_max(struct.pack("<fff", float("nan"), 0.0, 0.0))
        except ValueError:
            pass
        else:
            raise AssertionError("non-finite float-position lanes must fail closed")

        request_identity = _request_contract_identity(
            "G:/fixture/source.mod",
            "0" * 64,
            include_normals=True,
            fix_lp2=True,
            fix_dmc=False,
            fix_processing_mode=FIX_PROCESSING_MODE_CODEX,
        )
        fixture_request_sha256 = _request_contract_digest(request_identity)
        if fixture_request_sha256 != "65aa8cc7ac9470eb3756e11d06da8f32e90fbc0c2ed5f09fc522fe31ab61365c":
            raise AssertionError("canonical import request contract SHA-256 regression")
        changed_request_identity = dict(request_identity)
        changed_request_identity["fix_dmc"] = True
        if _request_contract_digest(changed_request_identity) == fixture_request_sha256:
            raise AssertionError("request contract SHA-256 must bind every parser option")
        changed_mode_identity = dict(request_identity)
        changed_mode_identity["fix_processing_mode"] = FIX_PROCESSING_MODE_LEGACY_128
        if _request_contract_digest(changed_mode_identity) == fixture_request_sha256:
            raise AssertionError("request contract SHA-256 must bind the compatibility processing mode")
        fixture_contract_id = "re6mod-" + fixture_request_sha256[:24]

        def make_name_plan_fixture() -> dict[str, Any]:
            return {
                "scene_helpers": {
                    "lod_groups": ["LodGroup_0"],
                    "other_mesh": "OtherMesh",
                    "bounds": ["BoundSphere", "BoundBoxMin", "BoundBoxMax"],
                },
                "bones": [{"slot": 1, "name": "b_001", "parent_slot": None}],
                "meshes": [
                    {
                        "physical_slot": 1,
                        "node_name": "Mesh_001",
                        "parent_name": "LodGroup_0",
                    }
                ],
                "max_post_import": {
                    "bounds": {
                        "BoundSphere": {},
                        "BoundBoxMin": {},
                        "BoundBoxMax": {},
                    }
                },
            }

        source_names = [
            "boundsphere",
            "boundboxmin",
            "boundboxmax",
            "b_001",
            "lodgroup_0",
            "othermesh",
            "mesh_001",
        ]
        import2_scene = make_name_plan_fixture()
        import2_plan = _apply_reserved_scene_name_plan(import2_scene, source_names)
        if (
            int(import2_plan.get("import_number", 0) or 0) != 2
            or int(import2_plan.get("renamed_count", 0) or 0) != 7
            or import2_scene["meshes"][0]["node_name"] != "Mesh_001_Import2"
            or import2_scene["meshes"][0]["parent_name"] != "LodGroup_0_Import2"
            or import2_scene["bones"][0]["name"] != "b_001_Import2"
            or import2_scene["scene_helpers"]["bounds"][0] != "BoundSphere_Import2"
        ):
            raise AssertionError("pre-FBX Import2 scene-name planning regression")
        import3_scene = make_name_plan_fixture()
        import3_plan = _apply_reserved_scene_name_plan(
            import3_scene,
            source_names
            + [str(row.get("final_name", "") or "") for row in import2_plan["entries"]],
        )
        if (
            int(import3_plan.get("import_number", 0) or 0) != 3
            or import3_scene["meshes"][0]["node_name"] != "Mesh_001_Import3"
            or import3_scene["meshes"][0]["parent_name"] != "LodGroup_0_Import3"
        ):
            raise AssertionError("pre-FBX Import3 scene-name planning regression")
        checks["pre_fbx_import_name_plan_contract"] = "casefold+Import2+Import3+parent+Bounds"

        compact_name = "017_X64593023_0_48_3_1_65015"
        compact_import2_scene = make_name_plan_fixture()
        compact_import2_scene["meshes"][0]["node_name"] = compact_name
        compact_import2_plan = _apply_reserved_scene_name_plan(
            compact_import2_scene,
            [compact_name],
        )
        compact_import3_scene = make_name_plan_fixture()
        compact_import3_scene["meshes"][0]["node_name"] = compact_name
        compact_import3_plan = _apply_reserved_scene_name_plan(
            compact_import3_scene,
            [compact_name, compact_name + "_Import2"],
        )
        if (
            int(compact_import2_plan.get("import_number", 0) or 0) != 2
            or compact_import2_scene["meshes"][0]["node_name"] != compact_name + "_Import2"
            or int(compact_import3_plan.get("import_number", 0) or 0) != 3
            or compact_import3_scene["meshes"][0]["node_name"] != compact_name + "_Import3"
        ):
            raise AssertionError("Blender compact Import2/Import3 name planning regression")
        checks["blender_compact_import_name_plan_contract"] = "compact+Import2+Import3"

        test_normal = bytes((0, 0, 0, 0, 254, 127, 127, 255))
        max_normal, game_normal = _decode_packed_normal(test_normal, 4)
        if max_normal[0] < 0.99 or abs(game_normal[1]) > 0.01 or abs(game_normal[2]) > 0.01:
            raise AssertionError("packed normal axis conversion regression")
        test_node = _FbxNode(b"Root")
        test_node.add("Value", ("I", 7))
        encoded = _encode_fbx_node(test_node, len(FBX_MAGIC) + 4, is_last=True)
        if not encoded or b"Root" not in encoded:
            raise AssertionError("FBX binary node encoder regression")
        empty_node = _FbxNode(b"Empty")
        empty_non_last = _encode_fbx_node(empty_node, len(FBX_MAGIC) + 4, is_last=False)
        empty_last = _encode_fbx_node(empty_node, len(FBX_MAGIC) + 4, is_last=True)
        if len(empty_non_last) != len(empty_last) + len(FBX_NULL_RECORD):
            raise AssertionError("FBX empty non-final node sentinel regression")
        if (
            FBX_CREATION_TIME != "1970-01-01 10:00:00:000"
            or FBX_FILE_ID.hex() != "28b32aebb624ccc2bfc8b02aa92bfcf1"
            or FBX_FOOT_ID.hex() != "fabcab09d0c8d466b176fb831cf7267e"
        ):
            raise AssertionError("FBX timestamp/FileId/FooterId CRC compatibility triple regression")
        with tempfile.TemporaryDirectory(prefix="codex-re6-import-router-") as router_temp:
            router_root = Path(router_temp)
            fixture_mod_size = MOD_HEADER_PREFIX_SIZE + 68
            fixture_mod_bytes = (
                b"MOD\x00"
                + struct.pack("<H", 0)
                + MOD_HEADER_STRUCT.pack(
                    0, 0, 0,
                    0, 0, 0, 0, 0, 0,
                    0, 0, 0,
                    fixture_mod_size, fixture_mod_size, fixture_mod_size, fixture_mod_size,
                )
                + struct.pack(
                    "<12f",
                    0.0, 0.0, 0.0, 1.0,
                    -1.0, -1.0, -1.0, 0.0,
                    1.0, 1.0, 1.0, 0.0,
                )
                + struct.pack("<5I", 0, 0, 0, 0, 0)
            )
            if len(fixture_mod_bytes) != fixture_mod_size:
                raise AssertionError("zero-Mesh MOD integration fixture size regression")
            fixture_mod_path = router_root / "source.mod"
            fixture_mod_path.write_bytes(fixture_mod_bytes)
            output_fbx = router_root / "scene.fbx"
            normal_route_path = router_root / "scene.normal-route.json"
            manifest_path = router_root / "scene.manifest.json"
            integrated_scene = build_import_scene(
                fixture_mod_path,
                include_normals=True,
                fix_lp2=True,
                fix_dmc=False,
            )
            integrated_source_sha256 = hashlib.sha256(fixture_mod_bytes).hexdigest()
            integrated_request_sha256 = _request_contract_digest(
                _request_contract_identity(
                    str(_windows_lexical_full_path(fixture_mod_path)),
                    integrated_source_sha256,
                    include_normals=True,
                    fix_lp2=True,
                    fix_dmc=False,
                )
            )
            if (
                integrated_scene["request_contract_sha256"] != integrated_request_sha256
                or integrated_scene["contract_id"] != "re6mod-" + integrated_request_sha256[:24]
                or integrated_scene["source"]["sha256"] != integrated_source_sha256
            ):
                raise AssertionError("build_import_scene lost request/source contract identity")
            integrated_result = build_import_artifacts(
                fixture_mod_path,
                output_fbx,
                normal_route_path=normal_route_path,
                manifest_path=manifest_path,
                include_normals=True,
                fix_lp2=True,
                fix_dmc=False,
            )
            expected_integrated_result = {
                "status": "OK",
                "contract_revision": IMPORT_MODULE_CONTRACT_REVISION,
                "contract_id": integrated_scene["contract_id"],
                "request_contract_sha256": integrated_request_sha256,
                "source_sha256": integrated_source_sha256,
                "embedded_explicit_normals": True,
                "mesh_count": 0,
                "bone_count": 0,
                "fix_lp2": True,
                "fix_dmc": False,
            }
            if any(integrated_result.get(key) != value for key, value in expected_integrated_result.items()):
                raise AssertionError("build_import_artifacts result identity-field regression")
            checks["retired_file_job_bridge"] = True
            checks["artifact_result_identity_contract"] = True
        with tempfile.TemporaryDirectory(prefix="codex_re6_import_mesh_route_guard_") as temp_dir:
            fixture_mod_path = Path(temp_dir) / "mesh-route-fixture.mod"
            fixture_mod_path.write_bytes(_guard_build_mesh_route_mod())
            mesh_route_scene = build_import_scene(fixture_mod_path, include_normals=True)
            mesh_route = mesh_route_scene["meshes"][0]
            expected_route_name = (
                "Mesh_037_0CB68015_LODx249_"
                "MatID:42_Group:13_DisplayMode:17_Type:7"
            )
            if (
                mesh_route["physical_slot"] != 1
                or mesh_route["display_slot"] != 37
                or mesh_route["node_name"] != expected_route_name
                or mesh_route["parent_name"] != "OtherMesh"
                or mesh_route["header"]["mat_id"] != 42
                or mesh_route["header"]["lod_level"] != 249
                or mesh_route["header"]["unk01"] != 13
                or mesh_route["header"]["unk05"] != 17
                or mesh_route["header"]["meshtype"] != 7
            ):
                raise AssertionError("real MeshHeader/CDXM parse lost Mesh identity metadata")
            try:
                build_import_scene(
                    fixture_mod_path,
                    include_normals=True,
                    blender_compact_mesh_names=True,
                )
            except ValueError as exc:
                if "compact Mesh names are retired" not in str(exc):
                    raise
            else:
                raise AssertionError("retired Blender compact Mesh-name path remained available")
            if (
                mesh_route["raw_skin_bones"] != [[1], [1], [1]]
                or mesh_route["fbx_skin_bones"] != [None, None, None]
                or _scene_mesh_has_skin(mesh_route, len(mesh_route_scene["bones"]))
            ):
                raise AssertionError("0CB route must preserve static bytes without creating Skin rows")
            mesh_route_roots = _build_fbx_roots(
                mesh_route_scene,
                include_normals=True,
                route_file_name="mesh-route-fixture.normal-route.json",
            )
            _guard_require_fbx_mesh_route(
                mesh_route_roots,
                node_name=expected_route_name,
                parent_name="OtherMesh",
                physical_slot=1,
                display_slot=37,
            )
            mesh_route_objects = next(root for root in mesh_route_roots if root.name == b"Objects")
            if any(
                node.name == b"Deformer"
                and len(node.props) > 2
                and node.props[2] == ("S", "Skin")
                for node in mesh_route_objects.children
            ):
                raise AssertionError("0CB OtherMesh route emitted an FBX Skin deformer")
            checks["mesh_header_to_fbx_route_contract"] = True
            checks["static_0cb_other_mesh_no_skin_contract"] = True

            max_slot_mod_path = Path(temp_dir) / "mesh-route-uint16-max.mod"
            max_slot_mod_path.write_bytes(_guard_build_mesh_route_mod(display_slot=0xFFFF))
            max_slot_scene = build_import_scene(max_slot_mod_path, include_normals=True)
            max_slot_mesh = max_slot_scene["meshes"][0]
            max_slot_name = (
                "Mesh_65535_0CB68015_LODx249_"
                "MatID:42_Group:13_DisplayMode:17_Type:7"
            )
            if (
                max_slot_scene.get("cdxm_display_map") != [0xFFFF]
                or max_slot_mesh.get("physical_slot") != 1
                or max_slot_mesh.get("display_slot") != 0xFFFF
                or max_slot_mesh.get("node_name") != max_slot_name
            ):
                raise AssertionError("uint16 CDXM Mesh_65535 identity was truncated or reformatted")
            max_slot_roots = _build_fbx_roots(
                max_slot_scene,
                include_normals=True,
                route_file_name="mesh-route-uint16-max.normal-route.json",
            )
            _guard_require_fbx_mesh_route(
                max_slot_roots,
                node_name=max_slot_name,
                parent_name="OtherMesh",
                physical_slot=1,
                display_slot=0xFFFF,
            )
            checks["mesh_65535_uint16_cdxm_contract"] = True

        normal_fvf_count = _guard_require_all_normal_fvfs_reach_fbx()
        checks["normal_fvf_to_fbx_count"] = normal_fvf_count

        reachability_fixture = {
            "contract_id": fixture_contract_id,
            "request_contract_sha256": fixture_request_sha256,
            "source": {"sha256": "0" * 64, "name": "source.mod"},
            "options": {
                "include_normals": True,
                "fix_lp2": True,
                "fix_dmc": False,
            },
            "bounds": [
                [2.0, 3.0, 4.0, 1.25],
                [-5.0, 6.0, -7.0, 0.0],
                [8.0, -9.0, 10.0, 0.0],
            ],
            "bones": [],
            "root_mesh_scale": [1.0, 1.0, 1.0],
            "scene_helpers": {
                "lod_groups": ["LodGroup_0"],
                "other_mesh": "OtherMesh",
            },
            "meshes": [
                {
                    "physical_slot": 1,
                    "display_slot": 1,
                    "node_name": "Mesh_001_REACHABILITY_FIXTURE",
                    "source_fvf": 0x14D40019,
                    "parser_fvf": 0x14D40020,
                    "header": {
                        "mat_id": 5,
                        "lod_level": 0,
                        "unk01": 3,
                        "unk05": 4,
                        "meshtype": 1,
                    },
                    "parent_name": "LodGroup_0",
                    "positions": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0]],
                    "faces": [[0, 1, 2], [0, 2, 3]],
                    "uv1": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 0.0]],
                    "max_normals": [],
                    "fbx_skin_bones": [],
                    "fbx_skin_weights": [],
                }
            ],
        }
        reachability_route = build_normal_route_table(reachability_fixture)
        if reachability_route["request_contract_sha256"] != fixture_request_sha256:
            raise AssertionError("normal route lost the canonical request contract SHA-256")
        if (
            reachability_route["meshes"][0]["source_fvf"] != 0x14D40019
            or reachability_route["meshes"][0]["parser_fvf"] != 0x14D40020
        ):
            raise AssertionError("normal route must retain distinct source and parser FVF identities")
        reachability_roots = _build_fbx_roots(
            reachability_fixture,
            include_normals=True,
            route_file_name="fixture.normal-route.json",
        )
        reachability = _require_all_fbx_models_reachable(reachability_roots)
        if reachability["reachable_model_count"] != reachability["model_count"]:
            raise AssertionError("FBX Model reachability gate accepted a disconnected scene")
        fixture_objects = next(root for root in reachability_roots if root.name == b"Objects")
        # The MRL route must remain portable: one StandardMaterial, one
        # Texture/Video media owner, and one standard DiffuseColor property edge.
        with tempfile.TemporaryDirectory(prefix="codex-re6-mrl-embedded-fbx-") as material_temp:
            fixture_dds = Path(material_temp) / "fixture_bm.dds"
            fixture_dds_bytes = b"DDS " + bytes(range(32))
            fixture_dds.write_bytes(fixture_dds_bytes)
            material_roots = _build_fbx_roots(
                reachability_fixture,
                include_normals=True,
                route_file_name="fixture.normal-route.json",
                mrl_bindings={
                    1: {
                        "image_path": str(fixture_dds),
                        "image_sha256": hashlib.sha256(fixture_dds_bytes).hexdigest(),
                    }
                },
            )
        material_objects = next(root for root in material_roots if root.name == b"Objects")
        material_by_id, material_oo_edges = _guard_fbx_objects_and_edges(material_roots)
        material_node = next(node for node in material_objects.children if node.name == b"Material")
        texture_node = next(node for node in material_objects.children if node.name == b"Texture")
        video_node = next(node for node in material_objects.children if node.name == b"Video")
        material_id = int(material_node.props[0][1])
        texture_id = int(texture_node.props[0][1])
        video_id = int(video_node.props[0][1])
        if (material_id, int(next(
            node.props[0][1]
            for node in material_objects.children
            if node.name == b"Model"
            and _guard_fbx_object_name(node) == "Mesh_001_REACHABILITY_FIXTURE"
        ))) not in material_oo_edges:
            raise AssertionError("MRL Material is not connected to its Mesh")
        if (video_id, texture_id) not in material_oo_edges:
            raise AssertionError("MRL Video is not connected to its Texture")
        material_connections = next(root for root in material_roots if root.name == b"Connections")
        material_property_edges = {
            (int(node.props[1][1]), int(node.props[2][1]), str(node.props[3][1]))
            for node in material_connections.children
            if node.name == b"C"
            and len(node.props) == 4
            and node.props[0] == ("S", "OP")
        }
        material_edges = {
            edge for edge in material_property_edges if edge[1] == material_id
        }
        if material_edges != {(texture_id, material_id, "DiffuseColor")}:
            raise AssertionError("MRL Texture must use the standard DiffuseColor edge")
        material_props = next(child for child in material_node.children if child.name == b"Properties70")
        material_properties = {
            str(node.props[0][1]): node.props
            for node in material_props.children
            if node.name == b"P" and node.props
        }
        if "ShadingModel" not in material_properties:
            raise AssertionError("portable MRL Material is missing ShadingModel")
        if material_properties["ShadingModel"][-1] != ("S", "phong"):
            raise AssertionError("portable MRL Material is not the verified StandardMaterial phong route")
        for property_name in ("SpecularFactor", "ShininessExponent", "Opacity"):
            if property_name not in material_properties:
                raise AssertionError(f"portable MRL Material is missing {property_name}")
        if material_properties["SpecularFactor"][-1] != ("D", 0.0):
            raise AssertionError("portable MRL Material must use SpecularFactor=0")
        if (
            material_by_id[texture_id].name != b"Texture"
            or material_by_id[video_id].name != b"Video"
        ):
            raise AssertionError("MRL portable media object identity regression")
        checks["mrl_embedded_base_color_contract"] = (
            FBX_MAX_BITMAP_COMPATIBILITY_POLICY
        )

        # A real RE6 model commonly has many Meshes sharing just a few Base
        # Color DDS files. The old graph emitted an identically named Video for
        # each Mesh but only wrote Content to the first one. Max then resolved
        # the later, empty Videos as black textures. Lock the media-owner
        # topology itself: each unique DDS has exactly one named Video with
        # bytes, and every material points at one of those shared Textures.
        with tempfile.TemporaryDirectory(prefix="codex-re6-mrl-shared-media-") as media_temp:
            media_a = Path(media_temp) / "shared_a.dds"
            media_b = Path(media_temp) / "shared_b.dds"
            media_a_bytes = b"DDS " + bytes(range(48))
            media_b_bytes = b"DDS " + bytes(range(47, -1, -1))
            media_a.write_bytes(media_a_bytes)
            media_b.write_bytes(media_b_bytes)
            fixture_mesh = dict(reachability_fixture["meshes"][0])
            shared_media_fixture = dict(reachability_fixture)
            shared_media_fixture["meshes"] = [
                {
                    **fixture_mesh,
                    "physical_slot": 1,
                    "display_slot": 1,
                    "node_name": "Mesh_001_SHARED_MEDIA_A",
                },
                {
                    **fixture_mesh,
                    "physical_slot": 2,
                    "display_slot": 2,
                    "node_name": "Mesh_002_SHARED_MEDIA_A",
                },
                {
                    **fixture_mesh,
                    "physical_slot": 3,
                    "display_slot": 3,
                    "node_name": "Mesh_003_SHARED_MEDIA_B",
                },
            ]
            shared_media_roots = _build_fbx_roots(
                shared_media_fixture,
                include_normals=True,
                route_file_name="shared-media.normal-route.json",
                mrl_bindings={
                    1: {
                        "image_path": str(media_a),
                        "image_sha256": hashlib.sha256(media_a_bytes).hexdigest(),
                    },
                    2: {
                        "image_path": str(media_a),
                        "image_sha256": hashlib.sha256(media_a_bytes).hexdigest(),
                    },
                    3: {
                        "image_path": str(media_b),
                        "image_sha256": hashlib.sha256(media_b_bytes).hexdigest(),
                    },
                },
            )
        shared_media_objects = next(
            root for root in shared_media_roots if root.name == b"Objects"
        )
        shared_media_connections = next(
            root for root in shared_media_roots if root.name == b"Connections"
        )
        shared_material_nodes = [
            node for node in shared_media_objects.children if node.name == b"Material"
        ]
        shared_texture_nodes = [
            node for node in shared_media_objects.children if node.name == b"Texture"
        ]
        shared_video_nodes = [
            node for node in shared_media_objects.children if node.name == b"Video"
        ]
        if (
            len(shared_material_nodes) != 3
            or len(shared_texture_nodes) != 2
            or len(shared_video_nodes) != 2
        ):
            raise AssertionError(
                "MRL shared-media graph must emit 3 Materials and 2 Texture/Video owners"
            )
        shared_video_names = [_guard_fbx_object_name(node) for node in shared_video_nodes]
        if any(not name for name in shared_video_names) or len(set(shared_video_names)) != 2:
            raise AssertionError("MRL embedded Videos must have stable unique names")
        shared_video_content = []
        for node in shared_video_nodes:
            content = next((child for child in node.children if child.name == b"Content"), None)
            if content is None or content.props != [("R", media_a_bytes)] and content.props != [("R", media_b_bytes)]:
                raise AssertionError("MRL embedded Video is missing its exact DDS payload")
            shared_video_content.append(content.props[0][1])
        if set(shared_video_content) != {media_a_bytes, media_b_bytes}:
            raise AssertionError("MRL embedded Videos do not retain both unique DDS payloads")
        shared_texture_ids = {
            int(node.props[0][1]) for node in shared_texture_nodes
        }
        shared_video_ids = {
            int(node.props[0][1]) for node in shared_video_nodes
        }
        shared_material_ids = {
            int(node.props[0][1]) for node in shared_material_nodes
        }
        shared_oo_edges = {
            (int(node.props[1][1]), int(node.props[2][1]))
            for node in shared_media_connections.children
            if node.name == b"C"
            and len(node.props) >= 3
            and node.props[0] == ("S", "OO")
        }
        if sum(
            1
            for source, target in shared_oo_edges
            if source in shared_video_ids and target in shared_texture_ids
        ) != 2:
            raise AssertionError("every MRL shared Texture must be owned by one Video")
        shared_property_edges = {
            (int(node.props[1][1]), int(node.props[2][1]), str(node.props[3][1]))
            for node in shared_media_connections.children
            if node.name == b"C"
            and len(node.props) == 4
            and node.props[0] == ("S", "OP")
        }
        for shared_material_id in shared_material_ids:
            material_edges = {
                property_value
                for source, target, property_value in shared_property_edges
                if source in shared_texture_ids and target == shared_material_id
            }
            if ("DiffuseColor" not in material_edges):
                raise AssertionError("MRL shared Texture is missing its DiffuseColor edge")
        checks["mrl_embedded_shared_video_media_contract"] = (
            "unique-video-content-plus-shared-texture-links"
        )
        fixture_geometry = next(
            node
            for node in fixture_objects.children
            if node.name == b"Geometry"
            and any(
                prop_kind == "S" and "Mesh_001_REACHABILITY_FIXTURE" in str(prop_value)
                for prop_kind, prop_value in node.props
            )
        )
        fixture_smoothing = next(
            child for child in fixture_geometry.children if child.name == b"LayerElementSmoothing"
        )
        fixture_smoothing_version = next(
            child for child in fixture_smoothing.children if child.name == b"Version"
        )
        if fixture_smoothing_version.props != [("I", 102)]:
            raise AssertionError("FBX LayerElementSmoothing version regression")
        fixture_smoothing_values = next(
            child for child in fixture_smoothing.children if child.name == b"Smoothing"
        )
        if fixture_smoothing_values.props != [("i", [1, 2])]:
            raise AssertionError("V4 face-ordinal smoothing-group regression")
        tiny_bones, tiny_weights = _normalise_skin_pairs(
            [1, 2, 1, 1],
            [32766.0 / 32767.0, 1.0 / 32767.0, 32766.0 / 32767.0, 32766.0 / 32767.0],
        )
        if tiny_bones != [1, 2] or len(tiny_weights) != 2 or not (0.0 < tiny_weights[1] < 0.0001):
            raise AssertionError("V4 positive Skin lane below 1e-4 was discarded")
        v4_rotation_fixture = [
            0.9871689598094111, 0.12814087697585785, -0.09527517305865194, 0.0,
            -0.13513453874132597, 0.9882826211614991, -0.07096564229450797, 0.0,
            0.08506525252138017, 0.08292988817869654, 0.9929181905164672, 0.0,
            -27.963558395421984, -7.518925893618018, 3.1797027304389758, 1.0000001192092896,
        ]
        _fixture_translation, fixture_rotation, _fixture_scale = _require_local_trs_round_trip(
            v4_rotation_fixture,
            label="V4 b_37_38 regression fixture",
        )
        expected_rotation = (-4.088080536, 5.467157912, 7.396005347)
        if max(abs(actual - expected) for actual, expected in zip(fixture_rotation, expected_rotation)) > 1.0e-6:
            raise AssertionError("FBX XYZ Rz*Ry*Rx extraction regression")
        float32_near_gimbal_fixture = [
            0.016725549170448138, 0.045520055462852024, -0.9988233980817659, 0.0,
            -0.1585042939601241, 0.986451714525591, 0.04230134401794693, 0.0,
            0.9872166247965309, 0.15761037966426134, 0.02371294894003499, 0.0,
            -8.199996948242188, 1.999999999999999, -9.999999999999998, 1.0,
        ]
        near_gimbal_translation, near_gimbal_rotation, near_gimbal_scale = _require_local_trs_round_trip(
            float32_near_gimbal_fixture,
            label="edited b_51_52 float32 near-gimbal regression fixture",
        )
        near_gimbal_rebuilt = _local_trs_to_max_row_matrix(
            near_gimbal_translation,
            near_gimbal_rotation,
            near_gimbal_scale,
        )
        if max(
            abs(source - rebuilt)
            for source, rebuilt in zip(float32_near_gimbal_fixture, near_gimbal_rebuilt)
        ) > 2.0e-6:
            raise AssertionError("float32 near-gimbal bone axis drift was not repaired before FBX Euler extraction")
        sheared_fixture = [
            1.0, 0.0, 0.0, 0.0,
            0.001, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        ]
        try:
            _require_local_trs_round_trip(sheared_fixture, label="invalid shear regression fixture")
        except ValueError as exc:
            if "round-trip mismatch" not in str(exc):
                raise
        else:
            raise AssertionError("FBX XYZ TRS guard accepted a genuinely sheared bone matrix")
        reflected_fixture = [
            -2.0, 0.0, 0.0, 0.0,
            0.0, 3.0, 0.0, 0.0,
            0.0, 0.0, 4.0, 0.0,
            5.0, 6.0, 7.0, 1.0,
        ]
        reflected_translation, reflected_rotation, reflected_scale = _require_local_trs_round_trip(
            reflected_fixture,
            label="reflected bone regression fixture",
        )
        reflected_rebuilt = _local_trs_to_max_row_matrix(
            reflected_translation,
            reflected_rotation,
            reflected_scale,
        )
        if reflected_scale[0] >= 0.0 or max(
            abs(source - rebuilt)
            for source, rebuilt in zip(reflected_fixture, reflected_rebuilt)
        ) > 1.0e-12:
            raise AssertionError("FBX reflected bone transform no longer preserves deterministic negative X scale")
        fixture_mesh_model = next(
            node
            for node in fixture_objects.children
            if node.name == b"Model"
            and len(node.props) > 2
            and node.props[2] == ("S", "Mesh")
            and str(node.props[1][1]).split("\x00", 1)[0] == "Mesh_001_REACHABILITY_FIXTURE"
        )
        fixture_model_props = next(child for child in fixture_mesh_model.children if child.name == b"Properties70")
        if not any(
            prop.name == b"P" and prop.props and prop.props[0] == ("S", "DefaultAttributeIndex")
            for prop in fixture_model_props.children
        ):
            raise AssertionError("FBX Mesh Model DefaultAttributeIndex regression")
        fixture_model_property_rows = {
            str(row.props[0][1]): row.props
            for row in fixture_model_props.children
            if row.name == b"P" and row.props
        }
        fixture_model_udp = fixture_model_property_rows.get("UDP3DSMAX", [])
        fixture_model_user_properties = str(fixture_model_udp[-1][1]) if fixture_model_udp else ""
        expected_mesh_user_properties = (
            f"CodexRe6ImportContractId = {fixture_contract_id}\r\n",
            f"CodexRe6RequestSha256 = {fixture_request_sha256}\r\n",
            f"CodexRe6ImportSourceSha256 = {'0' * 64}\r\n",
            'CodexRe6ImportSourceName = "source.mod"\r\n',
            'CodexRe6SourceFVF = "0x14D40019"\r\n',
            'CodexRe6ParserFVF = "0x14D40020"\r\n',
        )
        if any(value not in fixture_model_user_properties for value in expected_mesh_user_properties):
            raise AssertionError("FBX Mesh UDP3DSMAX lost request/source/parser identity fields")
        fixture_bound_model = next(
            node
            for node in fixture_objects.children
            if node.name == b"Model"
            and len(node.props) > 1
            and str(node.props[1][1]).split("\x00", 1)[0] == "BoundSphere"
        )
        fixture_bound_props = next(child for child in fixture_bound_model.children if child.name == b"Properties70")
        fixture_bound_property_rows = {
            str(row.props[0][1]): row.props for row in fixture_bound_props.children if row.name == b"P" and row.props
        }
        fixture_bound_udp = fixture_bound_property_rows.get("UDP3DSMAX", [])
        if not fixture_bound_udp or "CodexRe6BoundRadius = 1.25\r\n" not in str(fixture_bound_udp[-1][1]):
            raise AssertionError("BoundSphere exact-radius Max user property regression")
        expected_bound_translations = {
            "BoundSphere": [2.0, 4.0, -3.0],
            "BoundBoxMin": [-5.0, -7.0, -6.0],
            "BoundBoxMax": [8.0, 10.0, 9.0],
        }
        for bound_name, expected_translation in expected_bound_translations.items():
            actual_translation = _guard_fbx_model_translation(reachability_roots, bound_name)
            if max(
                abs(actual - expected)
                for actual, expected in zip(actual_translation, expected_translation)
            ) > 1.0e-12:
                raise AssertionError(f"{bound_name} final FBX position regression")
        fixture_global_settings = next(root for root in reachability_roots if root.name == b"GlobalSettings")
        fixture_global_props = next(
            child for child in fixture_global_settings.children if child.name == b"Properties70"
        )
        unit_values = {
            str(prop.props[0][1]): float(prop.props[-1][1])
            for prop in fixture_global_props.children
            if prop.name == b"P"
            and prop.props
            and prop.props[0] in {("S", "UnitScaleFactor"), ("S", "OriginalUnitScaleFactor")}
        }
        expected_unit_values = {
            "UnitScaleFactor": FBX_UNIT_SCALE_FACTOR,
            "OriginalUnitScaleFactor": FBX_UNIT_SCALE_FACTOR,
        }
        if unit_values != expected_unit_values:
            raise AssertionError("FBX Max-compatible inch unit metadata regression")

        root_world_fixture = [
            2.0, 0.0, 0.0, 0.0,
            0.0, 2.0, 0.0, 0.0,
            0.0, 0.0, 2.0, 0.0,
            4.0, -6.0, 8.0, 1.0,
        ]
        child_world_fixture = [
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            4.0, -5.0, 8.0, 1.0,
        ]
        unused_world_fixture = list(child_world_fixture)
        unused_world_fixture[13] = -4.0
        skin_fixture = {
            "contract_id": "re6mod-skin-bind-fixture",
            "request_contract_sha256": "3" * 64,
            "source": {"sha256": "1" * 64},
            "bounds": [
                [0.0, 0.0, 0.0, 1.0],
                [-1.0, -1.0, -1.0, 0.0],
                [1.0, 1.0, 1.0, 0.0],
            ],
            "bones": [
                {
                    "slot": 1,
                    "name": "b_256_1",
                    "anim_map_id": 0,
                    "parent_slot": None,
                    "max_local_matrix": root_world_fixture,
                    "max_world_matrix": root_world_fixture,
                },
                {
                    "slot": 2,
                    "name": "b_1_2",
                    "anim_map_id": 1,
                    "parent_slot": 1,
                    "max_local_matrix": _matrix_multiply(child_world_fixture, _matrix_inverse(root_world_fixture)),
                    "max_world_matrix": child_world_fixture,
                },
                {
                    "slot": 3,
                    "name": "b_1_3",
                    "anim_map_id": 2,
                    "parent_slot": 1,
                    "max_local_matrix": _matrix_multiply(unused_world_fixture, _matrix_inverse(root_world_fixture)),
                    "max_world_matrix": unused_world_fixture,
                },
            ],
            "root_mesh_scale": [2.0, 2.0, 2.0],
            "scene_helpers": {"lod_groups": ["LodGroup_0"], "other_mesh": "OtherMesh"},
            "meshes": [
                {
                    "physical_slot": 1,
                    "display_slot": 1,
                    "node_name": "Mesh_001_SKIN_BIND_FIXTURE",
                    "source_fvf": 0x14D40020,
                    "parser_fvf": 0x14D40020,
                    "parent_name": "LodGroup_0",
                    "positions": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                    "faces": [[0, 1, 2]],
                    "uv1": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                    "max_normals": [],
                    "raw_skin_bones": [[1, 2], [2], [1]],
                    "raw_skin_weights": [[0.75, 0.25], [1.0], [1.0]],
                    "fbx_skin_bones": [[1, 2], [2], [1]],
                    "fbx_skin_weights": [[0.75, 0.25], [1.0], [1.0]],
                    "skin_bone_limit": 4,
                }
            ],
        }
        skin_roots = _build_fbx_roots(skin_fixture, include_normals=False, route_file_name="skin.normal-route.json")
        skin_objects = next(root for root in skin_roots if root.name == b"Objects")
        skin_poses = [node for node in skin_objects.children if node.name == b"Pose"]
        if len(skin_poses) != 1:
            raise AssertionError("FBX must contain one scene BindPose, not one Pose per skinned Mesh")
        pose_count_node = next(child for child in skin_poses[0].children if child.name == b"NbPoseNodes")
        pose_nodes = [child for child in skin_poses[0].children if child.name == b"PoseNode"]
        if pose_count_node.props != [("I", 5)] or len(pose_nodes) != 5:
            raise AssertionError("complete scene BindPose must include bones, skinned Mesh and helper parent")

        fixture_models = {
            str(node.props[1][1]).split("\x00", 1)[0]: node
            for node in skin_objects.children
            if node.name == b"Model" and len(node.props) > 2
        }
        if fixture_models["b_256_1"].props[2] != ("S", "LimbNode"):
            raise AssertionError("deforming root bone lost LimbNode classification")
        if fixture_models["b_1_3"].props[2] != ("S", "Null"):
            raise AssertionError("non-deforming V4 Dummy must stay an FBX Null")
        fixture_limb_attributes = [
            node
            for node in skin_objects.children
            if node.name == b"NodeAttribute" and len(node.props) > 2 and node.props[2] == ("S", "LimbNode")
        ]
        if len(fixture_limb_attributes) != 2:
            raise AssertionError("LimbNode attribute count must equal the deforming bone union")
        root_props = next(child for child in fixture_models["b_256_1"].children if child.name == b"Properties70")
        root_property_rows = {str(row.props[0][1]): row.props for row in root_props.children if row.name == b"P" and row.props}
        if root_property_rows.get("PreRotation", [])[-3:] != [("D", -90.0), ("D", -0.0), ("D", 0.0)]:
            raise AssertionError("root FBX axis conversion must use PreRotation")
        if root_property_rows.get("Lcl Rotation", [])[-3:] != [("D", 0.0), ("D", 0.0), ("D", 0.0)]:
            raise AssertionError("root Lcl Rotation must stay neutral when PreRotation carries the axis conversion")
        if root_property_rows.get("InheritType", [])[-1:] != [("I", 1)]:
            raise AssertionError("Max RSrs InheritType regression")
        udp_row = root_property_rows.get("UDP3DSMAX", [])
        if not udp_row or "CodexV4ExportBoneSlot = 0\r\n" not in str(udp_row[-1][1]):
            raise AssertionError("Max user properties must be serialized through UDP3DSMAX")
        if "CodexRe6MaxWorldTM = " in str(udp_row[-1][1]):
            raise AssertionError("retired Max bone world-matrix override leaked into FBX")

        fixture_clusters = [
            node
            for node in skin_objects.children
            if node.name == b"Deformer" and len(node.props) > 2 and node.props[2] == ("S", "Cluster")
        ]
        root_cluster = next(node for node in fixture_clusters if "_1\x00" in str(node.props[1][1]))
        root_cluster_transform = next(child for child in root_cluster.children if child.name == b"Transform").props[0][1]
        expected_cluster_transform = _matrix_multiply(_scale_matrix([2.0, 2.0, 2.0]), _matrix_inverse(root_world_fixture))
        if max(abs(float(actual) - float(expected)) for actual, expected in zip(root_cluster_transform, expected_cluster_transform)) > 1.0e-9:
            raise AssertionError("Max-space bone-specific Cluster Transform regression")
        expected_skin_graph = {
            "b_256_1": {
                "Indexes": [0, 2],
                "Weights": [0.75, 1.0],
                "Transform": _matrix_to_fbx_array(expected_cluster_transform),
                "TransformLink": _matrix_to_fbx_array(
                    _matrix_multiply(root_world_fixture, MAX_TO_FBX_YUP_MATRIX)
                ),
            },
            "b_1_2": {
                "Indexes": [0, 1],
                "Weights": [0.25, 1.0],
                "Transform": _matrix_to_fbx_array(
                    _matrix_multiply(
                        _scale_matrix([2.0, 2.0, 2.0]),
                        _matrix_inverse(child_world_fixture),
                    )
                ),
                "TransformLink": _matrix_to_fbx_array(
                    _matrix_multiply(child_world_fixture, MAX_TO_FBX_YUP_MATRIX)
                ),
            },
        }
        expected_bone_parents = {
            "b_256_1": None,
            "b_1_2": "b_256_1",
            "b_1_3": "b_256_1",
        }
        _guard_require_fbx_skin_graph(
            skin_roots,
            mesh_name="Mesh_001_SKIN_BIND_FIXTURE",
            bone_parents=expected_bone_parents,
            clusters=expected_skin_graph,
        )

        skin_by_id, skin_edges = _guard_fbx_objects_and_edges(skin_roots)
        skin_named_models = {
            _guard_fbx_object_name(node): object_id
            for object_id, node in skin_by_id.items()
            if node.name == b"Model"
        }
        skin_cluster_ids = {
            object_id
            for object_id, node in skin_by_id.items()
            if node.name == b"Deformer" and len(node.props) > 2 and node.props[2] == ("S", "Cluster")
        }
        root_cluster_id = next(
            cluster_id
            for cluster_id in skin_cluster_ids
            if (skin_named_models["b_256_1"], cluster_id) in skin_edges
        )
        mutation_cluster = skin_by_id[root_cluster_id]

        mutation_weights = next(child for child in mutation_cluster.children if child.name == b"Weights")
        original_weight_props = mutation_weights.props
        mutation_weights.props = [("d", [0.0] * len(original_weight_props[0][1]))]
        try:
            _guard_require_fbx_skin_graph(
                skin_roots,
                mesh_name="Mesh_001_SKIN_BIND_FIXTURE",
                bone_parents=expected_bone_parents,
                clusters=expected_skin_graph,
            )
        except AssertionError:
            pass
        else:
            raise AssertionError("FBX Skin graph guard accepted zeroed Cluster weights")
        finally:
            mutation_weights.props = original_weight_props

        mutation_indexes = next(child for child in mutation_cluster.children if child.name == b"Indexes")
        original_index_props = mutation_indexes.props
        mutation_indexes.props = [("i", [99] * len(original_index_props[0][1]))]
        try:
            _guard_require_fbx_skin_graph(
                skin_roots,
                mesh_name="Mesh_001_SKIN_BIND_FIXTURE",
                bone_parents=expected_bone_parents,
                clusters=expected_skin_graph,
            )
        except AssertionError:
            pass
        else:
            raise AssertionError("FBX Skin graph guard accepted wrong Cluster indexes")
        finally:
            mutation_indexes.props = original_index_props

        skin_connections = next(root for root in skin_roots if root.name == b"Connections")
        disconnected_edge_index = next(
            index
            for index, node in enumerate(skin_connections.children)
            if node.name == b"C"
            and len(node.props) >= 3
            and node.props[0] == ("S", "OO")
            and int(node.props[1][1]) == skin_named_models["b_1_2"]
            and int(node.props[2][1]) in skin_cluster_ids
        )
        disconnected_edge = skin_connections.children.pop(disconnected_edge_index)
        try:
            _guard_require_fbx_skin_graph(
                skin_roots,
                mesh_name="Mesh_001_SKIN_BIND_FIXTURE",
                bone_parents=expected_bone_parents,
                clusters=expected_skin_graph,
            )
        except AssertionError:
            pass
        else:
            raise AssertionError("FBX Skin graph guard accepted a disconnected Bone/Cluster edge")
        finally:
            skin_connections.children.insert(disconnected_edge_index, disconnected_edge)
        checks.update(
            {
                "fvf_layout_count": len(FVF_LAYOUTS),
                "default_embedded_normals": True,
                "route_schema": IMPORT_ROUTE_SCHEMA,
                "fbx_version": FBX_BINARY_VERSION,
                "fbx_root_model_count": reachability["root_model_count"],
                "fbx_reachable_model_count": reachability["reachable_model_count"],
                "fbx_xyz_rotation_contract": "Rz*Ry*Rx",
                "fbx_xyz_float32_axis_drift_contract": "orthonormalize-before-euler",
                "fbx_xyz_shear_rejection_contract": "strict-round-trip",
                "fbx_reflection_contract": "negative-x-scale",
                "mesh_smoothing_contract": "1-based-face-ordinal",
                "skin_tiny_positive_lane_contract": "preserved",
                "skin_cluster_transform_contract": "root-scale*inverse(max-space-bone-bind)",
                "skin_final_graph_contract": "bone-hierarchy+skin-geometry+cluster-skin+bone-cluster+payloads",
                "skin_graph_mutation_contract": "zero-weights+wrong-indexes+disconnected-edge",
                "skin_bind_pose_contract": "single-complete-scene-pose",
                "bone_model_type_contract": "deforming-limb-unused-null",
                "max_user_property_contract": "UDP3DSMAX",
                "max_post_import_bounds_contract": "native-sphere-point-from-exact-user-properties",
                "max_post_import_bone_matrix_contract": "native-fbx-unit-metadata-no-override",
                "da55_stride_contract": "writer-record-28/header-normal-stride",
                "bone_map_contract": "one-based-row-start/local-slot-to-global-strict",
                "writer_skin_layout_contract": "0CB-667-0D9E-DA-77-75C3-D84E",
                "legacy8_skin_contract": "eight-influences-normalized",
                "duplicate_global_bone_contract": "aggregate-positive-then-stable-capacity-normalize",
                "legacy4_duplicate_filler_contract": "first-occurrence-only-then-normalize",
                "c31_same_bone_zero_contract": "implicit-tail-full-weight",
                "packed_normal_contract": "component-clamp-before-normalize",
                "zero_uv_map1_contract": "four-fvf-zero-map1",
                "invalid_face_contract": "whole-face-degenerate",
                "cdxm_contract": "partial-first-hit-with-slot-fallback",
                "parser_fvf_contract": "LP2+7-then-DMC-1",
                "top4_skin_contract": "aggregate-duplicates-then-descending-source-ties",
                "bb_zero_skin_contract": "eighth-slot-fallback-exact-byte-compatible",
                "request_contract_sha256": fixture_request_sha256,
                "direct_artifact_identity_contract": True,
            }
        )
        result = {
            "status": "PASS",
            "strict": True,
            "blocking_user_import": False,
            "checks": checks,
        }
    except Exception as exc:
        result = {
            "status": "FAIL",
            "strict": True,
            "blocking_user_import": False,
            "checks": checks,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    IMPORT_MODULE_REGRESSION_STATUS = result
    return dict(result)


if bool(globals().get("__codex_trusted_runtime_fast_load__", False)):
    IMPORT_MODULE_REGRESSION_STATUS = {
        "status": "PASS",
        "mode": "trusted_source_sha_fast_load",
        "source_sha256": str(globals().get("__codex_source_sha256__", "") or ""),
        "blocking_user_import": False,
    }
else:
    IMPORT_MODULE_REGRESSION_STATUS = {
        "status": "DEFERRED",
        "mode": "explicit_maintenance_only",
        "blocking_user_import": False,
    }


def build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parse a RE6 MOD, always write a normal route receipt, and build an FBX 7.4 import scene.",
    )
    parser.add_argument("--mod", required=True, help="Source .MOD file.")
    parser.add_argument("--fbx", required=True, help="Output FBX file.")
    parser.add_argument(
        "--mrl",
        help="Optional source .MRL. Enables MRL Base Color material binding and embedded DDS media.",
    )
    parser.add_argument(
        "--texture-mode",
        choices=sorted(MRL_TEXTURE_SOURCE_MODES),
        default="dds",
        help="Resolve matching MRL texture resources from DDS directly or decode TEX to DDS before embedding.",
    )
    parser.add_argument(
        "--texture-root",
        action="append",
        default=[],
        help="Optional bounded texture search root; may be supplied more than once.",
    )
    parser.add_argument("--normal-route", help="Normal route JSON path; defaults beside the FBX.")
    parser.add_argument("--manifest", help="Full import manifest JSON path; defaults beside the FBX.")
    parser.add_argument(
        "--include-normals",
        dest="include_normals",
        action="store_true",
        default=True,
        help="Embed explicit MOD normals into the FBX Editable Mesh (default).",
    )
    parser.add_argument(
        "--no-normals",
        dest="include_normals",
        action="store_false",
        help="Generate the legacy PC-REHD 1.2.8 FBX without an explicit normal layer.",
    )
    parser.add_argument("--fix-lp2", action="store_true", help="Apply the LP2 parser FVF +7 adjustment.")
    parser.add_argument("--fix-dmc", action="store_true", help="Apply the DMC parser FVF -1 adjustment.")
    parser.add_argument(
        "--fix-processing-mode",
        choices=sorted(FIX_PROCESSING_MODES),
        default=FIX_PROCESSING_MODE_CODEX,
        help="Choose CODE X or the legacy PC 1.2.8 FVF dispatch semantics.",
    )
    parser.add_argument("--pretty-json", action="store_true", help="Pretty-print route and manifest files.")
    parser.add_argument("--verify", action="store_true", help="Read the output through codex_fbx_probe/ufbx when available.")
    parser.add_argument("--status", help="Optional machine-readable result JSON path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_cli()
    args = parser.parse_args(argv)
    result: dict[str, Any]
    exit_code = 0
    started = time.perf_counter()
    try:
        mod_path = _windows_lexical_full_path(args.mod)
        output_fbx = _windows_lexical_full_path(args.fbx)
        result = build_import_artifacts(
            mod_path,
            output_fbx,
            normal_route_path=args.normal_route,
            manifest_path=args.manifest,
            include_normals=bool(args.include_normals),
            fix_lp2=bool(args.fix_lp2),
            fix_dmc=bool(args.fix_dmc),
            fix_processing_mode=str(args.fix_processing_mode),
            mrl_path=args.mrl,
            texture_mode=str(args.texture_mode),
            texture_roots=tuple(args.texture_root),
            pretty_json=bool(args.pretty_json),
        )
        result["detail"] = "Python import FBX and route receipt are ready."
        if bool(args.verify):
            result["verify"] = verify_fbx_artifact(output_fbx, result)
            if result["verify"].get("status") == "FAIL":
                result["status"] = "VERIFY_FAIL"
                result["detail"] = "Generated FBX failed the Python verification contract."
                exit_code = 2
    except Exception as exc:
        result = {
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "detail": str(exc),
            "contract_revision": IMPORT_MODULE_CONTRACT_REVISION,
        }
        diagnostic = getattr(exc, "diagnostic", None)
        if isinstance(diagnostic, dict):
            result["diagnostic"] = dict(diagnostic)
        exit_code = 1
    result["python_elapsed_ms"] = int(round((time.perf_counter() - started) * 1000.0))
    if args.status:
        _atomic_json_write(_windows_lexical_full_path(args.status), result, pretty=True)
    print(runtime_json_dumps_text(result, pretty=True))
    return exit_code


# END IMPORT ARTIFACT TRANSACTION AND SELF-TEST
# ============================================================================


if __name__ == "__main__":
    raise SystemExit(main())
