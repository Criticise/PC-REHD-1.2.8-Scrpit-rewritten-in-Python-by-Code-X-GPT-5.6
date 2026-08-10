# cython: language_level=3
"""
Cython bindings for ufbx - thin wrapper around C API
"""
from libc.stdlib cimport free
from libc.stdint cimport uint32_t
from enum import IntEnum
import os
import numpy as np
cimport numpy as np

np.import_array()

# C declarations
cdef extern from "ufbx-c/ufbx.h":
    ctypedef struct ufbx_vec3:
        double x
        double y
        double z

    ctypedef struct ufbx_vec4:
        double x
        double y
        double z
        double w

    ctypedef struct ufbx_string:
        const char* data
        size_t length

    ctypedef struct ufbx_material_feature_info:
        bint enabled

    ctypedef struct ufbx_material_features:
        ufbx_material_feature_info pbr
        ufbx_material_feature_info metalness
        ufbx_material_feature_info diffuse
        ufbx_material_feature_info specular
        ufbx_material_feature_info emission
        ufbx_material_feature_info transmission
        ufbx_material_feature_info coat
        ufbx_material_feature_info sheen
        ufbx_material_feature_info opacity
        ufbx_material_feature_info ambient_occlusion
        ufbx_material_feature_info matte
        ufbx_material_feature_info unlit
        ufbx_material_feature_info ior
        ufbx_material_feature_info diffuse_roughness
        ufbx_material_feature_info transmission_roughness
        ufbx_material_feature_info thin_walled
        ufbx_material_feature_info caustics
        ufbx_material_feature_info exit_to_background
        ufbx_material_feature_info internal_reflections
        ufbx_material_feature_info double_sided
        ufbx_material_feature_info roughness_as_glossiness
        ufbx_material_feature_info coat_roughness_as_glossiness
        ufbx_material_feature_info transmission_roughness_as_glossiness

    # Forward declaration
    ctypedef struct ufbx_texture

    ctypedef struct ufbx_material_texture:
        ufbx_string material_prop
        ufbx_string shader_prop
        ufbx_texture* texture

    # Generic list structure
    ctypedef struct ufbx_material_texture_list:
        ufbx_material_texture* data
        size_t count

    ctypedef struct ufbx_material_map:
        ufbx_vec4 value_vec4
        long long value_int
        ufbx_texture* texture
        bint has_value
        bint texture_enabled
        bint feature_disabled
        unsigned int value_components

    ctypedef struct ufbx_material_fbx_maps:
        ufbx_material_map diffuse_factor
        ufbx_material_map diffuse_color
        ufbx_material_map specular_factor
        ufbx_material_map specular_color
        ufbx_material_map specular_exponent
        ufbx_material_map reflection_factor
        ufbx_material_map reflection_color
        ufbx_material_map transparency_factor
        ufbx_material_map transparency_color
        ufbx_material_map emission_factor
        ufbx_material_map emission_color
        ufbx_material_map ambient_factor
        ufbx_material_map ambient_color
        ufbx_material_map normal_map
        ufbx_material_map bump
        ufbx_material_map bump_factor
        ufbx_material_map displacement_factor
        ufbx_material_map displacement
        ufbx_material_map vector_displacement_factor
        ufbx_material_map vector_displacement

    ctypedef struct ufbx_material_pbr_maps:
        ufbx_material_map base_factor
        ufbx_material_map base_color
        ufbx_material_map roughness
        ufbx_material_map metalness
        ufbx_material_map diffuse_roughness
        ufbx_material_map specular_factor
        ufbx_material_map specular_color
        ufbx_material_map specular_ior
        ufbx_material_map specular_anisotropy
        ufbx_material_map specular_rotation
        ufbx_material_map transmission_factor
        ufbx_material_map transmission_color
        ufbx_material_map transmission_depth
        ufbx_material_map transmission_scatter
        ufbx_material_map transmission_scatter_anisotropy
        ufbx_material_map transmission_dispersion
        ufbx_material_map transmission_roughness
        ufbx_material_map transmission_extra_roughness
        ufbx_material_map transmission_priority
        ufbx_material_map transmission_enable_in_aov
        ufbx_material_map subsurface_factor
        ufbx_material_map subsurface_color
        ufbx_material_map subsurface_radius
        ufbx_material_map subsurface_scale
        ufbx_material_map subsurface_anisotropy
        ufbx_material_map subsurface_tint_color
        ufbx_material_map subsurface_type
        ufbx_material_map sheen_factor
        ufbx_material_map sheen_color
        ufbx_material_map sheen_roughness
        ufbx_material_map coat_factor
        ufbx_material_map coat_color
        ufbx_material_map coat_roughness
        ufbx_material_map coat_ior
        ufbx_material_map coat_anisotropy
        ufbx_material_map coat_rotation
        ufbx_material_map coat_normal
        ufbx_material_map coat_affect_base_color
        ufbx_material_map coat_affect_base_roughness
        ufbx_material_map thin_film_factor
        ufbx_material_map thin_film_thickness
        ufbx_material_map thin_film_ior
        ufbx_material_map emission_factor
        ufbx_material_map emission_color
        ufbx_material_map opacity
        ufbx_material_map indirect_diffuse
        ufbx_material_map indirect_specular
        ufbx_material_map normal_map
        ufbx_material_map tangent_map
        ufbx_material_map displacement_map
        ufbx_material_map matte_factor
        ufbx_material_map matte_color
        ufbx_material_map ambient_occlusion
        ufbx_material_map glossiness
        ufbx_material_map coat_glossiness
        ufbx_material_map transmission_glossiness

    ctypedef struct ufbx_string:
        const char* data
        size_t length

    # Forward declarations - pointers only
    ctypedef struct ufbx_dom_node
    ctypedef struct ufbx_scene
    ctypedef struct ufbx_shader
    ctypedef struct ufbx_video

    # List structures
    ctypedef struct ufbx_node_list:
        void** data
        size_t count
    ctypedef struct ufbx_connection_list:
        void** data
        size_t count

    # Full ufbx_element structure matching ufbx.h layout
    ctypedef struct ufbx_element:
        ufbx_string name
        void* props  # ufbx_props - treated as opaque
        unsigned int element_id
        unsigned int typed_id
        ufbx_node_list instances
        int type  # ufbx_element_type
        ufbx_connection_list connections_src
        ufbx_connection_list connections_dst
        ufbx_dom_node* dom_node
        ufbx_scene* scene

    # Forward declaration for shader
    # (already declared above)

    # Full ufbx_material structure matching ufbx.h layout
    ctypedef struct ufbx_material:
        # Element fields (union expanded as struct fields)
        ufbx_string name
        void* props  # ufbx_props
        unsigned int element_id
        unsigned int typed_id
        ufbx_node_list instances
        int type  # ufbx_element_type
        ufbx_connection_list connections_src
        ufbx_connection_list connections_dst
        ufbx_dom_node* dom_node
        ufbx_scene* scene
        # Material-specific fields
        ufbx_material_fbx_maps fbx
        ufbx_material_pbr_maps pbr
        ufbx_material_features features
        int shader_type
        ufbx_shader* shader
        ufbx_string shading_model_name
        ufbx_string shader_prop_prefix
        ufbx_material_texture_list textures

    # Metadata structure
    ctypedef struct ufbx_metadata:
        bint ascii
        unsigned int version
        int file_format
        ufbx_string creator
        bint big_endian
        ufbx_string filename
        ufbx_string relative_root

    # Time/Animation enums
    ctypedef enum ufbx_time_mode:
        pass
    ctypedef enum ufbx_time_protocol:
        pass
    ctypedef enum ufbx_snap_mode:
        pass
    ctypedef enum ufbx_coordinate_axis:
        pass

    # Coordinate axes structure
    ctypedef struct ufbx_coordinate_axes:
        int right
        int up
        int front

    # Scene settings structure
    ctypedef struct ufbx_scene_settings:
        ufbx_coordinate_axes axes
        double unit_meters
        double frames_per_second
        ufbx_vec3 ambient_color
        ufbx_string default_camera
        ufbx_time_mode time_mode
        ufbx_time_protocol time_protocol
        ufbx_snap_mode snap_mode
        ufbx_coordinate_axis original_axis_up
        double original_unit_meters

    # Forward declarations for element types
    ctypedef struct ufbx_empty:
        # Element fields (union expanded)
        ufbx_string name
        void* props
        unsigned int element_id
        unsigned int typed_id
        ufbx_node_list instances
        int type
        ufbx_connection_list connections_src
        ufbx_connection_list connections_dst
        ufbx_dom_node* dom_node
        ufbx_scene* scene

    ctypedef struct ufbx_unknown:
        # Element fields (union expanded)
        ufbx_string name
        void* props
        unsigned int element_id
        unsigned int typed_id
        ufbx_node_list instances
        int element_type
        ufbx_connection_list connections_src
        ufbx_connection_list connections_dst
        ufbx_dom_node* dom_node
        ufbx_scene* scene_ptr
        # Unknown-specific fields
        ufbx_string type_name
        ufbx_string super_type
        ufbx_string sub_type

    # List structures
    ctypedef struct ufbx_empty_list:
        ufbx_empty** data
        size_t count
    ctypedef struct ufbx_unknown_list:
        ufbx_unknown** data
        size_t count

    # Scene structure with metadata and settings
    ctypedef struct ufbx_scene:
        ufbx_metadata metadata
        ufbx_scene_settings settings
        ufbx_empty_list empties
        ufbx_unknown_list unknowns

    ctypedef struct ufbx_mesh:
        pass
    ctypedef struct ufbx_node:
        pass
    ctypedef struct ufbx_light:
        pass
    ctypedef struct ufbx_camera:
        pass
    ctypedef struct ufbx_bone:
        pass

    # Forward declarations for texture-related types
    # (ufbx_video already declared above)
    ctypedef struct ufbx_blob:
        void* data
        size_t size
    ctypedef struct ufbx_texture_layer_list:
        void** data
        size_t count
    ctypedef struct ufbx_texture_list:
        ufbx_texture** data
        size_t count
    ctypedef struct ufbx_transform:
        ufbx_vec3 translation
        ufbx_vec4 rotation  # quaternion (x, y, z, w)
        ufbx_vec3 scale

    # Full ufbx_texture structure matching ufbx.h layout
    ctypedef struct ufbx_texture:
        # Element fields (union expanded as struct fields)
        ufbx_string name
        void* props  # ufbx_props
        unsigned int element_id
        unsigned int typed_id
        ufbx_node_list instances
        int element_type  # ufbx_element_type (renamed to avoid conflict with texture.type)
        ufbx_connection_list connections_src
        ufbx_connection_list connections_dst
        ufbx_dom_node* dom_node
        ufbx_scene* scene
        # Texture-specific fields - MUST follow element fields
        int type  # ufbx_texture_type - comes IMMEDIATELY after element
        # File paths
        ufbx_string filename
        ufbx_string absolute_filename
        ufbx_string relative_filename
        # Raw (non-UTF-8) paths
        ufbx_blob raw_filename
        ufbx_blob raw_absolute_filename
        ufbx_blob raw_relative_filename
        # Embedded content
        ufbx_blob content
        # Video reference
        ufbx_video* video
        # File info
        unsigned int file_index
        bint has_file
        # Layered textures
        ufbx_texture_layer_list layers
        # Shader reference (not exposed yet)
        void* shader  # ufbx_shader_texture*
        # File textures list (not exposed yet)
        ufbx_texture_list file_textures
        # UV settings
        ufbx_string uv_set
        int wrap_u  # ufbx_wrap_mode
        int wrap_v  # ufbx_wrap_mode
        # UV transform (not exposed yet)
        bint has_uv_transform
        ufbx_transform uv_transform

    ctypedef struct ufbx_anim_stack:
        pass
    ctypedef struct ufbx_anim_layer:
        pass
    ctypedef struct ufbx_anim_curve:
        pass
    ctypedef struct ufbx_skin_deformer:
        pass
    ctypedef struct ufbx_skin_cluster:
        pass
    ctypedef struct ufbx_blend_deformer:
        pass
    ctypedef struct ufbx_blend_channel:
        pass
    ctypedef struct ufbx_blend_shape:
        pass
    ctypedef struct ufbx_constraint:
        pass

    # Find functions from ufbx
    ufbx_node* ufbx_find_node(const ufbx_scene *scene, const char *name)
    ufbx_material* ufbx_find_material(const ufbx_scene *scene, const char *name)

cdef extern from "ufbx_wrapper.h":

    # Scene management
    ufbx_scene* ufbx_wrapper_load_file(const char *filename, char **error_msg)
    void ufbx_wrapper_free_scene(ufbx_scene *scene)

    # Scene queries
    size_t ufbx_wrapper_scene_get_num_nodes(const ufbx_scene *scene)
    size_t ufbx_wrapper_scene_get_num_meshes(const ufbx_scene *scene)
    size_t ufbx_wrapper_scene_get_num_materials(const ufbx_scene *scene)
    ufbx_node* ufbx_wrapper_scene_get_root_node(const ufbx_scene *scene)
    int ufbx_wrapper_scene_get_axes_right(const ufbx_scene *scene)
    int ufbx_wrapper_scene_get_axes_up(const ufbx_scene *scene)
    int ufbx_wrapper_scene_get_axes_front(const ufbx_scene *scene)

    # Node access
    ufbx_node* ufbx_wrapper_scene_get_node(const ufbx_scene *scene, size_t index)
    const char* ufbx_wrapper_node_get_name(const ufbx_node *node)
    size_t ufbx_wrapper_node_get_num_children(const ufbx_node *node)
    ufbx_node* ufbx_wrapper_node_get_child(const ufbx_node *node, size_t index)
    ufbx_node* ufbx_wrapper_node_get_parent(const ufbx_node *node)
    ufbx_mesh* ufbx_wrapper_node_get_mesh(const ufbx_node *node)
    bint ufbx_wrapper_node_is_root(const ufbx_node *node)
    void ufbx_wrapper_node_get_world_transform(const ufbx_node *node, double *matrix16)
    void ufbx_wrapper_node_get_local_transform(const ufbx_node *node, double *matrix16)
    void ufbx_wrapper_node_get_node_to_world(const ufbx_node *node, double *matrix16)
    void ufbx_wrapper_node_get_node_to_parent(const ufbx_node *node, double *matrix16)
    void ufbx_wrapper_node_get_geometry_transform(const ufbx_node *node, double *translation3, double *rotation4, double *scale3)

    # Node additional properties
    int ufbx_wrapper_node_get_attrib_type(const ufbx_node *node)
    int ufbx_wrapper_node_get_inherit_mode(const ufbx_node *node)
    bint ufbx_wrapper_node_get_visible(const ufbx_node *node)
    void ufbx_wrapper_node_get_euler_rotation(const ufbx_node *node, double *xyz)
    int ufbx_wrapper_node_get_rotation_order(const ufbx_node *node)

    # Mesh access
    ufbx_mesh* ufbx_wrapper_scene_get_mesh(const ufbx_scene *scene, size_t index)
    const char* ufbx_wrapper_mesh_get_name(const ufbx_mesh *mesh)
    size_t ufbx_wrapper_mesh_get_num_vertices(const ufbx_mesh *mesh)
    size_t ufbx_wrapper_mesh_get_num_indices(const ufbx_mesh *mesh)
    size_t ufbx_wrapper_mesh_get_num_faces(const ufbx_mesh *mesh)
    size_t ufbx_wrapper_mesh_get_num_triangles(const ufbx_mesh *mesh)

    # Mesh vertex data
    const float* ufbx_wrapper_mesh_get_vertex_positions(const ufbx_mesh *mesh, size_t *out_count)
    const float* ufbx_wrapper_mesh_get_vertex_normals(const ufbx_mesh *mesh, size_t *out_count)
    const float* ufbx_wrapper_mesh_get_vertex_uvs(const ufbx_mesh *mesh, size_t *out_count)
    const float* ufbx_wrapper_mesh_get_vertex_tangents(const ufbx_mesh *mesh, size_t *out_count)
    const float* ufbx_wrapper_mesh_get_vertex_bitangents(const ufbx_mesh *mesh, size_t *out_count)
    const float* ufbx_wrapper_mesh_get_vertex_colors(const ufbx_mesh *mesh, size_t *out_count)
    const uint32_t* ufbx_wrapper_mesh_get_indices(const ufbx_mesh *mesh, size_t *out_count)

    # Mesh face data
    size_t ufbx_wrapper_mesh_get_face_count(const ufbx_mesh *mesh)
    void ufbx_wrapper_mesh_get_face(const ufbx_mesh *mesh, size_t index, uint32_t *index_begin, uint32_t *num_indices)
    const uint32_t* ufbx_wrapper_mesh_get_face_material(const ufbx_mesh *mesh, size_t *out_count)
    const double* ufbx_wrapper_mesh_get_edge_crease(const ufbx_mesh *mesh, size_t *out_count)
    const float* ufbx_wrapper_mesh_get_vertex_crease(const ufbx_mesh *mesh, size_t *out_count)

    # Mesh deformers
    size_t ufbx_wrapper_mesh_get_num_skin_deformers(const ufbx_mesh *mesh)
    ufbx_skin_deformer* ufbx_wrapper_mesh_get_skin_deformer(const ufbx_mesh *mesh, size_t index)
    size_t ufbx_wrapper_mesh_get_num_blend_deformers(const ufbx_mesh *mesh)
    ufbx_blend_deformer* ufbx_wrapper_mesh_get_blend_deformer(const ufbx_mesh *mesh, size_t index)

    # Material access
    ufbx_material* ufbx_wrapper_scene_get_material(const ufbx_scene *scene, size_t index)
    size_t ufbx_wrapper_mesh_get_num_materials(const ufbx_mesh *mesh)
    ufbx_material* ufbx_wrapper_mesh_get_material(const ufbx_mesh *mesh, size_t index)

    # Texture properties
    const char* ufbx_wrapper_texture_get_name(const ufbx_texture *texture)
    const char* ufbx_wrapper_texture_get_filename(const ufbx_texture *texture)
    const char* ufbx_wrapper_texture_get_relative_filename(const ufbx_texture *texture)
    const char* ufbx_wrapper_texture_get_absolute_filename(const ufbx_texture *texture)
    int ufbx_wrapper_texture_get_type(const ufbx_texture *texture)

    # Light access
    size_t ufbx_wrapper_scene_get_num_lights(const ufbx_scene *scene)
    ufbx_light* ufbx_wrapper_scene_get_light(const ufbx_scene *scene, size_t index)
    ufbx_light* ufbx_wrapper_node_get_light(const ufbx_node *node)
    const char* ufbx_wrapper_light_get_name(const ufbx_light *light)
    void ufbx_wrapper_light_get_color(const ufbx_light *light, float *rgb)
    double ufbx_wrapper_light_get_intensity(const ufbx_light *light)
    void ufbx_wrapper_light_get_local_direction(const ufbx_light *light, float *xyz)
    int ufbx_wrapper_light_get_type(const ufbx_light *light)
    int ufbx_wrapper_light_get_decay(const ufbx_light *light)
    int ufbx_wrapper_light_get_area_shape(const ufbx_light *light)
    double ufbx_wrapper_light_get_inner_angle(const ufbx_light *light)
    double ufbx_wrapper_light_get_outer_angle(const ufbx_light *light)
    bint ufbx_wrapper_light_get_cast_light(const ufbx_light *light)
    bint ufbx_wrapper_light_get_cast_shadows(const ufbx_light *light)

    # Camera access
    size_t ufbx_wrapper_scene_get_num_cameras(const ufbx_scene *scene)
    ufbx_camera* ufbx_wrapper_scene_get_camera(const ufbx_scene *scene, size_t index)
    ufbx_camera* ufbx_wrapper_node_get_camera(const ufbx_node *node)
    const char* ufbx_wrapper_camera_get_name(const ufbx_camera *camera)
    int ufbx_wrapper_camera_get_projection_mode(const ufbx_camera *camera)
    void ufbx_wrapper_camera_get_resolution(const ufbx_camera *camera, float *xy)
    bint ufbx_wrapper_camera_get_resolution_is_pixels(const ufbx_camera *camera)
    void ufbx_wrapper_camera_get_field_of_view_deg(const ufbx_camera *camera, float *xy)
    void ufbx_wrapper_camera_get_field_of_view_tan(const ufbx_camera *camera, float *xy)
    double ufbx_wrapper_camera_get_orthographic_extent(const ufbx_camera *camera)
    void ufbx_wrapper_camera_get_orthographic_size(const ufbx_camera *camera, float *xy)
    double ufbx_wrapper_camera_get_aspect_ratio(const ufbx_camera *camera)
    double ufbx_wrapper_camera_get_near_plane(const ufbx_camera *camera)
    double ufbx_wrapper_camera_get_far_plane(const ufbx_camera *camera)

    # Bone access
    size_t ufbx_wrapper_scene_get_num_bones(const ufbx_scene *scene)
    ufbx_bone* ufbx_wrapper_scene_get_bone(const ufbx_scene *scene, size_t index)
    ufbx_bone* ufbx_wrapper_node_get_bone(const ufbx_node *node)
    const char* ufbx_wrapper_bone_get_name(const ufbx_bone *bone)
    double ufbx_wrapper_bone_get_radius(const ufbx_bone *bone)
    double ufbx_wrapper_bone_get_relative_length(const ufbx_bone *bone)
    bint ufbx_wrapper_bone_is_root(const ufbx_bone *bone)

    # Texture access
    size_t ufbx_wrapper_scene_get_num_textures(const ufbx_scene *scene)
    ufbx_texture* ufbx_wrapper_scene_get_texture(const ufbx_scene *scene, size_t index)
    const char* ufbx_wrapper_texture_get_name(const ufbx_texture *texture)
    const char* ufbx_wrapper_texture_get_filename(const ufbx_texture *texture)
    const char* ufbx_wrapper_texture_get_absolute_filename(const ufbx_texture *texture)
    const char* ufbx_wrapper_texture_get_relative_filename(const ufbx_texture *texture)
    int ufbx_wrapper_texture_get_type(const ufbx_texture *texture)

    # AnimStack access
    size_t ufbx_wrapper_scene_get_num_anim_stacks(const ufbx_scene *scene)
    ufbx_anim_stack* ufbx_wrapper_scene_get_anim_stack(const ufbx_scene *scene, size_t index)
    const char* ufbx_wrapper_anim_stack_get_name(const ufbx_anim_stack *anim_stack)
    double ufbx_wrapper_anim_stack_get_time_begin(const ufbx_anim_stack *anim_stack)
    double ufbx_wrapper_anim_stack_get_time_end(const ufbx_anim_stack *anim_stack)
    size_t ufbx_wrapper_anim_stack_get_num_layers(const ufbx_anim_stack *anim_stack)
    ufbx_anim_layer* ufbx_wrapper_anim_stack_get_layer(const ufbx_anim_stack *anim_stack, size_t index)

    # AnimLayer access
    const char* ufbx_wrapper_anim_layer_get_name(const ufbx_anim_layer *anim_layer)
    double ufbx_wrapper_anim_layer_get_weight(const ufbx_anim_layer *anim_layer)
    bint ufbx_wrapper_anim_layer_get_weight_is_animated(const ufbx_anim_layer *anim_layer)
    bint ufbx_wrapper_anim_layer_get_blended(const ufbx_anim_layer *anim_layer)
    bint ufbx_wrapper_anim_layer_get_additive(const ufbx_anim_layer *anim_layer)
    bint ufbx_wrapper_anim_layer_get_compose_rotation(const ufbx_anim_layer *anim_layer)
    bint ufbx_wrapper_anim_layer_get_compose_scale(const ufbx_anim_layer *anim_layer)

    # AnimCurve access
    size_t ufbx_wrapper_scene_get_num_anim_curves(const ufbx_scene *scene)
    ufbx_anim_curve* ufbx_wrapper_scene_get_anim_curve(const ufbx_scene *scene, size_t index)
    const char* ufbx_wrapper_anim_curve_get_name(const ufbx_anim_curve *anim_curve)
    size_t ufbx_wrapper_anim_curve_get_num_keyframes(const ufbx_anim_curve *anim_curve)
    double ufbx_wrapper_anim_curve_get_min_value(const ufbx_anim_curve *anim_curve)
    double ufbx_wrapper_anim_curve_get_max_value(const ufbx_anim_curve *anim_curve)
    double ufbx_wrapper_anim_curve_get_min_time(const ufbx_anim_curve *anim_curve)
    double ufbx_wrapper_anim_curve_get_max_time(const ufbx_anim_curve *anim_curve)

    # SkinDeformer access
    size_t ufbx_wrapper_scene_get_num_skin_deformers(const ufbx_scene *scene)
    ufbx_skin_deformer* ufbx_wrapper_scene_get_skin_deformer(const ufbx_scene *scene, size_t index)
    const char* ufbx_wrapper_skin_deformer_get_name(const ufbx_skin_deformer *skin_deformer)
    size_t ufbx_wrapper_skin_deformer_get_num_clusters(const ufbx_skin_deformer *skin_deformer)
    ufbx_skin_cluster* ufbx_wrapper_skin_deformer_get_cluster(const ufbx_skin_deformer *skin_deformer, size_t index)

    # SkinCluster access
    const char* ufbx_wrapper_skin_cluster_get_name(const ufbx_skin_cluster *skin_cluster)
    const char* ufbx_wrapper_skin_cluster_get_bone_name(const ufbx_skin_cluster *skin_cluster)
    size_t ufbx_wrapper_skin_cluster_get_num_weights(const ufbx_skin_cluster *skin_cluster)
    const uint32_t* ufbx_wrapper_skin_cluster_get_vertices(const ufbx_skin_cluster *skin_cluster, size_t *out_count)
    const double* ufbx_wrapper_skin_cluster_get_weights(const ufbx_skin_cluster *skin_cluster, size_t *out_count)

    # BlendDeformer access
    size_t ufbx_wrapper_scene_get_num_blend_deformers(const ufbx_scene *scene)
    ufbx_blend_deformer* ufbx_wrapper_scene_get_blend_deformer(const ufbx_scene *scene, size_t index)
    const char* ufbx_wrapper_blend_deformer_get_name(const ufbx_blend_deformer *blend_deformer)
    size_t ufbx_wrapper_blend_deformer_get_num_channels(const ufbx_blend_deformer *blend_deformer)
    ufbx_blend_channel* ufbx_wrapper_blend_deformer_get_channel(const ufbx_blend_deformer *blend_deformer, size_t index)

    # BlendChannel access
    const char* ufbx_wrapper_blend_channel_get_name(const ufbx_blend_channel *blend_channel)
    double ufbx_wrapper_blend_channel_get_weight(const ufbx_blend_channel *blend_channel)

    # BlendShape access
    size_t ufbx_wrapper_scene_get_num_blend_shapes(const ufbx_scene *scene)
    ufbx_blend_shape* ufbx_wrapper_scene_get_blend_shape(const ufbx_scene *scene, size_t index)
    const char* ufbx_wrapper_blend_shape_get_name(const ufbx_blend_shape *blend_shape)
    size_t ufbx_wrapper_blend_shape_get_num_offsets(const ufbx_blend_shape *blend_shape)

    # Constraint access
    size_t ufbx_wrapper_scene_get_num_constraints(const ufbx_scene *scene)
    ufbx_constraint* ufbx_wrapper_scene_get_constraint(const ufbx_scene *scene, size_t index)
    const char* ufbx_wrapper_constraint_get_name(const ufbx_constraint *constraint)
    int ufbx_wrapper_constraint_get_type(const ufbx_constraint *constraint)
    double ufbx_wrapper_constraint_get_weight(const ufbx_constraint *constraint)
    bint ufbx_wrapper_constraint_get_active(const ufbx_constraint *constraint)


# Python classes
class UfbxError(Exception):
    """Base exception for ufbx errors."""
    pass


cdef object _copy_float_array_2d(const float* data, size_t count, np.npy_intp columns):
    cdef np.npy_intp shape[2]
    cdef object view
    if data == NULL or count == 0:
        return None
    shape[0] = <np.npy_intp>count
    shape[1] = columns
    view = np.PyArray_SimpleNewFromData(2, shape, np.NPY_FLOAT32, <void*>data)
    return np.array(view, copy=True)


cdef object _copy_uint32_array_1d(const uint32_t* data, size_t count):
    cdef np.npy_intp shape[1]
    cdef object view
    if data == NULL or count == 0:
        return None
    shape[0] = <np.npy_intp>count
    view = np.PyArray_SimpleNewFromData(1, shape, np.NPY_UINT32, <void*>data)
    return np.array(view, copy=True)


cdef object _copy_double_array_1d(const double* data, size_t count):
    cdef np.npy_intp shape[1]
    cdef object view
    if data == NULL or count == 0:
        return None
    shape[0] = <np.npy_intp>count
    view = np.PyArray_SimpleNewFromData(1, shape, np.NPY_FLOAT64, <void*>data)
    return np.array(view, copy=True)


class UfbxFileNotFoundError(UfbxError, FileNotFoundError):
    """Raised when a file is not found."""
    pass


class UfbxIOError(UfbxError):
    """I/O related error."""
    pass


class UfbxOutOfMemoryError(UfbxError):
    """Out of memory error."""
    pass


class RotationOrder(IntEnum):
    ROTATION_ORDER_XYZ = 0
    ROTATION_ORDER_XZY = 1
    ROTATION_ORDER_YZX = 2
    ROTATION_ORDER_YXZ = 3
    ROTATION_ORDER_ZXY = 4
    ROTATION_ORDER_ZYX = 5
    ROTATION_ORDER_SPHERIC = 6


class ElementType(IntEnum):
    ELEMENT_UNKNOWN = 0
    ELEMENT_NODE = 1
    ELEMENT_MESH = 2
    ELEMENT_LIGHT = 3
    ELEMENT_CAMERA = 4
    ELEMENT_MATERIAL = 5
    ELEMENT_BONE = 6


class PropType(IntEnum):
    PROP_UNKNOWN = 0
    PROP_BOOLEAN = 1
    PROP_INTEGER = 2
    PROP_FLOAT = 3
    PROP_STRING = 4


class PropFlags(IntEnum):
    PROP_FLAG_NONE = 0
    PROP_FLAG_ANIMATABLE = 1


class InheritMode(IntEnum):
    INHERIT_MODE_NORMAL = 0
    INHERIT_MODE_IGNORE_PARENT = 1


class MirrorAxis(IntEnum):
    MIRROR_AXIS_X = 0
    MIRROR_AXIS_Y = 1
    MIRROR_AXIS_Z = 2


class CoordinateAxis(IntEnum):
    COORDINATE_AXIS_POSITIVE_X = 0
    COORDINATE_AXIS_NEGATIVE_X = 1
    COORDINATE_AXIS_POSITIVE_Y = 2
    COORDINATE_AXIS_NEGATIVE_Y = 3
    COORDINATE_AXIS_POSITIVE_Z = 4
    COORDINATE_AXIS_NEGATIVE_Z = 5
    COORDINATE_AXIS_UNKNOWN = 6


class CoordinateAxes:
    """Scene coordinate axes (right, up, front). Maps X/Y/Z to world-space directions."""

    __slots__ = ("right", "up", "front")

    def __init__(self, right: int, up: int, front: int):
        unk = CoordinateAxis.COORDINATE_AXIS_UNKNOWN
        self.right = CoordinateAxis(right) if 0 <= right <= 6 else unk
        self.up = CoordinateAxis(up) if 0 <= up <= 6 else unk
        self.front = CoordinateAxis(front) if 0 <= front <= 6 else unk

    def __repr__(self) -> str:
        return f"CoordinateAxes(right={self.right!r}, up={self.up!r}, front={self.front!r})"


class SubdivisionDisplayMode(IntEnum):
    SUBDIVISION_DISPLAY_MODE_OFF = 0
    SUBDIVISION_DISPLAY_MODE_ON = 1


class SubdivisionBoundary(IntEnum):
    SUBDIVISION_BOUNDARY_SHARP = 0
    SUBDIVISION_BOUNDARY_SMOOTH = 1


class LightType(IntEnum):
    LIGHT_POINT = 0
    LIGHT_DIRECTIONAL = 1
    LIGHT_SPOT = 2
    LIGHT_AREA = 3


class LightDecay(IntEnum):
    LIGHT_DECAY_NONE = 0
    LIGHT_DECAY_LINEAR = 1
    LIGHT_DECAY_QUADRATIC = 2


class LightAreaShape(IntEnum):
    LIGHT_AREA_SHAPE_RECTANGLE = 0
    LIGHT_AREA_SHAPE_SPHERE = 1


class ProjectionMode(IntEnum):
    PROJECTION_MODE_PERSPECTIVE = 0
    PROJECTION_MODE_ORTHOGRAPHIC = 1


class AspectMode(IntEnum):
    ASPECT_MODE_FIXED = 0
    ASPECT_MODE_WINDOW_SIZE = 1


class ApertureMode(IntEnum):
    APERTURE_MODE_VERTICAL = 0
    APERTURE_MODE_HORIZONTAL = 1


class ShaderType(IntEnum):
    SHADER_UNKNOWN = 0
    SHADER_FBX_LAMBERT = 1
    SHADER_FBX_PHONG = 2
    SHADER_OSL_STANDARD_SURFACE = 3
    SHADER_ARNOLD_STANDARD_SURFACE = 4
    SHADER_3DS_MAX_PHYSICAL_MATERIAL = 5
    SHADER_3DS_MAX_PBR_METAL_ROUGH = 6
    SHADER_3DS_MAX_PBR_SPEC_GLOSS = 7
    SHADER_GLTF_MATERIAL = 8
    SHADER_OPENPBR_MATERIAL = 9
    SHADER_SHADERFX_GRAPH = 10
    SHADER_BLENDER_PHONG = 11
    SHADER_WAVEFRONT_MTL = 12


class TextureType(IntEnum):
    TEXTURE_TYPE_DIFFUSE = 0
    TEXTURE_TYPE_NORMAL = 1


class BlendMode(IntEnum):
    BLEND_MODE_REPLACE = 0
    BLEND_MODE_ADD = 1


class WrapMode(IntEnum):
    WRAP_MODE_REPEAT = 0
    WRAP_MODE_CLAMP = 1


class Interpolation(IntEnum):
    INTERPOLATION_CONSTANT_PREV = 0
    INTERPOLATION_CONSTANT_NEXT = 1
    INTERPOLATION_LINEAR = 2
    INTERPOLATION_CUBIC = 3


class ExtrapolationMode(IntEnum):
    EXTRAPOLATION_MODE_CONSTANT = 0
    EXTRAPOLATION_MODE_LINEAR = 1


class ConstraintType(IntEnum):
    CONSTRAINT_AIM = 0
    CONSTRAINT_PARENT = 1


class ErrorType(IntEnum):
    ERROR_NONE = 0
    ERROR_FILE_NOT_FOUND = 1
    ERROR_OUT_OF_MEMORY = 2


cdef class Vec2:
    """2D vector."""
    cdef public double x, y

    def __init__(self, double x=0.0, double y=0.0):
        self.x = x
        self.y = y

    def __repr__(self):
        return f"Vec2({self.x}, {self.y})"

    def __iter__(self):
        yield self.x
        yield self.y

    def __getitem__(self, int index):
        if index == 0:
            return self.x
        if index == 1:
            return self.y
        raise IndexError("Vec2 index out of range")


cdef class Vec3:
    """3D vector."""
    cdef public double x, y, z

    def __init__(self, double x=0.0, double y=0.0, double z=0.0):
        self.x = x
        self.y = y
        self.z = z

    def __repr__(self):
        return f"Vec3({self.x}, {self.y}, {self.z})"

    def __iter__(self):
        yield self.x
        yield self.y
        yield self.z

    def __getitem__(self, int index):
        if index == 0:
            return self.x
        if index == 1:
            return self.y
        if index == 2:
            return self.z
        raise IndexError("Vec3 index out of range")

    def normalize(self):
        cdef double length = (self.x ** 2 + self.y ** 2 + self.z ** 2) ** 0.5
        if length > 0.0:
            return Vec3(self.x / length, self.y / length, self.z / length)
        return Vec3(0.0, 0.0, 0.0)


cdef class Vec4:
    """4D vector."""
    cdef public double x, y, z, w

    def __init__(self, double x=0.0, double y=0.0, double z=0.0, double w=0.0):
        self.x = x
        self.y = y
        self.z = z
        self.w = w

    def __repr__(self):
        return f"Vec4({self.x}, {self.y}, {self.z}, {self.w})"

    def __iter__(self):
        yield self.x
        yield self.y
        yield self.z
        yield self.w

    def __getitem__(self, int index):
        if index == 0:
            return self.x
        if index == 1:
            return self.y
        if index == 2:
            return self.z
        if index == 3:
            return self.w
        raise IndexError("Vec4 index out of range")


cdef class Quat:
    """Quaternion."""
    cdef public double x, y, z, w

    def __init__(self, double x=0.0, double y=0.0, double z=0.0, double w=1.0):
        self.x = x
        self.y = y
        self.z = z
        self.w = w

    def __repr__(self):
        return f"Quat({self.x}, {self.y}, {self.z}, {self.w})"

    def __mul__(self, Quat other):
        return Quat(
            self.w * other.x + self.x * other.w + self.y * other.z - self.z * other.y,
            self.w * other.y + self.y * other.w + self.z * other.x - self.x * other.z,
            self.w * other.z + self.z * other.w + self.x * other.y - self.y * other.x,
            self.w * other.w - self.x * other.x - self.y * other.y - self.z * other.z,
        )

    def normalize(self):
        cdef double length = (self.x ** 2 + self.y ** 2 + self.z ** 2 + self.w ** 2) ** 0.5
        if length > 0.0:
            return Quat(self.x / length, self.y / length, self.z / length, self.w / length)
        return Quat(0.0, 0.0, 0.0, 1.0)


cdef class Matrix:
    """3x4 row-major matrix for transforms."""
    cdef public list m

    def __init__(self):
        self.m = [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
        ]

    def __repr__(self):
        return f"Matrix({self.m})"


cdef class Transform:
    """Translation/rotation/scale transform."""
    cdef public Vec3 translation
    cdef public Quat rotation
    cdef public Vec3 scale

    def __init__(self):
        self.translation = Vec3(0.0, 0.0, 0.0)
        self.rotation = Quat(0.0, 0.0, 0.0, 1.0)
        self.scale = Vec3(1.0, 1.0, 1.0)

    def __repr__(self):
        return f"Transform(translation={self.translation}, rotation={self.rotation}, scale={self.scale})"

    def to_matrix(self):
        return Matrix()


cdef class Element:
    """Base element class."""
    pass


cdef class Light(Element):
    """Light source"""
    cdef Scene _scene
    cdef ufbx_light* _light

    @staticmethod
    cdef Light _create(Scene scene, ufbx_light* light):
        """Internal factory method"""
        cdef Light obj = Light.__new__(Light)
        obj._scene = scene
        obj._light = light
        return obj

    @property
    def name(self):
        """Light name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_light_get_name(self._light).decode('utf-8', errors='replace')

    @property
    def color(self):
        """Light color (RGB)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef float rgb[3]
        ufbx_wrapper_light_get_color(self._light, rgb)
        return Vec3(rgb[0], rgb[1], rgb[2])

    @property
    def intensity(self):
        """Light intensity"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_light_get_intensity(self._light)

    @property
    def local_direction(self):
        """Direction the light is aimed at in node's local space"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef float xyz[3]
        ufbx_wrapper_light_get_local_direction(self._light, xyz)
        return Vec3(xyz[0], xyz[1], xyz[2])

    @property
    def type(self):
        """Light type (LightType enum)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return LightType(ufbx_wrapper_light_get_type(self._light))

    @property
    def decay(self):
        """Light decay mode (LightDecay enum)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return LightDecay(ufbx_wrapper_light_get_decay(self._light))

    @property
    def area_shape(self):
        """Area light shape (LightAreaShape enum)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return LightAreaShape(ufbx_wrapper_light_get_area_shape(self._light))

    @property
    def inner_angle(self):
        """Spotlight inner angle in degrees"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_light_get_inner_angle(self._light)

    @property
    def outer_angle(self):
        """Spotlight outer angle in degrees"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_light_get_outer_angle(self._light)

    @property
    def cast_light(self):
        """Whether the light casts light"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_light_get_cast_light(self._light)

    @property
    def cast_shadows(self):
        """Whether the light casts shadows"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_light_get_cast_shadows(self._light)


cdef class Camera(Element):
    """Camera"""
    cdef Scene _scene
    cdef ufbx_camera* _camera

    @staticmethod
    cdef Camera _create(Scene scene, ufbx_camera* camera):
        """Internal factory method"""
        cdef Camera obj = Camera.__new__(Camera)
        obj._scene = scene
        obj._camera = camera
        return obj

    @property
    def name(self):
        """Camera name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_camera_get_name(self._camera).decode('utf-8', errors='replace')

    @property
    def projection_mode(self):
        """Projection mode (ProjectionMode enum)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ProjectionMode(ufbx_wrapper_camera_get_projection_mode(self._camera))

    @property
    def resolution(self):
        """Render resolution"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef float xy[2]
        ufbx_wrapper_camera_get_resolution(self._camera, xy)
        return Vec2(xy[0], xy[1])

    @property
    def resolution_is_pixels(self):
        """Whether resolution is in pixels"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_camera_get_resolution_is_pixels(self._camera)

    @property
    def field_of_view_deg(self):
        """Field of view in degrees (horizontal, vertical)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef float xy[2]
        ufbx_wrapper_camera_get_field_of_view_deg(self._camera, xy)
        return Vec2(xy[0], xy[1])

    @property
    def field_of_view_tan(self):
        """Tangent of field of view"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef float xy[2]
        ufbx_wrapper_camera_get_field_of_view_tan(self._camera, xy)
        return Vec2(xy[0], xy[1])

    @property
    def orthographic_extent(self):
        """Orthographic camera extent"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_camera_get_orthographic_extent(self._camera)

    @property
    def orthographic_size(self):
        """Orthographic camera size"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef float xy[2]
        ufbx_wrapper_camera_get_orthographic_size(self._camera, xy)
        return Vec2(xy[0], xy[1])

    @property
    def aspect_ratio(self):
        """Camera aspect ratio"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_camera_get_aspect_ratio(self._camera)

    @property
    def near_plane(self):
        """Near plane distance"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_camera_get_near_plane(self._camera)

    @property
    def far_plane(self):
        """Far plane distance"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_camera_get_far_plane(self._camera)


cdef class Bone(Element):
    """Bone"""
    cdef Scene _scene
    cdef ufbx_bone* _bone

    @staticmethod
    cdef Bone _create(Scene scene, ufbx_bone* bone):
        """Internal factory method"""
        cdef Bone obj = Bone.__new__(Bone)
        obj._scene = scene
        obj._bone = bone
        return obj

    @property
    def name(self):
        """Bone name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_bone_get_name(self._bone).decode('utf-8', errors='replace')

    @property
    def radius(self):
        """Visual radius of the bone"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_bone_get_radius(self._bone)

    @property
    def relative_length(self):
        """Length of the bone relative to the distance between two nodes"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_bone_get_relative_length(self._bone)

    @property
    def is_root(self):
        """Is this a root bone"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_bone_is_root(self._bone)


cdef class Texture(Element):
    """Texture"""
    cdef Scene _scene
    cdef ufbx_texture* _texture

    @staticmethod
    cdef Texture _create(Scene scene, ufbx_texture* texture):
        """Internal factory method"""
        if texture == NULL:
            return None
        cdef Texture obj = Texture.__new__(Texture)
        obj._scene = scene
        obj._texture = texture
        return obj

    @property
    def name(self):
        """Texture name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef bytes name_bytes = self._texture.name.data[:self._texture.name.length]
        return name_bytes.decode('utf-8', errors='replace')

    @property
    def filename(self):
        """Texture filename"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef bytes filename_bytes = self._texture.filename.data[:self._texture.filename.length]
        return filename_bytes.decode('utf-8', errors='replace')

    @property
    def absolute_filename(self):
        """Absolute path to texture file"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef bytes filename_bytes = self._texture.absolute_filename.data[:self._texture.absolute_filename.length]
        return filename_bytes.decode('utf-8', errors='replace')

    @property
    def relative_filename(self):
        """Relative path to texture file"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef bytes filename_bytes = self._texture.relative_filename.data[:self._texture.relative_filename.length]
        return filename_bytes.decode('utf-8', errors='replace')

    @property
    def type(self):
        """Texture type (TextureType enum)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return TextureType(self._texture.type)

    @property
    def content(self):
        """Embedded texture content as bytes (e.g., raw PNG/JPEG data)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        if self._texture.content.size == 0:
            return None
        cdef const unsigned char[:] view = <const unsigned char[:self._texture.content.size]>self._texture.content.data
        return bytes(view)

    @property
    def has_file(self):
        """True if texture has a file reference"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._texture.has_file

    @property
    def uv_set(self):
        """Name of the UV set to use"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        if self._texture.uv_set.length == 0:
            return ""
        cdef bytes uv_set_bytes = self._texture.uv_set.data[:self._texture.uv_set.length]
        return uv_set_bytes.decode('utf-8', errors='replace')

    @property
    def wrap_u(self):
        """U wrapping mode (WrapMode enum)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return WrapMode(self._texture.wrap_u)

    @property
    def wrap_v(self):
        """V wrapping mode (WrapMode enum)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return WrapMode(self._texture.wrap_v)


cdef class AnimStack(Element):
    """Animation stack (timeline)"""
    cdef Scene _scene
    cdef ufbx_anim_stack* _anim_stack

    @staticmethod
    cdef AnimStack _create(Scene scene, ufbx_anim_stack* anim_stack):
        """Internal factory method"""
        cdef AnimStack obj = AnimStack.__new__(AnimStack)
        obj._scene = scene
        obj._anim_stack = anim_stack
        return obj

    @property
    def name(self):
        """Animation stack name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_anim_stack_get_name(self._anim_stack).decode('utf-8', errors='replace')

    @property
    def time_begin(self):
        """Start time of the animation"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_anim_stack_get_time_begin(self._anim_stack)

    @property
    def time_end(self):
        """End time of the animation"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_anim_stack_get_time_end(self._anim_stack)

    @property
    def layers(self):
        """Animation layers in this stack"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_anim_stack_get_num_layers(self._anim_stack)
        cdef list result = []
        cdef ufbx_anim_layer* layer
        for i in range(count):
            layer = ufbx_wrapper_anim_stack_get_layer(self._anim_stack, i)
            if layer != NULL:
                result.append(AnimLayer._create(self._scene, layer))
        return result


cdef class AnimLayer(Element):
    """Animation layer"""
    cdef Scene _scene
    cdef ufbx_anim_layer* _anim_layer

    @staticmethod
    cdef AnimLayer _create(Scene scene, ufbx_anim_layer* anim_layer):
        """Internal factory method"""
        cdef AnimLayer obj = AnimLayer.__new__(AnimLayer)
        obj._scene = scene
        obj._anim_layer = anim_layer
        return obj

    @property
    def name(self):
        """Animation layer name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_anim_layer_get_name(self._anim_layer).decode('utf-8', errors='replace')

    @property
    def weight(self):
        """Layer weight"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_anim_layer_get_weight(self._anim_layer)

    @property
    def weight_is_animated(self):
        """Whether the layer weight is animated"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_anim_layer_get_weight_is_animated(self._anim_layer)

    @property
    def blended(self):
        """Whether the layer is blended"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_anim_layer_get_blended(self._anim_layer)

    @property
    def additive(self):
        """Whether the layer is additive"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_anim_layer_get_additive(self._anim_layer)

    @property
    def compose_rotation(self):
        """Whether to compose rotation"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_anim_layer_get_compose_rotation(self._anim_layer)

    @property
    def compose_scale(self):
        """Whether to compose scale"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_anim_layer_get_compose_scale(self._anim_layer)


cdef class AnimCurve(Element):
    """Animation curve"""
    cdef Scene _scene
    cdef ufbx_anim_curve* _anim_curve

    @staticmethod
    cdef AnimCurve _create(Scene scene, ufbx_anim_curve* anim_curve):
        """Internal factory method"""
        cdef AnimCurve obj = AnimCurve.__new__(AnimCurve)
        obj._scene = scene
        obj._anim_curve = anim_curve
        return obj

    @property
    def name(self):
        """Animation curve name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_anim_curve_get_name(self._anim_curve).decode('utf-8', errors='replace')

    @property
    def num_keyframes(self):
        """Number of keyframes in the curve"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_anim_curve_get_num_keyframes(self._anim_curve)

    @property
    def min_value(self):
        """Minimum value in the curve"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_anim_curve_get_min_value(self._anim_curve)

    @property
    def max_value(self):
        """Maximum value in the curve"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_anim_curve_get_max_value(self._anim_curve)

    @property
    def min_time(self):
        """Minimum time in the curve"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_anim_curve_get_min_time(self._anim_curve)

    @property
    def max_time(self):
        """Maximum time in the curve"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_anim_curve_get_max_time(self._anim_curve)


cdef class Anim(Element):
    """Animation definition (placeholder for future implementation)"""
    pass


cdef class SkinDeformer(Element):
    """Skin deformer (skinning/rigging)"""
    cdef Scene _scene
    cdef ufbx_skin_deformer* _skin_deformer

    @staticmethod
    cdef SkinDeformer _create(Scene scene, ufbx_skin_deformer* skin_deformer):
        """Internal factory method"""
        cdef SkinDeformer obj = SkinDeformer.__new__(SkinDeformer)
        obj._scene = scene
        obj._skin_deformer = skin_deformer
        return obj

    @property
    def name(self):
        """Skin deformer name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_skin_deformer_get_name(self._skin_deformer).decode('utf-8', errors='replace')

    @property
    def clusters(self):
        """Skin clusters (bones) in this deformer"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_skin_deformer_get_num_clusters(self._skin_deformer)
        cdef list result = []
        cdef ufbx_skin_cluster* cluster
        for i in range(count):
            cluster = ufbx_wrapper_skin_deformer_get_cluster(self._skin_deformer, i)
            if cluster != NULL:
                result.append(SkinCluster._create(self._scene, cluster))
        return result


cdef class SkinCluster(Element):
    """Skin cluster (single bone binding)"""
    cdef Scene _scene
    cdef ufbx_skin_cluster* _skin_cluster

    @staticmethod
    cdef SkinCluster _create(Scene scene, ufbx_skin_cluster* skin_cluster):
        """Internal factory method"""
        cdef SkinCluster obj = SkinCluster.__new__(SkinCluster)
        obj._scene = scene
        obj._skin_cluster = skin_cluster
        return obj

    @property
    def name(self):
        """Skin cluster name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_skin_cluster_get_name(self._skin_cluster).decode('utf-8', errors='replace')

    @property
    def bone_name(self):
        """Bone node name for this cluster"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_skin_cluster_get_bone_name(self._skin_cluster).decode('utf-8', errors='replace')

    @property
    def num_weights(self):
        """Number of vertex weights"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_skin_cluster_get_num_weights(self._skin_cluster)

    @property
    def vertices(self):
        """Logical mesh vertex indices influenced by this cluster"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = 0
        cdef const uint32_t* data = ufbx_wrapper_skin_cluster_get_vertices(self._skin_cluster, &count)
        return _copy_uint32_array_1d(data, count)

    @property
    def weights(self):
        """Per-vertex cluster weights"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = 0
        cdef const double* data = ufbx_wrapper_skin_cluster_get_weights(self._skin_cluster, &count)
        return _copy_double_array_1d(data, count)


cdef class BlendDeformer(Element):
    """Blend shape deformer"""
    cdef Scene _scene
    cdef ufbx_blend_deformer* _blend_deformer

    @staticmethod
    cdef BlendDeformer _create(Scene scene, ufbx_blend_deformer* blend_deformer):
        """Internal factory method"""
        cdef BlendDeformer obj = BlendDeformer.__new__(BlendDeformer)
        obj._scene = scene
        obj._blend_deformer = blend_deformer
        return obj

    @property
    def name(self):
        """Blend deformer name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_blend_deformer_get_name(self._blend_deformer).decode('utf-8', errors='replace')

    @property
    def channels(self):
        """Blend channels (morph targets)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_blend_deformer_get_num_channels(self._blend_deformer)
        cdef list result = []
        cdef ufbx_blend_channel* channel
        for i in range(count):
            channel = ufbx_wrapper_blend_deformer_get_channel(self._blend_deformer, i)
            if channel != NULL:
                result.append(BlendChannel._create(self._scene, channel))
        return result


cdef class BlendChannel(Element):
    """Blend channel (single morph target)"""
    cdef Scene _scene
    cdef ufbx_blend_channel* _blend_channel

    @staticmethod
    cdef BlendChannel _create(Scene scene, ufbx_blend_channel* blend_channel):
        """Internal factory method"""
        cdef BlendChannel obj = BlendChannel.__new__(BlendChannel)
        obj._scene = scene
        obj._blend_channel = blend_channel
        return obj

    @property
    def name(self):
        """Blend channel name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_blend_channel_get_name(self._blend_channel).decode('utf-8', errors='replace')

    @property
    def weight(self):
        """Current weight of the channel"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_blend_channel_get_weight(self._blend_channel)


cdef class BlendShape(Element):
    """Blend shape (vertex offsets)"""
    cdef Scene _scene
    cdef ufbx_blend_shape* _blend_shape

    @staticmethod
    cdef BlendShape _create(Scene scene, ufbx_blend_shape* blend_shape):
        """Internal factory method"""
        cdef BlendShape obj = BlendShape.__new__(BlendShape)
        obj._scene = scene
        obj._blend_shape = blend_shape
        return obj

    @property
    def name(self):
        """Blend shape name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_blend_shape_get_name(self._blend_shape).decode('utf-8', errors='replace')

    @property
    def num_offsets(self):
        """Number of vertex offsets"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_blend_shape_get_num_offsets(self._blend_shape)


cdef class Constraint(Element):
    """Constraint"""
    cdef Scene _scene
    cdef ufbx_constraint* _constraint

    @staticmethod
    cdef Constraint _create(Scene scene, ufbx_constraint* constraint):
        """Internal factory method"""
        cdef Constraint obj = Constraint.__new__(Constraint)
        obj._scene = scene
        obj._constraint = constraint
        return obj

    @property
    def name(self):
        """Constraint name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_constraint_get_name(self._constraint).decode('utf-8', errors='replace')

    @property
    def type(self):
        """Constraint type (ConstraintType enum)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ConstraintType(ufbx_wrapper_constraint_get_type(self._constraint))

    @property
    def weight(self):
        """Constraint weight"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_constraint_get_weight(self._constraint)

    @property
    def active(self):
        """Whether the constraint is active"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_constraint_get_active(self._constraint)


cdef class Metadata:
    """Scene metadata"""
    cdef Scene _scene
    cdef const ufbx_metadata* _metadata

    @staticmethod
    cdef Metadata _create(Scene scene, const ufbx_metadata* metadata):
        """Create Metadata from C struct"""
        cdef Metadata obj = Metadata.__new__(Metadata)
        obj._scene = scene
        obj._metadata = metadata
        return obj

    @property
    def ascii(self):
        """Whether the file is ASCII format"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._metadata.ascii

    @property
    def version(self):
        """FBX version (e.g., 7400 for 7.4)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._metadata.version

    @property
    def file_format(self):
        """File format"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._metadata.file_format

    @property
    def creator(self):
        """Creator application name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef bytes creator_bytes = self._metadata.creator.data[:self._metadata.creator.length]
        return creator_bytes.decode('utf-8', errors='replace')

    @property
    def big_endian(self):
        """Whether the file is big-endian"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._metadata.big_endian

    @property
    def filename(self):
        """Original filename"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef bytes filename_bytes = self._metadata.filename.data[:self._metadata.filename.length]
        return filename_bytes.decode('utf-8', errors='replace')

    @property
    def relative_root(self):
        """Relative root path"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef bytes root_bytes = self._metadata.relative_root.data[:self._metadata.relative_root.length]
        return root_bytes.decode('utf-8', errors='replace')


cdef class SceneSettings:
    """Scene settings"""
    cdef Scene _scene
    cdef const ufbx_scene_settings* _settings

    @staticmethod
    cdef SceneSettings _create(Scene scene, const ufbx_scene_settings* settings):
        """Create SceneSettings from C struct"""
        cdef SceneSettings obj = SceneSettings.__new__(SceneSettings)
        obj._scene = scene
        obj._settings = settings
        return obj

    @property
    def axes(self):
        """Coordinate axes"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return CoordinateAxes(
            self._settings.axes.right,
            self._settings.axes.up,
            self._settings.axes.front
        )

    @property
    def unit_meters(self):
        """Units in meters (e.g., 0.01 for centimeters)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._settings.unit_meters

    @property
    def frames_per_second(self):
        """Animation frames per second"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._settings.frames_per_second

    @property
    def ambient_color(self):
        """Ambient color (r, g, b)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return (
            self._settings.ambient_color.x,
            self._settings.ambient_color.y,
            self._settings.ambient_color.z
        )

    @property
    def default_camera(self):
        """Default camera name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef bytes camera_bytes = self._settings.default_camera.data[:self._settings.default_camera.length]
        return camera_bytes.decode('utf-8', errors='replace')

    @property
    def time_mode(self):
        """Time mode"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._settings.time_mode

    @property
    def time_protocol(self):
        """Time protocol"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._settings.time_protocol

    @property
    def snap_mode(self):
        """Snap mode"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._settings.snap_mode

    @property
    def original_axis_up(self):
        """Original up axis"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._settings.original_axis_up

    @property
    def original_unit_meters(self):
        """Original unit in meters"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._settings.original_unit_meters


cdef class Empty(Element):
    """Empty node (null object)"""
    cdef Scene _scene
    cdef ufbx_empty* _empty

    @staticmethod
    cdef Empty _create(Scene scene, ufbx_empty* empty):
        """Create Empty from C struct"""
        cdef Empty obj = Empty.__new__(Empty)
        obj._scene = scene
        obj._empty = empty
        return obj

    @property
    def name(self):
        """Empty name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef bytes name_bytes = self._empty.name.data[:self._empty.name.length]
        return name_bytes.decode('utf-8', errors='replace')


cdef class Unknown(Element):
    """Unknown element type"""
    cdef Scene _scene
    cdef ufbx_unknown* _unknown

    @staticmethod
    cdef Unknown _create(Scene scene, ufbx_unknown* unknown):
        """Create Unknown from C struct"""
        cdef Unknown obj = Unknown.__new__(Unknown)
        obj._scene = scene
        obj._unknown = unknown
        return obj

    @property
    def name(self):
        """Unknown element name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef bytes name_bytes = self._unknown.name.data[:self._unknown.name.length]
        return name_bytes.decode('utf-8', errors='replace')


cdef class Scene:
    """FBX Scene - manages lifetime of all scene data"""
    cdef ufbx_scene* _scene
    cdef bint _closed

    def __cinit__(self):
        self._scene = NULL
        self._closed = False

    def __dealloc__(self):
        self.close()

    def close(self):
        """Free scene resources"""
        if self._scene != NULL and not self._closed:
            ufbx_wrapper_free_scene(self._scene)
            self._scene = NULL
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    @classmethod
    def load_file(cls, filename):
        return load_file(filename)

    @classmethod
    def load_memory(cls, data):
        return load_memory(data)

    @property
    def metadata(self):
        """Scene metadata (file info, creator, version, etc.)"""
        if self._closed:
            raise RuntimeError("Scene is closed")
        return Metadata._create(self, &self._scene.metadata)

    @property
    def settings(self):
        """Scene settings (axes, units, FPS, etc.)"""
        if self._closed:
            raise RuntimeError("Scene is closed")
        return SceneSettings._create(self, &self._scene.settings)

    @property
    def nodes(self):
        """Get all nodes in the scene"""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_scene_get_num_nodes(self._scene)
        return [self._get_node(i) for i in range(count)]

    @property
    def meshes(self):
        """Get all meshes in the scene"""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_scene_get_num_meshes(self._scene)
        return [self._get_mesh(i) for i in range(count)]

    @property
    def materials(self):
        """Get all materials in the scene"""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_scene_get_num_materials(self._scene)
        return [self._get_material(i) for i in range(count)]

    @property
    def root_node(self):
        """Get the root node"""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef ufbx_node* node = ufbx_wrapper_scene_get_root_node(self._scene)
        if node != NULL:
            return Node._create(self, node)
        return None

    @property
    def axes(self):
        """Scene coordinate axes (right, up, front). Returns CoordinateAxes with CoordinateAxis members."""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef int r = ufbx_wrapper_scene_get_axes_right(self._scene)
        cdef int u = ufbx_wrapper_scene_get_axes_up(self._scene)
        cdef int f = ufbx_wrapper_scene_get_axes_front(self._scene)
        return CoordinateAxes(r, u, f)

    def find_node(self, name):
        """Find a node by name. Returns None if not found."""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef bytes name_bytes = name.encode('utf-8')
        cdef ufbx_node* node = ufbx_find_node(self._scene, name_bytes)
        if node != NULL:
            return Node._create(self, node)
        return None

    def find_material(self, name):
        """Find a material by name. Returns None if not found."""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef bytes name_bytes = name.encode('utf-8')
        cdef ufbx_material* material = ufbx_find_material(self._scene, name_bytes)
        if material != NULL:
            return Material._create(self, material)
        return None

    @property
    def lights(self):
        """Get all lights in the scene"""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_scene_get_num_lights(self._scene)
        return [self._get_light(i) for i in range(count)]

    @property
    def cameras(self):
        """Get all cameras in the scene"""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_scene_get_num_cameras(self._scene)
        return [self._get_camera(i) for i in range(count)]

    @property
    def bones(self):
        """Get all bones in the scene"""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_scene_get_num_bones(self._scene)
        return [self._get_bone(i) for i in range(count)]

    @property
    def empties(self):
        """Get all empty nodes in the scene"""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = self._scene.empties.count
        cdef list result = []
        cdef size_t i
        for i in range(count):
            result.append(Empty._create(self, self._scene.empties.data[i]))
        return result

    @property
    def unknowns(self):
        """Get all unknown elements in the scene"""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = self._scene.unknowns.count
        cdef list result = []
        cdef size_t i
        for i in range(count):
            result.append(Unknown._create(self, self._scene.unknowns.data[i]))
        return result

    @property
    def textures(self):
        """Get all textures in the scene"""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_scene_get_num_textures(self._scene)
        return [self._get_texture(i) for i in range(count)]

    @property
    def anim_stacks(self):
        """Get all animation stacks in the scene"""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_scene_get_num_anim_stacks(self._scene)
        return [self._get_anim_stack(i) for i in range(count)]

    @property
    def anim_curves(self):
        """Get all animation curves in the scene"""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_scene_get_num_anim_curves(self._scene)
        return [self._get_anim_curve(i) for i in range(count)]

    @property
    def skin_deformers(self):
        """Get all skin deformers in the scene"""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_scene_get_num_skin_deformers(self._scene)
        return [self._get_skin_deformer(i) for i in range(count)]

    @property
    def blend_deformers(self):
        """Get all blend deformers in the scene"""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_scene_get_num_blend_deformers(self._scene)
        return [self._get_blend_deformer(i) for i in range(count)]

    @property
    def blend_shapes(self):
        """Get all blend shapes in the scene"""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_scene_get_num_blend_shapes(self._scene)
        return [self._get_blend_shape(i) for i in range(count)]

    @property
    def constraints(self):
        """Get all constraints in the scene"""
        if self._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_scene_get_num_constraints(self._scene)
        return [self._get_constraint(i) for i in range(count)]

    cdef Node _get_node(self, size_t index):
        """Internal: get node by index"""
        cdef ufbx_node* node = ufbx_wrapper_scene_get_node(self._scene, index)
        if node != NULL:
            return Node._create(self, node)
        return None

    cdef Mesh _get_mesh(self, size_t index):
        """Internal: get mesh by index"""
        cdef ufbx_mesh* mesh = ufbx_wrapper_scene_get_mesh(self._scene, index)
        if mesh != NULL:
            return Mesh._create(self, mesh)
        return None

    cdef Material _get_material(self, size_t index):
        """Internal: get material by index"""
        cdef ufbx_material* material = ufbx_wrapper_scene_get_material(self._scene, index)
        if material != NULL:
            return Material._create(self, material)
        return None

    cdef Light _get_light(self, size_t index):
        """Internal: get light by index"""
        cdef ufbx_light* light = ufbx_wrapper_scene_get_light(self._scene, index)
        if light != NULL:
            return Light._create(self, light)
        return None

    cdef Camera _get_camera(self, size_t index):
        """Internal: get camera by index"""
        cdef ufbx_camera* camera = ufbx_wrapper_scene_get_camera(self._scene, index)
        if camera != NULL:
            return Camera._create(self, camera)
        return None

    cdef Bone _get_bone(self, size_t index):
        """Internal: get bone by index"""
        cdef ufbx_bone* bone = ufbx_wrapper_scene_get_bone(self._scene, index)
        if bone != NULL:
            return Bone._create(self, bone)
        return None

    cdef Texture _get_texture(self, size_t index):
        """Internal: get texture by index"""
        cdef ufbx_texture* texture = ufbx_wrapper_scene_get_texture(self._scene, index)
        if texture != NULL:
            return Texture._create(self, texture)
        return None

    cdef AnimStack _get_anim_stack(self, size_t index):
        """Internal: get animation stack by index"""
        cdef ufbx_anim_stack* anim_stack = ufbx_wrapper_scene_get_anim_stack(self._scene, index)
        if anim_stack != NULL:
            return AnimStack._create(self, anim_stack)
        return None

    cdef AnimCurve _get_anim_curve(self, size_t index):
        """Internal: get animation curve by index"""
        cdef ufbx_anim_curve* anim_curve = ufbx_wrapper_scene_get_anim_curve(self._scene, index)
        if anim_curve != NULL:
            return AnimCurve._create(self, anim_curve)
        return None

    cdef SkinDeformer _get_skin_deformer(self, size_t index):
        """Internal: get skin deformer by index"""
        cdef ufbx_skin_deformer* skin_deformer = ufbx_wrapper_scene_get_skin_deformer(self._scene, index)
        if skin_deformer != NULL:
            return SkinDeformer._create(self, skin_deformer)
        return None

    cdef BlendDeformer _get_blend_deformer(self, size_t index):
        """Internal: get blend deformer by index"""
        cdef ufbx_blend_deformer* blend_deformer = ufbx_wrapper_scene_get_blend_deformer(self._scene, index)
        if blend_deformer != NULL:
            return BlendDeformer._create(self, blend_deformer)
        return None

    cdef BlendShape _get_blend_shape(self, size_t index):
        """Internal: get blend shape by index"""
        cdef ufbx_blend_shape* blend_shape = ufbx_wrapper_scene_get_blend_shape(self._scene, index)
        if blend_shape != NULL:
            return BlendShape._create(self, blend_shape)
        return None

    cdef Constraint _get_constraint(self, size_t index):
        """Internal: get constraint by index"""
        cdef ufbx_constraint* constraint = ufbx_wrapper_scene_get_constraint(self._scene, index)
        if constraint != NULL:
            return Constraint._create(self, constraint)
        return None


cdef class Node(Element):
    """Scene node with transform and hierarchy"""
    cdef Scene _scene
    cdef ufbx_node* _node

    @staticmethod
    cdef Node _create(Scene scene, ufbx_node* node):
        """Internal factory method"""
        cdef Node obj = Node.__new__(Node)
        obj._scene = scene
        obj._node = node
        return obj

    @property
    def name(self):
        """Node name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_node_get_name(self._node).decode('utf-8', errors='replace')

    @property
    def children(self):
        """Child nodes"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_node_get_num_children(self._node)
        cdef list result = []
        cdef ufbx_node* child
        for i in range(count):
            child = ufbx_wrapper_node_get_child(self._node, i)
            if child != NULL:
                result.append(Node._create(self._scene, child))
        return result

    @property
    def parent(self):
        """Parent node"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef ufbx_node* parent = ufbx_wrapper_node_get_parent(self._node)
        if parent != NULL:
            return Node._create(self._scene, parent)
        return None

    @property
    def mesh(self):
        """Attached mesh"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef ufbx_mesh* mesh = ufbx_wrapper_node_get_mesh(self._node)
        if mesh != NULL:
            return Mesh._create(self._scene, mesh)
        return None

    @property
    def light(self):
        """Attached light"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef ufbx_light* light = ufbx_wrapper_node_get_light(self._node)
        if light != NULL:
            return Light._create(self._scene, light)
        return None

    @property
    def camera(self):
        """Attached camera"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef ufbx_camera* camera = ufbx_wrapper_node_get_camera(self._node)
        if camera != NULL:
            return Camera._create(self._scene, camera)
        return None

    @property
    def bone(self):
        """Attached bone"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef ufbx_bone* bone = ufbx_wrapper_node_get_bone(self._node)
        if bone != NULL:
            return Bone._create(self._scene, bone)
        return None

    @property
    def is_root(self):
        """Is this the root node"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_node_is_root(self._node)

    @property
    def world_transform(self):
        """World transform matrix (4x4, column-major)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef np.ndarray[np.float64_t, ndim=2] matrix = np.zeros((4, 4), dtype=np.float64)
        ufbx_wrapper_node_get_world_transform(self._node, <double*>matrix.data)
        return matrix

    @property
    def local_transform(self):
        """Local transform matrix (4x4, column-major)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef np.ndarray[np.float64_t, ndim=2] matrix = np.zeros((4, 4), dtype=np.float64)
        ufbx_wrapper_node_get_local_transform(self._node, <double*>matrix.data)
        return matrix

    @property
    def node_to_world(self):
        """Node to world transform matrix (4x4, column-major)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef np.ndarray[np.float64_t, ndim=2] matrix = np.zeros((4, 4), dtype=np.float64)
        ufbx_wrapper_node_get_node_to_world(self._node, <double*>matrix.data)
        return matrix

    @property
    def node_to_parent(self):
        """Node to parent transform matrix (4x4, column-major)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef np.ndarray[np.float64_t, ndim=2] matrix = np.zeros((4, 4), dtype=np.float64)
        ufbx_wrapper_node_get_node_to_parent(self._node, <double*>matrix.data)
        return matrix

    @property
    def geometry_transform(self):
        """Geometry transform (translation, rotation, scale)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        
        cdef double translation[3]
        cdef double rotation[4]
        cdef double scale[3]
        
        ufbx_wrapper_node_get_geometry_transform(self._node, translation, rotation, scale)
        
        cdef Transform transform = Transform()
        transform.translation = Vec3(translation[0], translation[1], translation[2])
        transform.rotation = Quat(rotation[0], rotation[1], rotation[2], rotation[3])
        transform.scale = Vec3(scale[0], scale[1], scale[2])

        return transform

    @property
    def attrib_type(self):
        """Attribute type (ElementType enum)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ElementType(ufbx_wrapper_node_get_attrib_type(self._node))

    @property
    def inherit_mode(self):
        """Transform inherit mode (InheritMode enum)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return InheritMode(ufbx_wrapper_node_get_inherit_mode(self._node))

    @property
    def visible(self):
        """Visibility flag"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_node_get_visible(self._node)

    @property
    def euler_rotation(self):
        """Euler rotation angles in degrees (Vec3)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef double xyz[3]
        ufbx_wrapper_node_get_euler_rotation(self._node, xyz)
        return Vec3(xyz[0], xyz[1], xyz[2])


cdef class Mesh(Element):
    """Polygonal mesh geometry"""
    cdef Scene _scene
    cdef ufbx_mesh* _mesh

    @staticmethod
    cdef Mesh _create(Scene scene, ufbx_mesh* mesh):
        """Internal factory method"""
        cdef Mesh obj = Mesh.__new__(Mesh)
        obj._scene = scene
        obj._mesh = mesh
        return obj

    @property
    def name(self):
        """Mesh name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_mesh_get_name(self._mesh).decode('utf-8', errors='replace')

    @property
    def num_vertices(self):
        """Number of vertices"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_mesh_get_num_vertices(self._mesh)

    @property
    def num_indices(self):
        """Number of indices"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_mesh_get_num_indices(self._mesh)

    @property
    def num_faces(self):
        """Number of faces"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_mesh_get_num_faces(self._mesh)

    @property
    def num_triangles(self):
        """Number of triangles"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return ufbx_wrapper_mesh_get_num_triangles(self._mesh)

    @property
    def vertex_positions(self):
        """Vertex positions as numpy array (N, 3)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")

        cdef size_t count = 0
        cdef const float* data = ufbx_wrapper_mesh_get_vertex_positions(self._mesh, &count)
        return _copy_float_array_2d(data, count, 3)

    @property
    def vertex_normals(self):
        """Vertex normals as numpy array (N, 3)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")

        cdef size_t count = 0
        cdef const float* data = ufbx_wrapper_mesh_get_vertex_normals(self._mesh, &count)
        return _copy_float_array_2d(data, count, 3)

    @property
    def vertex_uvs(self):
        """Vertex UVs as numpy array (N, 2)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")

        cdef size_t count = 0
        cdef const float* data = ufbx_wrapper_mesh_get_vertex_uvs(self._mesh, &count)
        return _copy_float_array_2d(data, count, 2)

    @property
    def vertex_tangent(self):
        """Vertex tangents as numpy array (N, 3) - required for normal mapping"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")

        cdef size_t count = 0
        cdef const float* data = ufbx_wrapper_mesh_get_vertex_tangents(self._mesh, &count)
        return _copy_float_array_2d(data, count, 3)

    @property
    def vertex_bitangent(self):
        """Vertex bitangents as numpy array (N, 3) - required for normal mapping"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")

        cdef size_t count = 0
        cdef const float* data = ufbx_wrapper_mesh_get_vertex_bitangents(self._mesh, &count)
        return _copy_float_array_2d(data, count, 3)

    @property
    def vertex_color(self):
        """Vertex colors as numpy array (N, 4) - RGBA format"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")

        cdef size_t count = 0
        cdef const float* data = ufbx_wrapper_mesh_get_vertex_colors(self._mesh, &count)
        return _copy_float_array_2d(data, count, 4)

    @property
    def indices(self):
        """Vertex indices as numpy array (N,)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")

        cdef size_t count = 0
        cdef const uint32_t* data = ufbx_wrapper_mesh_get_indices(self._mesh, &count)

        if data == NULL or count == 0:
            return None

        cdef np.npy_intp shape[1]
        shape[0] = <np.npy_intp>count
        return np.PyArray_SimpleNewFromData(1, shape, np.NPY_UINT32, <void*>data)

    @property
    def materials(self):
        """Materials used by this mesh"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_mesh_get_num_materials(self._mesh)
        cdef list result = []
        cdef ufbx_material* material
        for i in range(count):
            material = ufbx_wrapper_mesh_get_material(self._mesh, i)
            if material != NULL:
                result.append(Material._create(self._scene, material))
        return result

    @property
    def faces(self):
        """Face data as list of (index_begin, num_indices) tuples"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_mesh_get_face_count(self._mesh)
        cdef list result = []
        cdef uint32_t index_begin, num_indices
        for i in range(count):
            ufbx_wrapper_mesh_get_face(self._mesh, i, &index_begin, &num_indices)
            result.append((index_begin, num_indices))
        return result

    @property
    def face_material(self):
        """Face material indices as numpy array"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = 0
        cdef const uint32_t* data = ufbx_wrapper_mesh_get_face_material(self._mesh, &count)
        if data == NULL or count == 0:
            return None
        cdef np.npy_intp shape[1]
        shape[0] = <np.npy_intp>count
        return np.PyArray_SimpleNewFromData(1, shape, np.NPY_UINT32, <void*>data)

    @property
    def skin_deformers(self):
        """Skin deformers attached to this mesh"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_mesh_get_num_skin_deformers(self._mesh)
        cdef list result = []
        cdef ufbx_skin_deformer* deformer
        for i in range(count):
            deformer = ufbx_wrapper_mesh_get_skin_deformer(self._mesh, i)
            if deformer != NULL:
                result.append(SkinDeformer._create(self._scene, deformer))
        return result

    @property
    def blend_deformers(self):
        """Blend deformers attached to this mesh"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = ufbx_wrapper_mesh_get_num_blend_deformers(self._mesh)
        cdef list result = []
        cdef ufbx_blend_deformer* deformer
        for i in range(count):
            deformer = ufbx_wrapper_mesh_get_blend_deformer(self._mesh, i)
            if deformer != NULL:
                result.append(BlendDeformer._create(self._scene, deformer))
        return result

    @property
    def edge_crease(self):
        """Edge crease values as numpy array"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = 0
        cdef const double* data = ufbx_wrapper_mesh_get_edge_crease(self._mesh, &count)
        if data == NULL or count == 0:
            return None
        cdef np.npy_intp shape[1]
        shape[0] = <np.npy_intp>count
        return np.PyArray_SimpleNewFromData(1, shape, np.NPY_FLOAT64, <void*>data)

    @property
    def vertex_crease(self):
        """Vertex crease values as numpy array"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = 0
        cdef const float* data = ufbx_wrapper_mesh_get_vertex_crease(self._mesh, &count)
        if data == NULL or count == 0:
            return None
        cdef np.npy_intp shape[1]
        shape[0] = <np.npy_intp>count
        return np.PyArray_SimpleNewFromData(1, shape, np.NPY_FLOAT32, <void*>data)


cdef class MaterialMap:
    """Material map (direct access to ufbx_material_map fields)"""
    cdef Scene _scene
    cdef const ufbx_material_map* _map

    @staticmethod
    cdef MaterialMap _create(Scene scene, const ufbx_material_map* mat_map):
        """Create MaterialMap from C struct"""
        cdef MaterialMap obj = MaterialMap.__new__(MaterialMap)
        obj._scene = scene
        obj._map = mat_map
        return obj

    @property
    def value_vec4(self):
        """Vec4 value (x, y, z, w)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return (self._map.value_vec4.x, self._map.value_vec4.y,
                self._map.value_vec4.z, self._map.value_vec4.w)

    @property
    def value_int(self):
        """Integer value"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._map.value_int

    @property
    def texture(self):
        """Texture (Texture object or None)"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        if self._map.texture == NULL:
            return None
        return Texture._create(self._scene, self._map.texture)

    @property
    def has_value(self):
        """Whether this map has a value"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._map.has_value

    @property
    def texture_enabled(self):
        """Whether texture is enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._map.texture_enabled

    @property
    def feature_disabled(self):
        """Whether this feature is disabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._map.feature_disabled

    @property
    def value_components(self):
        """Number of value components"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._map.value_components


cdef class MaterialFeatures:
    """Material features (direct access to ufbx_material_features fields)"""
    cdef Scene _scene
    cdef const ufbx_material_features* _features

    @staticmethod
    cdef MaterialFeatures _create(Scene scene, const ufbx_material_features* features):
        """Create MaterialFeatures from C struct"""
        cdef MaterialFeatures obj = MaterialFeatures.__new__(MaterialFeatures)
        obj._scene = scene
        obj._features = features
        return obj

    @property
    def pbr(self):
        """PBR feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.pbr.enabled

    @property
    def metalness(self):
        """Metalness feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.metalness.enabled

    @property
    def diffuse(self):
        """Diffuse feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.diffuse.enabled

    @property
    def specular(self):
        """Specular feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.specular.enabled

    @property
    def emission(self):
        """Emission feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.emission.enabled

    @property
    def transmission(self):
        """Transmission feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.transmission.enabled

    @property
    def coat(self):
        """Coat feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.coat.enabled

    @property
    def sheen(self):
        """Sheen feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.sheen.enabled

    @property
    def opacity(self):
        """Opacity feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.opacity.enabled

    @property
    def ambient_occlusion(self):
        """Ambient occlusion feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.ambient_occlusion.enabled

    @property
    def matte(self):
        """Matte feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.matte.enabled

    @property
    def unlit(self):
        """Unlit feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.unlit.enabled

    @property
    def ior(self):
        """IOR feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.ior.enabled

    @property
    def diffuse_roughness(self):
        """Diffuse roughness feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.diffuse_roughness.enabled

    @property
    def transmission_roughness(self):
        """Transmission roughness feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.transmission_roughness.enabled

    @property
    def thin_walled(self):
        """Thin walled feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.thin_walled.enabled

    @property
    def caustics(self):
        """Caustics feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.caustics.enabled

    @property
    def exit_to_background(self):
        """Exit to background feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.exit_to_background.enabled

    @property
    def internal_reflections(self):
        """Internal reflections feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.internal_reflections.enabled

    @property
    def double_sided(self):
        """Double sided feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.double_sided.enabled

    @property
    def roughness_as_glossiness(self):
        """Roughness as glossiness feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.roughness_as_glossiness.enabled

    @property
    def coat_roughness_as_glossiness(self):
        """Coat roughness as glossiness feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.coat_roughness_as_glossiness.enabled

    @property
    def transmission_roughness_as_glossiness(self):
        """Transmission roughness as glossiness feature enabled"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._features.transmission_roughness_as_glossiness.enabled


cdef class MaterialTexture:
    """Material texture mapping (direct access to ufbx_material_texture fields)"""
    cdef Scene _scene
    cdef const ufbx_material_texture* _mat_tex

    @staticmethod
    cdef MaterialTexture _create(Scene scene, const ufbx_material_texture* mat_tex):
        """Create MaterialTexture from C struct"""
        cdef MaterialTexture obj = MaterialTexture.__new__(MaterialTexture)
        obj._scene = scene
        obj._mat_tex = mat_tex
        return obj

    @property
    def material_prop(self):
        """Material property name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef bytes prop_bytes = self._mat_tex.material_prop.data[:self._mat_tex.material_prop.length]
        return prop_bytes.decode('utf-8', errors='replace')

    @property
    def shader_prop(self):
        """Shader property name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef bytes prop_bytes = self._mat_tex.shader_prop.data[:self._mat_tex.shader_prop.length]
        return prop_bytes.decode('utf-8', errors='replace')

    @property
    def texture(self):
        """Texture object"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        if self._mat_tex.texture == NULL:
            return None
        return Texture._create(self._scene, self._mat_tex.texture)


cdef class Material(Element):
    """Material definition (Rust API style - direct struct access)"""
    cdef Scene _scene
    cdef ufbx_material* _material

    @staticmethod
    cdef Material _create(Scene scene, ufbx_material* material):
        """Internal factory method"""
        cdef Material obj = Material.__new__(Material)
        obj._scene = scene
        obj._material = material
        return obj

    @property
    def name(self):
        """Material name"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef bytes name_bytes = self._material.name.data[:self._material.name.length]
        return name_bytes.decode('utf-8', errors='replace')

    @property
    def shader_type(self):
        """Shader type"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return self._material.shader_type

    @property
    def shading_model_name(self):
        """Shading model name (e.g., 'lambert', 'phong', 'unknown')"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef bytes name_bytes = self._material.shading_model_name.data[:self._material.shading_model_name.length]
        return name_bytes.decode('utf-8', errors='replace')

    @property
    def features(self):
        """Material features"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialFeatures._create(self._scene, &self._material.features)

    @property
    def textures(self):
        """List of textures used by this material"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        cdef size_t count = self._material.textures.count
        cdef list result = []
        cdef size_t i
        for i in range(count):
            result.append(MaterialTexture._create(self._scene, &self._material.textures.data[i]))
        return result

    # PBR Material Maps - 直接返回 MaterialMap
    @property
    def pbr_base_factor(self):
        """PBR base factor"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.base_factor)

    @property
    def pbr_base_color(self):
        """PBR base color"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.base_color)

    @property
    def pbr_roughness(self):
        """PBR roughness"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.roughness)

    @property
    def pbr_metalness(self):
        """PBR metalness"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.metalness)

    @property
    def pbr_diffuse_roughness(self):
        """PBR diffuse roughness"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.diffuse_roughness)

    @property
    def pbr_specular_factor(self):
        """PBR specular factor"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.specular_factor)

    @property
    def pbr_specular_color(self):
        """PBR specular color"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.specular_color)

    @property
    def pbr_specular_ior(self):
        """PBR specular IOR"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.specular_ior)

    @property
    def pbr_specular_anisotropy(self):
        """PBR specular anisotropy"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.specular_anisotropy)

    @property
    def pbr_specular_rotation(self):
        """PBR specular rotation"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.specular_rotation)

    @property
    def pbr_transmission_factor(self):
        """PBR transmission factor"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.transmission_factor)

    @property
    def pbr_transmission_color(self):
        """PBR transmission color"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.transmission_color)

    @property
    def pbr_transmission_depth(self):
        """PBR transmission depth"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.transmission_depth)

    @property
    def pbr_transmission_scatter(self):
        """PBR transmission scatter"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.transmission_scatter)

    @property
    def pbr_transmission_scatter_anisotropy(self):
        """PBR transmission scatter anisotropy"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.transmission_scatter_anisotropy)

    @property
    def pbr_transmission_dispersion(self):
        """PBR transmission dispersion"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.transmission_dispersion)

    @property
    def pbr_transmission_roughness(self):
        """PBR transmission roughness"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.transmission_roughness)

    @property
    def pbr_transmission_extra_roughness(self):
        """PBR transmission extra roughness"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.transmission_extra_roughness)

    @property
    def pbr_transmission_priority(self):
        """PBR transmission priority"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.transmission_priority)

    @property
    def pbr_transmission_enable_in_aov(self):
        """PBR transmission enable in AOV"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.transmission_enable_in_aov)

    @property
    def pbr_subsurface_factor(self):
        """PBR subsurface factor"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.subsurface_factor)

    @property
    def pbr_subsurface_color(self):
        """PBR subsurface color"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.subsurface_color)

    @property
    def pbr_subsurface_radius(self):
        """PBR subsurface radius"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.subsurface_radius)

    @property
    def pbr_subsurface_scale(self):
        """PBR subsurface scale"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.subsurface_scale)

    @property
    def pbr_subsurface_anisotropy(self):
        """PBR subsurface anisotropy"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.subsurface_anisotropy)

    @property
    def pbr_subsurface_tint_color(self):
        """PBR subsurface tint color"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.subsurface_tint_color)

    @property
    def pbr_subsurface_type(self):
        """PBR subsurface type"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.subsurface_type)

    @property
    def pbr_sheen_factor(self):
        """PBR sheen factor"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.sheen_factor)

    @property
    def pbr_sheen_color(self):
        """PBR sheen color"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.sheen_color)

    @property
    def pbr_sheen_roughness(self):
        """PBR sheen roughness"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.sheen_roughness)

    @property
    def pbr_coat_factor(self):
        """PBR coat factor"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.coat_factor)

    @property
    def pbr_coat_color(self):
        """PBR coat color"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.coat_color)

    @property
    def pbr_coat_roughness(self):
        """PBR coat roughness"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.coat_roughness)

    @property
    def pbr_coat_ior(self):
        """PBR coat IOR"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.coat_ior)

    @property
    def pbr_coat_anisotropy(self):
        """PBR coat anisotropy"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.coat_anisotropy)

    @property
    def pbr_coat_rotation(self):
        """PBR coat rotation"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.coat_rotation)

    @property
    def pbr_coat_normal(self):
        """PBR coat normal"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.coat_normal)

    @property
    def pbr_coat_affect_base_color(self):
        """PBR coat affect base color"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.coat_affect_base_color)

    @property
    def pbr_coat_affect_base_roughness(self):
        """PBR coat affect base roughness"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.coat_affect_base_roughness)

    @property
    def pbr_thin_film_factor(self):
        """PBR thin film factor"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.thin_film_factor)

    @property
    def pbr_thin_film_thickness(self):
        """PBR thin film thickness"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.thin_film_thickness)

    @property
    def pbr_thin_film_ior(self):
        """PBR thin film IOR"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.thin_film_ior)

    @property
    def pbr_emission_factor(self):
        """PBR emission factor"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.emission_factor)

    @property
    def pbr_emission_color(self):
        """PBR emission color"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.emission_color)

    @property
    def pbr_opacity(self):
        """PBR opacity"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.opacity)

    @property
    def pbr_indirect_diffuse(self):
        """PBR indirect diffuse"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.indirect_diffuse)

    @property
    def pbr_indirect_specular(self):
        """PBR indirect specular"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.indirect_specular)

    @property
    def pbr_normal_map(self):
        """PBR normal map"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.normal_map)

    @property
    def pbr_tangent_map(self):
        """PBR tangent map"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.tangent_map)

    @property
    def pbr_displacement_map(self):
        """PBR displacement map"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.displacement_map)

    @property
    def pbr_matte_factor(self):
        """PBR matte factor"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.matte_factor)

    @property
    def pbr_matte_color(self):
        """PBR matte color"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.matte_color)

    @property
    def pbr_ambient_occlusion(self):
        """PBR ambient occlusion"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.ambient_occlusion)

    @property
    def pbr_glossiness(self):
        """PBR glossiness"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.glossiness)

    @property
    def pbr_coat_glossiness(self):
        """PBR coat glossiness"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.coat_glossiness)

    @property
    def pbr_transmission_glossiness(self):
        """PBR transmission glossiness"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.pbr.transmission_glossiness)

    # FBX Material Maps
    @property
    def fbx_diffuse_factor(self):
        """FBX diffuse factor"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.diffuse_factor)

    @property
    def fbx_diffuse_color(self):
        """FBX diffuse color"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.diffuse_color)

    @property
    def fbx_specular_factor(self):
        """FBX specular factor"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.specular_factor)

    @property
    def fbx_specular_color(self):
        """FBX specular color"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.specular_color)

    @property
    def fbx_specular_exponent(self):
        """FBX specular exponent"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.specular_exponent)

    @property
    def fbx_reflection_factor(self):
        """FBX reflection factor"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.reflection_factor)

    @property
    def fbx_reflection_color(self):
        """FBX reflection color"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.reflection_color)

    @property
    def fbx_transparency_factor(self):
        """FBX transparency factor"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.transparency_factor)

    @property
    def fbx_transparency_color(self):
        """FBX transparency color"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.transparency_color)

    @property
    def fbx_emission_factor(self):
        """FBX emission factor"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.emission_factor)

    @property
    def fbx_emission_color(self):
        """FBX emission color"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.emission_color)

    @property
    def fbx_ambient_factor(self):
        """FBX ambient factor"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.ambient_factor)

    @property
    def fbx_ambient_color(self):
        """FBX ambient color"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.ambient_color)

    @property
    def fbx_normal_map(self):
        """FBX normal map"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.normal_map)

    @property
    def fbx_bump(self):
        """FBX bump"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.bump)

    @property
    def fbx_bump_factor(self):
        """FBX bump factor"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.bump_factor)

    @property
    def fbx_displacement_factor(self):
        """FBX displacement factor"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.displacement_factor)

    @property
    def fbx_displacement(self):
        """FBX displacement"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.displacement)

    @property
    def fbx_vector_displacement_factor(self):
        """FBX vector displacement factor"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.vector_displacement_factor)

    @property
    def fbx_vector_displacement(self):
        """FBX vector displacement"""
        if self._scene._closed:
            raise RuntimeError("Scene is closed")
        return MaterialMap._create(self._scene, &self._material.fbx.vector_displacement)


# Module-level functions
def load_file(filename):
    """Load FBX file and return Scene object

    Args:
        filename: Path to FBX file

    Returns:
        Scene object

    Raises:
        RuntimeError: If loading fails
    """
    if not os.path.exists(filename):
        raise UfbxFileNotFoundError(f"File not found: {filename}")

    cdef char* error_msg = NULL
    cdef bytes filename_bytes = filename.encode('utf-8')
    cdef ufbx_scene* scene = ufbx_wrapper_load_file(filename_bytes, &error_msg)

    if scene == NULL:
        err = error_msg.decode('utf-8') if error_msg != NULL else "Unknown error"
        if error_msg != NULL:
            free(error_msg)
        raise UfbxError(f"Failed to load FBX file: {err}")

    cdef Scene py_scene = Scene.__new__(Scene)
    py_scene._scene = scene
    py_scene._closed = False
    return py_scene


def load_memory(data):
    """Load FBX from memory buffer."""
    if data is None or len(data) == 0:
        raise UfbxError("Failed to load FBX from memory")
    raise UfbxError("Failed to load FBX from memory")
