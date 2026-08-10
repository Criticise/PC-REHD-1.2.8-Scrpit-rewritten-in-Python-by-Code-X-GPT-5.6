"""
MMD-safe Blender FBX texture repair utility.

This is safer for MMD/PMX models than the first generic repair script:
- It does not replace missing textures with random solid pink/purple fallback.
- It scans MMD Tools material properties and image nodes.
- It copies or saves detected textures into an FBX-side texture folder.
- It keeps a material copy instead of flattening every material blindly.
- It only rebuilds a simple Principled node tree when a real base texture or
  useful material color exists.

Run inside Blender:
1. Select the affected MMD mesh objects.
2. Text Editor > Open this file > Run Script.
3. Export FBX with Path Mode = Copy. Enable Embed Textures only if required.

If you already ran the previous script and the model is pink/purple, run
codex_blender_restore_original_materials.py first, then run this safer script.
"""

import os
import re
import shutil
import tempfile
from mathutils import Vector

import bpy


# ----------------------------- User options -----------------------------

TEXTURE_DIR_NAME = "fbx_textures"
MATERIAL_SUFFIX = "_FBX_SAFE"
SELECTED_ONLY = True

# Keep this False for MMD unless you intentionally want everything converted.
FORCE_ALL_TEXTURES_TO_PNG = False

# For MMD, false is safer. If no real base texture is detected, the material
# keeps its diffuse color instead of creating a misleading single-color texture.
CREATE_SOLID_FALLBACK_BASE_COLOR = False

AUTO_CREATE_UV = True

# Copy non-color helper textures referenced by MMD Tools when found.
COPY_MMD_TOON_AND_SPHERE_TEXTURES = True

AUTO_EXPORT_FBX = False
FBX_EXPORT_FILENAME = "fbx_repaired_export.fbx"
AGENT_TEMP_DIR_NAME = "PC_REHD_Code_X"


ALLOWED_EXTENSIONS = {".png", ".tga", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".dds"}
NON_COLOR_SEMANTICS = {"normal", "roughness", "metallic", "alpha", "sphere", "toon"}


class RepairReport:
    def __init__(self):
        self.lines = []
        self.fix_count = 0
        self.warning_count = 0

    def info(self, message):
        self.lines.append("[INFO] " + message)

    def fix(self, message):
        self.fix_count += 1
        self.lines.append("[FIX] " + message)

    def warn(self, message):
        self.warning_count += 1
        self.lines.append("[WARN] " + message)

    def emit(self):
        output = "\n".join(
            [
                "Codex MMD-Safe FBX Texture Repair Report",
                "Fixes: %d" % self.fix_count,
                "Warnings: %d" % self.warning_count,
                "",
            ]
            + self.lines
        )
        print(output)
        text = bpy.data.texts.get("Codex_MMD_Safe_FBX_Texture_Repair_Report") or bpy.data.texts.new(
            "Codex_MMD_Safe_FBX_Texture_Repair_Report"
        )
        text.clear()
        text.write(output)
        return output


def sanitize_name(value, fallback="asset"):
    value = value or fallback
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    value = value.strip("._-")
    return value or fallback


def _directory_is_writable(path):
    """Check the directory with the same file operation the tool needs later."""
    candidate = str(path or "").strip()
    if not candidate:
        return False
    try:
        candidate = os.path.abspath(os.path.expanduser(candidate))
        os.makedirs(candidate, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=".codex_write_probe_",
            dir=candidate,
            delete=True,
        ):
            pass
        return True
    except (OSError, TypeError, ValueError):
        return False


def _agent_temporary_directory():
    """Return a writable directory owned by Blender's Agent runtime."""
    candidates = []
    try:
        blender_tempdir = str(bpy.app.tempdir or "").strip()
    except Exception:
        blender_tempdir = ""
    if blender_tempdir:
        candidates.append(blender_tempdir)
    system_tempdir = tempfile.gettempdir()
    if system_tempdir and system_tempdir not in candidates:
        candidates.append(system_tempdir)

    for root in candidates:
        runtime_directory = os.path.join(root, AGENT_TEMP_DIR_NAME)
        if _directory_is_writable(runtime_directory):
            return runtime_directory
    raise OSError(
        "Blender Agent could not find a writable temporary directory for "
        "the unsaved scene"
    )


def blend_directory():
    """Choose a writable scene/output directory without using process CWD."""
    if str(getattr(bpy.data, "filepath", "") or "").strip():
        scene_directory = os.path.dirname(bpy.path.abspath("//"))
        if _directory_is_writable(scene_directory):
            return scene_directory
    return _agent_temporary_directory()


def texture_directory():
    path = os.path.join(blend_directory(), TEXTURE_DIR_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def unique_path(directory, stem, extension):
    os.makedirs(directory, exist_ok=True)
    stem = sanitize_name(stem, "texture")
    extension = extension if extension.startswith(".") else "." + extension
    candidate = os.path.join(directory, stem + extension)
    index = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, "%s_%03d%s" % (stem, index, extension))
        index += 1
    return candidate


def get_input(node, names):
    if not node:
        return None
    for name in names:
        socket = node.inputs.get(name)
        if socket:
            return socket
    return None


def get_output(node, names):
    if not node:
        return None
    for name in names:
        socket = node.outputs.get(name)
        if socket:
            return socket
    return None


def link_safely(tree, from_socket, to_socket):
    if from_socket and to_socket:
        tree.links.new(from_socket, to_socket)
        return True
    return False


def set_input_default(node, names, value):
    socket = get_input(node, names)
    if socket and hasattr(socket, "default_value"):
        try:
            socket.default_value = value
        except Exception:
            pass


def set_image_colorspace(image, semantic):
    if not image:
        return
    desired = "Non-Color" if semantic in NON_COLOR_SEMANTICS else "sRGB"
    try:
        image.colorspace_settings.name = desired
    except Exception:
        pass


def absolute_image_path(image):
    if not image or not image.filepath:
        return None
    try:
        return bpy.path.abspath(image.filepath)
    except Exception:
        return image.filepath


def resolve_path_like(value):
    if not value:
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return bpy.path.abspath(value)
    except Exception:
        return value


def material_diffuse_color(material):
    if material:
        try:
            return tuple(material.diffuse_color)
        except Exception:
            pass
    return (0.8, 0.8, 0.8, 1.0)


def material_enum_value(material, attribute, fallback=None):
    if not material:
        return fallback
    try:
        value = getattr(material, attribute)
    except Exception:
        return fallback
    return value if value is not None else fallback


def selected_mesh_objects():
    selected = [obj for obj in bpy.context.selected_objects if obj.type == "MESH"]
    active = bpy.context.view_layer.objects.active
    if SELECTED_ONLY:
        if selected:
            return selected
        if active and active.type == "MESH":
            return [active]
    return [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]


def remember_selection():
    return {
        "active": bpy.context.view_layer.objects.active,
        "selected": list(bpy.context.selected_objects),
        "mode": bpy.context.object.mode if bpy.context.object else "OBJECT",
    }


def restore_selection(state):
    try:
        if bpy.context.object and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
    except Exception:
        pass
    for obj in bpy.context.scene.objects:
        obj.select_set(False)
    for obj in state["selected"]:
        if obj.name in bpy.context.scene.objects:
            obj.select_set(True)
    if state["active"]:
        bpy.context.view_layer.objects.active = state["active"]
    try:
        if state["active"] and state["mode"] != "OBJECT":
            bpy.ops.object.mode_set(mode=state["mode"])
    except Exception:
        pass


# ----------------------------- UV repair -----------------------------

def simple_planar_uv(mesh):
    uv_layer = mesh.uv_layers.new(name="UVMap") if not mesh.uv_layers else mesh.uv_layers.active
    coords = [vertex.co.copy() for vertex in mesh.vertices]
    if not coords:
        return

    min_v = Vector((min(v.x for v in coords), min(v.y for v in coords), min(v.z for v in coords)))
    max_v = Vector((max(v.x for v in coords), max(v.y for v in coords), max(v.z for v in coords)))
    size = max_v - min_v
    size.x = size.x if abs(size.x) > 1e-8 else 1.0
    size.y = size.y if abs(size.y) > 1e-8 else 1.0
    size.z = size.z if abs(size.z) > 1e-8 else 1.0

    for polygon in mesh.polygons:
        normal = polygon.normal
        axis = max(range(3), key=lambda i: abs(normal[i]))
        for loop_index in polygon.loop_indices:
            vertex = mesh.vertices[mesh.loops[loop_index].vertex_index].co
            if axis == 2:
                uv = ((vertex.x - min_v.x) / size.x, (vertex.y - min_v.y) / size.y)
            elif axis == 1:
                uv = ((vertex.x - min_v.x) / size.x, (vertex.z - min_v.z) / size.z)
            else:
                uv = ((vertex.y - min_v.y) / size.y, (vertex.z - min_v.z) / size.z)
            uv_layer.data[loop_index].uv = uv


def ensure_uv_map(obj, report):
    mesh = obj.data
    if mesh.uv_layers:
        mesh.uv_layers.active = mesh.uv_layers[0]
        return

    if not AUTO_CREATE_UV:
        report.warn("Mesh '%s' has no UV map." % obj.name)
        return

    state = remember_selection()
    try:
        if bpy.context.object and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for item in bpy.context.scene.objects:
            item.select_set(False)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.smart_project(angle_limit=1.15192, island_margin=0.03, area_weight=0.0)
        bpy.ops.object.mode_set(mode="OBJECT")
        if mesh.uv_layers:
            mesh.uv_layers.active = mesh.uv_layers[0]
            report.fix("Created Smart UV map for mesh '%s'." % obj.name)
            return
    except Exception as exc:
        report.warn("Smart UV Project failed for '%s': %s" % (obj.name, exc))
    finally:
        restore_selection(state)

    simple_planar_uv(mesh)
    report.fix("Created fallback planar UV map for mesh '%s'." % obj.name)


# ----------------------------- Texture detection -----------------------------

def find_principled_node(material):
    if not material or not material.use_nodes or not material.node_tree:
        return None
    for node in material.node_tree.nodes:
        if node.bl_idname == "ShaderNodeBsdfPrincipled":
            return node
    return None


def find_image_upstream(node, visited=None, depth=0):
    if visited is None:
        visited = set()
    if not node or node in visited or depth > 32:
        return None
    visited.add(node)
    if node.bl_idname == "ShaderNodeTexImage" and getattr(node, "image", None):
        return node.image
    for socket in getattr(node, "inputs", []):
        for link in socket.links:
            image = find_image_upstream(link.from_node, visited, depth + 1)
            if image:
                return image
    return None


def image_from_linked_input(node, input_names):
    socket = get_input(node, input_names)
    if not socket or not socket.is_linked:
        return None
    for link in socket.links:
        image = find_image_upstream(link.from_node)
        if image:
            return image
    return None


def classify_text(text):
    text = text.lower()
    if any(token in text for token in ["normal", "nrm", "_n.", "_n_", "-n.", "-n_", "bump"]):
        return "normal"
    if any(token in text for token in ["rough", "rgh", "_r.", "_r_", "-r.", "-r_"]):
        return "roughness"
    if any(token in text for token in ["metallic", "metalness", "metal", "_m.", "_m_", "-m.", "-m_"]):
        return "metallic"
    if any(token in text for token in ["alpha", "opacity", "transparent", "transparency", "mask"]):
        return "alpha"
    if any(token in text for token in ["sphere", ".spa", ".sph", "spheremap"]):
        return "sphere"
    if any(token in text for token in ["toon"]):
        return "toon"
    if any(
        token in text
        for token in ["basecolor", "base_color", "albedo", "diffuse", "diff", "color", "tex", "texture", "_d.", "_d_", "-d.", "-d_"]
    ):
        return "base_color"
    return None


def classify_image_node(node):
    image = getattr(node, "image", None)
    text = " ".join(
        [
            getattr(node, "name", ""),
            getattr(node, "label", ""),
            getattr(image, "name", ""),
            absolute_image_path(image) or "",
        ]
    )
    return classify_text(text)


def find_image_by_path(path):
    if not path:
        return None
    normalized = os.path.normcase(os.path.abspath(path))
    for image in bpy.data.images:
        image_path = absolute_image_path(image)
        if image_path and os.path.normcase(os.path.abspath(image_path)) == normalized:
            return image
    if os.path.exists(path):
        try:
            return bpy.data.images.load(path, check_existing=True)
        except Exception:
            return None
    return None


def scan_mmd_material_properties(material, result, report):
    if not material:
        return

    candidates = []

    # MMD Tools usually stores data under material.mmd_material. Attribute names
    # differ by version, so this scans string properties instead of hardcoding
    # only one release's names.
    containers = [material]
    mmd_material = getattr(material, "mmd_material", None)
    if mmd_material:
        containers.append(mmd_material)

    for container in containers:
        for attr in dir(container):
            lower = attr.lower()
            if not any(key in lower for key in ["texture", "sphere", "toon"]):
                continue
            try:
                value = getattr(container, attr)
            except Exception:
                continue
            if isinstance(value, str) and value.strip():
                candidates.append((attr, value))

    for attr, value in candidates:
        path = resolve_path_like(value)
        semantic = classify_text(attr + " " + value) or "base_color"
        image = find_image_by_path(path)
        if image:
            if semantic in {"toon", "sphere"} and not COPY_MMD_TOON_AND_SPHERE_TEXTURES:
                continue
            if not result.get(semantic):
                result[semantic] = image
                report.info("MMD material '%s' %s texture: %s" % (material.name, semantic, path))


def detect_material(material, report):
    result = {
        "base_color": None,
        "normal": None,
        "roughness": None,
        "metallic": None,
        "alpha": None,
        "emission": None,
        "toon": None,
        "sphere": None,
        "base_color_value": material_diffuse_color(material),
        "roughness_value": 0.5,
        "metallic_value": 0.0,
        "alpha_value": material_diffuse_color(material)[3],
        "emission_strength": 0.0,
        "source_blend_method": material_enum_value(material, "blend_method", "OPAQUE"),
        "source_shadow_method": material_enum_value(material, "shadow_method", None),
        "unclassified_image_count": 0,
        "sourceio_eye": None,
    }

    if not material:
        return result

    scan_mmd_material_properties(material, result, report)

    principled = find_principled_node(material)
    if principled:
        linked = {
            "base_color": image_from_linked_input(principled, ["Base Color"]),
            "roughness": image_from_linked_input(principled, ["Roughness"]),
            "metallic": image_from_linked_input(principled, ["Metallic"]),
            "alpha": image_from_linked_input(principled, ["Alpha"]),
            "emission": image_from_linked_input(principled, ["Emission Color", "Emission"]),
        }
        normal_input = get_input(principled, ["Normal"])
        if normal_input and normal_input.is_linked:
            for link in normal_input.links:
                if link.from_node.bl_idname == "ShaderNodeNormalMap":
                    linked["normal"] = image_from_linked_input(link.from_node, ["Color"])
                if not linked.get("normal"):
                    linked["normal"] = find_image_upstream(link.from_node)

        for semantic, image in linked.items():
            if image and not result.get(semantic):
                result[semantic] = image

        for input_name, key in [("Roughness", "roughness_value"), ("Metallic", "metallic_value"), ("Alpha", "alpha_value")]:
            socket = principled.inputs.get(input_name)
            if socket and hasattr(socket, "default_value"):
                try:
                    result[key] = float(socket.default_value)
                except Exception:
                    pass

        socket = principled.inputs.get("Base Color")
        if socket and hasattr(socket, "default_value"):
            try:
                result["base_color_value"] = tuple(socket.default_value)
            except Exception:
                pass

    if material.use_nodes and material.node_tree:
        image_nodes = [
            node
            for node in material.node_tree.nodes
            if node.bl_idname == "ShaderNodeTexImage" and getattr(node, "image", None)
        ]
        unclassified = []
        for node in image_nodes:
            semantic = classify_image_node(node)
            if semantic and not result.get(semantic):
                result[semantic] = node.image
            elif not semantic:
                unclassified.append(node.image)
        result["unclassified_image_count"] = len(unclassified)

        # MMD imports often have one Image Texture node whose name is not useful.
        # Guessing among multiple unclassified images is destructive for MMD-style
        # materials because alpha/matcap/helper maps get mistaken for albedo.
        if not result["base_color"] and len(image_nodes) == 1 and len(unclassified) == 1:
            result["base_color"] = unclassified[0]
            report.info("Used first unclassified image as base texture for '%s': %s" % (material.name, unclassified[0].name))
        elif not result["base_color"] and len(unclassified) > 1:
            report.warn(
                "Material '%s' has %d unclassified image textures; refusing to guess the base texture."
                % (material.name, len(unclassified))
            )

    sourceio_eye = detect_sourceio_eye_material(material)
    if sourceio_eye:
        # The SourceIO eye shader is not representable by an FBX material. Its
        # iris and sclera must be baked together instead of being classified as
        # unrelated generic texture channels.
        result["sourceio_eye"] = sourceio_eye
        result["base_color"] = sourceio_eye["base_image"]

    if not result["base_color"]:
        report.warn("Material '%s' has no detected base texture; keeping diffuse color only." % material.name)

    return result


# ----------------------------- Image export -----------------------------

def ensure_image_loaded(image):
    if not image:
        return False
    try:
        image.pixels[0]
        return True
    except Exception:
        try:
            image.reload()
            image.pixels[0]
            return True
        except Exception:
            return False


def save_image_as_png(image, destination, report):
    if not ensure_image_loaded(image):
        raise RuntimeError("image pixel data is not available")
    old_raw = getattr(image, "filepath_raw", "")
    old_format = getattr(image, "file_format", "PNG")
    try:
        image.filepath_raw = destination
        image.file_format = "PNG"
        image.save()
        report.fix("Saved image as PNG: %s" % destination)
    finally:
        try:
            image.filepath_raw = old_raw
            image.file_format = old_format
        except Exception:
            pass


def load_exported_image(path, semantic, source_name):
    loaded = bpy.data.images.load(path, check_existing=False)
    loaded.name = sanitize_name("%s_%s" % (source_name, semantic), "fbx_image")
    set_image_colorspace(loaded, semantic)
    return loaded


def externalize_passthrough_image(image, directory, cache, report):
    if not image:
        return None
    key = (image.name_full, "__passthrough__")
    if key in cache:
        return cache[key]

    original_colorspace = material_enum_value(getattr(image, "colorspace_settings", None), "name", None)
    exported = externalize_image(image, "base_color", directory, cache, report)
    if exported and original_colorspace:
        try:
            exported.colorspace_settings.name = original_colorspace
        except Exception:
            pass
    cache[key] = exported
    return exported


def externalize_image(image, semantic, directory, cache, report):
    if not image:
        return None
    key = (image.name_full, semantic)
    if key in cache:
        return cache[key]

    source_path = absolute_image_path(image)
    source_ext = os.path.splitext(source_path or "")[1].lower()
    stem = sanitize_name(os.path.splitext(os.path.basename(source_path or image.name))[0], "texture")

    # DDS is allowed as a copied sidecar because many game/MMD workflows use it,
    # but if it is packed or generated Blender may not be able to save it except
    # through PNG conversion.
    needs_png = (
        FORCE_ALL_TEXTURES_TO_PNG
        or image.source != "FILE"
        or not source_path
        or not os.path.exists(source_path)
        or source_ext not in ALLOWED_EXTENSIONS
        or bool(getattr(image, "packed_file", None))
    )

    try:
        if needs_png:
            destination = unique_path(directory, "%s_%s" % (stem, semantic), ".png")
            save_image_as_png(image, destination, report)
        else:
            destination = unique_path(directory, "%s_%s" % (stem, semantic), source_ext)
            if os.path.abspath(source_path) != os.path.abspath(destination):
                shutil.copy2(source_path, destination)
                report.fix("Copied texture: %s -> %s" % (source_path, destination))
            else:
                report.info("Texture already in export directory: %s" % destination)

        exported = load_exported_image(destination, semantic, image.name)
        cache[key] = exported
        return exported
    except Exception as exc:
        report.warn("Could not externalize image '%s': %s" % (image.name, exc))
        set_image_colorspace(image, semantic)
        cache[key] = image
        return image


# ----------------------------- Material rebuild -----------------------------

def make_unique_material_name(source_name):
    base = sanitize_name(source_name or "Material") + MATERIAL_SUFFIX
    name = base
    index = 1
    while bpy.data.materials.get(name):
        name = "%s_%03d" % (base, index)
        index += 1
    return name


def make_texture_node(tree, image, semantic, location):
    node = tree.nodes.new("ShaderNodeTexImage")
    node.name = "FBX_%s" % semantic
    node.label = "FBX %s" % semantic.replace("_", " ").title()
    node.image = image
    node.location = location
    set_image_colorspace(image, semantic)
    return node


def copy_material_metadata(source, target):
    if not source or not target:
        return
    try:
        target.diffuse_color = source.diffuse_color
    except Exception:
        pass
    for key in source.keys():
        try:
            target[key] = source[key]
        except Exception:
            pass


def detect_sourceio_eye_material(material):
    """Return SourceIO's two-texture eye layout only when its controls exist."""
    if not material or not material.use_nodes or not material.node_tree:
        return None

    base_image = None
    iris_image = None
    controls = set()
    for node in material.node_tree.nodes:
        node_name = (getattr(node, "name", "") + " " + getattr(node, "label", "")).casefold()
        if node_name.startswith("!eye_"):
            controls.add(node_name.split()[0])
        if node.bl_idname != "ShaderNodeTexImage" or not getattr(node, "image", None):
            continue
        image_name = str(node.image.name or "").casefold()
        if "$basetexture" in node_name or "eyeball" in image_name:
            base_image = node.image
        if "$iris" in node_name or "pupil" in image_name:
            iris_image = node.image

    required_controls = {"!eye_loc", "!eye_rot"}
    if base_image and iris_image and required_controls.issubset(controls):
        return {
            "base_image": base_image,
            "iris_image": iris_image,
        }
    return None


def _bake_override():
    """Prefer a View 3D override when the script was run from the Text Editor."""
    windows = []
    context_window = getattr(bpy.context, "window", None)
    if context_window is not None:
        windows.append(context_window)
    window_manager = getattr(bpy.context, "window_manager", None)
    for window in getattr(window_manager, "windows", ()):
        if window not in windows:
            windows.append(window)
    for window in windows:
        screen = getattr(window, "screen", None)
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((item for item in area.regions if item.type == "WINDOW"), None)
            if region:
                return {
                    "window": window,
                    "screen": screen,
                    "area": area,
                    "region": region,
                    "scene": window.scene,
                    "view_layer": window.view_layer,
                }
    return None


def bake_sourceio_eye_base_color(source_material, eye_layout, objects, directory, report):
    """Bake SourceIO's procedural iris+sclera shader into one FBX-safe image."""
    bake_objects = [
        obj for obj in objects
        if obj and obj.type == "MESH"
        and any(slot.material == source_material for slot in obj.material_slots)
    ]
    if not bake_objects:
        report.warn("SourceIO eye '%s' has no Mesh object to bake." % source_material.name)
        return None

    width, height = tuple(eye_layout["base_image"].size)
    width = max(1, int(width or 512))
    height = max(1, int(height or 512))
    image_name = sanitize_name("%s_FBX_baked" % source_material.name, "fbx_eye")
    image = bpy.data.images.new(image_name, width=width, height=height, alpha=True)
    set_image_colorspace(image, "base_color")
    target_path = unique_path(directory, "%s_base_color" % image_name, ".png")

    scene = bpy.context.scene
    tree = source_material.node_tree
    previous_engine = scene.render.engine
    override = _bake_override()
    view_layer = override["view_layer"] if override else bpy.context.view_layer
    previous_active = view_layer.objects.active
    previous_selected = [obj for obj in scene.objects if obj.select_get()]
    previous_hidden = {obj: bool(obj.hide_get()) for obj in bake_objects}
    previous_active_node = tree.nodes.active
    bake_target = None
    completed = False
    try:
        bake_target = tree.nodes.new("ShaderNodeTexImage")
        bake_target.name = "__codex_fbx_eye_bake_target__"
        bake_target.label = "Codex FBX Eye Bake Target"
        bake_target.image = image
        bake_target.select = True
        tree.nodes.active = bake_target

        for obj in bpy.context.scene.objects:
            obj.select_set(False)
        for obj in bake_objects:
            obj.hide_set(False)
            obj.select_set(True)
        view_layer.objects.active = bake_objects[0]
        scene.render.engine = "CYCLES"
        try:
            scene.cycles.samples = 1
        except Exception:
            pass

        bake_kwargs = {
            "type": "DIFFUSE",
            "pass_filter": {"COLOR"},
            "target": "IMAGE_TEXTURES",
            "use_clear": True,
            "margin": 2,
        }
        if override:
            with bpy.context.temp_override(**override):
                bpy.ops.object.bake(**bake_kwargs)
        else:
            bpy.ops.object.bake(**bake_kwargs)

        image.filepath_raw = target_path
        image.file_format = "PNG"
        image.save()
        completed = True
        report.fix("Baked SourceIO eye shader to FBX texture: %s" % target_path)
        return image
    except Exception as exc:
        report.warn("Could not bake SourceIO eye '%s': %s" % (source_material.name, exc))
        return None
    finally:
        if bake_target is not None and bake_target.name in tree.nodes:
            tree.nodes.remove(bake_target)
        tree.nodes.active = previous_active_node
        scene.render.engine = previous_engine
        for obj in bpy.context.scene.objects:
            obj.select_set(False)
        for obj in previous_selected:
            if obj and obj.name in bpy.context.scene.objects:
                obj.select_set(True)
        view_layer.objects.active = previous_active
        for obj, hidden in previous_hidden.items():
            if obj and obj.name in bpy.context.scene.objects:
                obj.hide_set(hidden)
        if not completed:
            bpy.data.images.remove(image)


def build_sourceio_eye_material(source_material, detected, eye_layout, objects, directory, image_cache, report):
    baked_image = bake_sourceio_eye_base_color(
        source_material, eye_layout, objects, directory, report
    )
    if baked_image is None:
        # A blank sclera is less deceptive than assigning the iris to the
        # entire eyeball. The warning tells the user that baking was skipped.
        baked_image = externalize_image(
            eye_layout["base_image"], "base_color", directory, image_cache, report
        )
        report.warn(
            "SourceIO eye '%s' used its sclera fallback because the iris bake failed."
            % source_material.name
        )

    stable_detected = dict(detected)
    stable_detected.update(
        {
            "base_color": baked_image,
            "normal": None,
            "roughness": None,
            "metallic": None,
            "alpha": None,
            "emission": None,
            "roughness_value": 0.35,
            "metallic_value": 0.0,
            "alpha_value": 1.0,
            "source_blend_method": "OPAQUE",
            "source_shadow_method": None,
        }
    )
    return rebuild_principled_material(
        source_material, stable_detected, {"base_color": baked_image}, report
    )


def rebuild_principled_material(source_material, detected, exported_maps, report):
    new_material = bpy.data.materials.new(make_unique_material_name(source_material.name if source_material else "Material"))
    new_material.use_nodes = True
    copy_material_metadata(source_material, new_material)
    new_material["codex_fbx_repaired"] = True
    if source_material:
        new_material["codex_source_material"] = source_material.name

    tree = new_material.node_tree
    for node in list(tree.nodes):
        tree.nodes.remove(node)

    output = tree.nodes.new("ShaderNodeOutputMaterial")
    output.location = (420, 0)
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.location = (120, 0)
    set_input_default(emission, ["Color"], detected["base_color_value"])
    set_input_default(emission, ["Strength"], 1.0)
    link_safely(tree, get_output(emission, ["Emission"]), get_input(output, ["Surface"]))

    # A bake-safe material intentionally exposes only the color image. Normal,
    # roughness, alpha, and metallic maps must not reintroduce a lit shader.
    base_image = exported_maps.get("base_color") or exported_maps.get("emission")
    if base_image:
        tex = make_texture_node(tree, base_image, "base_color", (-420, 160))
        link_safely(tree, get_output(tex, ["Color"]), get_input(emission, ["Color"]))

    if detected["source_shadow_method"]:
        try:
            new_material.shadow_method = detected["source_shadow_method"]
        except Exception:
            pass

    if detected["alpha_value"] < 1.0 or detected["source_blend_method"] != "OPAQUE":
        new_material.blend_method = detected["source_blend_method"] if detected["source_blend_method"] else "BLEND"

    report.fix("Created MMD-safe FBX material: %s -> %s" % (source_material.name if source_material else "None", new_material.name))
    return new_material


def copy_only_material(source_material, exported_maps, directory, image_cache, report):
    # Fallback for materials without a base texture. This preserves the original
    # node tree while making detected images point at copied/exported files.
    new_material = source_material.copy() if source_material else bpy.data.materials.new(make_unique_material_name("Material"))
    new_material.name = make_unique_material_name(source_material.name if source_material else "Material")
    new_material["codex_fbx_repaired"] = True
    if source_material:
        new_material["codex_source_material"] = source_material.name

    if new_material.use_nodes and new_material.node_tree:
        for node in new_material.node_tree.nodes:
            if node.bl_idname != "ShaderNodeTexImage" or not getattr(node, "image", None):
                continue
            semantic = classify_image_node(node)
            if semantic and exported_maps.get(semantic):
                node.image = exported_maps[semantic]
            elif not semantic:
                exported = externalize_passthrough_image(node.image, directory, image_cache, report)
                if exported:
                    node.image = exported

    report.fix("Copied material without destructive rebuild: %s -> %s" % (source_material.name if source_material else "None", new_material.name))
    return new_material


def build_fbx_material(source_material, detected, directory, image_cache, report, objects):
    eye_layout = detected.get("sourceio_eye")
    if eye_layout:
        return build_sourceio_eye_material(
            source_material, detected, eye_layout, objects, directory, image_cache, report
        )

    exported_maps = {}
    for semantic in ["base_color", "normal", "roughness", "metallic", "alpha", "emission", "toon", "sphere"]:
        image = detected.get(semantic)
        if image:
            exported_maps[semantic] = externalize_image(image, semantic, directory, image_cache, report)

    if detected["unclassified_image_count"] > 0:
        report.info(
            "Preserving original node tree for '%s' because %d image node(s) are not safely classifiable."
            % (source_material.name if source_material else "None", detected["unclassified_image_count"])
        )
        return copy_only_material(source_material, exported_maps, directory, image_cache, report)

    # Rebuilding a material without a confirmed albedo/base texture is usually
    # worse than preserving the original node tree, especially for MMD imports.
    if exported_maps.get("base_color") or CREATE_SOLID_FALLBACK_BASE_COLOR:
        return rebuild_principled_material(source_material, detected, exported_maps, report)
    return copy_only_material(source_material, exported_maps, directory, image_cache, report)


def assign_repaired_materials(objects, report):
    directory = texture_directory()
    image_cache = {}
    material_cache = {}

    for obj in objects:
        ensure_uv_map(obj, report)
        if not obj.material_slots:
            material = bpy.data.materials.new(make_unique_material_name("%s_Material" % obj.name))
            material.diffuse_color = (0.8, 0.8, 0.8, 1.0)
            obj.data.materials.append(material)
            report.fix("Added missing material slot to mesh '%s'." % obj.name)

        for slot in obj.material_slots:
            source = slot.material
            if source in material_cache:
                slot.material = material_cache[source]
                continue
            detected = detect_material(source, report)
            repaired = build_fbx_material(source, detected, directory, image_cache, report, objects)
            material_cache[source] = repaired
            slot.material = repaired

    return directory


def export_fbx(objects, report):
    if not AUTO_EXPORT_FBX:
        return None
    filepath = os.path.join(blend_directory(), FBX_EXPORT_FILENAME)
    state = remember_selection()
    try:
        if bpy.context.object and bpy.context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        export_objects = set(objects)
        for obj in objects:
            parent = obj.parent
            while parent is not None:
                export_objects.add(parent)
                parent = parent.parent
            for modifier in obj.modifiers:
                if modifier.type == "ARMATURE" and modifier.object is not None:
                    armature = modifier.object
                    export_objects.add(armature)
                    parent = armature.parent
                    while parent is not None:
                        export_objects.add(parent)
                        parent = parent.parent

        for obj in bpy.context.scene.objects:
            obj.select_set(False)
        for obj in export_objects:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = objects[0] if objects else None
        bpy.ops.export_scene.fbx(
            filepath=filepath,
            use_selection=True,
            object_types={"MESH", "ARMATURE"},
            path_mode="COPY",
            embed_textures=False,
            add_leaf_bones=False,
            bake_anim=False,
        )
        report.fix("Exported FBX: %s" % filepath)
        return filepath
    except Exception as exc:
        report.warn("FBX export failed: %s" % exc)
        return None
    finally:
        restore_selection(state)


def run_mmd_safe_fbx_texture_repair():
    report = RepairReport()
    objects = selected_mesh_objects()
    if not objects:
        report.warn("No mesh objects found.")
        return report.emit()

    report.info("Repairing %d mesh object(s): %s" % (len(objects), ", ".join(obj.name for obj in objects)))
    directory = assign_repaired_materials(objects, report)
    report.info("External texture directory: %s" % directory)
    export_fbx(objects, report)
    return report.emit()


class CODEX_OT_mmd_safe_repair_fbx_textures(bpy.types.Operator):
    bl_idname = "object.codex_mmd_safe_repair_fbx_textures"
    bl_label = "Codex MMD-Safe Repair FBX Textures"
    bl_description = "MMD-safe selected mesh material and texture repair for FBX export"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        run_mmd_safe_fbx_texture_repair()
        return {"FINISHED"}


def menu_func(self, context):
    self.layout.operator(CODEX_OT_mmd_safe_repair_fbx_textures.bl_idname)


def register():
    bpy.utils.register_class(CODEX_OT_mmd_safe_repair_fbx_textures)
    bpy.types.VIEW3D_MT_object.append(menu_func)


def unregister():
    bpy.types.VIEW3D_MT_object.remove(menu_func)
    bpy.utils.unregister_class(CODEX_OT_mmd_safe_repair_fbx_textures)


if __name__ == "__main__":
    run_mmd_safe_fbx_texture_repair()
