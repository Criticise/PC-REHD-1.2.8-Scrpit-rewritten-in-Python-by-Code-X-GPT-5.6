"""Isolated RE6 scene compatibility rules shared by import and export.

This module owns only the compatibility cases that cannot safely use ordinary
scene geometry.  It deliberately has no Max, Blender, FBX, file-system, or UI
dependency so the same answer is available to the Importer, Launcher, and
Writer without asking either DCC application to make a routing decision.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, MutableMapping


COMPATIBILITY_MODULE_CONTRACT_REVISION = 2
IMPORT_SKIN_COMPATIBILITY_SCHEMA = "codex-re6-mesh-skin-compatibility-v1"
EXPORT_COMPATIBILITY_SCHEMA = "codex-re6-scene-compatibility-v1"
EXPORT_COMPATIBILITY_ROUTE_SKIN_WITHOUT_FACES = "skin_without_scene_faces"
EXPORT_COMPATIBILITY_ROUTE_UNSKINNED_WITH_FACES = "unskinned_with_scene_faces"
CROSS_MOD_COMPATIBILITY_EXPORT_RULE = "cross_mod_compatibility_export"
CROSS_MOD_COMPATIBILITY_SCHEMA = "pc-rehd-cross-mod-skin-compatibility-v1"
CROSS_MOD_COMPATIBILITY_RECORD_LIMIT = 256
CROSS_MOD_COMPATIBILITY_FBX_BONES_JOB_KEY = "_cross_mod_compatibility_fbx_bones"
CROSS_MOD_COMPATIBILITY_DEFAULT_ENABLED = True
CROSS_MOD_COMPATIBILITY_POLICY = "nearest_target_ancestor_then_root_with_palette_fit"
_LEGACY_AUTO_ROUTE_REASONS = frozenset(
    {
        "blender_source_degenerate_faces_header_only",
        "blender_unskinned_mesh_header_only_policy",
    }
)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _int_or_default(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _finite_skin_weight(value: Any) -> float:
    try:
        weight = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return weight if math.isfinite(weight) else 0.0


def _normalize_cross_mod_bone_export_mode(value: Any) -> str:
    mode = str(value or "disabled").strip().casefold()
    if mode in {"disabled", "bones_only", "bones_plus_mesh"}:
        return mode
    return "unsupported"


def describe_cross_mod_export_compatibility(
    *,
    source_mod_available: bool,
    bone_export_mode: Any,
) -> dict[str, Any]:
    """Resolve the always-on Skin compatibility branch without UI state.

    The branch has geometry work only for ordinary export and Bones+Mesh.  A
    Bones Only export has no Mesh payload and therefore reports not applicable
    instead of changing bone tables or inventing geometry.
    """

    mode = _normalize_cross_mod_bone_export_mode(bone_export_mode)
    applicable = bool(source_mod_available) and mode in {
        "disabled",
        "bones_plus_mesh",
    }
    if not source_mod_available:
        reason = "source_mod_missing"
    elif mode == "bones_only":
        reason = "bones_only_has_no_mesh"
    elif mode == "bones_plus_mesh":
        reason = "enabled_for_bones_plus_mesh"
    elif mode == "disabled":
        reason = "enabled_for_ordinary_export"
    else:
        reason = "unsupported_export_mode"
    return {
        "schema": CROSS_MOD_COMPATIBILITY_SCHEMA,
        "default_enabled": CROSS_MOD_COMPATIBILITY_DEFAULT_ENABLED,
        "enabled": CROSS_MOD_COMPATIBILITY_DEFAULT_ENABLED,
        "applicable": applicable,
        "status": "PENDING" if applicable else "NOT_APPLICABLE",
        "reason": reason,
        "bone_export_mode": mode,
        "scope": "modify_skin_meshes_only",
        "policy": CROSS_MOD_COMPATIBILITY_POLICY,
    }


def new_cross_mod_compatibility_receipt(
    *,
    source_mod_available: bool,
    bone_export_mode: Any,
    hierarchy_bone_count: int = 0,
) -> dict[str, Any]:
    """Create the one exporter receipt owned by the compatibility module."""

    receipt = describe_cross_mod_export_compatibility(
        source_mod_available=source_mod_available,
        bone_export_mode=bone_export_mode,
    )
    receipt.update(
        {
            "hierarchy_authority": "current_fbx_probe",
            "hierarchy_bone_count": _nonnegative_int(hierarchy_bone_count),
            "remapped_influence_count": 0,
            "skeleton_ancestor_remap_count": 0,
            "skeleton_root_fallback_count": 0,
            "palette_ancestor_remap_count": 0,
            "palette_root_fallback_count": 0,
            "palette_existing_fallback_count": 0,
            "affected_vertex_count": 0,
            "affected_mesh_count": 0,
            "record_limit": CROSS_MOD_COMPATIBILITY_RECORD_LIMIT,
            "records_truncated_count": 0,
            "records": [],
        }
    )
    return receipt


def cross_mod_export_is_applicable(
    *,
    source_mod_available: bool,
    bone_export_mode: Any,
) -> bool:
    return bool(
        describe_cross_mod_export_compatibility(
            source_mod_available=source_mod_available,
            bone_export_mode=bone_export_mode,
        )["applicable"]
    )


def build_cross_mod_parent_bone_map(entries: Any) -> dict[int, int]:
    """Build a child-to-parent ID map from the current FBX hierarchy facts."""

    if not isinstance(entries, list):
        return {}
    parent_by_bone: dict[int, int] = {}
    for raw_entry in entries:
        if not isinstance(raw_entry, Mapping):
            continue
        bone_id = _int_or_default(raw_entry.get("parsed_bone_id"), -1)
        parent_id = _int_or_default(raw_entry.get("parent_parsed_bone_id"), -1)
        if bone_id < 0 or parent_id < 0 or bone_id == parent_id:
            continue
        parent_by_bone.setdefault(bone_id, parent_id)
    return parent_by_bone


def nearest_cross_mod_ancestor(
    bone_id: int,
    parent_by_bone: Mapping[int, int],
    is_allowed: Any,
) -> int | None:
    """Return the nearest usable parent while rejecting cyclic hierarchy facts."""

    if not callable(is_allowed):
        return None
    current = _int_or_default(bone_id, -1)
    if current < 0:
        return None
    visited = {current}
    while current in parent_by_bone:
        parent = _int_or_default(parent_by_bone.get(current), -1)
        if parent < 0 or parent in visited:
            return None
        if bool(is_allowed(parent)):
            return parent
        visited.add(parent)
        current = parent
    return None


def _merge_and_normalize_cross_mod_skin_row(
    bones: list[int],
    weights: list[float],
) -> tuple[list[int], list[float]]:
    ordered: list[int] = []
    totals: dict[int, float] = {}
    for bone_id, raw_weight in zip(bones, weights):
        weight = _finite_skin_weight(raw_weight)
        if weight <= 0.0:
            continue
        parsed_bone_id = _int_or_default(bone_id, -1)
        if parsed_bone_id < 0:
            continue
        if parsed_bone_id not in totals:
            ordered.append(parsed_bone_id)
            totals[parsed_bone_id] = 0.0
        totals[parsed_bone_id] += weight
    total = sum(totals.values())
    if total <= 0.000001:
        return [], []
    return ordered, [totals[bone_id] / total for bone_id in ordered]


def _record_cross_mod_compatibility_remap(
    receipt: MutableMapping[str, Any],
    affected_vertices: set[tuple[int, int]],
    affected_meshes: set[int],
    *,
    mesh_slot: int,
    scene_node: str,
    vertex_index: int,
    source_bone_id: int,
    target_bone_id: int,
    reason: str,
    weight: float,
) -> None:
    receipt["remapped_influence_count"] = _nonnegative_int(
        receipt.get("remapped_influence_count")
    ) + 1
    count_key = f"{reason}_count"
    receipt[count_key] = _nonnegative_int(receipt.get(count_key)) + 1
    normalized_slot = _nonnegative_int(mesh_slot)
    normalized_vertex = _nonnegative_int(vertex_index)
    affected_vertices.add((normalized_slot, normalized_vertex))
    affected_meshes.add(normalized_slot)
    records = receipt.get("records")
    if not isinstance(records, list):
        records = []
        receipt["records"] = records
    record_limit = _nonnegative_int(
        receipt.get("record_limit", CROSS_MOD_COMPATIBILITY_RECORD_LIMIT)
    )
    if len(records) < record_limit:
        records.append(
            {
                "mesh_slot": normalized_slot,
                "scene_node": str(scene_node or ""),
                "vertex_index": normalized_vertex,
                "source_bone_id": _int_or_default(source_bone_id, -1),
                "target_bone_id": _int_or_default(target_bone_id, -1),
                "reason": str(reason),
                "weight": float(weight),
            }
        )
    else:
        receipt["records_truncated_count"] = _nonnegative_int(
            receipt.get("records_truncated_count")
        ) + 1


def remap_cross_mod_skeleton_skin_row(
    scene_bones: Any,
    scene_weights: Any,
    *,
    source_bone_count: int,
    parent_by_bone: Mapping[int, int],
    receipt: MutableMapping[str, Any],
    affected_vertices: set[tuple[int, int]],
    affected_meshes: set[int],
    mesh_slot: int,
    scene_node: str,
    vertex_index: int,
) -> tuple[list[int], list[float]]:
    """Map out-of-range Skin IDs to a current-hierarchy ancestor or root."""

    if not isinstance(scene_bones, list) or not isinstance(scene_weights, list):
        return [], []
    target_bone_count = _nonnegative_int(source_bone_count)

    def is_valid_target(bone_id: int) -> bool:
        return 0 <= bone_id <= 0xFF and (
            target_bone_count <= 0 or bone_id < target_bone_count
        )

    mapped_bones: list[int] = []
    mapped_weights: list[float] = []
    changed = False
    for row_index in range(min(len(scene_bones), len(scene_weights))):
        bone_id = _int_or_default(scene_bones[row_index], -1)
        weight = _finite_skin_weight(scene_weights[row_index])
        if weight <= 0.0:
            continue
        target_bone = bone_id
        reason = ""
        if not is_valid_target(target_bone):
            ancestor = nearest_cross_mod_ancestor(
                target_bone,
                parent_by_bone,
                is_valid_target,
            )
            if ancestor is not None:
                target_bone = ancestor
                reason = "skeleton_ancestor_remap"
            else:
                target_bone = 0
                reason = "skeleton_root_fallback"
        if reason:
            changed = True
            _record_cross_mod_compatibility_remap(
                receipt,
                affected_vertices,
                affected_meshes,
                mesh_slot=mesh_slot,
                scene_node=scene_node,
                vertex_index=vertex_index,
                source_bone_id=bone_id,
                target_bone_id=target_bone,
                reason=reason,
                weight=weight,
            )
        mapped_bones.append(target_bone)
        mapped_weights.append(weight)
    if not changed:
        return list(scene_bones), list(scene_weights)
    return _merge_and_normalize_cross_mod_skin_row(mapped_bones, mapped_weights)


def _cross_mod_palette_slot_is_empty(value: Any) -> bool:
    raw_value = _int_or_default(value, -1) & 0xFFFFFFFF
    return raw_value in {0xCDCDCDCD, 0xFFFFFFFF}


def fit_cross_mod_skin_row_to_source_palette(
    ordered_bones: list[int],
    ordered_weights: list[float],
    source_bones: Any,
    source_palette_rows: list[list[int]],
    *,
    parent_by_bone: Mapping[int, int],
    receipt: MutableMapping[str, Any],
    affected_vertices: set[tuple[int, int]],
    affected_meshes: set[int],
    mesh_slot: int,
    scene_node: str,
    vertex_index: int,
) -> tuple[list[int], list[float]]:
    """Fit valid global IDs into the source Mesh palette without changing valid rows."""

    palette_order: list[int] = []
    empty_slot_count = 0
    for palette_row in source_palette_rows:
        if not isinstance(palette_row, list):
            continue
        for raw_value in palette_row[:8]:
            if _cross_mod_palette_slot_is_empty(raw_value):
                empty_slot_count += 1
                continue
            bone_id = _int_or_default(raw_value, -1)
            if 0 <= bone_id <= 0xFF and bone_id not in palette_order:
                palette_order.append(bone_id)
    palette_ids = set(palette_order)
    reserved_new_ids: set[int] = set()
    source_fallback_order: list[int] = []
    if isinstance(source_bones, list):
        for raw_value in source_bones:
            bone_id = _int_or_default(raw_value, -1)
            if bone_id in palette_ids and bone_id not in source_fallback_order:
                source_fallback_order.append(bone_id)

    fitted_bones: list[int] = []
    fitted_weights: list[float] = []
    changed = False
    for bone_id, weight in zip(ordered_bones, ordered_weights):
        target_bone = _int_or_default(bone_id, 0)
        reason = ""
        if target_bone not in palette_ids and target_bone not in reserved_new_ids:
            if len(reserved_new_ids) < empty_slot_count:
                reserved_new_ids.add(target_bone)
            else:
                writable_ids = palette_ids | reserved_new_ids
                ancestor = nearest_cross_mod_ancestor(
                    target_bone,
                    parent_by_bone,
                    lambda candidate: candidate in writable_ids,
                )
                if ancestor is not None:
                    target_bone = ancestor
                    reason = "palette_ancestor_remap"
                elif 0 in writable_ids:
                    target_bone = 0
                    reason = "palette_root_fallback"
                elif source_fallback_order:
                    target_bone = source_fallback_order[0]
                    reason = "palette_existing_fallback"
                elif palette_order:
                    target_bone = palette_order[0]
                    reason = "palette_existing_fallback"
        if reason:
            changed = True
            _record_cross_mod_compatibility_remap(
                receipt,
                affected_vertices,
                affected_meshes,
                mesh_slot=mesh_slot,
                scene_node=scene_node,
                vertex_index=vertex_index,
                source_bone_id=bone_id,
                target_bone_id=target_bone,
                reason=reason,
                weight=_finite_skin_weight(weight),
            )
        fitted_bones.append(target_bone)
        fitted_weights.append(_finite_skin_weight(weight))
    if not changed:
        return list(ordered_bones), list(ordered_weights)
    return _merge_and_normalize_cross_mod_skin_row(fitted_bones, fitted_weights)


def finalize_cross_mod_compatibility_receipt(
    receipt: MutableMapping[str, Any],
    *,
    affected_vertices: set[tuple[int, int]],
    affected_meshes: set[int],
) -> dict[str, Any]:
    """Finalize the receipt after Writer has performed the compatibility work."""

    receipt["affected_vertex_count"] = len(affected_vertices)
    receipt["affected_mesh_count"] = len(affected_meshes)
    if _as_bool(receipt.get("applicable"), False):
        receipt["status"] = (
            "WARN"
            if _nonnegative_int(receipt.get("remapped_influence_count")) > 0
            else "PASS"
        )
    return dict(receipt)


def _mesh_has_import_skin(mesh: Mapping[str, Any], bone_count: int) -> bool:
    rows = mesh.get("fbx_skin_bones", [])
    return bone_count > 0 and isinstance(rows, list) and bool(rows) and rows[0] is not None


def describe_import_skin_compatibility(
    mesh: Mapping[str, Any],
    bone_count: int,
) -> dict[str, Any]:
    """Describe out-of-range source Skin slots without changing source data."""

    resolved_bone_count = max(0, int(bone_count))
    if not _mesh_has_import_skin(mesh, resolved_bone_count):
        return {
            "schema": IMPORT_SKIN_COMPATIBILITY_SCHEMA,
            "status": "PASS",
            "policy": "ignore_out_of_range_skin_slots_non_blocking",
            "non_blocking": True,
            "source_bone_count": resolved_bone_count,
            "valid_skin_bone_slots": [],
            "ignored_out_of_range_reference_count": 0,
            "ignored_out_of_range_positive_weight_reference_count": 0,
            "affected_vertex_count": 0,
            "renormalized_vertex_count": 0,
            "fully_unbound_vertex_count": 0,
            "ignored_source_bone_slots": [],
        }

    valid_slots: list[int] = []
    valid_slots_seen: set[int] = set()
    raw_bone_rows = mesh.get("raw_skin_bones", [])
    raw_weight_rows = mesh.get("raw_skin_weights", [])
    if not (
        isinstance(raw_bone_rows, list)
        and isinstance(raw_weight_rows, list)
        and any(row is not None for row in raw_bone_rows)
    ):
        raw_bone_rows = mesh.get("fbx_skin_bones", [])
        raw_weight_rows = mesh.get("fbx_skin_weights", [])

    ignored_count = 0
    ignored_positive_weight_count = 0
    ignored_slots: set[int] = set()
    affected_vertices: set[int] = set()
    renormalized_vertices: set[int] = set()
    fully_unbound_vertices: set[int] = set()
    for vertex_index, (bones, weights) in enumerate(zip(raw_bone_rows, raw_weight_rows)):
        if bones is None or weights is None:
            continue
        has_legal_positive_weight = False
        has_ignored_positive_weight = False
        for bone, weight in zip(bones, weights):
            bone_id = int(bone)
            weight_value = float(weight)
            if not math.isfinite(weight_value):
                weight_value = 0.0
            if 0 < bone_id <= resolved_bone_count:
                if weight_value > 0.0:
                    has_legal_positive_weight = True
                    if bone_id not in valid_slots_seen:
                        valid_slots_seen.add(bone_id)
                        valid_slots.append(bone_id)
                continue
            if bone_id <= 0:
                continue
            ignored_count += 1
            ignored_slots.add(bone_id)
            affected_vertices.add(vertex_index)
            if weight_value > 0.0:
                ignored_positive_weight_count += 1
                has_ignored_positive_weight = True
        if has_ignored_positive_weight:
            if has_legal_positive_weight:
                renormalized_vertices.add(vertex_index)
            else:
                fully_unbound_vertices.add(vertex_index)

    for bones, weights in zip(
        mesh.get("fbx_skin_bones", []),
        mesh.get("fbx_skin_weights", []),
    ):
        if bones is None or weights is None:
            continue
        for bone, weight in zip(bones, weights):
            bone_id = int(bone)
            weight_value = float(weight)
            if not math.isfinite(weight_value):
                weight_value = 0.0
            if (
                0 < bone_id <= resolved_bone_count
                and weight_value > 0.0
                and bone_id not in valid_slots_seen
            ):
                valid_slots_seen.add(bone_id)
                valid_slots.append(bone_id)
    if not valid_slots:
        valid_slots = list(range(1, resolved_bone_count + 1))

    return {
        "schema": IMPORT_SKIN_COMPATIBILITY_SCHEMA,
        "status": "WARN" if ignored_count else "PASS",
        "policy": "ignore_out_of_range_skin_slots_non_blocking",
        "non_blocking": True,
        "source_bone_count": resolved_bone_count,
        "valid_skin_bone_slots": valid_slots,
        "ignored_out_of_range_reference_count": ignored_count,
        "ignored_out_of_range_positive_weight_reference_count": ignored_positive_weight_count,
        "affected_vertex_count": len(affected_vertices),
        "renormalized_vertex_count": len(renormalized_vertices),
        "fully_unbound_vertex_count": len(fully_unbound_vertices),
        "ignored_source_bone_slots": sorted(ignored_slots),
    }


def classify_export_mesh(mesh: Mapping[str, Any]) -> dict[str, Any]:
    """Return the one allowed source-header route for a Mesh, if any.

    Real editable geometry is defined by scene faces, never surviving vertices.
    This keeps Blender's FBX topology loss from becoming a geometry export.
    """

    has_skin = _as_bool(mesh.get("has_skin"), False)
    parsed_face_count = _optional_nonnegative_int(mesh.get("scene_face_count"))
    face_count_known = parsed_face_count is not None
    face_count = parsed_face_count if parsed_face_count is not None else 0
    has_geometry = face_count_known and face_count > 0
    route = ""
    if has_skin and face_count_known and not has_geometry:
        route = EXPORT_COMPATIBILITY_ROUTE_SKIN_WITHOUT_FACES
    elif not has_skin and has_geometry:
        route = EXPORT_COMPATIBILITY_ROUTE_UNSKINNED_WITH_FACES
    matched = bool(route)
    return {
        "schema": EXPORT_COMPATIBILITY_SCHEMA,
        "contract_revision": COMPATIBILITY_MODULE_CONTRACT_REVISION,
        "matched": matched,
        "route": route,
        "has_skin": has_skin,
        "scene_face_count_known": face_count_known,
        "scene_face_count": face_count,
        "has_geometry": has_geometry,
        "auto_header_only": matched,
        "source_passthrough": matched,
        "requires_selected_fbx": not matched,
    }


def _compatibility_owned(row: Mapping[str, Any]) -> bool:
    return str(row.get("re6_compatibility_schema", "") or "") == EXPORT_COMPATIBILITY_SCHEMA


def _legacy_auto_route(row: Mapping[str, Any]) -> bool:
    return (
        _as_bool(row.get("auto_header_only"), False)
        and str(row.get("source_fallback_reason", "") or "")
        in _LEGACY_AUTO_ROUTE_REASONS
    )


def apply_export_compatibility(
    mesh: MutableMapping[str, Any],
    *,
    clear_legacy_auto_route: bool = False,
) -> dict[str, Any]:
    """Apply only the two explicit source-header compatibility routes."""

    decision = classify_export_mesh(mesh)
    if decision["matched"]:
        mesh["auto_header_only"] = True
        mesh["source_passthrough"] = True
        mesh["requires_selected_fbx"] = False
        mesh["source_fallback_reason"] = ""
        mesh["re6_compatibility_schema"] = EXPORT_COMPATIBILITY_SCHEMA
        mesh["re6_compatibility_route"] = decision["route"]
        mesh["re6_compatibility_face_count_authority"] = "scene_face_count"
        return decision

    owns_route = _compatibility_owned(mesh)
    if owns_route or (clear_legacy_auto_route and _legacy_auto_route(mesh)):
        mesh["auto_header_only"] = False
        mesh["source_passthrough"] = False
        mesh["requires_selected_fbx"] = True
        if _legacy_auto_route(mesh):
            mesh["source_fallback_reason"] = ""
        mesh.pop("re6_compatibility_schema", None)
        mesh.pop("re6_compatibility_route", None)
        mesh.pop("re6_compatibility_face_count_authority", None)
    return decision


def apply_export_compatibility_rows(
    rows: list[Any],
    *,
    collection_name: str,
    clear_legacy_auto_route: bool = False,
) -> dict[str, Any]:
    """Apply the export rules to Mesh rows in one signed scene collection."""

    matched_rows: list[dict[str, Any]] = []
    mesh_count = 0
    for row in rows:
        if not isinstance(row, MutableMapping):
            continue
        if collection_name == "nodes" and not _as_bool(row.get("is_mesh"), False):
            continue
        mesh_count += 1
        decision = apply_export_compatibility(
            row,
            clear_legacy_auto_route=clear_legacy_auto_route,
        )
        if decision["matched"]:
            matched_rows.append(
                {
                    "scene_node": str(row.get("scene_node", "") or ""),
                    "scene_node_handle": _nonnegative_int(row.get("scene_node_handle")),
                    "mesh_slot": _nonnegative_int(row.get("mesh_slot")),
                    "route": str(decision["route"]),
                }
            )
    return {
        "schema": EXPORT_COMPATIBILITY_SCHEMA,
        "contract_revision": COMPATIBILITY_MODULE_CONTRACT_REVISION,
        "collection": collection_name,
        "mesh_count": mesh_count,
        "matched_meshes": matched_rows,
    }


def apply_export_compatibility_contract(
    scene_contract: MutableMapping[str, Any],
    *,
    clear_legacy_auto_route: bool = False,
) -> dict[str, Any]:
    """Apply shared rules to both signed scene tables without DCC access."""

    collections: list[dict[str, Any]] = []
    matched: dict[tuple[int, str], dict[str, Any]] = {}
    for collection_name in ("nodes", "meshes"):
        raw_rows = scene_contract.get(collection_name, [])
        rows = raw_rows if isinstance(raw_rows, list) else []
        receipt = apply_export_compatibility_rows(
            rows,
            collection_name=collection_name,
            clear_legacy_auto_route=clear_legacy_auto_route,
        )
        collections.append(receipt)
        for row in receipt["matched_meshes"]:
            key = (int(row["scene_node_handle"]), str(row["route"]))
            matched[key] = dict(row)
    ordered_matches = sorted(
        matched.values(),
        key=lambda row: (
            int(row["scene_node_handle"]),
            str(row["route"]),
            str(row["scene_node"]).casefold(),
        ),
    )
    return {
        "schema": EXPORT_COMPATIBILITY_SCHEMA,
        "contract_revision": COMPATIBILITY_MODULE_CONTRACT_REVISION,
        "face_count_authority": "scene_face_count",
        "routes": [
            EXPORT_COMPATIBILITY_ROUTE_SKIN_WITHOUT_FACES,
            EXPORT_COMPATIBILITY_ROUTE_UNSKINNED_WITH_FACES,
        ],
        "matched_mesh_count": len(ordered_matches),
        "matched_meshes": ordered_matches,
        "collections": collections,
    }


def export_compatibility_is_consistent(mesh: Mapping[str, Any]) -> bool:
    """Verify only the Header-only state owned by this compatibility module.

    Ordinary routes may carry their own pre-existing source-preservation facts.
    This module must never reject or rewrite those routes merely because neither
    of the two compatibility predicates matched.
    """

    decision = classify_export_mesh(mesh)
    auto_header_only = _as_bool(mesh.get("auto_header_only"), False)
    source_passthrough = _as_bool(mesh.get("source_passthrough"), False)
    requires_selected_fbx = _as_bool(mesh.get("requires_selected_fbx"), True)
    if decision["matched"]:
        return (
            auto_header_only
            and source_passthrough
            and not requires_selected_fbx
        )
    if _compatibility_owned(mesh):
        return (
            not auto_header_only
            and not source_passthrough
            and requires_selected_fbx
        )
    return True


def run_compatibility_regression_guard() -> dict[str, Any]:
    """Small pure-Python guard for both isolated compatibility directions."""

    import_fixture = {
        "fbx_skin_bones": [[1, 4]],
        "fbx_skin_weights": [[0.5, 0.5]],
        "raw_skin_bones": [[1, 4]],
        "raw_skin_weights": [[0.5, 0.5]],
    }
    import_report = describe_import_skin_compatibility(import_fixture, 2)
    if (
        import_report["status"] != "WARN"
        or import_report["valid_skin_bone_slots"] != [1]
        or import_report["ignored_source_bone_slots"] != [4]
        or import_report["renormalized_vertex_count"] != 1
    ):
        raise RuntimeError("Import Skin compatibility regression")

    cases = (
        ("skin_without_faces", True, 9, 0, True),
        ("unskinned_with_faces", False, 9, 4, True),
        ("skin_with_faces", True, 9, 4, False),
        ("unskinned_without_faces", False, 9, 0, False),
    )
    failures: list[str] = []
    for label, has_skin, vertex_count, face_count, expected in cases:
        decision = classify_export_mesh(
            {
                "has_skin": has_skin,
                "scene_vert_count": vertex_count,
                "scene_face_count": face_count,
            }
        )
        if bool(decision["matched"]) is not expected:
            failures.append(label)
    if failures:
        raise RuntimeError("Export compatibility regression: " + ", ".join(failures))

    ordinary_mesh = {
        "has_skin": True,
        "scene_face_count": 4,
        "auto_header_only": False,
        "source_passthrough": False,
        "requires_selected_fbx": True,
        "source_fallback_reason": "ordinary_route",
    }
    ordinary_before = dict(ordinary_mesh)
    apply_export_compatibility(ordinary_mesh)
    if ordinary_mesh != ordinary_before or not export_compatibility_is_consistent(
        ordinary_mesh
    ):
        raise RuntimeError("Ordinary export route was changed by compatibility")

    skin_without_faces = {
        "has_skin": True,
        "scene_face_count": 0,
        "auto_header_only": False,
        "source_passthrough": False,
        "requires_selected_fbx": True,
    }
    apply_export_compatibility(skin_without_faces)
    if not export_compatibility_is_consistent(skin_without_faces):
        raise RuntimeError("Skin-without-faces route was not preserved")

    unskinned_with_faces = {
        "has_skin": False,
        "scene_face_count": 4,
        "auto_header_only": False,
        "source_passthrough": False,
        "requires_selected_fbx": True,
    }
    apply_export_compatibility(unskinned_with_faces)
    if not export_compatibility_is_consistent(unskinned_with_faces):
        raise RuntimeError("Unskinned-with-faces route was not preserved")

    ordinary_cross_mod = describe_cross_mod_export_compatibility(
        source_mod_available=True,
        bone_export_mode="disabled",
    )
    bones_plus_mesh_cross_mod = describe_cross_mod_export_compatibility(
        source_mod_available=True,
        bone_export_mode="bones_plus_mesh",
    )
    bones_only_cross_mod = describe_cross_mod_export_compatibility(
        source_mod_available=True,
        bone_export_mode="bones_only",
    )
    if (
        not ordinary_cross_mod["applicable"]
        or not bones_plus_mesh_cross_mod["applicable"]
        or bones_only_cross_mod["applicable"]
        or bones_only_cross_mod["reason"] != "bones_only_has_no_mesh"
    ):
        raise RuntimeError("Cross-MOD export applicability regression")

    cross_mod_receipt = new_cross_mod_compatibility_receipt(
        source_mod_available=True,
        bone_export_mode="bones_plus_mesh",
        hierarchy_bone_count=3,
    )
    affected_vertices: set[tuple[int, int]] = set()
    affected_meshes: set[int] = set()
    parent_by_bone = build_cross_mod_parent_bone_map(
        [
            {"parsed_bone_id": 27, "parent_parsed_bone_id": 26},
            {"parsed_bone_id": 26, "parent_parsed_bone_id": 5},
        ]
    )
    mapped_bones, mapped_weights = remap_cross_mod_skeleton_skin_row(
        [27, 99, 5],
        [0.25, 0.25, 0.5],
        source_bone_count=27,
        parent_by_bone=parent_by_bone,
        receipt=cross_mod_receipt,
        affected_vertices=affected_vertices,
        affected_meshes=affected_meshes,
        mesh_slot=40,
        scene_node="Mesh_040_14D40020_LODx255",
        vertex_index=12,
    )
    fitted_bones, fitted_weights = fit_cross_mod_skin_row_to_source_palette(
        mapped_bones,
        mapped_weights,
        [0, 5, 6, 7, 8, 9, 10, 11],
        [[0, 5, 6, 7, 8, 9, 10, 11]],
        parent_by_bone=parent_by_bone,
        receipt=cross_mod_receipt,
        affected_vertices=affected_vertices,
        affected_meshes=affected_meshes,
        mesh_slot=40,
        scene_node="Mesh_040_14D40020_LODx255",
        vertex_index=12,
    )
    finalized_cross_mod = finalize_cross_mod_compatibility_receipt(
        cross_mod_receipt,
        affected_vertices=affected_vertices,
        affected_meshes=affected_meshes,
    )
    if (
        mapped_bones != [26, 0, 5]
        or fitted_bones != [5, 0]
        or any(
            abs(actual - expected) > 0.000001
            for actual, expected in zip(fitted_weights, [0.75, 0.25])
        )
        or finalized_cross_mod["status"] != "WARN"
        or finalized_cross_mod["affected_vertex_count"] != 1
        or finalized_cross_mod["affected_mesh_count"] != 1
    ):
        raise RuntimeError("Cross-MOD Skin compatibility regression")

    valid_receipt = new_cross_mod_compatibility_receipt(
        source_mod_available=True,
        bone_export_mode="disabled",
    )
    valid_bones, valid_weights = remap_cross_mod_skeleton_skin_row(
        [5, 0],
        [0.6, 0.4],
        source_bone_count=27,
        parent_by_bone=parent_by_bone,
        receipt=valid_receipt,
        affected_vertices=set(),
        affected_meshes=set(),
        mesh_slot=40,
        scene_node="Mesh_040_14D40020_LODx255",
        vertex_index=13,
    )
    if (
        valid_bones != [5, 0]
        or any(
            abs(actual - expected) > 0.000001
            for actual, expected in zip(valid_weights, [0.6, 0.4])
        )
        or valid_receipt["remapped_influence_count"] != 0
    ):
        raise RuntimeError("Cross-MOD compatibility changed a valid Skin row")
    return {
        "status": "PASS",
        "contract_revision": COMPATIBILITY_MODULE_CONTRACT_REVISION,
        "import_metadata_only": True,
        "export_face_count_authority": "scene_face_count",
        "ordinary_routes_untouched": True,
        "cross_mod_default_enabled": CROSS_MOD_COMPATIBILITY_DEFAULT_ENABLED,
        "cross_mod_bones_plus_mesh_supported": True,
        "cross_mod_bones_only_mesh_forbidden": True,
    }
