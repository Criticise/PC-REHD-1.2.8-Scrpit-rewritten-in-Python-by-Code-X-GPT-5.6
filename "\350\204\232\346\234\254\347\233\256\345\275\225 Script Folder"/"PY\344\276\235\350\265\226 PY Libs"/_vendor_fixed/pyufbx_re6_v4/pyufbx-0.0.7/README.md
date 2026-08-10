# ufbx-python

[![PyPI version](https://badge.fury.io/py/pyufbx.svg)](https://badge.fury.io/py/pyufbx)
[![Tests](https://github.com/popomore/ufbx-python/workflows/Tests/badge.svg)](https://github.com/popomore/ufbx-python/actions/workflows/test.yml)
[![Python Versions](https://img.shields.io/pypi/pyversions/pyufbx.svg)](https://pypi.org/project/pyufbx/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Python bindings for [ufbx](https://github.com/ufbx/ufbx) - a single source file FBX loader library.

## Status

🚧 **In Progress** - Core API is available, broader coverage is under active development.

Currently implemented:
- Core scene loading and querying (Scene, Node, Mesh, Material)
- Zero-copy numpy arrays for vertex data
- Basic math types (Vec2/Vec3/Vec4/Quat/Matrix/Transform)
- Core enums and error types
- Type hints via `.pyi`

## Installation

```bash
pip install pyufbx
```

**Note**: The PyPI package name is `pyufbx`, but you import it as `ufbx`:

```python
import ufbx  # Not "import pyufbx"
```

Install from source:

```bash
git clone https://github.com/popomore/ufbx-python.git
cd ufbx-python
pip install .
```

## Technical Stack

- **Binding Method**: Cython with thin C wrapper
- **Performance**: Zero-copy numpy arrays for vertex data
- **Architecture**: C wrapper layer hides complex ufbx structures, Cython provides Pythonic API
- **Dependency Management**: Using sfs.py to manage dependencies with exact commit hashes

## Quick Start

### Build from Source

```bash
# Install dependencies
pip install Cython numpy

# Update ufbx C library (if needed)
python3 sfs.py update --all

# Build Cython extension
python setup.py build_ext --inplace

# Or install in development mode
pip install -e .
```

### Usage Example

```python
import ufbx

# Load FBX file
with ufbx.load_file("model.fbx") as scene:
    # Basic scene info
    print(f"Nodes: {len(scene.nodes)}")
    print(f"Meshes: {len(scene.meshes)}")
    print(f"Materials: {len(scene.materials)}")

    # Access scene hierarchy
    for node in scene.nodes:
        print(f"Node: {node.name}")
        if node.mesh:
            mesh = node.mesh
            print(f"  Mesh: {mesh.num_vertices} vertices, {mesh.num_faces} faces")
        # Light/Camera/etc. wrappers are placeholders for now

    # Access mesh data
    for mesh in scene.meshes:
        positions = mesh.vertex_positions
        normals = mesh.vertex_normals
        uvs = mesh.vertex_uvs
        print(f"Mesh '{mesh.name}': {len(positions)} vertices")

    # Math operations
    node = scene.find_node("MyNode")
    if node:
        local_matrix = node.local_transform
        print(f"Local matrix: {local_matrix}")

# Or use class method
scene = ufbx.Scene.load_file("model.fbx")
try:
    # Use scene
    print(scene)
finally:
    scene.close()
```

Run example script:

```bash
python3 examples/basic_usage.py tests/data/your_model.fbx
```

## Current Progress

- [x] Project structure setup
- [x] Dependency management with sfs.py
- [x] Download ufbx source (commit: `6ecd6177af59c82ec363356ac36c3a4245b85321`)
- [x] Build system (setup.py, pyproject.toml)
- [x] Cython implementation with C wrapper
- [x] Implement core API (Scene, Node, Mesh, Material classes)
- [x] Zero-copy numpy array support for vertex data
- [x] Proper lifetime management with context managers
- [x] Write example code
- [ ] Add more element types (Light, Camera, Animation, Deformers, etc.)
- [x] Add math types (Vec2, Vec3, Vec4, Quat, Matrix, Transform)
- [x] Add enum types (core set used by tests)
- [x] Add type hints (.pyi files)

## Dependency Management

This project uses [sfs.py](https://github.com/bqqbarbhg/sfs) for dependency management:

```bash
# Update dependencies
python3 sfs.py update --all

# View current version
cat sfs-deps.json.lock
```

## Features

- ✅ **High Performance**: Cython-based bindings compiled to native code
- ✅ **Zero-Copy Access**: Numpy arrays directly reference ufbx memory
- ✅ **Memory Management**: Automatic resource cleanup with context managers
- ✅ **Pythonic API**: Context managers, property access, Python idioms
- ✅ **Core Functionality**: Scene loading, mesh data, materials, node hierarchy
- 🚧 **Expanding Coverage**: More element types, enums, and APIs are being added

## Development

### Build from Source

```bash
# Clone repository
git clone https://github.com/popomore/ufbx-python.git
cd ufbx-python

# Download dependencies
python3 sfs.py update --all

# Install development dependencies
pip install -e .[dev]

# Build
python setup.py build_ext --inplace

# Run tests
pytest tests/ -v
```

### Publishing to PyPI

See [Release Guide](RELEASING.md) for how to publish new versions to PyPI.

## References

- ufbx Documentation: https://ufbx.github.io/
- ufbx C Library: https://github.com/ufbx/ufbx
- Cython Documentation: https://cython.readthedocs.io/
- PyPI Project Page: https://pypi.org/project/pyufbx/

## License

MIT - See [LICENSE](LICENSE) file for details.

This project includes [ufbx](https://github.com/ufbx/ufbx), also under the MIT License.
