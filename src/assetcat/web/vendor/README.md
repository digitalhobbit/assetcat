# Vendored frontend libraries

Checked in so the browser works offline and needs no build step.

- `preact-htm.js`: `htm@3.1.1/preact/standalone.module.js` from unpkg. Preact (with hooks) plus
  htm in one ES module. Preact is MIT (Jason Miller), htm is Apache-2.0 (Google).
- `three/`: three.js 0.186.1 from the npm package (MIT, `three/LICENSE`): `build/three.module.js`
  and `three.core.js`, plus the addons the 3D viewer uses, at their paths under
  `examples/jsm/`: GLTF, OBJ, MTL and FBX loaders, OrbitControls, RoomEnvironment and their
  imports (BufferGeometryUtils, SkeletonUtils, NURBS curves, fflate, MIT). `index.html` maps
  `three` and `three/addons/` to them; `model3d.js` loads them only when a model is shown.
