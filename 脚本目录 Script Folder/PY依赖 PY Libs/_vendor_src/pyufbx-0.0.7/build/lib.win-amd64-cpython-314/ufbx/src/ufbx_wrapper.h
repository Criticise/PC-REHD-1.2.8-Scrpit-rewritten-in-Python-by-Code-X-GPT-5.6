#ifndef UFBX_WRAPPER_H
#define UFBX_WRAPPER_H

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

// Opaque handles
typedef struct ufbx_scene ufbx_scene;
typedef struct ufbx_mesh ufbx_mesh;
typedef struct ufbx_node ufbx_node;
typedef struct ufbx_material ufbx_material;
typedef struct ufbx_texture ufbx_texture;
typedef struct ufbx_light ufbx_light;
typedef struct ufbx_camera ufbx_camera;
typedef struct ufbx_bone ufbx_bone;
typedef struct ufbx_anim_stack ufbx_anim_stack;
typedef struct ufbx_anim_layer ufbx_anim_layer;
typedef struct ufbx_anim_curve ufbx_anim_curve;
typedef struct ufbx_skin_deformer ufbx_skin_deformer;
typedef struct ufbx_skin_cluster ufbx_skin_cluster;
typedef struct ufbx_blend_deformer ufbx_blend_deformer;
typedef struct ufbx_blend_channel ufbx_blend_channel;
typedef struct ufbx_blend_shape ufbx_blend_shape;
typedef struct ufbx_constraint ufbx_constraint;

// Scene management
ufbx_scene* ufbx_wrapper_load_file(const char *filename, char **error_msg);
void ufbx_wrapper_free_scene(ufbx_scene *scene);

// Scene queries
size_t ufbx_wrapper_scene_get_num_nodes(const ufbx_scene *scene);
size_t ufbx_wrapper_scene_get_num_meshes(const ufbx_scene *scene);
size_t ufbx_wrapper_scene_get_num_materials(const ufbx_scene *scene);
ufbx_node* ufbx_wrapper_scene_get_root_node(const ufbx_scene *scene);

// Scene settings.axes (CoordinateAxis: 0-6)
int ufbx_wrapper_scene_get_axes_right(const ufbx_scene *scene);
int ufbx_wrapper_scene_get_axes_up(const ufbx_scene *scene);
int ufbx_wrapper_scene_get_axes_front(const ufbx_scene *scene);

// Node access
ufbx_node* ufbx_wrapper_scene_get_node(const ufbx_scene *scene, size_t index);
const char* ufbx_wrapper_node_get_name(const ufbx_node *node);
size_t ufbx_wrapper_node_get_num_children(const ufbx_node *node);
ufbx_node* ufbx_wrapper_node_get_child(const ufbx_node *node, size_t index);
ufbx_node* ufbx_wrapper_node_get_parent(const ufbx_node *node);
ufbx_mesh* ufbx_wrapper_node_get_mesh(const ufbx_node *node);
bool ufbx_wrapper_node_is_root(const ufbx_node *node);

// Node transform (4x4 matrix stored in column-major order)
void ufbx_wrapper_node_get_world_transform(const ufbx_node *node, double *matrix16);
void ufbx_wrapper_node_get_local_transform(const ufbx_node *node, double *matrix16);
void ufbx_wrapper_node_get_node_to_world(const ufbx_node *node, double *matrix16);
void ufbx_wrapper_node_get_node_to_parent(const ufbx_node *node, double *matrix16);
void ufbx_wrapper_node_get_geometry_transform(const ufbx_node *node, double *translation3, double *rotation4, double *scale3);

// Node additional properties
int ufbx_wrapper_node_get_attrib_type(const ufbx_node *node);
int ufbx_wrapper_node_get_inherit_mode(const ufbx_node *node);
bool ufbx_wrapper_node_get_visible(const ufbx_node *node);
void ufbx_wrapper_node_get_euler_rotation(const ufbx_node *node, double *xyz);
int ufbx_wrapper_node_get_rotation_order(const ufbx_node *node);

// Mesh access
ufbx_mesh* ufbx_wrapper_scene_get_mesh(const ufbx_scene *scene, size_t index);
const char* ufbx_wrapper_mesh_get_name(const ufbx_mesh *mesh);
size_t ufbx_wrapper_mesh_get_num_vertices(const ufbx_mesh *mesh);
size_t ufbx_wrapper_mesh_get_num_indices(const ufbx_mesh *mesh);
size_t ufbx_wrapper_mesh_get_num_faces(const ufbx_mesh *mesh);
size_t ufbx_wrapper_mesh_get_num_triangles(const ufbx_mesh *mesh);

// Mesh vertex data.
// Vector attributes are copied into wrapper-owned scratch buffers and returned as
// packed float arrays. The caller should copy the values immediately.
const float* ufbx_wrapper_mesh_get_vertex_positions(const ufbx_mesh *mesh, size_t *out_count);
const float* ufbx_wrapper_mesh_get_vertex_normals(const ufbx_mesh *mesh, size_t *out_count);
const float* ufbx_wrapper_mesh_get_vertex_uvs(const ufbx_mesh *mesh, size_t *out_count);
const float* ufbx_wrapper_mesh_get_vertex_tangents(const ufbx_mesh *mesh, size_t *out_count);
const float* ufbx_wrapper_mesh_get_vertex_bitangents(const ufbx_mesh *mesh, size_t *out_count);
const float* ufbx_wrapper_mesh_get_vertex_colors(const ufbx_mesh *mesh, size_t *out_count);
const uint32_t* ufbx_wrapper_mesh_get_indices(const ufbx_mesh *mesh, size_t *out_count);

// Mesh face data
size_t ufbx_wrapper_mesh_get_face_count(const ufbx_mesh *mesh);
void ufbx_wrapper_mesh_get_face(const ufbx_mesh *mesh, size_t index, uint32_t *index_begin, uint32_t *num_indices);
const uint32_t* ufbx_wrapper_mesh_get_face_material(const ufbx_mesh *mesh, size_t *out_count);
const double* ufbx_wrapper_mesh_get_edge_crease(const ufbx_mesh *mesh, size_t *out_count);
const float* ufbx_wrapper_mesh_get_vertex_crease(const ufbx_mesh *mesh, size_t *out_count);

// Mesh deformers
size_t ufbx_wrapper_mesh_get_num_skin_deformers(const ufbx_mesh *mesh);
ufbx_skin_deformer* ufbx_wrapper_mesh_get_skin_deformer(const ufbx_mesh *mesh, size_t index);
size_t ufbx_wrapper_mesh_get_num_blend_deformers(const ufbx_mesh *mesh);
ufbx_blend_deformer* ufbx_wrapper_mesh_get_blend_deformer(const ufbx_mesh *mesh, size_t index);

// Material access
ufbx_material* ufbx_wrapper_scene_get_material(const ufbx_scene *scene, size_t index);
size_t ufbx_wrapper_mesh_get_num_materials(const ufbx_mesh *mesh);
ufbx_material* ufbx_wrapper_mesh_get_material(const ufbx_mesh *mesh, size_t index);
const char* ufbx_wrapper_material_get_name(const ufbx_material *material);
int ufbx_wrapper_material_get_shader_type(const ufbx_material *material);
const char* ufbx_wrapper_material_get_shading_model_name(const ufbx_material *material);
size_t ufbx_wrapper_material_get_num_textures(const ufbx_material *material);

// Material PBR properties (most common)
void ufbx_wrapper_material_get_pbr_base_color(const ufbx_material *material, double *out_vec3, bool *has_texture);
void ufbx_wrapper_material_get_pbr_roughness(const ufbx_material *material, double *out_value, bool *has_texture);
void ufbx_wrapper_material_get_pbr_metalness(const ufbx_material *material, double *out_value, bool *has_texture);
void ufbx_wrapper_material_get_pbr_normal(const ufbx_material *material, bool *has_texture);

// Material PBR properties (extended)
void ufbx_wrapper_material_get_pbr_emission_color(const ufbx_material *material, double *out_vec3, bool *has_texture);
void ufbx_wrapper_material_get_pbr_emission_factor(const ufbx_material *material, double *out_value, bool *has_texture);
void ufbx_wrapper_material_get_pbr_opacity(const ufbx_material *material, double *out_value, bool *has_texture);
void ufbx_wrapper_material_get_pbr_ambient_occlusion(const ufbx_material *material, bool *has_texture);
void ufbx_wrapper_material_get_pbr_specular_factor(const ufbx_material *material, double *out_value, bool *has_texture);
void ufbx_wrapper_material_get_pbr_specular_color(const ufbx_material *material, double *out_vec3, bool *has_texture);
void ufbx_wrapper_material_get_pbr_specular_ior(const ufbx_material *material, double *out_value, bool *has_texture);

// Material PBR properties (advanced - Phase 3)
void ufbx_wrapper_material_get_pbr_transmission_factor(const ufbx_material *material, double *out_value, bool *has_texture);
void ufbx_wrapper_material_get_pbr_transmission_color(const ufbx_material *material, double *out_vec3, bool *has_texture);
void ufbx_wrapper_material_get_pbr_subsurface_factor(const ufbx_material *material, double *out_value, bool *has_texture);
void ufbx_wrapper_material_get_pbr_subsurface_color(const ufbx_material *material, double *out_vec3, bool *has_texture);
void ufbx_wrapper_material_get_pbr_subsurface_radius(const ufbx_material *material, double *out_vec3, bool *has_texture);
void ufbx_wrapper_material_get_pbr_coat_factor(const ufbx_material *material, double *out_value, bool *has_texture);
void ufbx_wrapper_material_get_pbr_coat_color(const ufbx_material *material, double *out_vec3, bool *has_texture);
void ufbx_wrapper_material_get_pbr_coat_roughness(const ufbx_material *material, double *out_value, bool *has_texture);
void ufbx_wrapper_material_get_pbr_coat_normal(const ufbx_material *material, bool *has_texture);
void ufbx_wrapper_material_get_pbr_sheen_factor(const ufbx_material *material, double *out_value, bool *has_texture);
void ufbx_wrapper_material_get_pbr_sheen_color(const ufbx_material *material, double *out_vec3, bool *has_texture);
void ufbx_wrapper_material_get_pbr_sheen_roughness(const ufbx_material *material, double *out_value, bool *has_texture);

// Material FBX properties (most common)
void ufbx_wrapper_material_get_fbx_diffuse_color(const ufbx_material *material, double *out_vec3, bool *has_texture);
void ufbx_wrapper_material_get_fbx_specular_color(const ufbx_material *material, double *out_vec3, bool *has_texture);
void ufbx_wrapper_material_get_fbx_normal(const ufbx_material *material, bool *has_texture);

// Material FBX properties (extended)
void ufbx_wrapper_material_get_fbx_emission_color(const ufbx_material *material, double *out_vec3, bool *has_texture);
void ufbx_wrapper_material_get_fbx_emission_factor(const ufbx_material *material, double *out_value, bool *has_texture);
void ufbx_wrapper_material_get_fbx_ambient_color(const ufbx_material *material, double *out_vec3, bool *has_texture);
void ufbx_wrapper_material_get_fbx_bump(const ufbx_material *material, bool *has_texture);
void ufbx_wrapper_material_get_fbx_bump_factor(const ufbx_material *material, double *out_value, bool *has_texture);
void ufbx_wrapper_material_get_fbx_displacement(const ufbx_material *material, bool *has_texture);

// Texture access from material maps - PBR
ufbx_texture* ufbx_wrapper_material_get_pbr_base_color_texture(const ufbx_material *material);
ufbx_texture* ufbx_wrapper_material_get_pbr_roughness_texture(const ufbx_material *material);
ufbx_texture* ufbx_wrapper_material_get_pbr_metalness_texture(const ufbx_material *material);
ufbx_texture* ufbx_wrapper_material_get_pbr_normal_texture(const ufbx_material *material);
ufbx_texture* ufbx_wrapper_material_get_pbr_emission_texture(const ufbx_material *material);
ufbx_texture* ufbx_wrapper_material_get_pbr_opacity_texture(const ufbx_material *material);
ufbx_texture* ufbx_wrapper_material_get_pbr_ambient_occlusion_texture(const ufbx_material *material);
ufbx_texture* ufbx_wrapper_material_get_pbr_specular_color_texture(const ufbx_material *material);
ufbx_texture* ufbx_wrapper_material_get_pbr_transmission_texture(const ufbx_material *material);
ufbx_texture* ufbx_wrapper_material_get_pbr_subsurface_color_texture(const ufbx_material *material);
ufbx_texture* ufbx_wrapper_material_get_pbr_coat_color_texture(const ufbx_material *material);
ufbx_texture* ufbx_wrapper_material_get_pbr_coat_normal_texture(const ufbx_material *material);
ufbx_texture* ufbx_wrapper_material_get_pbr_sheen_color_texture(const ufbx_material *material);

// Texture access from material maps - FBX
ufbx_texture* ufbx_wrapper_material_get_fbx_diffuse_texture(const ufbx_material *material);
ufbx_texture* ufbx_wrapper_material_get_fbx_specular_texture(const ufbx_material *material);
ufbx_texture* ufbx_wrapper_material_get_fbx_normal_texture(const ufbx_material *material);
ufbx_texture* ufbx_wrapper_material_get_fbx_emission_texture(const ufbx_material *material);
ufbx_texture* ufbx_wrapper_material_get_fbx_ambient_texture(const ufbx_material *material);
ufbx_texture* ufbx_wrapper_material_get_fbx_bump_texture(const ufbx_material *material);
ufbx_texture* ufbx_wrapper_material_get_fbx_displacement_texture(const ufbx_material *material);

// Texture properties
const char* ufbx_wrapper_texture_get_name(const ufbx_texture *texture);
const char* ufbx_wrapper_texture_get_filename(const ufbx_texture *texture);
const char* ufbx_wrapper_texture_get_relative_filename(const ufbx_texture *texture);
const char* ufbx_wrapper_texture_get_absolute_filename(const ufbx_texture *texture);
int ufbx_wrapper_texture_get_type(const ufbx_texture *texture);

// Light access
size_t ufbx_wrapper_scene_get_num_lights(const ufbx_scene *scene);
ufbx_light* ufbx_wrapper_scene_get_light(const ufbx_scene *scene, size_t index);
ufbx_light* ufbx_wrapper_node_get_light(const ufbx_node *node);
const char* ufbx_wrapper_light_get_name(const ufbx_light *light);
void ufbx_wrapper_light_get_color(const ufbx_light *light, float *rgb);
double ufbx_wrapper_light_get_intensity(const ufbx_light *light);
void ufbx_wrapper_light_get_local_direction(const ufbx_light *light, float *xyz);
int ufbx_wrapper_light_get_type(const ufbx_light *light);
int ufbx_wrapper_light_get_decay(const ufbx_light *light);
int ufbx_wrapper_light_get_area_shape(const ufbx_light *light);
double ufbx_wrapper_light_get_inner_angle(const ufbx_light *light);
double ufbx_wrapper_light_get_outer_angle(const ufbx_light *light);
bool ufbx_wrapper_light_get_cast_light(const ufbx_light *light);
bool ufbx_wrapper_light_get_cast_shadows(const ufbx_light *light);

// Camera access
size_t ufbx_wrapper_scene_get_num_cameras(const ufbx_scene *scene);
ufbx_camera* ufbx_wrapper_scene_get_camera(const ufbx_scene *scene, size_t index);
ufbx_camera* ufbx_wrapper_node_get_camera(const ufbx_node *node);
const char* ufbx_wrapper_camera_get_name(const ufbx_camera *camera);
int ufbx_wrapper_camera_get_projection_mode(const ufbx_camera *camera);
void ufbx_wrapper_camera_get_resolution(const ufbx_camera *camera, float *xy);
bool ufbx_wrapper_camera_get_resolution_is_pixels(const ufbx_camera *camera);
void ufbx_wrapper_camera_get_field_of_view_deg(const ufbx_camera *camera, float *xy);
void ufbx_wrapper_camera_get_field_of_view_tan(const ufbx_camera *camera, float *xy);
double ufbx_wrapper_camera_get_orthographic_extent(const ufbx_camera *camera);
void ufbx_wrapper_camera_get_orthographic_size(const ufbx_camera *camera, float *xy);
double ufbx_wrapper_camera_get_aspect_ratio(const ufbx_camera *camera);
double ufbx_wrapper_camera_get_near_plane(const ufbx_camera *camera);
double ufbx_wrapper_camera_get_far_plane(const ufbx_camera *camera);

// Bone access
size_t ufbx_wrapper_scene_get_num_bones(const ufbx_scene *scene);
ufbx_bone* ufbx_wrapper_scene_get_bone(const ufbx_scene *scene, size_t index);
ufbx_bone* ufbx_wrapper_node_get_bone(const ufbx_node *node);
const char* ufbx_wrapper_bone_get_name(const ufbx_bone *bone);
double ufbx_wrapper_bone_get_radius(const ufbx_bone *bone);
double ufbx_wrapper_bone_get_relative_length(const ufbx_bone *bone);
bool ufbx_wrapper_bone_is_root(const ufbx_bone *bone);

// Texture access
size_t ufbx_wrapper_scene_get_num_textures(const ufbx_scene *scene);
ufbx_texture* ufbx_wrapper_scene_get_texture(const ufbx_scene *scene, size_t index);

// AnimStack access
size_t ufbx_wrapper_scene_get_num_anim_stacks(const ufbx_scene *scene);
ufbx_anim_stack* ufbx_wrapper_scene_get_anim_stack(const ufbx_scene *scene, size_t index);
const char* ufbx_wrapper_anim_stack_get_name(const ufbx_anim_stack *anim_stack);
double ufbx_wrapper_anim_stack_get_time_begin(const ufbx_anim_stack *anim_stack);
double ufbx_wrapper_anim_stack_get_time_end(const ufbx_anim_stack *anim_stack);
size_t ufbx_wrapper_anim_stack_get_num_layers(const ufbx_anim_stack *anim_stack);
ufbx_anim_layer* ufbx_wrapper_anim_stack_get_layer(const ufbx_anim_stack *anim_stack, size_t index);

// AnimLayer access
const char* ufbx_wrapper_anim_layer_get_name(const ufbx_anim_layer *anim_layer);
double ufbx_wrapper_anim_layer_get_weight(const ufbx_anim_layer *anim_layer);
bool ufbx_wrapper_anim_layer_get_weight_is_animated(const ufbx_anim_layer *anim_layer);
bool ufbx_wrapper_anim_layer_get_blended(const ufbx_anim_layer *anim_layer);
bool ufbx_wrapper_anim_layer_get_additive(const ufbx_anim_layer *anim_layer);
bool ufbx_wrapper_anim_layer_get_compose_rotation(const ufbx_anim_layer *anim_layer);
bool ufbx_wrapper_anim_layer_get_compose_scale(const ufbx_anim_layer *anim_layer);

// AnimCurve access
size_t ufbx_wrapper_scene_get_num_anim_curves(const ufbx_scene *scene);
ufbx_anim_curve* ufbx_wrapper_scene_get_anim_curve(const ufbx_scene *scene, size_t index);
const char* ufbx_wrapper_anim_curve_get_name(const ufbx_anim_curve *anim_curve);
size_t ufbx_wrapper_anim_curve_get_num_keyframes(const ufbx_anim_curve *anim_curve);
double ufbx_wrapper_anim_curve_get_min_value(const ufbx_anim_curve *anim_curve);
double ufbx_wrapper_anim_curve_get_max_value(const ufbx_anim_curve *anim_curve);
double ufbx_wrapper_anim_curve_get_min_time(const ufbx_anim_curve *anim_curve);
double ufbx_wrapper_anim_curve_get_max_time(const ufbx_anim_curve *anim_curve);

// SkinDeformer access
size_t ufbx_wrapper_scene_get_num_skin_deformers(const ufbx_scene *scene);
ufbx_skin_deformer* ufbx_wrapper_scene_get_skin_deformer(const ufbx_scene *scene, size_t index);
const char* ufbx_wrapper_skin_deformer_get_name(const ufbx_skin_deformer *skin_deformer);
size_t ufbx_wrapper_skin_deformer_get_num_clusters(const ufbx_skin_deformer *skin_deformer);
ufbx_skin_cluster* ufbx_wrapper_skin_deformer_get_cluster(const ufbx_skin_deformer *skin_deformer, size_t index);

// SkinCluster access
const char* ufbx_wrapper_skin_cluster_get_name(const ufbx_skin_cluster *skin_cluster);
const char* ufbx_wrapper_skin_cluster_get_bone_name(const ufbx_skin_cluster *skin_cluster);
size_t ufbx_wrapper_skin_cluster_get_num_weights(const ufbx_skin_cluster *skin_cluster);
const uint32_t* ufbx_wrapper_skin_cluster_get_vertices(const ufbx_skin_cluster *skin_cluster, size_t *out_count);
const double* ufbx_wrapper_skin_cluster_get_weights(const ufbx_skin_cluster *skin_cluster, size_t *out_count);

// BlendDeformer access
size_t ufbx_wrapper_scene_get_num_blend_deformers(const ufbx_scene *scene);
ufbx_blend_deformer* ufbx_wrapper_scene_get_blend_deformer(const ufbx_scene *scene, size_t index);
const char* ufbx_wrapper_blend_deformer_get_name(const ufbx_blend_deformer *blend_deformer);
size_t ufbx_wrapper_blend_deformer_get_num_channels(const ufbx_blend_deformer *blend_deformer);
ufbx_blend_channel* ufbx_wrapper_blend_deformer_get_channel(const ufbx_blend_deformer *blend_deformer, size_t index);

// BlendChannel access
const char* ufbx_wrapper_blend_channel_get_name(const ufbx_blend_channel *blend_channel);
double ufbx_wrapper_blend_channel_get_weight(const ufbx_blend_channel *blend_channel);

// BlendShape access
size_t ufbx_wrapper_scene_get_num_blend_shapes(const ufbx_scene *scene);
ufbx_blend_shape* ufbx_wrapper_scene_get_blend_shape(const ufbx_scene *scene, size_t index);
const char* ufbx_wrapper_blend_shape_get_name(const ufbx_blend_shape *blend_shape);
size_t ufbx_wrapper_blend_shape_get_num_offsets(const ufbx_blend_shape *blend_shape);

// Constraint access
size_t ufbx_wrapper_scene_get_num_constraints(const ufbx_scene *scene);
ufbx_constraint* ufbx_wrapper_scene_get_constraint(const ufbx_scene *scene, size_t index);
const char* ufbx_wrapper_constraint_get_name(const ufbx_constraint *constraint);
int ufbx_wrapper_constraint_get_type(const ufbx_constraint *constraint);
double ufbx_wrapper_constraint_get_weight(const ufbx_constraint *constraint);
bool ufbx_wrapper_constraint_get_active(const ufbx_constraint *constraint);

#ifdef __cplusplus
}
#endif

#endif // UFBX_WRAPPER_H
