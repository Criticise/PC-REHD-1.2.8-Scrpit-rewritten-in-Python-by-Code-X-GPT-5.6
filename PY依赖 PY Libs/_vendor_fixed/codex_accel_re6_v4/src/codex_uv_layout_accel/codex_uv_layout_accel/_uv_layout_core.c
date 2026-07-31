#define PY_SSIZE_T_CLEAN
#include <Python.h>


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


static PyObject *clone_uv_pair(PyObject *value)
{
    PyObject *seq = NULL;
    PyObject *item0 = NULL;
    PyObject *item1 = NULL;
    PyObject *out = NULL;
    double uv0 = 0.0;
    double uv1 = 0.0;

    seq = PySequence_Fast(value, "uv pair must be a sequence");
    if (seq == NULL) {
        PyErr_Clear();
        return Py_BuildValue("[dd]", 0.0, 0.0);
    }
    if (PySequence_Fast_GET_SIZE(seq) >= 2) {
        item0 = PySequence_Fast_GET_ITEM(seq, 0);
        item1 = PySequence_Fast_GET_ITEM(seq, 1);
        object_to_double(item0, &uv0);
        object_to_double(item1, &uv1);
    }
    Py_DECREF(seq);
    out = Py_BuildValue("[dd]", uv0, uv1);
    return out;
}


static PyObject *build_layout_split_core(PyObject *self, PyObject *args)
{
    PyObject *geom_face_indices_obj = NULL;
    PyObject *uv_corner_indices_obj = NULL;
    PyObject *prepared_uvs_obj = NULL;
    PyObject *geom_seq = NULL;
    PyObject *uv_seq = NULL;
    PyObject *out_uvs = NULL;
    PyObject *out_source_verts = NULL;
    PyObject *extra_geom_by_tv = NULL;
    PyObject *out_faces = NULL;
    PyObject *result = NULL;
    Py_ssize_t geom_count = 0;
    Py_ssize_t tv_count = 0;
    Py_ssize_t face_offset = 0;
    Py_ssize_t tv_index = 0;
    long source_vertex_count = 0;

    if (!PyArg_ParseTuple(
            args,
            "OOOl",
            &geom_face_indices_obj,
            &uv_corner_indices_obj,
            &prepared_uvs_obj,
            &source_vertex_count)) {
        return NULL;
    }

    geom_seq = PySequence_Fast(geom_face_indices_obj, "geom_face_indices must be a sequence");
    uv_seq = PySequence_Fast(uv_corner_indices_obj, "uv_corner_indices must be a sequence");
    out_uvs = PySequence_List(prepared_uvs_obj);
    if (geom_seq == NULL || uv_seq == NULL || out_uvs == NULL) {
        goto cleanup;
    }

    geom_count = PySequence_Fast_GET_SIZE(geom_seq);
    if (geom_count != PySequence_Fast_GET_SIZE(uv_seq)) {
        PyErr_SetString(PyExc_ValueError, "uv_corner_indices length must match geom_face_indices length");
        goto cleanup;
    }

    tv_count = PyList_GET_SIZE(out_uvs);
    out_source_verts = PyList_New(tv_count);
    extra_geom_by_tv = PyList_New(tv_count);
    out_faces = PyList_New(0);
    if (out_source_verts == NULL || extra_geom_by_tv == NULL || out_faces == NULL) {
        goto cleanup;
    }

    for (tv_index = 0; tv_index < tv_count; ++tv_index) {
        PyObject *dict_obj = PyDict_New();
        if (dict_obj == NULL) {
            goto cleanup;
        }
        Py_INCREF(Py_None);
        PyList_SET_ITEM(out_source_verts, tv_index, Py_None);
        PyList_SET_ITEM(extra_geom_by_tv, tv_index, dict_obj);
    }

    for (face_offset = 0; face_offset + 2 < geom_count; face_offset += 3) {
        int corner_index = 0;
        for (corner_index = 0; corner_index < 3; ++corner_index) {
            Py_ssize_t source_index = face_offset + corner_index;
            PyObject *geom_item = PySequence_Fast_GET_ITEM(geom_seq, source_index);
            PyObject *uv_item = PySequence_Fast_GET_ITEM(uv_seq, source_index);
            long geom_vertex = 0;
            long uv_vertex = -1;
            Py_ssize_t export_index = 0;

            object_to_long(geom_item, &geom_vertex);
            object_to_long(uv_item, &uv_vertex);

            if (geom_vertex < 0 || geom_vertex >= source_vertex_count) {
                geom_vertex = 0;
            }

            if (uv_vertex >= 0 && uv_vertex < tv_count) {
                PyObject *primary_geom = PyList_GET_ITEM(out_source_verts, uv_vertex);
                if (primary_geom == Py_None) {
                    PyObject *geom_value = PyLong_FromLong(geom_vertex);
                    if (geom_value == NULL) {
                        goto cleanup;
                    }
                    if (PyList_SetItem(out_source_verts, uv_vertex, geom_value) != 0) {
                        Py_DECREF(geom_value);
                        goto cleanup;
                    }
                    export_index = uv_vertex;
                } else {
                    long primary_long = 0;
                    object_to_long(primary_geom, &primary_long);
                    if (primary_long == geom_vertex) {
                        export_index = uv_vertex;
                    } else {
                        PyObject *tv_map = PyList_GET_ITEM(extra_geom_by_tv, uv_vertex);
                        PyObject *geom_key = PyLong_FromLong(geom_vertex);
                        PyObject *mapped_index = NULL;
                        if (geom_key == NULL) {
                            goto cleanup;
                        }
                        mapped_index = PyDict_GetItemWithError(tv_map, geom_key);
                        if (mapped_index == NULL && PyErr_Occurred()) {
                            Py_DECREF(geom_key);
                            goto cleanup;
                        }
                        if (mapped_index == NULL) {
                            PyObject *geom_value = NULL;
                            PyObject *uv_clone = NULL;
                            PyObject *export_value = NULL;
                            geom_value = PyLong_FromLong(geom_vertex);
                            if (geom_value == NULL) {
                                Py_DECREF(geom_key);
                                goto cleanup;
                            }
                            if (PyList_Append(out_source_verts, geom_value) != 0) {
                                Py_DECREF(geom_value);
                                Py_DECREF(geom_key);
                                goto cleanup;
                            }
                            Py_DECREF(geom_value);
                            uv_clone = clone_uv_pair(PyList_GET_ITEM(out_uvs, uv_vertex));
                            if (uv_clone == NULL) {
                                Py_DECREF(geom_key);
                                goto cleanup;
                            }
                            if (PyList_Append(out_uvs, uv_clone) != 0) {
                                Py_DECREF(uv_clone);
                                Py_DECREF(geom_key);
                                goto cleanup;
                            }
                            Py_DECREF(uv_clone);
                            export_index = PyList_GET_SIZE(out_source_verts) - 1;
                            export_value = PyLong_FromSsize_t(export_index);
                            if (export_value == NULL) {
                                Py_DECREF(geom_key);
                                goto cleanup;
                            }
                            if (PyDict_SetItem(tv_map, geom_key, export_value) != 0) {
                                Py_DECREF(export_value);
                                Py_DECREF(geom_key);
                                goto cleanup;
                            }
                            Py_DECREF(export_value);
                        } else {
                            export_index = PyLong_AsSsize_t(mapped_index);
                            if (PyErr_Occurred()) {
                                Py_DECREF(geom_key);
                                goto cleanup;
                            }
                        }
                        Py_DECREF(geom_key);
                    }
                }
            } else {
                PyObject *uv_clone = NULL;
                PyObject *geom_value = PyLong_FromLong(geom_vertex);
                if (geom_value == NULL) {
                    goto cleanup;
                }
                if (PyList_Append(out_source_verts, geom_value) != 0) {
                    Py_DECREF(geom_value);
                    goto cleanup;
                }
                Py_DECREF(geom_value);
                if (geom_vertex >= 0 && geom_vertex < PyList_GET_SIZE(out_uvs)) {
                    uv_clone = clone_uv_pair(PyList_GET_ITEM(out_uvs, geom_vertex));
                } else {
                    uv_clone = Py_BuildValue("[dd]", 0.0, 0.0);
                }
                if (uv_clone == NULL) {
                    goto cleanup;
                }
                if (PyList_Append(out_uvs, uv_clone) != 0) {
                    Py_DECREF(uv_clone);
                    goto cleanup;
                }
                Py_DECREF(uv_clone);
                export_index = PyList_GET_SIZE(out_source_verts) - 1;
            }

            {
                PyObject *export_value = PyLong_FromSsize_t(export_index);
                if (export_value == NULL) {
                    goto cleanup;
                }
                if (PyList_Append(out_faces, export_value) != 0) {
                    Py_DECREF(export_value);
                    goto cleanup;
                }
                Py_DECREF(export_value);
            }
        }
    }

    for (tv_index = 0; tv_index < tv_count; ++tv_index) {
        PyObject *current = PyList_GET_ITEM(out_source_verts, tv_index);
        if (current == Py_None) {
            long fallback_value = (tv_index < source_vertex_count) ? (long)tv_index : 0L;
            PyObject *replacement = PyLong_FromLong(fallback_value);
            if (replacement == NULL) {
                goto cleanup;
            }
            if (PyList_SetItem(out_source_verts, tv_index, replacement) != 0) {
                Py_DECREF(replacement);
                goto cleanup;
            }
        }
    }

    result = Py_BuildValue(
        "{s:O,s:O,s:O}",
        "source_vertex_indices",
        out_source_verts,
        "uvs",
        out_uvs,
        "face_indices",
        out_faces);

cleanup:
    Py_XDECREF(geom_seq);
    Py_XDECREF(uv_seq);
    Py_XDECREF(out_uvs);
    Py_XDECREF(out_source_verts);
    Py_XDECREF(extra_geom_by_tv);
    Py_XDECREF(out_faces);
    return result;
}


static PyMethodDef module_methods[] = {
    {
        "build_layout_split_core",
        build_layout_split_core,
        METH_VARARGS,
        "Split FBX UV layout rows into export vertices.",
    },
    {NULL, NULL, 0, NULL},
};


static struct PyModuleDef module_def = {
    PyModuleDef_HEAD_INIT,
    "_uv_layout_core",
    "Codex UV layout accelerator core.",
    -1,
    module_methods,
};


PyMODINIT_FUNC PyInit__uv_layout_core(void)
{
    return PyModule_Create(&module_def);
}
