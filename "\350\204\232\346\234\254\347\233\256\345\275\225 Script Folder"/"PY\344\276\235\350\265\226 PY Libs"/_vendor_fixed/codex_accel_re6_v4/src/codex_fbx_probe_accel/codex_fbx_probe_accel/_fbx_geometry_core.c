#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>


static double round6(double value)
{
    if (value >= 0.0) {
        return floor((value * 1000000.0) + 0.5) / 1000000.0;
    }
    return ceil((value * 1000000.0) - 0.5) / 1000000.0;
}


static int object_to_long(PyObject *value, long *out_value)
{
    PyObject *long_obj = PyNumber_Long(value);
    long converted = 0;
    if (long_obj == NULL) {
        PyErr_Clear();
        return 0;
    }
    converted = PyLong_AsLong(long_obj);
    Py_DECREF(long_obj);
    if (PyErr_Occurred()) {
        PyErr_Clear();
        return 0;
    }
    *out_value = converted;
    return 1;
}


static int object_to_double(PyObject *value, double *out_value)
{
    double converted = PyFloat_AsDouble(value);
    if (PyErr_Occurred()) {
        PyErr_Clear();
        return 0;
    }
    *out_value = converted;
    return 1;
}


static int read_vec2(PyObject *value, double out_vec[2])
{
    PyObject *seq = PySequence_Fast(value, "vec2 must be a sequence");
    if (seq == NULL) {
        PyErr_Clear();
        return 0;
    }
    if (PySequence_Fast_GET_SIZE(seq) < 2) {
        Py_DECREF(seq);
        return 0;
    }
    object_to_double(PySequence_Fast_GET_ITEM(seq, 0), &out_vec[0]);
    object_to_double(PySequence_Fast_GET_ITEM(seq, 1), &out_vec[1]);
    Py_DECREF(seq);
    return 1;
}


static int read_vec3(PyObject *value, double out_vec[3])
{
    PyObject *seq = PySequence_Fast(value, "vec3 must be a sequence");
    if (seq == NULL) {
        PyErr_Clear();
        return 0;
    }
    if (PySequence_Fast_GET_SIZE(seq) < 3) {
        Py_DECREF(seq);
        return 0;
    }
    object_to_double(PySequence_Fast_GET_ITEM(seq, 0), &out_vec[0]);
    object_to_double(PySequence_Fast_GET_ITEM(seq, 1), &out_vec[1]);
    object_to_double(PySequence_Fast_GET_ITEM(seq, 2), &out_vec[2]);
    Py_DECREF(seq);
    return 1;
}


static void normalize_vec3(double vec[3], const double fallback[3])
{
    double length = sqrt((vec[0] * vec[0]) + (vec[1] * vec[1]) + (vec[2] * vec[2]));
    if (length <= 0.000001) {
        vec[0] = fallback[0];
        vec[1] = fallback[1];
        vec[2] = fallback[2];
        return;
    }
    vec[0] = vec[0] / length;
    vec[1] = vec[1] / length;
    vec[2] = vec[2] / length;
}


static void transform_position_row_major(const double in_vec[3], const double matrix[16], int has_matrix, double out_vec[3])
{
    double x = in_vec[0];
    double y = in_vec[1];
    double z = in_vec[2];
    if (!has_matrix) {
        out_vec[0] = x;
        out_vec[1] = y;
        out_vec[2] = z;
        return;
    }
    out_vec[0] = round6((x * matrix[0]) + (y * matrix[4]) + (z * matrix[8]) + matrix[12]);
    out_vec[1] = round6((x * matrix[1]) + (y * matrix[5]) + (z * matrix[9]) + matrix[13]);
    out_vec[2] = round6((x * matrix[2]) + (y * matrix[6]) + (z * matrix[10]) + matrix[14]);
}


static void transform_direction_row_major(const double in_vec[3], const double matrix[16], int has_matrix, double out_vec[3])
{
    const double fallback[3] = {0.0, 0.0, 1.0};
    double x = in_vec[0];
    double y = in_vec[1];
    double z = in_vec[2];
    if (!has_matrix) {
        out_vec[0] = x;
        out_vec[1] = y;
        out_vec[2] = z;
        normalize_vec3(out_vec, fallback);
        return;
    }
    out_vec[0] = (x * matrix[0]) + (y * matrix[4]) + (z * matrix[8]);
    out_vec[1] = (x * matrix[1]) + (y * matrix[5]) + (z * matrix[9]);
    out_vec[2] = (x * matrix[2]) + (y * matrix[6]) + (z * matrix[10]);
    normalize_vec3(out_vec, fallback);
}


static void transform_normal_row_major(const double in_vec[3], const double matrix[16], int has_matrix, double out_vec[3])
{
    const double fallback[3] = {0.0, 0.0, 1.0};
    double a00, a01, a02, a10, a11, a12, a20, a21, a22;
    double c00, c01, c02, determinant, inverse_det;
    double inverse[9];
    double x = in_vec[0];
    double y = in_vec[1];
    double z = in_vec[2];

    if (!has_matrix) {
        out_vec[0] = x;
        out_vec[1] = y;
        out_vec[2] = z;
        normalize_vec3(out_vec, fallback);
        return;
    }

    a00 = matrix[0];
    a01 = matrix[1];
    a02 = matrix[2];
    a10 = matrix[4];
    a11 = matrix[5];
    a12 = matrix[6];
    a20 = matrix[8];
    a21 = matrix[9];
    a22 = matrix[10];
    c00 = (a11 * a22) - (a12 * a21);
    c01 = (a12 * a20) - (a10 * a22);
    c02 = (a10 * a21) - (a11 * a20);
    determinant = (a00 * c00) + (a01 * c01) + (a02 * c02);
    if (fabs(determinant) <= 0.000000000001) {
        transform_direction_row_major(in_vec, matrix, has_matrix, out_vec);
        return;
    }

    inverse_det = 1.0 / determinant;
    inverse[0] = c00 * inverse_det;
    inverse[1] = ((a02 * a21) - (a01 * a22)) * inverse_det;
    inverse[2] = ((a01 * a12) - (a02 * a11)) * inverse_det;
    inverse[3] = c01 * inverse_det;
    inverse[4] = ((a00 * a22) - (a02 * a20)) * inverse_det;
    inverse[5] = ((a02 * a10) - (a00 * a12)) * inverse_det;
    inverse[6] = c02 * inverse_det;
    inverse[7] = ((a01 * a20) - (a00 * a21)) * inverse_det;
    inverse[8] = ((a00 * a11) - (a01 * a10)) * inverse_det;

    out_vec[0] = (inverse[0] * x) + (inverse[1] * y) + (inverse[2] * z);
    out_vec[1] = (inverse[3] * x) + (inverse[4] * y) + (inverse[5] * z);
    out_vec[2] = (inverse[6] * x) + (inverse[7] * y) + (inverse[8] * z);
    normalize_vec3(out_vec, fallback);
}


static void fbx_world_to_max_vec3(const double in_vec[3], double out_vec[3])
{
    out_vec[0] = round6(in_vec[0]);
    out_vec[1] = round6(-in_vec[2]);
    out_vec[2] = round6(in_vec[1]);
}


static void fbx_world_to_max_normal(const double in_vec[3], double out_vec[3])
{
    const double fallback[3] = {0.0, 0.0, 1.0};
    out_vec[0] = in_vec[0];
    out_vec[1] = -in_vec[2];
    out_vec[2] = in_vec[1];
    normalize_vec3(out_vec, fallback);
}


static PyObject *build_vec2_object(const double vec[2])
{
    return Py_BuildValue("[dd]", round6(vec[0]), round6(vec[1]));
}


static PyObject *build_vec3_object(const double vec[3])
{
    return Py_BuildValue("[ddd]", round6(vec[0]), round6(vec[1]), round6(vec[2]));
}


static PyObject *build_normal_vec3_object(const double vec[3])
{
    return Py_BuildValue("[ddd]", vec[0], vec[1], vec[2]);
}


static PyObject *clone_object_vec2(PyObject *value)
{
    double vec[2] = {0.0, 0.0};
    read_vec2(value, vec);
    return build_vec2_object(vec);
}


static PyObject *clone_object_vec3(PyObject *value, const double fallback[3])
{
    double vec[3] = {fallback[0], fallback[1], fallback[2]};
    read_vec3(value, vec);
    return build_vec3_object(vec);
}


static int append_object(PyObject *list_obj, PyObject *value)
{
    int rc = PyList_Append(list_obj, value);
    Py_DECREF(value);
    return rc;
}


static PyObject *extract_geometry_core(PyObject *self, PyObject *args)
{
    PyObject *positions_obj = NULL;
    PyObject *source_normals_obj = NULL;
    PyObject *geom_indices_obj = NULL;
    PyObject *faces_obj = NULL;
    PyObject *uv_channels_obj = NULL;
    PyObject *matrix_obj = NULL;
    PyObject *positions_seq = NULL;
    PyObject *normals_seq = NULL;
    PyObject *geom_seq = NULL;
    PyObject *faces_seq = NULL;
    PyObject *uv_channels_seq = NULL;
    PyObject *vertex_map = NULL;
    PyObject *out_positions = NULL;
    PyObject *out_max_positions = NULL;
    PyObject *out_world_positions = NULL;
    PyObject *out_normals = NULL;
    PyObject *out_max_normals = NULL;
    PyObject *out_uvs = NULL;
    PyObject *out_face_indices = NULL;
    PyObject *out_source_vertex_indices = NULL;
    PyObject *out_geom_face_indices = NULL;
    PyObject *out_uv_channel_payloads = NULL;
    PyObject *result = NULL;
    PyObject **source_corner_lists = NULL;
    PyObject **payload_corner_lists = NULL;
    PyObject **channel_values_lists = NULL;
    Py_ssize_t channel_count = 0;
    Py_ssize_t geom_count = 0;
    Py_ssize_t positions_count = 0;
    Py_ssize_t normals_count = 0;
    long vertex_count = 0;
    double matrix[16] = {0.0};
    int has_matrix = 0;
    Py_ssize_t channel_index = 0;

    if (!PyArg_ParseTuple(
            args,
            "OOOOOlO",
            &positions_obj,
            &source_normals_obj,
            &geom_indices_obj,
            &faces_obj,
            &uv_channels_obj,
            &vertex_count,
            &matrix_obj)) {
        return NULL;
    }

    positions_seq = PySequence_Fast(positions_obj, "positions must be a sequence");
    normals_seq = PySequence_Fast(source_normals_obj, "source_normals must be a sequence");
    geom_seq = PySequence_Fast(geom_indices_obj, "geom_indices must be a sequence");
    faces_seq = PySequence_Fast(faces_obj, "faces must be a sequence");
    uv_channels_seq = PySequence_Fast(uv_channels_obj, "uv_channels must be a sequence");
    if (positions_seq == NULL || normals_seq == NULL || geom_seq == NULL || faces_seq == NULL || uv_channels_seq == NULL) {
        goto cleanup;
    }

    positions_count = PySequence_Fast_GET_SIZE(positions_seq);
    normals_count = PySequence_Fast_GET_SIZE(normals_seq);
    geom_count = PySequence_Fast_GET_SIZE(geom_seq);
    channel_count = PySequence_Fast_GET_SIZE(uv_channels_seq);
    if (channel_count <= 0) {
        PyErr_SetString(PyExc_ValueError, "uv_channels must not be empty");
        goto cleanup;
    }

    if (matrix_obj != NULL && matrix_obj != Py_None) {
        PyObject *matrix_seq = PySequence_Fast(matrix_obj, "matrix must be a sequence");
        if (matrix_seq != NULL) {
            if (PySequence_Fast_GET_SIZE(matrix_seq) >= 16) {
                Py_ssize_t matrix_index = 0;
                has_matrix = 1;
                for (matrix_index = 0; matrix_index < 16; ++matrix_index) {
                    object_to_double(PySequence_Fast_GET_ITEM(matrix_seq, matrix_index), &matrix[matrix_index]);
                }
            }
            Py_DECREF(matrix_seq);
        } else {
            PyErr_Clear();
        }
    }

    source_corner_lists = PyMem_Calloc((size_t)channel_count, sizeof(PyObject *));
    payload_corner_lists = PyMem_Calloc((size_t)channel_count, sizeof(PyObject *));
    channel_values_lists = PyMem_Calloc((size_t)channel_count, sizeof(PyObject *));
    if (source_corner_lists == NULL || payload_corner_lists == NULL || channel_values_lists == NULL) {
        PyErr_NoMemory();
        goto cleanup;
    }

    out_uv_channel_payloads = PyList_New(channel_count);
    if (out_uv_channel_payloads == NULL) {
        goto cleanup;
    }

    for (channel_index = 0; channel_index < channel_count; ++channel_index) {
        PyObject *channel_dict = PySequence_Fast_GET_ITEM(uv_channels_seq, channel_index);
        PyObject *values_obj = NULL;
        PyObject *corner_indices_list = NULL;
        PyObject *payload_dict = NULL;
        PyObject *payload_corner_list = NULL;
        PyObject *name_obj = NULL;
        PyObject *channel_number_obj = NULL;
        long channel_number = (long)(channel_index + 1);

        if (!PyDict_Check(channel_dict)) {
            PyErr_SetString(PyExc_ValueError, "uv channel entries must be dict objects");
            goto cleanup;
        }
        values_obj = PyDict_GetItemString(channel_dict, "values");
        corner_indices_list = PyDict_GetItemString(channel_dict, "corner_indices");
        if (!PyList_Check(values_obj) || !PyList_Check(corner_indices_list)) {
            PyErr_SetString(PyExc_ValueError, "uv channel values and corner_indices must be lists");
            goto cleanup;
        }
        if (PyList_GET_SIZE(corner_indices_list) != geom_count) {
            PyErr_SetString(PyExc_ValueError, "uv channel corner_indices length must match geom_indices length");
            goto cleanup;
        }
        source_corner_lists[channel_index] = corner_indices_list;
        channel_values_lists[channel_index] = values_obj;

        payload_dict = PyDict_New();
        payload_corner_list = PyList_New(0);
        if (payload_dict == NULL || payload_corner_list == NULL) {
            Py_XDECREF(payload_dict);
            Py_XDECREF(payload_corner_list);
            goto cleanup;
        }

        channel_number_obj = PyDict_GetItemString(channel_dict, "channel");
        if (channel_number_obj != NULL) {
            object_to_long(channel_number_obj, &channel_number);
        }
        name_obj = PyDict_GetItemString(channel_dict, "name");
        if (name_obj == NULL) {
            name_obj = PyUnicode_FromString("");
        } else {
            Py_INCREF(name_obj);
        }
        if (name_obj == NULL) {
            Py_DECREF(payload_dict);
            Py_DECREF(payload_corner_list);
            goto cleanup;
        }
        if (PyDict_SetItemString(payload_dict, "channel", PyLong_FromLong(channel_number)) != 0 ||
            PyDict_SetItemString(payload_dict, "name", name_obj) != 0 ||
            PyDict_SetItemString(payload_dict, "values", values_obj) != 0 ||
            PyDict_SetItemString(payload_dict, "corner_indices", payload_corner_list) != 0) {
            Py_DECREF(name_obj);
            Py_DECREF(payload_dict);
            Py_DECREF(payload_corner_list);
            goto cleanup;
        }
        Py_DECREF(name_obj);
        Py_DECREF(payload_corner_list);
        payload_corner_lists[channel_index] = PyDict_GetItemString(payload_dict, "corner_indices");
        PyList_SET_ITEM(out_uv_channel_payloads, channel_index, payload_dict);
    }

    vertex_map = PyDict_New();
    out_positions = PyList_New(0);
    out_max_positions = PyList_New(0);
    out_world_positions = PyList_New(0);
    out_normals = PyList_New(0);
    out_max_normals = PyList_New(0);
    out_uvs = PyList_New(0);
    out_face_indices = PyList_New(0);
    out_source_vertex_indices = PyList_New(0);
    out_geom_face_indices = PyList_New(0);
    if (vertex_map == NULL || out_positions == NULL || out_max_positions == NULL || out_world_positions == NULL ||
        out_normals == NULL || out_max_normals == NULL || out_uvs == NULL || out_face_indices == NULL ||
        out_source_vertex_indices == NULL || out_geom_face_indices == NULL) {
        goto cleanup;
    }

    {
        Py_ssize_t face_index = 0;
        for (face_index = 0; face_index < PySequence_Fast_GET_SIZE(faces_seq); ++face_index) {
            PyObject *face_row = PySequence_Fast_GET_ITEM(faces_seq, face_index);
            PyObject *face_row_seq = PySequence_Fast(face_row, "face row must be a sequence");
            long face_begin = 0;
            long face_size = 0;
            long *face_vertex_ids = NULL;
            long *face_geom_vertex_ids = NULL;
            long **face_uv_channel_ids = NULL;
            long used_count = 0;
            long local_offset = 0;

            if (face_row_seq == NULL) {
                goto cleanup;
            }
            if (PySequence_Fast_GET_SIZE(face_row_seq) >= 2) {
                object_to_long(PySequence_Fast_GET_ITEM(face_row_seq, 0), &face_begin);
                object_to_long(PySequence_Fast_GET_ITEM(face_row_seq, 1), &face_size);
            }
            Py_DECREF(face_row_seq);

            if (face_size <= 0) {
                continue;
            }

            face_vertex_ids = PyMem_Calloc((size_t)face_size, sizeof(long));
            face_geom_vertex_ids = PyMem_Calloc((size_t)face_size, sizeof(long));
            face_uv_channel_ids = PyMem_Calloc((size_t)channel_count, sizeof(long *));
            if (face_vertex_ids == NULL || face_geom_vertex_ids == NULL || face_uv_channel_ids == NULL) {
                PyMem_Free(face_vertex_ids);
                PyMem_Free(face_geom_vertex_ids);
                PyMem_Free(face_uv_channel_ids);
                PyErr_NoMemory();
                goto cleanup;
            }
            for (channel_index = 0; channel_index < channel_count; ++channel_index) {
                face_uv_channel_ids[channel_index] = PyMem_Calloc((size_t)face_size, sizeof(long));
                if (face_uv_channel_ids[channel_index] == NULL) {
                    PyErr_NoMemory();
                    for (channel_index = 0; channel_index < channel_count; ++channel_index) {
                        PyMem_Free(face_uv_channel_ids[channel_index]);
                    }
                    PyMem_Free(face_uv_channel_ids);
                    PyMem_Free(face_vertex_ids);
                    PyMem_Free(face_geom_vertex_ids);
                    goto cleanup;
                }
            }

            for (local_offset = 0; local_offset < face_size; ++local_offset) {
                long corner_index = face_begin + local_offset;
                long position_index = 0;
                long default_uv_index = 0;
                PyObject *default_uv_obj = NULL;
                PyObject *key_obj = NULL;
                PyObject *mapped_value = NULL;
                long vertex_id = 0;

                if (corner_index < 0 || corner_index >= geom_count) {
                    continue;
                }
                object_to_long(PySequence_Fast_GET_ITEM(geom_seq, corner_index), &position_index);
                object_to_long(PyList_GET_ITEM(source_corner_lists[0], corner_index), &default_uv_index);
                if (default_uv_index < 0 || default_uv_index >= PyList_GET_SIZE(channel_values_lists[0])) {
                    continue;
                }
                default_uv_obj = PyList_GET_ITEM(channel_values_lists[0], default_uv_index);

                for (channel_index = 0; channel_index < channel_count; ++channel_index) {
                    long raw_uv_index = 0;
                    object_to_long(PyList_GET_ITEM(source_corner_lists[channel_index], corner_index), &raw_uv_index);
                    face_uv_channel_ids[channel_index][used_count] = raw_uv_index;
                }

                key_obj = Py_BuildValue("(ll)", position_index, default_uv_index);
                if (key_obj == NULL) {
                    for (channel_index = 0; channel_index < channel_count; ++channel_index) {
                        PyMem_Free(face_uv_channel_ids[channel_index]);
                    }
                    PyMem_Free(face_uv_channel_ids);
                    PyMem_Free(face_vertex_ids);
                    PyMem_Free(face_geom_vertex_ids);
                    goto cleanup;
                }
                mapped_value = PyDict_GetItemWithError(vertex_map, key_obj);
                if (mapped_value == NULL && PyErr_Occurred()) {
                    Py_DECREF(key_obj);
                    for (channel_index = 0; channel_index < channel_count; ++channel_index) {
                        PyMem_Free(face_uv_channel_ids[channel_index]);
                    }
                    PyMem_Free(face_uv_channel_ids);
                    PyMem_Free(face_vertex_ids);
                    PyMem_Free(face_geom_vertex_ids);
                    goto cleanup;
                }

                if (mapped_value == NULL) {
                    double local_pos[3] = {0.0, 0.0, 0.0};
                    double world_pos[3] = {0.0, 0.0, 0.0};
                    double max_pos[3] = {0.0, 0.0, 0.0};
                    double local_normal[3] = {0.0, 0.0, 1.0};
                    double max_normal[3] = {0.0, 0.0, 1.0};
                    double transformed_normal[3] = {0.0, 0.0, 1.0};
                    PyObject *dict_value = NULL;

                    vertex_id = (long)PyList_GET_SIZE(out_positions);
                    if (position_index >= 0 && position_index < positions_count) {
                        read_vec3(PySequence_Fast_GET_ITEM(positions_seq, position_index), local_pos);
                        transform_position_row_major(local_pos, matrix, has_matrix, world_pos);
                        fbx_world_to_max_vec3(world_pos, max_pos);
                    }
                    if (position_index >= 0 && position_index < normals_count) {
                        read_vec3(PySequence_Fast_GET_ITEM(normals_seq, position_index), local_normal);
                        transform_normal_row_major(local_normal, matrix, has_matrix, transformed_normal);
                        fbx_world_to_max_normal(transformed_normal, max_normal);
                    }

                    if (append_object(out_positions, build_vec3_object(local_pos)) != 0 ||
                        append_object(out_world_positions, build_vec3_object(max_pos)) != 0 ||
                        append_object(out_max_positions, build_vec3_object(max_pos)) != 0 ||
                        append_object(out_normals, build_normal_vec3_object(local_normal)) != 0 ||
                        append_object(out_max_normals, build_normal_vec3_object(max_normal)) != 0 ||
                        append_object(out_uvs, clone_object_vec2(default_uv_obj)) != 0 ||
                        append_object(out_source_vertex_indices, PyLong_FromLong(position_index)) != 0) {
                        Py_DECREF(key_obj);
                        for (channel_index = 0; channel_index < channel_count; ++channel_index) {
                            PyMem_Free(face_uv_channel_ids[channel_index]);
                        }
                        PyMem_Free(face_uv_channel_ids);
                        PyMem_Free(face_vertex_ids);
                        PyMem_Free(face_geom_vertex_ids);
                        goto cleanup;
                    }

                    dict_value = PyLong_FromLong(vertex_id);
                    if (dict_value == NULL || PyDict_SetItem(vertex_map, key_obj, dict_value) != 0) {
                        Py_XDECREF(dict_value);
                        Py_DECREF(key_obj);
                        for (channel_index = 0; channel_index < channel_count; ++channel_index) {
                            PyMem_Free(face_uv_channel_ids[channel_index]);
                        }
                        PyMem_Free(face_uv_channel_ids);
                        PyMem_Free(face_vertex_ids);
                        PyMem_Free(face_geom_vertex_ids);
                        goto cleanup;
                    }
                    Py_DECREF(dict_value);
                } else {
                    vertex_id = PyLong_AsLong(mapped_value);
                    if (PyErr_Occurred()) {
                        Py_DECREF(key_obj);
                        for (channel_index = 0; channel_index < channel_count; ++channel_index) {
                            PyMem_Free(face_uv_channel_ids[channel_index]);
                        }
                        PyMem_Free(face_uv_channel_ids);
                        PyMem_Free(face_vertex_ids);
                        PyMem_Free(face_geom_vertex_ids);
                        goto cleanup;
                    }
                }
                Py_DECREF(key_obj);

                face_vertex_ids[used_count] = vertex_id;
                face_geom_vertex_ids[used_count] = position_index;
                used_count += 1;
            }

            if (used_count >= 3) {
                long tri_offset = 0;
                for (tri_offset = 1; tri_offset < (used_count - 1); ++tri_offset) {
                    if (append_object(out_face_indices, PyLong_FromLong(face_vertex_ids[0])) != 0 ||
                        append_object(out_face_indices, PyLong_FromLong(face_vertex_ids[tri_offset])) != 0 ||
                        append_object(out_face_indices, PyLong_FromLong(face_vertex_ids[tri_offset + 1])) != 0 ||
                        append_object(out_geom_face_indices, PyLong_FromLong(face_geom_vertex_ids[0])) != 0 ||
                        append_object(out_geom_face_indices, PyLong_FromLong(face_geom_vertex_ids[tri_offset])) != 0 ||
                        append_object(out_geom_face_indices, PyLong_FromLong(face_geom_vertex_ids[tri_offset + 1])) != 0) {
                        for (channel_index = 0; channel_index < channel_count; ++channel_index) {
                            PyMem_Free(face_uv_channel_ids[channel_index]);
                        }
                        PyMem_Free(face_uv_channel_ids);
                        PyMem_Free(face_vertex_ids);
                        PyMem_Free(face_geom_vertex_ids);
                        goto cleanup;
                    }
                    for (channel_index = 0; channel_index < channel_count; ++channel_index) {
                        if (append_object(payload_corner_lists[channel_index], PyLong_FromLong(face_uv_channel_ids[channel_index][0])) != 0 ||
                            append_object(payload_corner_lists[channel_index], PyLong_FromLong(face_uv_channel_ids[channel_index][tri_offset])) != 0 ||
                            append_object(payload_corner_lists[channel_index], PyLong_FromLong(face_uv_channel_ids[channel_index][tri_offset + 1])) != 0) {
                            for (channel_index = 0; channel_index < channel_count; ++channel_index) {
                                PyMem_Free(face_uv_channel_ids[channel_index]);
                            }
                            PyMem_Free(face_uv_channel_ids);
                            PyMem_Free(face_vertex_ids);
                            PyMem_Free(face_geom_vertex_ids);
                            goto cleanup;
                        }
                    }
                }
            }

            for (channel_index = 0; channel_index < channel_count; ++channel_index) {
                PyMem_Free(face_uv_channel_ids[channel_index]);
            }
            PyMem_Free(face_uv_channel_ids);
            PyMem_Free(face_vertex_ids);
            PyMem_Free(face_geom_vertex_ids);
        }
    }

    result = Py_BuildValue(
        "{s:O,s:O,s:O,s:O,s:O,s:O,s:O,s:O,s:O,s:O,s:n,s:n,s:n}",
        "positions",
        out_positions,
        "max_positions",
        out_max_positions,
        "world_positions",
        out_world_positions,
        "normals",
        out_normals,
        "max_normals",
        out_max_normals,
        "uvs",
        out_uvs,
        "face_indices",
        out_face_indices,
        "source_vertex_indices",
        out_source_vertex_indices,
        "fbx_geom_face_indices",
        out_geom_face_indices,
        "fbx_uv_channels",
        out_uv_channel_payloads,
        "vertex_count",
        PyList_GET_SIZE(out_positions),
        "index_count",
        PyList_GET_SIZE(out_face_indices),
        "triangle_count",
        PyList_GET_SIZE(out_face_indices) / 3);

cleanup:
    Py_XDECREF(positions_seq);
    Py_XDECREF(normals_seq);
    Py_XDECREF(geom_seq);
    Py_XDECREF(faces_seq);
    Py_XDECREF(uv_channels_seq);
    Py_XDECREF(vertex_map);
    Py_XDECREF(out_positions);
    Py_XDECREF(out_max_positions);
    Py_XDECREF(out_world_positions);
    Py_XDECREF(out_normals);
    Py_XDECREF(out_max_normals);
    Py_XDECREF(out_uvs);
    Py_XDECREF(out_face_indices);
    Py_XDECREF(out_source_vertex_indices);
    Py_XDECREF(out_geom_face_indices);
    Py_XDECREF(out_uv_channel_payloads);
    PyMem_Free(source_corner_lists);
    PyMem_Free(payload_corner_lists);
    PyMem_Free(channel_values_lists);
    return result;
}


static PyMethodDef module_methods[] = {
    {
        "extract_geometry_core",
        extract_geometry_core,
        METH_VARARGS,
        "Extract normalized FBX mesh geometry for the Codex bridge.",
    },
    {NULL, NULL, 0, NULL},
};


static struct PyModuleDef module_def = {
    PyModuleDef_HEAD_INIT,
    "_fbx_geometry_core",
    "Codex FBX probe accelerator core.",
    -1,
    module_methods,
};


PyMODINIT_FUNC PyInit__fbx_geometry_core(void)
{
    return PyModule_Create(&module_def);
}
