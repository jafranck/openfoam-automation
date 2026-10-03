# Meshes

Place user provided OpenFOAM meshes here.

Research specific meshes used during development are intentionally excluded
from this public repository. A typical layout is:

`meshes/<mesh_name>/polyMesh/`

The `polyMesh` directory should contain files such as `boundary`, `faces`,
`neighbour`, `owner`, and `points`.

Set `paths.mesh` in the campaign JSON to the selected `polyMesh` directory.
A small synthetic example is available in `examples/demo_mesh/`.
