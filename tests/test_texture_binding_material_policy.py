from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "PC-REHD Code X Launcher.py"
IMPORTER = ROOT / "codex_re6_mod_import_fbx.py"
BLENDER_REPAIR = ROOT / "codex_blender_fbx_texture_to_FBX_safe.py"


def _function_source(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            start = node.lineno - 1
            end = getattr(node, "end_lineno", node.lineno)
            return "\n".join(source.splitlines()[start:end])
    # Blender's resident worker is embedded as a source string in the Launcher,
    # so those functions are not part of the Launcher's outer AST.
    lines = source.splitlines()
    start_re = re.compile(rf"^def {re.escape(name)}\b")
    start = next((index for index, line in enumerate(lines) if start_re.match(line)), None)
    if start is not None:
        end = len(lines)
        for index in range(start + 1, len(lines)):
            if re.match(r"^def [A-Za-z_]", lines[index]):
                end = index
                break
        return "\n".join(lines[start:end])
    raise AssertionError(f"function not found: {path.name}:{name}")


def test_max_mrl_factory_never_constructs_lit_material_types():
    source = _function_source(LAUNCHER, "_max_mrl_material")
    assert "OpenPBR" not in source
    assert "Physical" not in source
    assert "_max_bake_safe_standard_material" in source
    assert "StandardMaterial" in _function_source(LAUNCHER, "_max_bake_safe_standard_material")


def test_max_display_materials_use_bake_safe_parameters():
    source = _function_source(LAUNCHER, "_max_bake_safe_standard_material")
    for property_name in ("selfIllumAmount", "specularLevel", "glossiness", "twoSided"):
        assert property_name in source


def test_max_import_path_normalizes_native_materials():
    postprocess = _function_source(LAUNCHER, "_max_postprocess_import")
    assert "_max_normalize_import_material" in postprocess


def test_max_scene_material_toolbox_scans_the_whole_scene():
    source = _function_source(LAUNCHER, "_max_normalize_scene_materials")
    assert "rt.objects" in source
    assert "StandardMaterial" in source
    assert "selfIllumAmount" in source


def test_max_texture_entry_points_call_scene_material_toolbox():
    for function_name in ("_max_postprocess_import", "_max_apply_mrl_bind", "_max_manual_texture"):
        source = _function_source(LAUNCHER, function_name)
        assert "_max_normalize_scene_materials" in source


def test_blender_re6_factories_emit_image_to_emission():
    for function_name in (
        "_blender_mrl_material_for_image",
        "_blender_create_base_color_material",
    ):
        source = _function_source(LAUNCHER, function_name)
        assert "ShaderNodeEmission" in source
        assert "ShaderNodeBsdfPrincipled" not in source


def test_blender_re6_verifiers_accept_only_emission_surface():
    for function_name in (
        "_blender_mrl_material_has_image",
        "_blender_base_color_material_has_image",
    ):
        source = _function_source(LAUNCHER, function_name)
        assert "EMISSION" in source
        assert "Base Color" not in source


def test_blender_direct_scene_import_is_bake_safe():
    source = _function_source(LAUNCHER, "_scene_data_apply_display_material")
    assert "ShaderNodeEmission" in source
    assert "ShaderNodeBsdfPrincipled" not in source


def test_blender_scene_material_toolbox_scans_the_whole_scene():
    source = _function_source(LAUNCHER, "_blender_normalize_scene_materials")
    assert "bpy.context.scene.objects" in source
    assert "ShaderNodeEmission" in source


def test_blender_texture_entry_points_call_scene_material_toolbox():
    for function_name in (
        "_blender_apply_mrl_material_bind",
        "_blender_apply_manual_texture",
        "_apply_blender_mrl_material_contract",
        "_scene_data_build_scene",
    ):
        source = _function_source(LAUNCHER, function_name)
        assert "_blender_normalize_scene_materials" in source


def test_blender_fbx_repair_is_bake_safe():
    source = _function_source(BLENDER_REPAIR, "rebuild_principled_material")
    assert "ShaderNodeEmission" in source
    assert "ShaderNodeBsdfPrincipled" not in source


def test_fbx_builder_emits_only_portable_diffuse_material_graph():
    source = IMPORTER.read_text(encoding="utf-8")
    assert "_fbx_add_max_physical_material_binding" not in source
    assert "base_color_map" not in source
    assert "Implementation" not in source
    assert "BindingTable" not in source
    assert "ClassIDa" not in source
