// 3D models in the browser: the viewer stage and thumbnail renders. three.js is vendored and
// only loaded (by a dynamic import in app.js) once a model is on screen.
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OBJLoader } from 'three/addons/loaders/OBJLoader.js';
import { MTLLoader } from 'three/addons/loaders/MTLLoader.js';
import { FBXLoader } from 'three/addons/loaders/FBXLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

export const FORMATS = new Set(['glb', 'gltf', 'obj', 'fbx']);

const dirOf = (url) => url.slice(0, url.lastIndexOf('/') + 1);

// Draco/meshopt/KTX2 need decoders we don't ship; say so instead of failing somewhere inside.
const UNSUPPORTED = ['KHR_draco_mesh_compression', 'EXT_meshopt_compression', 'KHR_texture_basisu'];

async function loadObj(url) {
  const text = await (await fetch(url)).text();
  const loader = new OBJLoader();
  const libs = [...text.matchAll(/^mtllib\s+(.+?)\s*$/gm)].map((m) => m[1].replace(/\\/g, '/'));
  for (const lib of libs) {
    try {
      const mtl = new MTLLoader();
      const mtlUrl = dirOf(url) + lib.split('/').map(encodeURIComponent).join('/');
      mtl.setResourcePath(dirOf(mtlUrl));
      const mats = await mtl.loadAsync(mtlUrl);
      mats.preload();
      loader.setMaterials(mats);
      break;
    } catch { /* no .mtl: plain gray */ }
  }
  return { root: loader.parse(text), clips: [] };
}

/** Load a model file: {root, clips}. Missing textures just leave the material untextured. */
export async function loadModel(url, ext) {
  if (ext === 'glb' || ext === 'gltf') {
    const g = await new GLTFLoader().loadAsync(url);
    const need = (g.parser.json.extensionsRequired || []).filter((e) => UNSUPPORTED.includes(e));
    if (need.length) throw new Error(`uses ${need.join(', ')}, which the preview doesn't support yet`);
    return { root: g.scene, clips: g.animations || [] };
  }
  if (ext === 'obj') return loadObj(url);
  if (ext === 'fbx') {
    const o = await new FBXLoader().loadAsync(url);
    return { root: o, clips: o.animations || [] };
  }
  throw new Error(`no 3D preview for .${ext} files`);
}

/** OBJ and FBX textures load after the model does; wait for them (a while at most) before a render. */
export async function texturesReady(root, ms = 4000) {
  const texs = [];
  root.traverse((o) => { for (const m of [].concat(o.material || [])) for (const v of Object.values(m)) if (v && v.isTexture) texs.push(v); });
  const end = performance.now() + ms;
  while (texs.some((t) => !t.image) && performance.now() < end) await new Promise((r) => setTimeout(r, 50));
}

export function dispose(root) {
  root.traverse((o) => {
    o.geometry?.dispose();
    for (const m of [].concat(o.material || [])) {
      for (const v of Object.values(m)) if (v && v.isTexture) v.dispose();
      m.dispose();
    }
  });
}

/** Triangles, meshes, materials, textures and size of a loaded model. */
export function stats(root) {
  let tris = 0, meshes = 0;
  const mats = new Set(), texs = new Set();
  root.traverse((o) => {
    if (!o.isMesh) return;
    meshes++;
    const g = o.geometry;
    tris += (g.index ? g.index.count : g.attributes.position?.count || 0) / 3;
    for (const m of [].concat(o.material || [])) {
      mats.add(m);
      for (const v of Object.values(m)) if (v && v.isTexture) texs.add(v.image || v);
    }
  });
  const size = bounds(root).getSize(new THREE.Vector3());
  let bones = 0;
  root.traverse((o) => { if (o.isBone) bones++; });
  return { bones, tris: Math.round(tris), meshes, materials: mats.size, textures: texs.size, size: [size.x, size.y, size.z] };
}

function lights(scene, renderer) {
  const pmrem = new THREE.PMREMGenerator(renderer);
  const env = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
  pmrem.dispose();
  scene.environment = env;
  scene.environmentIntensity = 0.8;
  scene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 1.2)); // Phong/Lambert (OBJ, FBX) ignore the environment
  const sun = new THREE.DirectionalLight(0xffffff, 1.6);
  sun.position.set(3, 5, 4);
  scene.add(sun);
  return env;
}

function makeRenderer(canvas, preserve = false) {
  const r = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, preserveDrawingBuffer: preserve });
  r.setClearColor(0x000000, 0);
  r.toneMapping = THREE.NeutralToneMapping;
  return r;
}

/** The model's bounds; for a skeleton without meshes (animation-only files), its bones. */
function bounds(root) {
  const box = new THREE.Box3().setFromObject(root, true);
  if (box.isEmpty()) {
    root.updateMatrixWorld(true);
    const p = new THREE.Vector3();
    root.traverse((o) => { if (o.isBone) box.expandByPoint(o.getWorldPosition(p)); });
  }
  return box;
}

/** Point the camera at the model from a three-quarter view above, the whole model in frame. */
function frame(camera, root, controls) {
  const box = bounds(root);
  if (box.isEmpty()) return null;
  const size = box.getSize(new THREE.Vector3()), center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.length() / 2, 1e-4);
  const dist = radius / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * 1.05;
  const dir = new THREE.Vector3(1, 0.75, 1.25).normalize();
  camera.position.copy(center).addScaledVector(dir, dist);
  camera.near = dist / 100;
  camera.far = dist * 100;
  camera.updateProjectionMatrix();
  camera.lookAt(center);
  if (controls) {
    controls.target.copy(center);
    controls.update();
  }
  return { radius, floor: box.min.y, center };
}

/** The viewer: orbit around one model, play its clips. */
export class Stage {
  constructor(el) {
    this.el = el;
    this.canvas = document.createElement('canvas');
    this.canvas.className = 'model3d';
    el.appendChild(this.canvas);
    this.renderer = makeRenderer(this.canvas);
    this.renderer.setPixelRatio(window.devicePixelRatio || 1);
    this.scene = new THREE.Scene();
    this.env = lights(this.scene, this.renderer);
    this.camera = new THREE.PerspectiveCamera(35, 1, 0.01, 1000);
    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.enableDamping = true;
    this.timer = new THREE.Timer();
    this.mixer = null;
    this.action = null;
    this.paused = false;
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(el);
    this.resize();
    this.renderer.setAnimationLoop(() => this.tick());
  }

  resize() {
    const w = this.el.clientWidth, h = this.el.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  tick() {
    this.timer.update();
    const dt = this.timer.getDelta();
    if (this.mixer && !this.paused) this.mixer.update(dt);
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  }

  clear() {
    if (this.root) {
      this.scene.remove(this.root);
      dispose(this.root);
      this.root = null;
    }
    if (this.bones) {
      this.scene.remove(this.bones);
      this.bones.dispose();
      this.bones = null;
    }
    if (this.grid) {
      this.scene.remove(this.grid);
      this.grid.geometry.dispose();
      this.grid.material.dispose();
      this.grid = null;
    }
    this.mixer?.stopAllAction();
    this.mixer = this.action = null;
  }

  /** Show a loaded model ({root, clips}); returns its stats. */
  show(model) {
    this.clear();
    this.root = model.root;
    this.clips = model.clips;
    this.scene.add(this.root);
    const st = stats(this.root);
    if (!st.meshes && st.bones) { // an animation-only file: show the skeleton moving
      this.bones = new THREE.SkeletonHelper(this.root);
      this.scene.add(this.bones);
    }
    const f = frame(this.camera, this.root, this.controls);
    if (f) {
      this.grid = new THREE.GridHelper(f.radius * 4, 16, 0x888888, 0x888888);
      this.grid.material.transparent = true;
      this.grid.material.opacity = 0.35;
      this.grid.position.set(f.center.x, f.floor, f.center.z);
      this.grid.visible = this.showGrid ?? true;
      this.scene.add(this.grid);
    }
    if (this.clips.length) this.mixer = new THREE.AnimationMixer(this.root);
    this.wire(this.wireframe || false);
    return st;
  }

  play(name) {
    if (!this.mixer) return;
    this.action?.stop();
    const clip = this.clips.find((c) => c.name === name);
    this.action = clip ? this.mixer.clipAction(clip).reset().play() : null;
    if (!clip) this.mixer.stopAllAction();
  }

  setPaused(p) { this.paused = p; }
  setGrid(on) { this.showGrid = on; if (this.grid) this.grid.visible = on; }
  wire(on) {
    this.wireframe = on;
    this.root?.traverse((o) => { for (const m of [].concat(o.material || [])) m.wireframe = on; });
  }
  reset() { if (this.root) frame(this.camera, this.root, this.controls); }

  /** Put one texture on every material (for FBX files whose own texture paths don't resolve). */
  async retexture(url) {
    if (!this.root) return;
    const tex = url ? await new THREE.TextureLoader().loadAsync(url) : null;
    if (tex) tex.colorSpace = THREE.SRGBColorSpace;
    this.root.traverse((o) => {
      for (const m of [].concat(o.material || [])) {
        if (!('map' in m)) continue;
        if (m.userData.ownMap === undefined) m.userData.ownMap = m.map;
        m.map = tex || m.userData.ownMap;
        if (tex && m.color) m.color.set(0xffffff);
        m.needsUpdate = true;
      }
    });
  }

  dispose() {
    this.renderer.setAnimationLoop(null);
    this.ro.disconnect();
    this.clear();
    this.controls.dispose();
    this.env.dispose();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
    this.canvas.remove();
  }
}

// --- thumbnails: one offscreen renderer for every tile (browsers allow ~16 WebGL contexts) -------

let thumbCtx = null;
function thumbRenderer(size) {
  if (!thumbCtx) {
    const canvas = document.createElement('canvas');
    const renderer = makeRenderer(canvas, true);
    const scene = new THREE.Scene();
    lights(scene, renderer);
    thumbCtx = { canvas, renderer, scene, camera: new THREE.PerspectiveCamera(30, 1, 0.01, 1000) };
  }
  thumbCtx.renderer.setSize(size, size, false);
  return thumbCtx;
}

/** Render a model to a PNG blob: transparent background, the same three-quarter view. */
export async function renderThumb(url, ext, size = 384) {
  const model = await loadModel(url, ext);
  if (!stats(model.root).meshes) {
    dispose(model.root);
    throw new Error('nothing to draw (no meshes)');
  }
  await texturesReady(model.root);
  const t = thumbRenderer(size);
  try {
    if (model.clips.length) { // first frame of the first clip, so rigged characters stand up
      const mixer = new THREE.AnimationMixer(model.root);
      mixer.clipAction(model.clips[0]).play();
      mixer.update(0);
    }
    t.scene.add(model.root);
    frame(t.camera, model.root);
    t.renderer.render(t.scene, t.camera);
    return await new Promise((ok, bad) => t.canvas.toBlob((b) => (b ? ok(b) : bad(new Error('render failed'))), 'image/png'));
  } finally {
    t.scene.remove(model.root);
    dispose(model.root);
  }
}
