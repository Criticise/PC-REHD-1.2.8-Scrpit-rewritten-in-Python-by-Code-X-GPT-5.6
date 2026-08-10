from __future__ import annotations

# =============================================================================
# AI MAINTENANCE GATE: ACCELERATOR PACKAGE <-> BOOTSTRAP
# READ THIS BEFORE CHANGING VERSION, CAPABILITIES, C/PYD API, OR BUILD BEHAVIOR.
#
# This package is maintained together with codex_python_runtime_bootstrap.py.
# Any semantic dependency change MUST update the bootstrap health contract,
# setup.py, cp312/cp314 wrappers, release copies, accelerator README, and
# PYTHON_RUNTIME_MAINTENANCE_CHARTER.md in the same task. Do not change only
# this package. Search for "AI MAINTENANCE GATE" before finishing.
# =============================================================================

from ._fbx_geometry_core import extract_geometry_core

__all__ = [
    "extract_mesh_geometry",
    "BOOTSTRAP_ACCELERATOR_CONTRACT_REVISION",
    "PRESERVES_CORNER_NORMALS",
    "PRESERVES_RE6_NORMAL_BYTES",
    "USES_INVERSE_TRANSPOSE_NORMALS",
    "__version__",
]
__version__ = "0.3.0"
BOOTSTRAP_ACCELERATOR_CONTRACT_REVISION = 2
PRESERVES_CORNER_NORMALS = True
PRESERVES_RE6_NORMAL_BYTES = True
USES_INVERSE_TRANSPOSE_NORMALS = True


def _safe_int_list(values: object) -> list[int] | None:
    try:
        iterator = list(values)  # type: ignore[arg-type]
    except Exception:
        return None
    out: list[int] = []
    for value in iterator:
        try:
            out.append(int(value))
        except Exception:
            return None
    return out


def _safe_face_rows(values: object) -> list[list[int]] | None:
    try:
        iterator = list(values)  # type: ignore[arg-type]
    except Exception:
        return None
    out: list[list[int]] = []
    for value in iterator:
        try:
            face_begin = int(value[0])
            face_size = int(value[1])
        except Exception:
            return None
        out.append([face_begin, face_size])
    return out


def _build_normal_split_source_stream(
    probe: object,
    positions_src: object,
    normals_src: object,
    geom_indices: list[int],
    *,
    vertex_count: int,
    node_to_world: object,
) -> tuple[list[object], list[list[float]], list[int], list[int], list[int]]:
    positions_count = len(positions_src)  # type: ignore[arg-type]
    index_count = len(geom_indices)
    fallback_normals = probe._build_source_vertex_normals(  # type: ignore[attr-defined]
        positions_count,
        normals_src,
        geom_indices,
        vertex_count=vertex_count,
        index_count=index_count,
    )
    split_positions: list[object] = []
    split_normals: list[list[float]] = []
    split_source_indices: list[int] = []
    split_source_corner_indices: list[int] = []
    split_geom_indices: list[int] = []
    split_lookup: dict[tuple[int, tuple[int, int, int]], int] = {}
    prepared_transform = probe._prepare_row_major_transform(node_to_world)  # type: ignore[attr-defined]
    normal_payload_cache: dict[tuple[object, ...], tuple[list[float], tuple[int, int, int]]] = {}
    normals_count = len(normals_src) if normals_src is not None else 0  # type: ignore[arg-type]

    for corner_index, position_index in enumerate(geom_indices):
        normal_index = probe._resolve_vertex_attr_index(  # type: ignore[attr-defined]
            normals_count,
            position_index=position_index,
            corner_index=corner_index,
            vertex_count=vertex_count,
            index_count=index_count,
        )
        if normal_index is not None and 0 <= normal_index < normals_count:
            raw_values = probe._normal_vec3_to_list(normals_src[normal_index])  # type: ignore[attr-defined,index]
            cache_key = ("source", raw_values[0], raw_values[1], raw_values[2])
        elif 0 <= position_index < len(fallback_normals):
            raw_values = fallback_normals[position_index]
            cache_key = ("fallback", raw_values[0], raw_values[1], raw_values[2])
        else:
            cache_key = ("default", 0.0, 0.0, 1.0)
        cached_payload = normal_payload_cache.get(cache_key)
        if cached_payload is None:
            normal = probe._resolve_corner_normal(  # type: ignore[attr-defined]
                normals_src,
                fallback_normals,
                position_index=position_index,
                corner_index=corner_index,
                vertex_count=vertex_count,
                index_count=index_count,
            )
            normal_key = probe._encode_re6_normal_key_from_fbx_local(  # type: ignore[attr-defined]
                normal,
                prepared_transform,
            )
            normal_payload_cache[cache_key] = (normal, normal_key)
        else:
            normal, normal_key = cached_payload
        split_key = (position_index, normal_key)
        split_index = split_lookup.get(split_key)
        if split_index is None:
            split_index = len(split_positions)
            split_lookup[split_key] = split_index
            split_positions.append(positions_src[position_index])  # type: ignore[index]
            split_normals.append(list(normal))
            split_source_indices.append(position_index)
            split_source_corner_indices.append(corner_index)
        split_geom_indices.append(split_index)

    return split_positions, split_normals, split_source_indices, split_source_corner_indices, split_geom_indices


def extract_mesh_geometry(mesh: object, instance_node: object | None = None) -> dict[str, object] | None:
    try:
        import codex_fbx_probe as probe

        positions_src = getattr(mesh, "vertex_positions", None)
        normals_src = getattr(mesh, "vertex_normals", None)
        indices_src = getattr(mesh, "indices", None)
        faces = _safe_face_rows(getattr(mesh, "faces", []) or [])
        positions_count = len(positions_src) if positions_src is not None else 0
        index_count = len(indices_src) if indices_src is not None else 0
        vertex_count = int(getattr(mesh, "num_vertices", positions_count) or positions_count)

        if positions_src is None or indices_src is None or faces is None:
            return None

        geom_indices = _safe_int_list(probe._safe_list(indices_src))
        if geom_indices is None:
            return None

        uv_channels = probe._extract_mesh_uv_channels(
            mesh,
            geom_indices=geom_indices,
            vertex_count=vertex_count,
        )
        if not isinstance(uv_channels, list) or len(uv_channels) <= 0:
            return None
        default_uv_channel = uv_channels[0]
        if not isinstance(default_uv_channel, dict):
            return None
        default_values = default_uv_channel.get("values")
        default_corner_indices = default_uv_channel.get("corner_indices")
        if not isinstance(default_values, list) or not isinstance(default_corner_indices, list):
            return None
        if len(default_corner_indices) != len(geom_indices):
            return None

        node_to_world = getattr(instance_node, "node_to_world", None)
        (
            split_positions,
            split_normals,
            split_source_indices,
            split_source_corner_indices,
            split_geom_indices,
        ) = _build_normal_split_source_stream(
            probe,
            positions_src,
            normals_src,
            geom_indices,
            vertex_count=vertex_count,
            node_to_world=node_to_world,
        )
        matrix = probe._flatten_matrix4x4(node_to_world) if node_to_world is not None else []

        geometry = extract_geometry_core(
            split_positions,
            split_normals,
            split_geom_indices,
            faces,
            uv_channels,
            len(split_positions),
            matrix,
        )
        if not isinstance(geometry, dict):
            return None

        geometry["fbx_export_face_indices"] = list(geometry.get("face_indices", []))
        export_split_indices = geometry.get("source_vertex_indices")
        if isinstance(export_split_indices, list):
            geometry["fbx_export_corner_indices"] = [
                split_source_corner_indices[int(value)]
                if 0 <= int(value) < len(split_source_corner_indices)
                else 0
                for value in export_split_indices
            ]
        for key in ("source_vertex_indices", "fbx_geom_face_indices"):
            values = geometry.get(key)
            if not isinstance(values, list):
                continue
            geometry[key] = [
                split_source_indices[int(value)] if 0 <= int(value) < len(split_source_indices) else 0
                for value in values
            ]
        return geometry
    except Exception:
        return None
