"""
ufbx-python setup script - Cython implementation
"""

import os

import numpy as np
from Cython.Build import cythonize
from setuptools import Extension, find_packages, setup


# Read version
def get_version():
    version_file = os.path.join(os.path.dirname(__file__), "ufbx", "__init__.py")
    with open(version_file) as f:
        for line in f:
            if line.startswith("__version__"):
                return line.split("=")[1].strip().strip("'\"")
    return "0.1.0"


# Read long description
def get_long_description():
    readme_file = os.path.join(os.path.dirname(__file__), "README.md")
    if os.path.exists(readme_file):
        with open(readme_file, encoding="utf-8") as f:
            return f.read()
    return ""


# Define Cython extension
extensions = [
    Extension(
        "ufbx._ufbx",
        sources=[
            "ufbx/_ufbx.pyx",
            "ufbx/src/ufbx_wrapper.c",
            "ufbx-c/ufbx.c",
        ],
        include_dirs=[
            "ufbx",
            "ufbx/src",
            ".",
            np.get_include(),
        ],
        define_macros=[("NPY_NO_DEPRECATED_API", "NPY_1_7_API_VERSION")],
        extra_compile_args=["-O3"],
    )
]

setup(
    name="pyufbx",
    version=get_version(),
    description="Python bindings for ufbx - Single source file FBX loader",
    long_description=get_long_description(),
    long_description_content_type="text/markdown",
    author="ufbx-python contributors",
    url="https://github.com/popomore/ufbx-python",
    project_urls={
        "Bug Reports": "https://github.com/popomore/ufbx-python/issues",
        "Source": "https://github.com/popomore/ufbx-python",
        "Documentation": "https://github.com/popomore/ufbx-python#readme",
    },
    packages=find_packages(exclude=["tests", "tests.*", "examples", "bindgen"]),
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            "language_level": 3,
            "embedsignature": True,
        },
    ),
    install_requires=["numpy"],
    python_requires=">=3.9",
    keywords="fbx 3d graphics modeling autodesk loader cython",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: C",
        "Programming Language :: Cython",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: Implementation :: CPython",
        "Topic :: Multimedia :: Graphics :: 3D Modeling",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    zip_safe=False,
)
