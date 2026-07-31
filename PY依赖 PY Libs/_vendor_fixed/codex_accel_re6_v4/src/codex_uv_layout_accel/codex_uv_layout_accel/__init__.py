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

from ._uv_layout_core import build_layout_split_core

__all__ = [
    "build_fbx_tv_direct_layout",
    "BOOTSTRAP_ACCELERATOR_CONTRACT_REVISION",
    "PRESERVES_EXPORT_VERTEX_SPLITS",
    "__version__",
]
__version__ = "0.2.0"
BOOTSTRAP_ACCELERATOR_CONTRACT_REVISION = 2
PRESERVES_EXPORT_VERTEX_SPLITS = True


def _safe_int_list(values: object) -> list[int] | None:
    if not isinstance(values, list):
        return None
    out: list[int] = []
    for value in values:
        try:
            out.append(int(value))
        except Exception:
            return None
    return out


def build_fbx_tv_direct_layout(
    mesh: dict[str, object],
    *,
    selected_channel: int,
    use_half_safe: bool,
) -> dict[str, object] | None:
    try:
        import codex_python_export_bridge as bridge

        if not isinstance(mesh, dict):
            return None

        export_face_indices = _safe_int_list(mesh.get("fbx_export_face_indices"))
        uses_export_row_indices = export_face_indices is not None and len(export_face_indices) >= 3
        geom_face_indices = (
            export_face_indices
            if uses_export_row_indices
            else _safe_int_list(mesh.get("fbx_geom_face_indices"))
        )
        if geom_face_indices is None or len(geom_face_indices) < 3:
            return None

        uv_channel = bridge._select_fbx_uv_channel(mesh, int(selected_channel))
        if not isinstance(uv_channel, dict):
            return None

        raw_uv_values = uv_channel.get("values")
        uv_corner_indices = _safe_int_list(uv_channel.get("corner_indices"))
        if not isinstance(raw_uv_values, list) or uv_corner_indices is None:
            return None
        if len(raw_uv_values) <= 0 or len(uv_corner_indices) != len(geom_face_indices):
            return None

        positions = mesh.get("positions")
        source_vertex_count = len(positions) if isinstance(positions, list) else 0
        if not uses_export_row_indices:
            source_vertex_indices = mesh.get("source_vertex_indices")
            if isinstance(source_vertex_indices, list):
                for source_index in source_vertex_indices:
                    try:
                        source_vertex_count = max(source_vertex_count, int(source_index) + 1)
                    except Exception:
                        continue
            for geom_index in geom_face_indices:
                source_vertex_count = max(source_vertex_count, int(geom_index) + 1)

        prepared_uv_values = [bridge._coerce_vec2(value) for value in raw_uv_values]
        island_data = bridge._build_uv_island_data(prepared_uv_values, uv_corner_indices)
        prepared_export_uvs = [
            bridge._prepare_export_uv_value(
                prepared_uv_values[tv_index],
                tv_index,
                island_data,
                use_half_safe=bool(use_half_safe),
                use_atlas_safe=bool(use_half_safe),
            )
            for tv_index in range(len(prepared_uv_values))
        ]

        layout = build_layout_split_core(
            geom_face_indices,
            uv_corner_indices,
            prepared_export_uvs,
            int(source_vertex_count),
        )
        if not isinstance(layout, dict):
            return None
        layout["selected_uv_channel"] = int(selected_channel)
        if uses_export_row_indices:
            layout["source_row_indices"] = list(layout.get("source_vertex_indices", []))
        return layout
    except Exception:
        return None
