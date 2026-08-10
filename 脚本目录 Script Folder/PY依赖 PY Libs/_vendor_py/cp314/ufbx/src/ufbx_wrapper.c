#include "ufbx_wrapper.h"
#include "ufbx-c/ufbx.h"
#include <string.h>
#include <stdlib.h>

static float *ufbx_wrapper_position_buffer = NULL;
static size_t ufbx_wrapper_position_capacity = 0;
static float *ufbx_wrapper_normal_buffer = NULL;
static size_t ufbx_wrapper_normal_capacity = 0;
static float *ufbx_wrapper_uv_buffer = NULL;
static size_t ufbx_wrapper_uv_capacity = 0;
static float *ufbx_wrapper_tangent_buffer = NULL;
static size_t ufbx_wrapper_tangent_capacity = 0;
static float *ufbx_wrapper_bitangent_buffer = NULL;
static size_t ufbx_wrapper_bitangent_capacity = 0;
static float *ufbx_wrapper_color_buffer = NULL;
static size_t ufbx_wrapper_color_capacity = 0;

static float* ufbx_wrapper_ensure_float_buffer(float **buffer, size_t *capacity, size_t count) {
    float *next_buffer;
    if (!buffer || !capacity) return NULL;
    if (*capacity >= count) return *buffer;
    next_buffer = (float*)realloc(*buffer, count * sizeof(float));
    if (!next_buffer) return NULL;
    *buffer = next_buffer;
    *capacity = count;
    return next_buffer;
}

// Scene management
ufbx_scene* ufbx_wrapper_load_file(const char *filename, char **error_msg) {
    ufbx_load_opts opts = {0};
    ufbx_error error;
    ufbx_scene *scene = ufbx_load_file(filename, &opts, &error);

    if (!scene && error_msg) {
        size_t len = error.description.length;
        *error_msg = (char*)malloc(len + 1);
        if (*error_msg) {
            memcpy(*error_msg, error.description.data, len);
            (*error_msg)[len] = '\0';
        }
    }

    return scene;
}

void ufbx_wrapper_free_scene(ufbx_scene *scene) {
    if (scene) {
        ufbx_free_scene(scene);
    }
}

// Scene queries
size_t ufbx_wrapper_scene_get_num_nodes(const ufbx_scene *scene) {
    return scene ? scene->nodes.count : 0;
}

size_t ufbx_wrapper_scene_get_num_meshes(const ufbx_scene *scene) {
    return scene ? scene->meshes.count : 0;
}

size_t ufbx_wrapper_scene_get_num_materials(const ufbx_scene *scene) {
    return scene ? scene->materials.count : 0;
}

ufbx_node* ufbx_wrapper_scene_get_root_node(const ufbx_scene *scene) {
    return scene ? scene->root_node : NULL;
}

int ufbx_wrapper_scene_get_axes_right(const ufbx_scene *scene) {
    return scene ? (int)scene->settings.axes.right : (int)UFBX_COORDINATE_AXIS_UNKNOWN;
}

int ufbx_wrapper_scene_get_axes_up(const ufbx_scene *scene) {
    return scene ? (int)scene->settings.axes.up : (int)UFBX_COORDINATE_AXIS_UNKNOWN;
}

int ufbx_wrapper_scene_get_axes_front(const ufbx_scene *scene) {
    return scene ? (int)scene->settings.axes.front : (int)UFBX_COORDINATE_AXIS_UNKNOWN;
}

// Node access
ufbx_node* ufbx_wrapper_scene_get_node(const ufbx_scene *scene, size_t index) {
    if (!scene || index >= scene->nodes.count) return NULL;
    return scene->nodes.data[index];
}

const char* ufbx_wrapper_node_get_name(const ufbx_node *node) {
    if (!node) return "";
    return node->name.data ? node->name.data : "";
}

size_t ufbx_wrapper_node_get_num_children(const ufbx_node *node) {
    return node ? node->children.count : 0;
}

ufbx_node* ufbx_wrapper_node_get_child(const ufbx_node *node, size_t index) {
    if (!node || index >= node->children.count) return NULL;
    return node->children.data[index];
}

ufbx_node* ufbx_wrapper_node_get_parent(const ufbx_node *node) {
    return node ? node->parent : NULL;
}

ufbx_mesh* ufbx_wrapper_node_get_mesh(const ufbx_node *node) {
    return node ? node->mesh : NULL;
}

bool ufbx_wrapper_node_is_root(const ufbx_node *node) {
    return node ? node->is_root : false;
}

// Node transform
void ufbx_wrapper_node_get_world_transform(const ufbx_node *node, double *matrix16) {
    if (!node || !matrix16) return;

    const ufbx_matrix *m = &node->node_to_world;
    // Column-major order
    matrix16[0] = m->m00; matrix16[4] = m->m01; matrix16[8]  = m->m02; matrix16[12] = m->m03;
    matrix16[1] = m->m10; matrix16[5] = m->m11; matrix16[9]  = m->m12; matrix16[13] = m->m13;
    matrix16[2] = m->m20; matrix16[6] = m->m21; matrix16[10] = m->m22; matrix16[14] = m->m23;
    matrix16[3] = 0.0;    matrix16[7] = 0.0;    matrix16[11] = 0.0;    matrix16[15] = 1.0;
}

void ufbx_wrapper_node_get_local_transform(const ufbx_node *node, double *matrix16) {
    if (!node || !matrix16) return;

    const ufbx_matrix *m = &node->node_to_parent;
    // Column-major order
    matrix16[0] = m->m00; matrix16[4] = m->m01; matrix16[8]  = m->m02; matrix16[12] = m->m03;
    matrix16[1] = m->m10; matrix16[5] = m->m11; matrix16[9]  = m->m12; matrix16[13] = m->m13;
    matrix16[2] = m->m20; matrix16[6] = m->m21; matrix16[10] = m->m22; matrix16[14] = m->m23;
    matrix16[3] = 0.0;    matrix16[7] = 0.0;    matrix16[11] = 0.0;    matrix16[15] = 1.0;
}

void ufbx_wrapper_node_get_node_to_world(const ufbx_node *node, double *matrix16) {
    if (!node || !matrix16) return;

    const ufbx_matrix *m = &node->node_to_world;
    // Column-major order
    matrix16[0] = m->m00; matrix16[4] = m->m01; matrix16[8]  = m->m02; matrix16[12] = m->m03;
    matrix16[1] = m->m10; matrix16[5] = m->m11; matrix16[9]  = m->m12; matrix16[13] = m->m13;
    matrix16[2] = m->m20; matrix16[6] = m->m21; matrix16[10] = m->m22; matrix16[14] = m->m23;
    matrix16[3] = 0.0;    matrix16[7] = 0.0;    matrix16[11] = 0.0;    matrix16[15] = 1.0;
}

void ufbx_wrapper_node_get_node_to_parent(const ufbx_node *node, double *matrix16) {
    if (!node || !matrix16) return;

    const ufbx_matrix *m = &node->node_to_parent;
    // Column-major order
    matrix16[0] = m->m00; matrix16[4] = m->m01; matrix16[8]  = m->m02; matrix16[12] = m->m03;
    matrix16[1] = m->m10; matrix16[5] = m->m11; matrix16[9]  = m->m12; matrix16[13] = m->m13;
    matrix16[2] = m->m20; matrix16[6] = m->m21; matrix16[10] = m->m22; matrix16[14] = m->m23;
    matrix16[3] = 0.0;    matrix16[7] = 0.0;    matrix16[11] = 0.0;    matrix16[15] = 1.0;
}

void ufbx_wrapper_node_get_geometry_transform(const ufbx_node *node, double *translation3, double *rotation4, double *scale3) {
    if (!node) return;

    const ufbx_transform *t = &node->geometry_transform;
    
    if (translation3) {
        translation3[0] = t->translation.x;
        translation3[1] = t->translation.y;
        translation3[2] = t->translation.z;
    }
    
    if (rotation4) {
        rotation4[0] = t->rotation.x;
        rotation4[1] = t->rotation.y;
        rotation4[2] = t->rotation.z;
        rotation4[3] = t->rotation.w;
    }
    
    if (scale3) {
        scale3[0] = t->scale.x;
        scale3[1] = t->scale.y;
        scale3[2] = t->scale.z;
    }
}

// Node additional properties
int ufbx_wrapper_node_get_attrib_type(const ufbx_node *node) {
    return node ? (int)node->attrib_type : 0;
}

int ufbx_wrapper_node_get_inherit_mode(const ufbx_node *node) {
    return node ? (int)node->inherit_mode : 0;
}

bool ufbx_wrapper_node_get_visible(const ufbx_node *node) {
    return node ? node->visible : false;
}

void ufbx_wrapper_node_get_euler_rotation(const ufbx_node *node, double *xyz) {
    if (!node || !xyz) return;
    xyz[0] = node->euler_rotation.x;
    xyz[1] = node->euler_rotation.y;
    xyz[2] = node->euler_rotation.z;
}

int ufbx_wrapper_node_get_rotation_order(const ufbx_node *node) {
    return node ? (int)node->rotation_order : 0;
}

// Mesh access
ufbx_mesh* ufbx_wrapper_scene_get_mesh(const ufbx_scene *scene, size_t index) {
    if (!scene || index >= scene->meshes.count) return NULL;
    return scene->meshes.data[index];
}

const char* ufbx_wrapper_mesh_get_name(const ufbx_mesh *mesh) {
    if (!mesh) return "";
    return mesh->name.data ? mesh->name.data : "";
}

size_t ufbx_wrapper_mesh_get_num_vertices(const ufbx_mesh *mesh) {
    return mesh ? mesh->num_vertices : 0;
}

size_t ufbx_wrapper_mesh_get_num_indices(const ufbx_mesh *mesh) {
    return mesh ? mesh->num_indices : 0;
}

size_t ufbx_wrapper_mesh_get_num_faces(const ufbx_mesh *mesh) {
    return mesh ? mesh->num_faces : 0;
}

size_t ufbx_wrapper_mesh_get_num_triangles(const ufbx_mesh *mesh) {
    return mesh ? mesh->num_triangles : 0;
}

size_t ufbx_wrapper_mesh_get_num_uv_sets(const ufbx_mesh *mesh) {
    return mesh ? mesh->uv_sets.count : 0;
}

// Mesh vertex data
const float* ufbx_wrapper_mesh_get_vertex_positions(const ufbx_mesh *mesh, size_t *out_count) {
    size_t count, float_count, i;
    float *buffer;
    if (!mesh || !mesh->vertex_position.exists || !out_count) {
        if (out_count) *out_count = 0;
        return NULL;
    }

    count = mesh->vertex_position.values.count;
    *out_count = count;
    float_count = count * 3;
    buffer = ufbx_wrapper_ensure_float_buffer(&ufbx_wrapper_position_buffer, &ufbx_wrapper_position_capacity, float_count);
    if (!buffer) {
        *out_count = 0;
        return NULL;
    }
    for (i = 0; i < count; i++) {
        buffer[(i * 3) + 0] = (float)mesh->vertex_position.values.data[i].x;
        buffer[(i * 3) + 1] = (float)mesh->vertex_position.values.data[i].y;
        buffer[(i * 3) + 2] = (float)mesh->vertex_position.values.data[i].z;
    }
    return buffer;
}

const float* ufbx_wrapper_mesh_get_vertex_normals(const ufbx_mesh *mesh, size_t *out_count) {
    size_t count, float_count, i;
    float *buffer;
    if (!mesh || !mesh->vertex_normal.exists || !out_count) {
        if (out_count) *out_count = 0;
        return NULL;
    }

    count = mesh->vertex_normal.values.count;
    *out_count = count;
    float_count = count * 3;
    buffer = ufbx_wrapper_ensure_float_buffer(&ufbx_wrapper_normal_buffer, &ufbx_wrapper_normal_capacity, float_count);
    if (!buffer) {
        *out_count = 0;
        return NULL;
    }
    for (i = 0; i < count; i++) {
        buffer[(i * 3) + 0] = (float)mesh->vertex_normal.values.data[i].x;
        buffer[(i * 3) + 1] = (float)mesh->vertex_normal.values.data[i].y;
        buffer[(i * 3) + 2] = (float)mesh->vertex_normal.values.data[i].z;
    }
    return buffer;
}

const float* ufbx_wrapper_mesh_get_vertex_uvs(const ufbx_mesh *mesh, size_t *out_count) {
    size_t count, float_count, i;
    float *buffer;
    if (!mesh || !mesh->vertex_uv.exists || !out_count) {
        if (out_count) *out_count = 0;
        return NULL;
    }

    count = mesh->vertex_uv.values.count;
    *out_count = count;
    float_count = count * 2;
    buffer = ufbx_wrapper_ensure_float_buffer(&ufbx_wrapper_uv_buffer, &ufbx_wrapper_uv_capacity, float_count);
    if (!buffer) {
        *out_count = 0;
        return NULL;
    }
    for (i = 0; i < count; i++) {
        buffer[(i * 2) + 0] = (float)mesh->vertex_uv.values.data[i].x;
        buffer[(i * 2) + 1] = (float)mesh->vertex_uv.values.data[i].y;
    }
    return buffer;
}

const float* ufbx_wrapper_mesh_get_vertex_uvs_for_set(const ufbx_mesh *mesh, size_t set_index, size_t *out_count) {
    size_t count, float_count, i;
    float *buffer;
    const ufbx_uv_set *uv_set;
    if (!mesh || !out_count || set_index >= mesh->uv_sets.count) {
        if (out_count) *out_count = 0;
        return NULL;
    }

    uv_set = &mesh->uv_sets.data[set_index];
    if (!uv_set->vertex_uv.exists) {
        *out_count = 0;
        return NULL;
    }

    count = uv_set->vertex_uv.values.count;
    *out_count = count;
    float_count = count * 2;
    buffer = ufbx_wrapper_ensure_float_buffer(&ufbx_wrapper_uv_buffer, &ufbx_wrapper_uv_capacity, float_count);
    if (!buffer) {
        *out_count = 0;
        return NULL;
    }
    for (i = 0; i < count; i++) {
        buffer[(i * 2) + 0] = (float)uv_set->vertex_uv.values.data[i].x;
        buffer[(i * 2) + 1] = (float)uv_set->vertex_uv.values.data[i].y;
    }
    return buffer;
}

const uint32_t* ufbx_wrapper_mesh_get_vertex_uv_indices_for_set(const ufbx_mesh *mesh, size_t set_index, size_t *out_count) {
    const ufbx_uv_set *uv_set;
    if (!mesh || !out_count || set_index >= mesh->uv_sets.count) {
        if (out_count) *out_count = 0;
        return NULL;
    }

    uv_set = &mesh->uv_sets.data[set_index];
    if (!uv_set->vertex_uv.exists) {
        *out_count = 0;
        return NULL;
    }

    *out_count = uv_set->vertex_uv.indices.count;
    return uv_set->vertex_uv.indices.data;
}

const float* ufbx_wrapper_mesh_get_vertex_tangents(const ufbx_mesh *mesh, size_t *out_count) {
    size_t count, float_count, i;
    float *buffer;
    if (!mesh || !mesh->vertex_tangent.exists || !out_count) {
        if (out_count) *out_count = 0;
        return NULL;
    }

    count = mesh->vertex_tangent.values.count;
    *out_count = count;
    float_count = count * 3;
    buffer = ufbx_wrapper_ensure_float_buffer(&ufbx_wrapper_tangent_buffer, &ufbx_wrapper_tangent_capacity, float_count);
    if (!buffer) {
        *out_count = 0;
        return NULL;
    }
    for (i = 0; i < count; i++) {
        buffer[(i * 3) + 0] = (float)mesh->vertex_tangent.values.data[i].x;
        buffer[(i * 3) + 1] = (float)mesh->vertex_tangent.values.data[i].y;
        buffer[(i * 3) + 2] = (float)mesh->vertex_tangent.values.data[i].z;
    }
    return buffer;
}

const float* ufbx_wrapper_mesh_get_vertex_bitangents(const ufbx_mesh *mesh, size_t *out_count) {
    size_t count, float_count, i;
    float *buffer;
    if (!mesh || !mesh->vertex_bitangent.exists || !out_count) {
        if (out_count) *out_count = 0;
        return NULL;
    }

    count = mesh->vertex_bitangent.values.count;
    *out_count = count;
    float_count = count * 3;
    buffer = ufbx_wrapper_ensure_float_buffer(&ufbx_wrapper_bitangent_buffer, &ufbx_wrapper_bitangent_capacity, float_count);
    if (!buffer) {
        *out_count = 0;
        return NULL;
    }
    for (i = 0; i < count; i++) {
        buffer[(i * 3) + 0] = (float)mesh->vertex_bitangent.values.data[i].x;
        buffer[(i * 3) + 1] = (float)mesh->vertex_bitangent.values.data[i].y;
        buffer[(i * 3) + 2] = (float)mesh->vertex_bitangent.values.data[i].z;
    }
    return buffer;
}

const float* ufbx_wrapper_mesh_get_vertex_colors(const ufbx_mesh *mesh, size_t *out_count) {
    size_t count, float_count, i;
    float *buffer;
    if (!mesh || !mesh->vertex_color.exists || !out_count) {
        if (out_count) *out_count = 0;
        return NULL;
    }

    count = mesh->vertex_color.values.count;
    *out_count = count;
    float_count = count * 4;
    buffer = ufbx_wrapper_ensure_float_buffer(&ufbx_wrapper_color_buffer, &ufbx_wrapper_color_capacity, float_count);
    if (!buffer) {
        *out_count = 0;
        return NULL;
    }
    for (i = 0; i < count; i++) {
        buffer[(i * 4) + 0] = (float)mesh->vertex_color.values.data[i].x;
        buffer[(i * 4) + 1] = (float)mesh->vertex_color.values.data[i].y;
        buffer[(i * 4) + 2] = (float)mesh->vertex_color.values.data[i].z;
        buffer[(i * 4) + 3] = (float)mesh->vertex_color.values.data[i].w;
    }
    return buffer;
}

const uint32_t* ufbx_wrapper_mesh_get_indices(const ufbx_mesh *mesh, size_t *out_count) {
    if (!mesh || !mesh->vertex_position.exists || !out_count) {
        if (out_count) *out_count = 0;
        return NULL;
    }

    *out_count = mesh->vertex_position.indices.count;
    return mesh->vertex_position.indices.data;
}

// Mesh face data
size_t ufbx_wrapper_mesh_get_face_count(const ufbx_mesh *mesh) {
    return mesh ? mesh->faces.count : 0;
}

void ufbx_wrapper_mesh_get_face(const ufbx_mesh *mesh, size_t index, uint32_t *index_begin, uint32_t *num_indices) {
    if (!mesh || index >= mesh->faces.count) {
        if (index_begin) *index_begin = 0;
        if (num_indices) *num_indices = 0;
        return;
    }
    if (index_begin) *index_begin = mesh->faces.data[index].index_begin;
    if (num_indices) *num_indices = mesh->faces.data[index].num_indices;
}

const uint32_t* ufbx_wrapper_mesh_get_face_material(const ufbx_mesh *mesh, size_t *out_count) {
    if (!mesh || !out_count) {
        if (out_count) *out_count = 0;
        return NULL;
    }
    *out_count = mesh->face_material.count;
    return mesh->face_material.data;
}

const double* ufbx_wrapper_mesh_get_edge_crease(const ufbx_mesh *mesh, size_t *out_count) {
    if (!mesh || !out_count) {
        if (out_count) *out_count = 0;
        return NULL;
    }
    *out_count = mesh->edge_crease.count;
    return mesh->edge_crease.data;
}

const float* ufbx_wrapper_mesh_get_vertex_crease(const ufbx_mesh *mesh, size_t *out_count) {
    if (!mesh || !mesh->vertex_crease.exists || !out_count) {
        if (out_count) *out_count = 0;
        return NULL;
    }
    *out_count = mesh->vertex_crease.values.count;
    return (const float*)mesh->vertex_crease.values.data;
}

// Mesh deformers
size_t ufbx_wrapper_mesh_get_num_skin_deformers(const ufbx_mesh *mesh) {
    return mesh ? mesh->skin_deformers.count : 0;
}

ufbx_skin_deformer* ufbx_wrapper_mesh_get_skin_deformer(const ufbx_mesh *mesh, size_t index) {
    if (!mesh || index >= mesh->skin_deformers.count) return NULL;
    return mesh->skin_deformers.data[index];
}

size_t ufbx_wrapper_mesh_get_num_blend_deformers(const ufbx_mesh *mesh) {
    return mesh ? mesh->blend_deformers.count : 0;
}

ufbx_blend_deformer* ufbx_wrapper_mesh_get_blend_deformer(const ufbx_mesh *mesh, size_t index) {
    if (!mesh || index >= mesh->blend_deformers.count) return NULL;
    return mesh->blend_deformers.data[index];
}

// Material access
ufbx_material* ufbx_wrapper_scene_get_material(const ufbx_scene *scene, size_t index) {
    if (!scene || index >= scene->materials.count) return NULL;
    return scene->materials.data[index];
}

size_t ufbx_wrapper_mesh_get_num_materials(const ufbx_mesh *mesh) {
    return mesh ? mesh->materials.count : 0;
}

ufbx_material* ufbx_wrapper_mesh_get_material(const ufbx_mesh *mesh, size_t index) {
    if (!mesh || index >= mesh->materials.count) return NULL;
    return mesh->materials.data[index];
}

const char* ufbx_wrapper_material_get_name(const ufbx_material *material) {
    if (!material) return "";
    return material->name.data ? material->name.data : "";
}

int ufbx_wrapper_material_get_shader_type(const ufbx_material *material) {
    if (!material) return 0;
    return (int)material->shader_type;
}

const char* ufbx_wrapper_material_get_shading_model_name(const ufbx_material *material) {
    if (!material) return "";
    return material->shading_model_name.data ? material->shading_model_name.data : "";
}

size_t ufbx_wrapper_material_get_num_textures(const ufbx_material *material) {
    if (!material) return 0;
    return material->textures.count;
}

void ufbx_wrapper_material_get_pbr_base_color(const ufbx_material *material, double *out_vec3, bool *has_texture) {
    if (!material || !out_vec3 || !has_texture) return;
    out_vec3[0] = material->pbr.base_color.value_vec3.x;
    out_vec3[1] = material->pbr.base_color.value_vec3.y;
    out_vec3[2] = material->pbr.base_color.value_vec3.z;
    *has_texture = (material->pbr.base_color.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_roughness(const ufbx_material *material, double *out_value, bool *has_texture) {
    if (!material || !out_value || !has_texture) return;
    *out_value = material->pbr.roughness.value_real;
    *has_texture = (material->pbr.roughness.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_metalness(const ufbx_material *material, double *out_value, bool *has_texture) {
    if (!material || !out_value || !has_texture) return;
    *out_value = material->pbr.metalness.value_real;
    *has_texture = (material->pbr.metalness.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_normal(const ufbx_material *material, bool *has_texture) {
    if (!material || !has_texture) return;
    *has_texture = (material->pbr.normal_map.texture != NULL);
}

void ufbx_wrapper_material_get_fbx_diffuse_color(const ufbx_material *material, double *out_vec3, bool *has_texture) {
    if (!material || !out_vec3 || !has_texture) return;
    out_vec3[0] = material->fbx.diffuse_color.value_vec3.x;
    out_vec3[1] = material->fbx.diffuse_color.value_vec3.y;
    out_vec3[2] = material->fbx.diffuse_color.value_vec3.z;
    *has_texture = (material->fbx.diffuse_color.texture != NULL);
}

void ufbx_wrapper_material_get_fbx_specular_color(const ufbx_material *material, double *out_vec3, bool *has_texture) {
    if (!material || !out_vec3 || !has_texture) return;
    out_vec3[0] = material->fbx.specular_color.value_vec3.x;
    out_vec3[1] = material->fbx.specular_color.value_vec3.y;
    out_vec3[2] = material->fbx.specular_color.value_vec3.z;
    *has_texture = (material->fbx.specular_color.texture != NULL);
}

void ufbx_wrapper_material_get_fbx_normal(const ufbx_material *material, bool *has_texture) {
    if (!material || !has_texture) return;
    *has_texture = (material->fbx.normal_map.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_emission_color(const ufbx_material *material, double *out_vec3, bool *has_texture) {
    if (!material || !out_vec3 || !has_texture) return;
    out_vec3[0] = material->pbr.emission_color.value_vec3.x;
    out_vec3[1] = material->pbr.emission_color.value_vec3.y;
    out_vec3[2] = material->pbr.emission_color.value_vec3.z;
    *has_texture = (material->pbr.emission_color.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_emission_factor(const ufbx_material *material, double *out_value, bool *has_texture) {
    if (!material || !out_value || !has_texture) return;
    *out_value = material->pbr.emission_factor.value_real;
    *has_texture = (material->pbr.emission_factor.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_opacity(const ufbx_material *material, double *out_value, bool *has_texture) {
    if (!material || !out_value || !has_texture) return;
    *out_value = material->pbr.opacity.value_real;
    *has_texture = (material->pbr.opacity.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_ambient_occlusion(const ufbx_material *material, bool *has_texture) {
    if (!material || !has_texture) return;
    *has_texture = (material->pbr.ambient_occlusion.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_specular_factor(const ufbx_material *material, double *out_value, bool *has_texture) {
    if (!material || !out_value || !has_texture) return;
    *out_value = material->pbr.specular_factor.value_real;
    *has_texture = (material->pbr.specular_factor.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_specular_color(const ufbx_material *material, double *out_vec3, bool *has_texture) {
    if (!material || !out_vec3 || !has_texture) return;
    out_vec3[0] = material->pbr.specular_color.value_vec3.x;
    out_vec3[1] = material->pbr.specular_color.value_vec3.y;
    out_vec3[2] = material->pbr.specular_color.value_vec3.z;
    *has_texture = (material->pbr.specular_color.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_specular_ior(const ufbx_material *material, double *out_value, bool *has_texture) {
    if (!material || !out_value || !has_texture) return;
    *out_value = material->pbr.specular_ior.value_real;
    *has_texture = (material->pbr.specular_ior.texture != NULL);
}

void ufbx_wrapper_material_get_fbx_emission_color(const ufbx_material *material, double *out_vec3, bool *has_texture) {
    if (!material || !out_vec3 || !has_texture) return;
    out_vec3[0] = material->fbx.emission_color.value_vec3.x;
    out_vec3[1] = material->fbx.emission_color.value_vec3.y;
    out_vec3[2] = material->fbx.emission_color.value_vec3.z;
    *has_texture = (material->fbx.emission_color.texture != NULL);
}

void ufbx_wrapper_material_get_fbx_emission_factor(const ufbx_material *material, double *out_value, bool *has_texture) {
    if (!material || !out_value || !has_texture) return;
    *out_value = material->fbx.emission_factor.value_real;
    *has_texture = (material->fbx.emission_factor.texture != NULL);
}

void ufbx_wrapper_material_get_fbx_ambient_color(const ufbx_material *material, double *out_vec3, bool *has_texture) {
    if (!material || !out_vec3 || !has_texture) return;
    out_vec3[0] = material->fbx.ambient_color.value_vec3.x;
    out_vec3[1] = material->fbx.ambient_color.value_vec3.y;
    out_vec3[2] = material->fbx.ambient_color.value_vec3.z;
    *has_texture = (material->fbx.ambient_color.texture != NULL);
}

void ufbx_wrapper_material_get_fbx_bump(const ufbx_material *material, bool *has_texture) {
    if (!material || !has_texture) return;
    *has_texture = (material->fbx.bump.texture != NULL);
}

void ufbx_wrapper_material_get_fbx_bump_factor(const ufbx_material *material, double *out_value, bool *has_texture) {
    if (!material || !out_value || !has_texture) return;
    *out_value = material->fbx.bump_factor.value_real;
    *has_texture = (material->fbx.bump_factor.texture != NULL);
}

void ufbx_wrapper_material_get_fbx_displacement(const ufbx_material *material, bool *has_texture) {
    if (!material || !has_texture) return;
    *has_texture = (material->fbx.displacement.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_transmission_factor(const ufbx_material *material, double *out_value, bool *has_texture) {
    if (!material || !out_value || !has_texture) return;
    *out_value = material->pbr.transmission_factor.value_real;
    *has_texture = (material->pbr.transmission_factor.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_transmission_color(const ufbx_material *material, double *out_vec3, bool *has_texture) {
    if (!material || !out_vec3 || !has_texture) return;
    out_vec3[0] = material->pbr.transmission_color.value_vec3.x;
    out_vec3[1] = material->pbr.transmission_color.value_vec3.y;
    out_vec3[2] = material->pbr.transmission_color.value_vec3.z;
    *has_texture = (material->pbr.transmission_color.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_subsurface_factor(const ufbx_material *material, double *out_value, bool *has_texture) {
    if (!material || !out_value || !has_texture) return;
    *out_value = material->pbr.subsurface_factor.value_real;
    *has_texture = (material->pbr.subsurface_factor.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_subsurface_color(const ufbx_material *material, double *out_vec3, bool *has_texture) {
    if (!material || !out_vec3 || !has_texture) return;
    out_vec3[0] = material->pbr.subsurface_color.value_vec3.x;
    out_vec3[1] = material->pbr.subsurface_color.value_vec3.y;
    out_vec3[2] = material->pbr.subsurface_color.value_vec3.z;
    *has_texture = (material->pbr.subsurface_color.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_subsurface_radius(const ufbx_material *material, double *out_vec3, bool *has_texture) {
    if (!material || !out_vec3 || !has_texture) return;
    out_vec3[0] = material->pbr.subsurface_radius.value_vec3.x;
    out_vec3[1] = material->pbr.subsurface_radius.value_vec3.y;
    out_vec3[2] = material->pbr.subsurface_radius.value_vec3.z;
    *has_texture = (material->pbr.subsurface_radius.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_coat_factor(const ufbx_material *material, double *out_value, bool *has_texture) {
    if (!material || !out_value || !has_texture) return;
    *out_value = material->pbr.coat_factor.value_real;
    *has_texture = (material->pbr.coat_factor.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_coat_color(const ufbx_material *material, double *out_vec3, bool *has_texture) {
    if (!material || !out_vec3 || !has_texture) return;
    out_vec3[0] = material->pbr.coat_color.value_vec3.x;
    out_vec3[1] = material->pbr.coat_color.value_vec3.y;
    out_vec3[2] = material->pbr.coat_color.value_vec3.z;
    *has_texture = (material->pbr.coat_color.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_coat_roughness(const ufbx_material *material, double *out_value, bool *has_texture) {
    if (!material || !out_value || !has_texture) return;
    *out_value = material->pbr.coat_roughness.value_real;
    *has_texture = (material->pbr.coat_roughness.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_coat_normal(const ufbx_material *material, bool *has_texture) {
    if (!material || !has_texture) return;
    *has_texture = (material->pbr.coat_normal.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_sheen_factor(const ufbx_material *material, double *out_value, bool *has_texture) {
    if (!material || !out_value || !has_texture) return;
    *out_value = material->pbr.sheen_factor.value_real;
    *has_texture = (material->pbr.sheen_factor.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_sheen_color(const ufbx_material *material, double *out_vec3, bool *has_texture) {
    if (!material || !out_vec3 || !has_texture) return;
    out_vec3[0] = material->pbr.sheen_color.value_vec3.x;
    out_vec3[1] = material->pbr.sheen_color.value_vec3.y;
    out_vec3[2] = material->pbr.sheen_color.value_vec3.z;
    *has_texture = (material->pbr.sheen_color.texture != NULL);
}

void ufbx_wrapper_material_get_pbr_sheen_roughness(const ufbx_material *material, double *out_value, bool *has_texture) {
    if (!material || !out_value || !has_texture) return;
    *out_value = material->pbr.sheen_roughness.value_real;
    *has_texture = (material->pbr.sheen_roughness.texture != NULL);
}

ufbx_texture* ufbx_wrapper_material_get_pbr_base_color_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->pbr.base_color.texture;
}

ufbx_texture* ufbx_wrapper_material_get_pbr_roughness_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->pbr.roughness.texture;
}

ufbx_texture* ufbx_wrapper_material_get_pbr_metalness_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->pbr.metalness.texture;
}

ufbx_texture* ufbx_wrapper_material_get_pbr_normal_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->pbr.normal_map.texture;
}

ufbx_texture* ufbx_wrapper_material_get_fbx_diffuse_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->fbx.diffuse_color.texture;
}

ufbx_texture* ufbx_wrapper_material_get_fbx_specular_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->fbx.specular_color.texture;
}

ufbx_texture* ufbx_wrapper_material_get_fbx_normal_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->fbx.normal_map.texture;
}

ufbx_texture* ufbx_wrapper_material_get_pbr_emission_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->pbr.emission_color.texture;
}

ufbx_texture* ufbx_wrapper_material_get_pbr_opacity_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->pbr.opacity.texture;
}

ufbx_texture* ufbx_wrapper_material_get_pbr_ambient_occlusion_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->pbr.ambient_occlusion.texture;
}

ufbx_texture* ufbx_wrapper_material_get_pbr_specular_color_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->pbr.specular_color.texture;
}

ufbx_texture* ufbx_wrapper_material_get_fbx_emission_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->fbx.emission_color.texture;
}

ufbx_texture* ufbx_wrapper_material_get_fbx_ambient_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->fbx.ambient_color.texture;
}

ufbx_texture* ufbx_wrapper_material_get_fbx_bump_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->fbx.bump.texture;
}

ufbx_texture* ufbx_wrapper_material_get_fbx_displacement_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->fbx.displacement.texture;
}

ufbx_texture* ufbx_wrapper_material_get_pbr_transmission_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->pbr.transmission_color.texture;
}

ufbx_texture* ufbx_wrapper_material_get_pbr_subsurface_color_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->pbr.subsurface_color.texture;
}

ufbx_texture* ufbx_wrapper_material_get_pbr_coat_color_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->pbr.coat_color.texture;
}

ufbx_texture* ufbx_wrapper_material_get_pbr_coat_normal_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->pbr.coat_normal.texture;
}

ufbx_texture* ufbx_wrapper_material_get_pbr_sheen_color_texture(const ufbx_material *material) {
    if (!material) return NULL;
    return material->pbr.sheen_color.texture;
}

const char* ufbx_wrapper_texture_get_name(const ufbx_texture *texture) {
    if (!texture) return "";
    return texture->name.data ? texture->name.data : "";
}

const char* ufbx_wrapper_texture_get_filename(const ufbx_texture *texture) {
    if (!texture) return "";
    return texture->filename.data ? texture->filename.data : "";
}

const char* ufbx_wrapper_texture_get_relative_filename(const ufbx_texture *texture) {
    if (!texture) return "";
    return texture->relative_filename.data ? texture->relative_filename.data : "";
}

const char* ufbx_wrapper_texture_get_absolute_filename(const ufbx_texture *texture) {
    if (!texture) return "";
    return texture->absolute_filename.data ? texture->absolute_filename.data : "";
}

int ufbx_wrapper_texture_get_type(const ufbx_texture *texture) {
    if (!texture) return 0;
    return (int)texture->type;
}

// Light access
size_t ufbx_wrapper_scene_get_num_lights(const ufbx_scene *scene) {
    return scene ? scene->lights.count : 0;
}

ufbx_light* ufbx_wrapper_scene_get_light(const ufbx_scene *scene, size_t index) {
    if (!scene || index >= scene->lights.count) return NULL;
    return scene->lights.data[index];
}

ufbx_light* ufbx_wrapper_node_get_light(const ufbx_node *node) {
    return node ? node->light : NULL;
}

const char* ufbx_wrapper_light_get_name(const ufbx_light *light) {
    if (!light) return "";
    return light->name.data ? light->name.data : "";
}

void ufbx_wrapper_light_get_color(const ufbx_light *light, float *rgb) {
    if (!light || !rgb) return;
    rgb[0] = (float)light->color.x;
    rgb[1] = (float)light->color.y;
    rgb[2] = (float)light->color.z;
}

double ufbx_wrapper_light_get_intensity(const ufbx_light *light) {
    return light ? light->intensity : 0.0;
}

void ufbx_wrapper_light_get_local_direction(const ufbx_light *light, float *xyz) {
    if (!light || !xyz) return;
    xyz[0] = (float)light->local_direction.x;
    xyz[1] = (float)light->local_direction.y;
    xyz[2] = (float)light->local_direction.z;
}

int ufbx_wrapper_light_get_type(const ufbx_light *light) {
    return light ? (int)light->type : 0;
}

int ufbx_wrapper_light_get_decay(const ufbx_light *light) {
    return light ? (int)light->decay : 0;
}

int ufbx_wrapper_light_get_area_shape(const ufbx_light *light) {
    return light ? (int)light->area_shape : 0;
}

double ufbx_wrapper_light_get_inner_angle(const ufbx_light *light) {
    return light ? light->inner_angle : 0.0;
}

double ufbx_wrapper_light_get_outer_angle(const ufbx_light *light) {
    return light ? light->outer_angle : 0.0;
}

bool ufbx_wrapper_light_get_cast_light(const ufbx_light *light) {
    return light ? light->cast_light : false;
}

bool ufbx_wrapper_light_get_cast_shadows(const ufbx_light *light) {
    return light ? light->cast_shadows : false;
}

// Camera access
size_t ufbx_wrapper_scene_get_num_cameras(const ufbx_scene *scene) {
    return scene ? scene->cameras.count : 0;
}

ufbx_camera* ufbx_wrapper_scene_get_camera(const ufbx_scene *scene, size_t index) {
    if (!scene || index >= scene->cameras.count) return NULL;
    return scene->cameras.data[index];
}

ufbx_camera* ufbx_wrapper_node_get_camera(const ufbx_node *node) {
    return node ? node->camera : NULL;
}

const char* ufbx_wrapper_camera_get_name(const ufbx_camera *camera) {
    if (!camera) return "";
    return camera->name.data ? camera->name.data : "";
}

int ufbx_wrapper_camera_get_projection_mode(const ufbx_camera *camera) {
    return camera ? (int)camera->projection_mode : 0;
}

void ufbx_wrapper_camera_get_resolution(const ufbx_camera *camera, float *xy) {
    if (!camera || !xy) return;
    xy[0] = (float)camera->resolution.x;
    xy[1] = (float)camera->resolution.y;
}

bool ufbx_wrapper_camera_get_resolution_is_pixels(const ufbx_camera *camera) {
    return camera ? camera->resolution_is_pixels : false;
}

void ufbx_wrapper_camera_get_field_of_view_deg(const ufbx_camera *camera, float *xy) {
    if (!camera || !xy) return;
    xy[0] = (float)camera->field_of_view_deg.x;
    xy[1] = (float)camera->field_of_view_deg.y;
}

void ufbx_wrapper_camera_get_field_of_view_tan(const ufbx_camera *camera, float *xy) {
    if (!camera || !xy) return;
    xy[0] = (float)camera->field_of_view_tan.x;
    xy[1] = (float)camera->field_of_view_tan.y;
}

double ufbx_wrapper_camera_get_orthographic_extent(const ufbx_camera *camera) {
    return camera ? camera->orthographic_extent : 0.0;
}

void ufbx_wrapper_camera_get_orthographic_size(const ufbx_camera *camera, float *xy) {
    if (!camera || !xy) return;
    xy[0] = (float)camera->orthographic_size.x;
    xy[1] = (float)camera->orthographic_size.y;
}

double ufbx_wrapper_camera_get_aspect_ratio(const ufbx_camera *camera) {
    return camera ? camera->aspect_ratio : 1.0;
}

double ufbx_wrapper_camera_get_near_plane(const ufbx_camera *camera) {
    return camera ? camera->near_plane : 0.0;
}

double ufbx_wrapper_camera_get_far_plane(const ufbx_camera *camera) {
    return camera ? camera->far_plane : 0.0;
}

// Bone access
size_t ufbx_wrapper_scene_get_num_bones(const ufbx_scene *scene) {
    return scene ? scene->bones.count : 0;
}

ufbx_bone* ufbx_wrapper_scene_get_bone(const ufbx_scene *scene, size_t index) {
    if (!scene || index >= scene->bones.count) return NULL;
    return scene->bones.data[index];
}

ufbx_bone* ufbx_wrapper_node_get_bone(const ufbx_node *node) {
    return node ? node->bone : NULL;
}

const char* ufbx_wrapper_bone_get_name(const ufbx_bone *bone) {
    if (!bone) return "";
    return bone->name.data ? bone->name.data : "";
}

double ufbx_wrapper_bone_get_radius(const ufbx_bone *bone) {
    return bone ? bone->radius : 0.0;
}

double ufbx_wrapper_bone_get_relative_length(const ufbx_bone *bone) {
    return bone ? bone->relative_length : 0.0;
}

bool ufbx_wrapper_bone_is_root(const ufbx_bone *bone) {
    return bone ? bone->is_root : false;
}

// Texture access
size_t ufbx_wrapper_scene_get_num_textures(const ufbx_scene *scene) {
    return scene ? scene->textures.count : 0;
}

ufbx_texture* ufbx_wrapper_scene_get_texture(const ufbx_scene *scene, size_t index) {
    if (!scene || index >= scene->textures.count) return NULL;
    return scene->textures.data[index];
}

// AnimStack access
size_t ufbx_wrapper_scene_get_num_anim_stacks(const ufbx_scene *scene) {
    return scene ? scene->anim_stacks.count : 0;
}

ufbx_anim_stack* ufbx_wrapper_scene_get_anim_stack(const ufbx_scene *scene, size_t index) {
    if (!scene || index >= scene->anim_stacks.count) return NULL;
    return scene->anim_stacks.data[index];
}

const char* ufbx_wrapper_anim_stack_get_name(const ufbx_anim_stack *anim_stack) {
    if (!anim_stack) return "";
    return anim_stack->name.data ? anim_stack->name.data : "";
}

double ufbx_wrapper_anim_stack_get_time_begin(const ufbx_anim_stack *anim_stack) {
    return anim_stack ? anim_stack->time_begin : 0.0;
}

double ufbx_wrapper_anim_stack_get_time_end(const ufbx_anim_stack *anim_stack) {
    return anim_stack ? anim_stack->time_end : 0.0;
}

size_t ufbx_wrapper_anim_stack_get_num_layers(const ufbx_anim_stack *anim_stack) {
    return anim_stack ? anim_stack->layers.count : 0;
}

ufbx_anim_layer* ufbx_wrapper_anim_stack_get_layer(const ufbx_anim_stack *anim_stack, size_t index) {
    if (!anim_stack || index >= anim_stack->layers.count) return NULL;
    return anim_stack->layers.data[index];
}

// AnimLayer access
const char* ufbx_wrapper_anim_layer_get_name(const ufbx_anim_layer *anim_layer) {
    if (!anim_layer) return "";
    return anim_layer->name.data ? anim_layer->name.data : "";
}

double ufbx_wrapper_anim_layer_get_weight(const ufbx_anim_layer *anim_layer) {
    return anim_layer ? anim_layer->weight : 0.0;
}

bool ufbx_wrapper_anim_layer_get_weight_is_animated(const ufbx_anim_layer *anim_layer) {
    return anim_layer ? anim_layer->weight_is_animated : false;
}

bool ufbx_wrapper_anim_layer_get_blended(const ufbx_anim_layer *anim_layer) {
    return anim_layer ? anim_layer->blended : false;
}

bool ufbx_wrapper_anim_layer_get_additive(const ufbx_anim_layer *anim_layer) {
    return anim_layer ? anim_layer->additive : false;
}

bool ufbx_wrapper_anim_layer_get_compose_rotation(const ufbx_anim_layer *anim_layer) {
    return anim_layer ? anim_layer->compose_rotation : false;
}

bool ufbx_wrapper_anim_layer_get_compose_scale(const ufbx_anim_layer *anim_layer) {
    return anim_layer ? anim_layer->compose_scale : false;
}

// AnimCurve access
size_t ufbx_wrapper_scene_get_num_anim_curves(const ufbx_scene *scene) {
    return scene ? scene->anim_curves.count : 0;
}

ufbx_anim_curve* ufbx_wrapper_scene_get_anim_curve(const ufbx_scene *scene, size_t index) {
    if (!scene || index >= scene->anim_curves.count) return NULL;
    return scene->anim_curves.data[index];
}

const char* ufbx_wrapper_anim_curve_get_name(const ufbx_anim_curve *anim_curve) {
    if (!anim_curve) return "";
    return anim_curve->name.data ? anim_curve->name.data : "";
}

size_t ufbx_wrapper_anim_curve_get_num_keyframes(const ufbx_anim_curve *anim_curve) {
    return anim_curve ? anim_curve->keyframes.count : 0;
}

double ufbx_wrapper_anim_curve_get_min_value(const ufbx_anim_curve *anim_curve) {
    return anim_curve ? anim_curve->min_value : 0.0;
}

double ufbx_wrapper_anim_curve_get_max_value(const ufbx_anim_curve *anim_curve) {
    return anim_curve ? anim_curve->max_value : 0.0;
}

double ufbx_wrapper_anim_curve_get_min_time(const ufbx_anim_curve *anim_curve) {
    return anim_curve ? anim_curve->min_time : 0.0;
}

double ufbx_wrapper_anim_curve_get_max_time(const ufbx_anim_curve *anim_curve) {
    return anim_curve ? anim_curve->max_time : 0.0;
}

// SkinDeformer access
size_t ufbx_wrapper_scene_get_num_skin_deformers(const ufbx_scene *scene) {
    return scene ? scene->skin_deformers.count : 0;
}

ufbx_skin_deformer* ufbx_wrapper_scene_get_skin_deformer(const ufbx_scene *scene, size_t index) {
    if (!scene || index >= scene->skin_deformers.count) return NULL;
    return scene->skin_deformers.data[index];
}

const char* ufbx_wrapper_skin_deformer_get_name(const ufbx_skin_deformer *skin_deformer) {
    if (!skin_deformer) return "";
    return skin_deformer->name.data ? skin_deformer->name.data : "";
}

size_t ufbx_wrapper_skin_deformer_get_num_clusters(const ufbx_skin_deformer *skin_deformer) {
    return skin_deformer ? skin_deformer->clusters.count : 0;
}

ufbx_skin_cluster* ufbx_wrapper_skin_deformer_get_cluster(const ufbx_skin_deformer *skin_deformer, size_t index) {
    if (!skin_deformer || index >= skin_deformer->clusters.count) return NULL;
    return skin_deformer->clusters.data[index];
}

// SkinCluster access
const char* ufbx_wrapper_skin_cluster_get_name(const ufbx_skin_cluster *skin_cluster) {
    if (!skin_cluster) return "";
    return skin_cluster->name.data ? skin_cluster->name.data : "";
}

const char* ufbx_wrapper_skin_cluster_get_bone_name(const ufbx_skin_cluster *skin_cluster) {
    if (!skin_cluster || !skin_cluster->bone_node) return "";
    return skin_cluster->bone_node->name.data ? skin_cluster->bone_node->name.data : "";
}

size_t ufbx_wrapper_skin_cluster_get_num_weights(const ufbx_skin_cluster *skin_cluster) {
    return skin_cluster ? skin_cluster->num_weights : 0;
}

const uint32_t* ufbx_wrapper_skin_cluster_get_vertices(const ufbx_skin_cluster *skin_cluster, size_t *out_count) {
    if (!skin_cluster || !out_count) {
        if (out_count) *out_count = 0;
        return NULL;
    }
    *out_count = skin_cluster->vertices.count;
    return skin_cluster->vertices.data;
}

const double* ufbx_wrapper_skin_cluster_get_weights(const ufbx_skin_cluster *skin_cluster, size_t *out_count) {
    if (!skin_cluster || !out_count) {
        if (out_count) *out_count = 0;
        return NULL;
    }
    *out_count = skin_cluster->weights.count;
    return skin_cluster->weights.data;
}

// BlendDeformer access
size_t ufbx_wrapper_scene_get_num_blend_deformers(const ufbx_scene *scene) {
    return scene ? scene->blend_deformers.count : 0;
}

ufbx_blend_deformer* ufbx_wrapper_scene_get_blend_deformer(const ufbx_scene *scene, size_t index) {
    if (!scene || index >= scene->blend_deformers.count) return NULL;
    return scene->blend_deformers.data[index];
}

const char* ufbx_wrapper_blend_deformer_get_name(const ufbx_blend_deformer *blend_deformer) {
    if (!blend_deformer) return "";
    return blend_deformer->name.data ? blend_deformer->name.data : "";
}

size_t ufbx_wrapper_blend_deformer_get_num_channels(const ufbx_blend_deformer *blend_deformer) {
    return blend_deformer ? blend_deformer->channels.count : 0;
}

ufbx_blend_channel* ufbx_wrapper_blend_deformer_get_channel(const ufbx_blend_deformer *blend_deformer, size_t index) {
    if (!blend_deformer || index >= blend_deformer->channels.count) return NULL;
    return blend_deformer->channels.data[index];
}

// BlendChannel access
const char* ufbx_wrapper_blend_channel_get_name(const ufbx_blend_channel *blend_channel) {
    if (!blend_channel) return "";
    return blend_channel->name.data ? blend_channel->name.data : "";
}

double ufbx_wrapper_blend_channel_get_weight(const ufbx_blend_channel *blend_channel) {
    return blend_channel ? blend_channel->weight : 0.0;
}

// BlendShape access
size_t ufbx_wrapper_scene_get_num_blend_shapes(const ufbx_scene *scene) {
    return scene ? scene->blend_shapes.count : 0;
}

ufbx_blend_shape* ufbx_wrapper_scene_get_blend_shape(const ufbx_scene *scene, size_t index) {
    if (!scene || index >= scene->blend_shapes.count) return NULL;
    return scene->blend_shapes.data[index];
}

const char* ufbx_wrapper_blend_shape_get_name(const ufbx_blend_shape *blend_shape) {
    if (!blend_shape) return "";
    return blend_shape->name.data ? blend_shape->name.data : "";
}

size_t ufbx_wrapper_blend_shape_get_num_offsets(const ufbx_blend_shape *blend_shape) {
    return blend_shape ? blend_shape->num_offsets : 0;
}

// Constraint access
size_t ufbx_wrapper_scene_get_num_constraints(const ufbx_scene *scene) {
    return scene ? scene->constraints.count : 0;
}

ufbx_constraint* ufbx_wrapper_scene_get_constraint(const ufbx_scene *scene, size_t index) {
    if (!scene || index >= scene->constraints.count) return NULL;
    return scene->constraints.data[index];
}

const char* ufbx_wrapper_constraint_get_name(const ufbx_constraint *constraint) {
    if (!constraint) return "";
    return constraint->name.data ? constraint->name.data : "";
}

int ufbx_wrapper_constraint_get_type(const ufbx_constraint *constraint) {
    return constraint ? (int)constraint->type : 0;
}

double ufbx_wrapper_constraint_get_weight(const ufbx_constraint *constraint) {
    return constraint ? constraint->weight : 0.0;
}

bool ufbx_wrapper_constraint_get_active(const ufbx_constraint *constraint) {
    return constraint ? constraint->active : false;
}
