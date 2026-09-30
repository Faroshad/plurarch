// Plurarch 3D viewer for phones: three.js (WebGL2), loaded lazily from jsDelivr through the page's
// import map ("three", "three/addons/"). Self-contained: it owns a canvas, an HTML pin layer, the
// tour stop chips and a short tour hint inside `container`. Renders on demand only.
//
// API
//   import { createViewer, webgl2Available, THREE_VERSION } from './viewer3d.js';
//   const v = createViewer(container, {
//     onPinTap(questionKey),        // a pin was tapped
//     onReady(info),                // first frame is on screen: { tier, buildMs, parts }
//     onFallback(reason),           // 3D is unavailable ('webgl2' | 'load' | 'timeout' | 'init' | 'context-lost')
//     onModeChange(mode),           // 'orbit' | 'tour' (also when goToStop switches to the tour)
//     onStopChange(stopId | null),  // tour stop reached (null after walking freely)
//     questionTags,                 // QUESTION_TAGS: tag prefixes per question (highlight + pin order)
//     quality: 'auto' | 0 | 1 | 2,  // 0 minimal, 1 low, 2 high; a number pins the tier (no fps watchdog)
//     canvasLabel,                  // aria-label of the canvas
//     insetTop: 0,                  // px at the top covered by the app's own controls (pins avoid it)
//   });
//   v.setModel(model, { keepCamera })  // Model per docs/MODEL.md; applied at most once per frame
//   v.setMode('orbit' | 'tour'); v.getMode(); v.resetView()
//   v.goToStop(id)
//   v.setPins([{ question, label, value, state: 'todo'|'answered'|'active'|'changed', num, tag, aria }])
//   v.highlight(questionKey | null)    // accent tint + rim on that question's parts, others dimmed
//   v.focus(questionKey)               // turn the camera toward that question's pin
//   v.setViewInset(px)                 // bottom overlap (e.g. a sheet): keeps the model centred above it
//   v.setIdleRotation(on)              // slow, bounded turn in orbit mode (waiting screens)
//   v.stats()                          // numbers for tests: renders, buildMs, tier, frame times, ...
//   v.dispose()
//   v.ready                            // Promise<boolean>
//
// Coordinates: models are Z-up (Rhino); three.js is Y-up. model (x, y, z) -> three (x, z, -y).

import { buildMeshData, tagMatcher, rayFirstHit, TILE_M } from './viewer/meshdata.js';
import { drawTexture, drawBlobShadow } from './viewer/textures.js';

export const THREE_VERSION = '0.186.1';

const LOAD_TIMEOUT_MS = 25000;
const IDLE_MAX_MS = 90000; // idle rotation stops by itself after this (battery)
const IDLE_RESUME_MS = 6000; // pause after the user touches the model
const IDLE_FRAME_MS = 45; // idle-only frames are throttled to ~22 fps
const EYE_H = 1.6;
const HORIZON = 0xF1EFEA;
const ZENITH = 0xC9DBEE;
const ACCENT = 0xF0489B;
const ORBIT_FOV = 40;
const TOUR_FOV = 68;
const DEG = Math.PI / 180;

const TIERS = [
  { dpr: 1, shadow: 0, tex: 0.5 },
  { dpr: 1.5, shadow: 1024, tex: 1 },
  { dpr: 2, shadow: 2048, tex: 1 },
];

/** Quick check (no three.js needed): can this browser make a WebGL2 context? */
export function webgl2Available() {
  try {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl2');
    if (!gl) return false;
    const ext = gl.getExtension('WEBGL_lose_context');
    if (ext) ext.loseContext();
    return true;
  } catch (_) {
    return false;
  }
}

let libPromise = null;
function loadLib() {
  if (!libPromise) {
    libPromise = Promise.all([
      import('three'),
      import('three/addons/controls/OrbitControls.js'),
      import('three/addons/environments/RoomEnvironment.js'),
    ]).then(([T, oc, re]) => ({ T, OrbitControls: oc.OrbitControls, RoomEnvironment: re.RoomEnvironment }));
    libPromise.catch(() => { libPromise = null; });
  }
  return libPromise;
}

function autoTier() {
  const ua = navigator.userAgent || '';
  const ios = /iPhone|iPad|iPod/.test(ua) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  if (ios) return 2; // Safari reports few cores; Apple GPUs handle this scene. The watchdog still applies.
  const cores = navigator.hardwareConcurrency || 4;
  const mem = navigator.deviceMemory || 8;
  if (cores <= 2 || mem <= 1) return 0;
  if (cores <= 4 || mem <= 3) return 1;
  return 2;
}

const reducedMotion = () => {
  try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (_) { return false; }
};
const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
const easeOut = (t) => 1 - Math.pow(1 - t, 3);
const now = () => performance.now();

function h(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function checkSvg() {
  const ns = 'http://www.w3.org/2000/svg';
  const s = document.createElementNS(ns, 'svg');
  s.setAttribute('viewBox', '0 0 24 24');
  s.setAttribute('width', '14');
  s.setAttribute('height', '14');
  s.setAttribute('fill', 'none');
  s.setAttribute('stroke', 'currentColor');
  s.setAttribute('stroke-width', '3');
  s.setAttribute('stroke-linecap', 'round');
  s.setAttribute('stroke-linejoin', 'round');
  s.setAttribute('aria-hidden', 'true');
  const p = document.createElementNS(ns, 'path');
  p.setAttribute('d', 'M20 6 9 17l-5-5');
  s.appendChild(p);
  return s;
}

export function createViewer(container, options = {}) {
  const o = {
    onPinTap() {}, onReady() {}, onFallback() {}, onModeChange() {}, onStopChange() {},
    questionTags: null, quality: 'auto', canvasLabel: '3D model of the pavilion', insetTop: 0,
    ...options,
  };
  const qOf = tagMatcher(o.questionTags || {});
  const qKeys = qOf.keys;
  const pinnedTier = typeof o.quality === 'number' && TIERS[o.quality] ? o.quality : null;
  let tierIdx = pinnedTier != null ? pinnedTier : autoTier();

  /* ------------------------------------------------------------ DOM */
  container.classList.add('v3d');
  const canvas = h('canvas', 'v3d-canvas');
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', o.canvasLabel);
  const pinLayer = h('div', 'v3d-pins');
  const stopsBar = h('div', 'v3d-stops');
  stopsBar.setAttribute('role', 'toolbar');
  stopsBar.setAttribute('aria-label', 'Tour stops');
  stopsBar.hidden = true;
  const hint = h('div', 'v3d-hint', 'Drag to look around · tap the floor to walk');
  hint.setAttribute('aria-hidden', 'true');
  hint.hidden = true;
  container.append(canvas, pinLayer, stopsBar, hint);

  /* ------------------------------------------------------------ state */
  let T = null; let OrbitControlsCls = null; let RoomEnvironmentCls = null;
  let renderer = null; let scene = null; let camera = null; let controls = null;
  let sun = null; let hemi = null; let envRT = null;
  let modelGroup = null; let tourGroup = null; let skyMesh = null; let groundMesh = null; let blobMesh = null;
  let destRing = null;
  const meshes = new Map(); // material key -> Mesh
  const matCache = new Map(); // material key -> { sig, mat }
  const texCache = new Map(); // kind -> Texture
  const HL = { uHL: { value: 0 }, uHLColor: { value: null } };

  let disposed = false; let failed = false; let isReady = false;
  let model = null; let pending = null; let firstModel = true;
  let occ = null; let obstacles = new Float32Array(0); let footprint = null; let walkable = null; let stops = []; let floorZ = 0.3;
  const pinCands = new Map(); // question -> { exterior: [{pos, nrm, occ}], interior: [...] }
  const pinEls = new Map(); // question -> element record
  let pinList = [];
  let mode = 'orbit';
  let hlKey = null;
  let idleOn = false; let idleUntil = 0; let idlePausedUntil = 0;
  let userMovedOrbit = false;
  let orbitHome = null; let orbitSaved = null; let intro = null;
  let tween = null;
  const tour = { eye: null, yaw: 0, pitch: 0, fov: TOUR_FOV, stopId: null, vyaw: 0, vpitch: 0, inertia: false };
  let fovBase = ORBIT_FOV;
  const inset = { cur: 0, from: 0, to: 0, t0: 0 };
  let viewW = 0; let viewH = 0;
  let visible = true;
  let raf = 0; let needFrames = 0; let frameNo = 0; let lastFrameAt = 0; let lastRenderAt = 0;
  let occDirty = true; let sizesDirty = true;
  let contextLost = false; let lostTimer = null;
  const st = { tex: {}, renders: 0, buildMs: 0, meshMs: 0, uploadMs: 0, texMs: 0, parts: 0, tris: 0, calls: 0, cpuMs: 0, frameMsMedian: 0, tierChanges: 0, loadMs: 0, compileMs: 0 };
  const wd = { samples: [], lastChange: 0 };
  let maxAniso = 1;
  let tmpV = null; let tmpV2 = null; let tmpQ = null; let tmpM = null; let tmpE = null;

  let readyResolve;
  const ready = new Promise((r) => { readyResolve = r; });

  function fail(reason) {
    if (failed || disposed) return;
    failed = true;
    readyResolve(false);
    try { o.onFallback(reason); } catch (_) { /* app callback */ }
    teardown();
  }

  /* ------------------------------------------------------------ boot */
  const tLoad = now();
  if (!webgl2Available()) {
    setTimeout(() => fail('webgl2'), 0);
  } else {
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; fail('timeout'); }, LOAD_TIMEOUT_MS);
    loadLib().then((lib) => {
      clearTimeout(timer);
      if (timedOut || disposed || failed) return;
      st.loadMs = now() - tLoad;
      T = lib.T; OrbitControlsCls = lib.OrbitControls; RoomEnvironmentCls = lib.RoomEnvironment;
      try {
        init();
      } catch (e) {
        console.error('[viewer3d] init failed', e);
        fail('init');
        return;
      }
      start();
    }).catch((e) => {
      clearTimeout(timer);
      if (!timedOut) { console.warn('[viewer3d] three.js did not load', e); fail('load'); }
    });
  }

  function init() {
    tmpV = new T.Vector3(); tmpV2 = new T.Vector3(); tmpQ = new T.Quaternion(); tmpM = new T.Matrix4(); tmpE = new T.Euler(0, 0, 0, 'YXZ');
    tour.eye = new T.Vector3();
    HL.uHLColor.value = new T.Color(ACCENT);
    renderer = new T.WebGLRenderer({ canvas, antialias: true, alpha: false, stencil: false, powerPreference: 'default' });
    if (!renderer.capabilities.isWebGL2) throw new Error('no WebGL2');
    if (pinnedTier == null && renderer.capabilities.maxTextureSize < 4096) tierIdx = Math.min(tierIdx, 1);
    renderer.outputColorSpace = T.SRGBColorSpace;
    renderer.toneMapping = T.NeutralToneMapping;
    renderer.toneMappingExposure = 1.0;
    renderer.shadowMap.enabled = TIERS[tierIdx].shadow > 0;
    renderer.shadowMap.type = T.PCFShadowMap;
    renderer.shadowMap.autoUpdate = false; // sun and model are static: re-render shadows only on a rebuild
    renderer.setClearColor(HORIZON, 1);
    const ta = now();
    maxAniso = renderer.capabilities.getMaxAnisotropy();
    st.anisoMs = Math.round(now() - ta);

    scene = new T.Scene();
    scene.background = new T.Color(HORIZON);
    scene.fog = new T.Fog(HORIZON, 90, 340);

    camera = new T.PerspectiveCamera(ORBIT_FOV, 1, 0.2, 900);
    camera.position.set(-25, 18, 38);

    // Image-based fill: RoomEnvironment through PMREM (reflections on glass and metal).
    const pmrem = new T.PMREMGenerator(renderer);
    const room = new RoomEnvironmentCls();
    envRT = pmrem.fromScene(room, 0.04);
    scene.environment = envRT.texture;
    scene.environmentIntensity = 0.5;
    if (typeof room.dispose === 'function') room.dispose();
    pmrem.dispose();

    hemi = new T.HemisphereLight(0xE4EDF8, 0xCBC3B2, 0.85);
    sun = new T.DirectionalLight(0xFFF0DA, 2.5);
    sun.castShadow = TIERS[tierIdx].shadow > 0;
    const sm = TIERS[tierIdx].shadow || 1024;
    sun.shadow.mapSize.set(sm, sm);
    sun.shadow.bias = -0.0004;
    sun.shadow.normalBias = 0.03;
    sun.shadow.radius = tierIdx >= 2 ? 3 : 2;
    scene.add(hemi, sun, sun.target);

    // Sky dome: vertex-coloured gradient, horizon colour = fog colour (seamless horizon).
    const skyGeo = new T.SphereGeometry(600, 32, 16);
    const cols = new Float32Array(skyGeo.attributes.position.count * 3);
    const cH = new T.Color(HORIZON); const cZ = new T.Color(ZENITH); const c = new T.Color();
    for (let i = 0; i < skyGeo.attributes.position.count; i++) {
      const y = skyGeo.attributes.position.getY(i) / 600;
      c.copy(cH).lerp(cZ, Math.pow(clamp(y, 0, 1), 0.55));
      cols[i * 3] = c.r; cols[i * 3 + 1] = c.g; cols[i * 3 + 2] = c.b;
    }
    skyGeo.setAttribute('color', new T.BufferAttribute(cols, 3));
    skyMesh = new T.Mesh(skyGeo, new T.MeshBasicMaterial({ vertexColors: true, side: T.BackSide, fog: false, depthWrite: false, toneMapped: false }));
    skyMesh.renderOrder = -10;
    skyMesh.frustumCulled = false;
    scene.add(skyMesh);

    // Outer ground: a large disc with world-scale UVs. It carries the site's ground colour near the
    // building and fades to a calm neutral further out, then into the fog.
    groundMesh = new T.Mesh(groundGeometry(), new T.MeshStandardMaterial({ color: 0xFFFFFF, vertexColors: true, roughness: 1, metalness: 0, map: textureFor('ground') }));
    setGroundColor(new T.Color(0xC6CBBB));
    groundMesh.position.y = 0;
    groundMesh.receiveShadow = true;
    scene.add(groundMesh);

    // Contact shadow: a soft dark rounded rectangle under the building footprint.
    const blobTex = new T.CanvasTexture(drawBlobShadow(256));
    blobTex.colorSpace = T.SRGBColorSpace;
    blobMesh = new T.Mesh(new T.PlaneGeometry(1, 1).rotateX(-Math.PI / 2), new T.MeshBasicMaterial({
      color: 0x000000, map: blobTex, transparent: true, opacity: 0.32, depthWrite: false,
      polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2, fog: false,
    }));
    blobMesh.renderOrder = 1;
    blobMesh.visible = false;
    scene.add(blobMesh);

    modelGroup = new T.Group();
    tourGroup = new T.Group();
    tourGroup.visible = false;
    scene.add(modelGroup, tourGroup);

    const ringMat = new T.MeshBasicMaterial({ color: ACCENT, transparent: true, opacity: 0, depthWrite: false, fog: false, toneMapped: false });
    destRing = new T.Mesh(new T.RingGeometry(0.22, 0.34, 40).rotateX(-Math.PI / 2), ringMat);
    destRing.renderOrder = 11;
    destRing.visible = false;
    scene.add(destRing);

    controls = new OrbitControlsCls(camera, canvas);
    controls.enableDamping = true;
    controls.dampingFactor = 0.12;
    controls.rotateSpeed = 0.8;
    controls.zoomSpeed = 0.9;
    controls.panSpeed = 0.8;
    controls.screenSpacePanning = true;
    controls.minPolarAngle = 0.12 * Math.PI;
    controls.maxPolarAngle = 0.485 * Math.PI;
    controls.autoRotateSpeed = 0.5;
    controls.touches = { ONE: T.TOUCH.ROTATE, TWO: T.TOUCH.DOLLY_PAN };
    controls.addEventListener('change', () => invalidate());
    controls.addEventListener('start', () => {
      userMovedOrbit = true;
      intro = null;
      pauseIdle();
    });
    controls.addEventListener('end', () => invalidate(2));

    canvas.addEventListener('pointerdown', onPointerDown);
    canvas.addEventListener('pointermove', onPointerMove);
    canvas.addEventListener('pointerup', onPointerUp);
    canvas.addEventListener('pointercancel', onPointerUp);
    canvas.addEventListener('wheel', onWheel, { passive: false });
    canvas.addEventListener('webglcontextlost', onContextLost, false);
    canvas.addEventListener('webglcontextrestored', onContextRestored, false);
  }

  let ro = null; let io = null;
  function start() {
    ro = new ResizeObserver(() => resize());
    ro.observe(container);
    if ('IntersectionObserver' in window) {
      io = new IntersectionObserver((entries) => {
        for (const e of entries) visible = e.isIntersecting;
        if (visible) invalidate(); else cancelFrame();
      }, { threshold: 0 });
      io.observe(container);
    }
    document.addEventListener('visibilitychange', onVisibility);
    resize();
    if (document.fonts && document.fonts.addEventListener) document.fonts.addEventListener('loadingdone', onFonts);
    if (pending) {
      const p = pending; pending = null;
      applyModel(p.model, p.keepCamera);
    }
    if (model) reveal(); else revealPending = true;
  }

  function onFonts() {
    for (const rec of pinEls.values()) rec.w = 0;
    invalidate();
  }

  let revealPending = false;
  async function reveal() {
    revealPending = false;
    // Compile the shaders up front (parallel where supported) so the first frame is not a hitch.
    const t0 = now();
    try {
      if (renderer.compileAsync) await renderer.compileAsync(scene, camera);
    } catch (_) { /* compile on first render instead */ }
    if (disposed || failed) return;
    st.compileMs = now() - t0;
    invalidate(2);
    requestAnimationFrame(() => {
      if (disposed || failed) return;
      container.classList.add('is-ready');
      isReady = true;
      readyResolve(true);
      try { o.onReady({ tier: tierIdx, buildMs: st.buildMs, parts: st.parts, loadMs: st.loadMs }); } catch (_) { /* app */ }
      invalidate(2);
    });
  }

  /* ------------------------------------------------------------ ground */
  const GROUND_R = [0, 8, 16, 22, 30, 40, 55, 75, 110, 170, 280, 520];
  const GROUND_SEG = 72;
  function groundGeometry() {
    const nR = GROUND_R.length;
    const pos = new Float32Array((1 + (nR - 1) * GROUND_SEG) * 3);
    const uv = new Float32Array((1 + (nR - 1) * GROUND_SEG) * 2);
    const idx = [];
    let v = 1; // vertex 0 = centre
    for (let r = 1; r < nR; r++) {
      for (let k = 0; k < GROUND_SEG; k++) {
        const a = (k / GROUND_SEG) * Math.PI * 2;
        const x = Math.cos(a) * GROUND_R[r]; const z = -Math.sin(a) * GROUND_R[r];
        pos[v * 3] = x; pos[v * 3 + 2] = z;
        uv[v * 2] = x / TILE_M.ground; uv[v * 2 + 1] = -z / TILE_M.ground;
        v++;
      }
    }
    const ring = (r, k) => (r === 0 ? 0 : 1 + (r - 1) * GROUND_SEG + (k % GROUND_SEG));
    for (let k = 0; k < GROUND_SEG; k++) idx.push(0, ring(1, k), ring(1, k + 1));
    for (let r = 1; r < nR - 1; r++) {
      for (let k = 0; k < GROUND_SEG; k++) {
        const a = ring(r, k); const b = ring(r, k + 1); const c = ring(r + 1, k); const d = ring(r + 1, k + 1);
        idx.push(a, c, b, b, c, d);
      }
    }
    const g = new T.BufferGeometry();
    g.setAttribute('position', new T.BufferAttribute(pos, 3));
    g.setAttribute('normal', new T.BufferAttribute(new Float32Array(pos.length).map((_, i) => (i % 3 === 1 ? 1 : 0)), 3));
    g.setAttribute('uv', new T.BufferAttribute(uv, 2));
    g.setAttribute('color', new T.BufferAttribute(new Float32Array(pos.length), 3));
    g.setIndex(idx);
    return g;
  }
  function setGroundColor(siteColor) {
    const site = siteColor.clone().lerp(new T.Color(0xD6D9C9), 0.3); // calmer than a saturated lawn
    const far = new T.Color(0xCDD0C4).lerp(site, 0.28); // neutral, a hint of the site colour
    const col = groundMesh.geometry.attributes.color;
    const pos = groundMesh.geometry.attributes.position;
    const c = new T.Color();
    for (let i = 0; i < col.count; i++) {
      const r = Math.hypot(pos.getX(i), pos.getZ(i));
      const t = clamp((r - 18) / 50, 0, 1);
      c.copy(site).lerp(far, t * t * (3 - 2 * t));
      col.setXYZ(i, c.r, c.g, c.b);
    }
    col.needsUpdate = true;
  }

  /* ------------------------------------------------------------ textures + materials */
  function textureFor(kind) {
    if (texCache.has(kind)) return texCache.get(kind);
    const t0 = now();
    const cv = drawTexture(kind, TIERS[tierIdx].tex);
    st.tex[kind] = Math.round((now() - t0) * 10) / 10;
    let tex = null;
    if (cv) {
      tex = new T.CanvasTexture(cv);
      tex.colorSpace = T.SRGBColorSpace;
      tex.wrapS = T.RepeatWrapping;
      tex.wrapT = T.RepeatWrapping;
      tex.anisotropy = Math.min(4, maxAniso);
    }
    st.texMs += now() - t0;
    texCache.set(kind, tex);
    return tex;
  }

  function srgb(col, fallback = 0xBBBBBB) {
    const c = new T.Color(fallback);
    if (Array.isArray(col) && col.length >= 3 && col.every((v) => Number.isFinite(v))) {
      c.setRGB(clamp(col[0], 0, 255) / 255, clamp(col[1], 0, 255) / 255, clamp(col[2], 0, 255) / 255, T.SRGBColorSpace);
    }
    return c;
  }

  function patchHighlight(mat) {
    mat.onBeforeCompile = (shader) => {
      shader.uniforms.uHL = HL.uHL;
      shader.uniforms.uHLColor = HL.uHLColor;
      shader.vertexShader = 'attribute float aQ;\nvarying float vQ;\n' + shader.vertexShader
        .replace('#include <begin_vertex>', '#include <begin_vertex>\n\tvQ = aQ;');
      shader.fragmentShader = 'uniform float uHL;\nuniform vec3 uHLColor;\nvarying float vQ;\n' + shader.fragmentShader
        .replace('#include <color_fragment>', [
          '#include <color_fragment>',
          '\tfloat hlOn = step( 0.5, uHL );',
          '\tfloat hlMe = hlOn * ( 1.0 - step( 0.5, abs( vQ - uHL ) ) );',
          '\tdiffuseColor.rgb *= mix( 1.0, 0.76, hlOn * ( 1.0 - hlMe ) );', // others: slightly dimmed
          '\tdiffuseColor.rgb = mix( diffuseColor.rgb, uHLColor, 0.05 * hlMe );',
        ].join('\n'))
        .replace('#include <emissivemap_fragment>', [
          '#include <emissivemap_fragment>',
          '\t{',
          '\t\tfloat hlRim = 1.0 - abs( dot( normalize( normal ), normalize( vViewPosition ) ) );',
          // the element: a touch brighter, plus a thin accent rim toward grazing angles
          '\t\ttotalEmissiveRadiance += hlMe * ( diffuseColor.rgb * 0.12 + uHLColor * ( 0.025 + 0.3 * hlRim * hlRim * hlRim ) );',
          '\t}',
        ].join('\n'));
    };
    mat.customProgramCacheKey = () => 'plurarch-hl-2';
  }

  function materialFor(key, def) {
    const sig = JSON.stringify([def.kind, def.color, def.opacity, def.roughness, def.metalness, def.emissive]);
    const hit = matCache.get(key);
    if (hit && hit.sig === sig) return hit.mat;
    if (hit) hit.mat.dispose();
    const kind = def.kind in TILE_M ? def.kind : 'plaster';
    const opacity = Number.isFinite(def.opacity) ? clamp(def.opacity, 0, 1) : 1;
    const common = {
      color: srgb(def.color),
      roughness: Number.isFinite(def.roughness) ? clamp(def.roughness, 0.02, 1) : 0.8,
      metalness: Number.isFinite(def.metalness) ? clamp(def.metalness, 0, 1) : 0,
    };
    let mat;
    if (kind === 'glass') {
      mat = new T.MeshStandardMaterial({
        ...common, roughness: Math.min(common.roughness, 0.2), transparent: true,
        opacity: clamp(Number.isFinite(def.opacity) ? def.opacity : 0.35, 0.08, 0.95), depthWrite: false, envMapIntensity: 1.5,
      });
    } else if (kind === 'light') {
      mat = new T.MeshStandardMaterial({ ...common, emissive: srgb(def.emissive || def.color, 0xFFF4DD), emissiveIntensity: 1.6 });
    } else {
      mat = new T.MeshStandardMaterial({
        ...common, map: textureFor(kind), transparent: opacity < 0.99, opacity, depthWrite: opacity >= 0.99,
      });
      if (Array.isArray(def.emissive)) { mat.emissive = srgb(def.emissive, 0x000000); mat.emissiveIntensity = 1; }
    }
    patchHighlight(mat);
    matCache.set(key, { sig, mat });
    return mat;
  }

  /* ------------------------------------------------------------ model */
  const toThree = (p, out) => out.set(p[0], p[2], -p[1]);
  const validP3 = (p) => Array.isArray(p) && p.length >= 3 && Number.isFinite(p[0]) && Number.isFinite(p[1]) && Number.isFinite(p[2]);

  function applyModel(m, keepCamera) {
    const t0 = now();
    const texBefore = st.texMs;
    const data = buildMeshData(m, { qOf, dropGroundPlate: true });
    const t1 = now();
    const mats = m.materials || {};
    const seen = new Set();
    for (const g of data.groups) {
      seen.add(g.key);
      const def = mats[g.key] || { kind: 'plaster', color: [200, 200, 200], opacity: 1, roughness: 0.9, metalness: 0 };
      const mat = materialFor(g.key, def);
      const geo = new T.BufferGeometry();
      geo.setAttribute('position', new T.BufferAttribute(g.position, 3));
      geo.setAttribute('normal', new T.BufferAttribute(g.normal, 3));
      geo.setAttribute('uv', new T.BufferAttribute(g.uv, 2));
      geo.setAttribute('aQ', new T.BufferAttribute(g.q, 1));
      geo.setIndex(new T.BufferAttribute(g.index, 1));
      let mesh = meshes.get(g.key);
      if (!mesh) {
        mesh = new T.Mesh(geo, mat);
        mesh.frustumCulled = false; // one merged mesh per material around the building: always in view
        meshes.set(g.key, mesh);
        modelGroup.add(mesh);
      } else {
        mesh.geometry.dispose();
        mesh.geometry = geo;
        mesh.material = mat;
      }
      const transparent = !!mat.transparent;
      mesh.castShadow = !transparent && g.kind !== 'light';
      mesh.receiveShadow = !transparent;
      mesh.renderOrder = transparent ? 2 : 0;
    }
    for (const [k, mesh] of meshes) {
      if (!seen.has(k)) {
        modelGroup.remove(mesh);
        mesh.geometry.dispose();
        meshes.delete(k);
      }
    }
    const groundDef = Object.values(mats).find((d) => d && d.kind === 'ground');
    if (groundDef) {
      setGroundColor(srgb(groundDef.color));
      groundMesh.material.roughness = Number.isFinite(groundDef.roughness) ? clamp(groundDef.roughness, 0.3, 1) : 1;
    }
    groundMesh.position.y = data.groundZ != null ? data.groundZ : Math.min(0, data.footprint.min[2]) - 0.002;

    model = m;
    occ = data.occ;
    obstacles = data.obstacles;
    footprint = data.footprint;
    const wk = m.walkable;
    walkable = wk && Array.isArray(wk.min) && Array.isArray(wk.max) ? {
      min: [Math.min(wk.min[0], wk.max[0]), Math.min(wk.min[1], wk.max[1])],
      max: [Math.max(wk.min[0], wk.max[0]), Math.max(wk.min[1], wk.max[1])],
      z: Number.isFinite(wk.floor_z) ? wk.floor_z : footprint.min[2],
    } : { min: [footprint.min[0] + 0.6, footprint.min[1] + 0.6], max: [footprint.max[0] - 0.6, footprint.max[1] - 0.6], z: footprint.min[2] };
    floorZ = walkable.z;

    // pins (positions from the model; texts from setPins)
    pinCands.clear();
    for (const p of Array.isArray(m.pins) ? m.pins : []) {
      if (!p || typeof p.question !== 'string' || !validP3(p.pos)) continue;
      const md = p.mode === 'interior' ? 'interior' : 'exterior';
      const nrm = validP3(p.normal) ? toThree(p.normal, new T.Vector3()) : new T.Vector3(0, 0, 0);
      if (nrm.lengthSq() > 0) nrm.normalize();
      let c = pinCands.get(p.question);
      if (!c) { c = { exterior: [], interior: [], label: '' }; pinCands.set(p.question, c); }
      if (!c.label && typeof p.label === 'string') c.label = p.label;
      const nm = validP3(p.normal) ? p.normal.slice(0, 3) : [0, 0, 0];
      const nl = Math.hypot(nm[0], nm[1], nm[2]) || 1;
      // visibility is tested at a point 0.7 m in front of the element (where the pin's stem is),
      // so a pin on glass behind fins or mullions still shows when its surroundings are in view
      const probe = [p.pos[0] + (nm[0] / nl) * 0.7, p.pos[1] + (nm[1] / nl) * 0.7, p.pos[2] + (nm[2] / nl) * 0.7];
      c[md].push({ pos: toThree(p.pos, new T.Vector3()), nrm, model: probe, occ: false });
    }
    stops = (Array.isArray(m.tour) ? m.tour : [])
      .filter((s) => s && validP3(s.eye) && validP3(s.target))
      .map((s, i) => ({ id: String(s.id || 'stop' + i), label: String(s.label || s.id || 'Stop ' + (i + 1)), eye: s.eye.slice(0, 3), target: s.target.slice(0, 3) }));
    for (const item of pinList) { const rec = pinEls.get(item.question); if (rec) renderPinContent(rec, item); } // labels from the model
    buildStopsUI();
    buildRings();
    fitLights(data);
    if (firstModel || !keepCamera || !orbitHome) {
      computeOrbitHome();
      if (mode === 'orbit' && !tween) placeOrbitHome(firstModel && !reducedMotion());
      firstModel = false;
    } else {
      computeOrbitHome(true);
    }
    renderer.shadowMap.needsUpdate = true;
    st.meshMs = t1 - t0;
    st.buildMs = now() - t0 - (st.texMs - texBefore);
    st.uploadMs = st.buildMs - st.meshMs;
    st.parts = data.stats.parts;
    st.tris = data.stats.tris;
    occDirty = true;
    sizesDirty = true;
    invalidate(2);
  }

  function fitLights() {
    const fp = footprint;
    const cx = (fp.min[0] + fp.max[0]) / 2; const cy = (fp.min[1] + fp.max[1]) / 2; const cz = (fp.min[2] + fp.max[2]) / 2;
    const r = 0.5 * Math.hypot(fp.max[0] - fp.min[0], fp.max[1] - fp.min[1], fp.max[2] - fp.min[2]) + 2;
    const center = toThree([cx, cy, cz], new T.Vector3());
    const dir = toThree([-0.55, -0.62, 0.82], new T.Vector3()).normalize(); // sun from the front-left, high
    sun.target.position.copy(center);
    sun.position.copy(center).addScaledVector(dir, r * 3);
    const cam = sun.shadow.camera;
    cam.left = -r; cam.right = r; cam.top = r; cam.bottom = -r;
    cam.near = r * 0.5; cam.far = r * 6;
    cam.updateProjectionMatrix();
    sun.target.updateMatrixWorld();
    // contact shadow
    const sx = fp.max[0] - fp.min[0]; const sy = fp.max[1] - fp.min[1];
    blobMesh.scale.set((sx + 3) / 0.6, 1, (sy + 3) / 0.6);
    blobMesh.position.set(cx, fp.min[2] + 0.012, -cy);
    blobMesh.visible = true;
  }

  /* ------------------------------------------------------------ orbit camera */
  function orbitPos(home, theta, phi, dist, out) {
    out.setFromSphericalCoords(dist, phi, theta).add(home.target);
    return out;
  }

  // Distance at which the footprint box fits the frame for a view direction.
  function fitDistance(target, theta, phi, margin, fov) {
    const fp = footprint;
    const pts = [];
    for (let i = 0; i < 8; i++) {
      pts.push(toThree([i & 1 ? fp.max[0] : fp.min[0], i & 2 ? fp.max[1] : fp.min[1], i & 4 ? fp.max[2] : fp.min[2]], new T.Vector3()));
    }
    const cam = camera.clone();
    cam.fov = fov;
    cam.clearViewOffset();
    cam.aspect = viewW > 0 && viewH > 0 ? viewW / viewH : 0.7;
    cam.updateProjectionMatrix();
    let lo = 4; let hi = 400;
    for (let it = 0; it < 18; it++) {
      const d = (lo + hi) / 2;
      cam.position.setFromSphericalCoords(d, phi, theta).add(target);
      cam.lookAt(target);
      cam.updateMatrixWorld();
      let fits = true;
      for (const p of pts) {
        tmpV.copy(p).project(cam);
        if (tmpV.z > 1 || Math.abs(tmpV.x) > margin || Math.abs(tmpV.y) > margin) { fits = false; break; }
      }
      if (fits) hi = d; else lo = d;
    }
    return hi;
  }

  function computeOrbitHome(keep) {
    const fp = footprint;
    const cx = (fp.min[0] + fp.max[0]) / 2; const cy = (fp.min[1] + fp.max[1]) / 2;
    const zc = fp.min[2] + (fp.max[2] - fp.min[2]) * 0.82; // well above mid-height: the building sits low, pins fit above
    const target = new T.Vector3(cx, zc, -cy);
    const phi = 1.27; // about 17 degrees above the horizon
    // Three-quarter views from the entrance side; keep the one that shows the most exterior pins.
    let theta = -0.66; let dist = fitDistance(target, theta, phi, 0.94, ORBIT_FOV); let bestSeen = -1;
    for (const th of [-0.66, 0.66, -0.95, 0.95, -0.4, 0.4]) {
      const d = th === -0.66 ? dist : fitDistance(target, th, phi, 0.94, ORBIT_FOV);
      const seen = visiblePinsFrom(tmpV.setFromSphericalCoords(d, phi, th).add(target));
      if (seen > bestSeen) { bestSeen = seen; theta = th; dist = d; }
    }
    orbitHome = { target, theta, phi, dist };
    controls.minDistance = Math.max(3, dist * 0.3);
    controls.maxDistance = dist * 2.2;
    if (!keep) orbitSaved = null;
  }

  // Exterior pins that face a camera at `pos` (three coords) and are not occluded.
  function visiblePinsFrom(pos) {
    let n = 0;
    const ox = pos.x; const oy = -pos.z; const oz = pos.y;
    for (const c of pinCands.values()) {
      if (c.exterior.some((cd) => {
        tmpV2.copy(pos).sub(cd.pos).normalize();
        if (cd.nrm.lengthSq() > 0 && cd.nrm.dot(tmpV2) < 0.05) return false;
        const dx = cd.model[0] - ox; const dy = cd.model[1] - oy; const dz = cd.model[2] - oz;
        const len = Math.hypot(dx, dy, dz);
        return rayFirstHit(occ, ox, oy, oz, dx, dy, dz, 1 - 0.12 / len) === Infinity;
      })) n++;
    }
    return n;
  }

  function placeOrbitHome(withIntro) {
    const hm = orbitHome;
    fovBase = ORBIT_FOV;
    camera.near = 0.2;
    controls.target.copy(hm.target);
    if (withIntro) {
      intro = { t0: now(), dur: 2600, theta0: hm.theta - 0.7, dist0: hm.dist * 1.15 };
      orbitPos(hm, intro.theta0, hm.phi, intro.dist0, camera.position);
    } else {
      orbitPos(hm, hm.theta, hm.phi, hm.dist, camera.position);
    }
    camera.lookAt(hm.target);
    applyProjection();
    controls.update();
    userMovedOrbit = false;
  }

  function clampTarget() {
    if (!footprint) return;
    const fp = footprint;
    const t = controls.target;
    const x = clamp(t.x, fp.min[0] - 4, fp.max[0] + 4);
    const y = clamp(t.y, fp.min[2] + 0.3, fp.max[2] + 1);
    const z = clamp(t.z, -fp.max[1] - 4, -fp.min[1] + 4);
    if (x !== t.x || y !== t.y || z !== t.z) {
      tmpV.set(x - t.x, y - t.y, z - t.z);
      t.add(tmpV);
      camera.position.add(tmpV);
    }
  }

  /* ------------------------------------------------------------ projection + inset */
  function applyProjection() {
    if (!camera) return;
    const w = viewW || 1; const hgt = viewH || 1;
    camera.aspect = w / hgt;
    const ins = inset.cur;
    if (ins > 0.5) {
      camera.fov = 2 * Math.atan(Math.tan((fovBase * DEG) / 2) * ((hgt + ins) / hgt)) / DEG;
      camera.setViewOffset(w, hgt + ins, 0, ins, w, hgt);
    } else {
      camera.fov = fovBase;
      camera.clearViewOffset();
    }
    camera.updateProjectionMatrix();
  }

  function resize() {
    if (!renderer) return;
    const r = container.getBoundingClientRect();
    const w = Math.round(r.width); const hgt = Math.round(r.height);
    if (w < 2 || hgt < 2) return;
    if (w === viewW && hgt === viewH) return;
    viewW = w; viewH = hgt;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, TIERS[tierIdx].dpr));
    renderer.setSize(w, hgt, false);
    applyProjection();
    if (orbitHome && mode === 'orbit' && !userMovedOrbit && !tween) {
      computeOrbitHome(true);
      if (!intro) placeOrbitHome(false);
    }
    sizesDirty = true;
    occDirty = true;
    invalidate(2);
  }

  /* ------------------------------------------------------------ render loop */
  function canRender() {
    return isReadyToDraw() && visible && !document.hidden && !contextLost && viewW > 1 && viewH > 1;
  }
  function isReadyToDraw() { return !!renderer && !disposed && !failed && !!model; }

  function invalidate(n = 1) {
    if (n > needFrames) needFrames = n;
    schedule();
  }
  function schedule() {
    if (!raf && canRender()) raf = requestAnimationFrame(frame);
  }
  function cancelFrame() {
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
    lastFrameAt = 0;
  }

  function idleActive(t) {
    return idleOn && mode === 'orbit' && !reducedMotion() && t < idleUntil && t >= idlePausedUntil && !tween && !intro;
  }
  function pauseIdle() {
    const t = now();
    idlePausedUntil = t + IDLE_RESUME_MS;
    if (idleOn) idleUntil = Math.max(idleUntil, t + IDLE_RESUME_MS + 30000);
    if (idleOn) setTimeout(() => invalidate(), IDLE_RESUME_MS + 50);
  }

  function frame(t) {
    raf = 0;
    if (!canRender()) return;
    const consecutive = lastFrameAt > 0 && t - lastFrameAt < 120;
    const dt = consecutive ? (t - lastFrameAt) / 1000 : 1 / 60;
    lastFrameAt = t;
    if (pending) {
      const p = pending; pending = null;
      applyModel(p.model, p.keepCamera);
    }
    let active = false;
    if (stepTween(t)) active = true;
    if (stepInset(t)) active = true;
    if (stepIntro(t)) active = true;
    if (stepInertia(dt)) active = true;
    if (stepDestRing(t)) active = true;
    let idleOnly = false;
    if (mode === 'orbit' && controls.enabled) {
      const idle = idleActive(t);
      controls.autoRotate = idle;
      const moved = controls.update(dt);
      clampTarget();
      if (moved) {
        if (idle && !active && needFrames === 0) idleOnly = true; else active = true;
      }
    } else if (mode === 'tour' && !tween) {
      applyTourCamera();
    }
    if (idleOnly && t - lastRenderAt < IDLE_FRAME_MS) { schedule(); return; }
    const c0 = now();
    renderer.render(scene, camera);
    st.cpuMs = now() - c0;
    st.renders++;
    st.calls = renderer.info.render.calls;
    lastRenderAt = t;
    frameNo++;
    updatePins(active || idleOnly);
    if (needFrames > 0) needFrames--;
    watchdog(dt, consecutive && !idleOnly && (active || needFrames > 0));
    if (active || idleOnly || needFrames > 0 || pending) schedule();
    else if (occDirty) { occDirty = false; invalidate(1); }
  }

  function watchdog(dt, sample) {
    if (pinnedTier != null || !sample) return;
    wd.samples.push(dt);
    if (wd.samples.length < 30) return;
    const s = wd.samples.slice().sort((a, b) => a - b);
    const med = s[s.length >> 1];
    wd.samples.length = 0;
    st.frameMsMedian = Math.round(med * 10000) / 10;
    const t = now();
    if (med > 0.038 && tierIdx > 0 && t - wd.lastChange > 2500) {
      wd.lastChange = t;
      setTier(tierIdx - 1);
    }
  }

  function setTier(i) {
    const prev = TIERS[tierIdx];
    tierIdx = i;
    const q = TIERS[i];
    st.tierChanges++;
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, q.dpr));
    renderer.setSize(viewW, viewH, false);
    if (q.shadow !== prev.shadow) {
      if (q.shadow > 0) {
        sun.shadow.mapSize.set(q.shadow, q.shadow);
        if (sun.shadow.map) { sun.shadow.map.dispose(); sun.shadow.map = null; }
        renderer.shadowMap.needsUpdate = true;
      } else {
        renderer.shadowMap.enabled = false;
        sun.castShadow = false;
        for (const { mat } of matCache.values()) mat.needsUpdate = true;
        groundMesh.material.needsUpdate = true;
      }
    }
    invalidate(2);
  }

  /* ------------------------------------------------------------ tweens */
  function stepTween(t) {
    if (!tween) return false;
    const k = tween.dur > 0 ? clamp((t - tween.t0) / tween.dur, 0, 1) : 1;
    const e = tween.ease(k);
    camera.position.lerpVectors(tween.fromPos, tween.toPos, e);
    if (tween.kind === 'fly') {
      camera.quaternion.slerpQuaternions(tween.fromQuat, tween.toQuat, e);
      fovBase = tween.fromFov + (tween.toFov - tween.fromFov) * e;
      applyProjection();
    } else {
      tour.eye.copy(camera.position);
      applyTourCamera();
    }
    if (k >= 1) {
      const done = tween.onDone;
      tween = null;
      occDirty = true;
      if (done) done();
      return true;
    }
    return true;
  }

  function lookQuat(eye, target, out) {
    tmpM.lookAt(eye, target, camera.up);
    return out.setFromRotationMatrix(tmpM);
  }

  function fly(toPos, toQuat, toFov, durMs, onDone) {
    tween = {
      kind: 'fly', t0: now(), dur: reducedMotion() ? 0 : durMs, ease: easeInOut,
      fromPos: camera.position.clone(), toPos: toPos.clone(),
      fromQuat: camera.quaternion.clone(), toQuat: toQuat.clone(),
      fromFov: fovBase, toFov, onDone,
    };
    hidePinsNow();
    invalidate();
  }

  function stepIntro(t) {
    if (!intro || mode !== 'orbit') return false;
    const k = clamp((t - intro.t0) / intro.dur, 0, 1);
    const e = easeOut(k);
    const hm = orbitHome;
    orbitPos(hm, intro.theta0 + (hm.theta - intro.theta0) * e, hm.phi, intro.dist0 + (hm.dist - intro.dist0) * e, camera.position);
    camera.lookAt(controls.target);
    if (k >= 1) intro = null;
    return true;
  }

  function stepInset(t) {
    if (inset.cur === inset.to) return false;
    const k = reducedMotion() ? 1 : clamp((t - inset.t0) / 260, 0, 1);
    inset.cur = inset.from + (inset.to - inset.from) * easeOut(k);
    if (k >= 1) inset.cur = inset.to;
    applyProjection();
    occDirty = true;
    return true;
  }

  function stepDestRing(t) {
    if (!destRing.visible) return false;
    const k = clamp((t - destRing.userData.t0) / destRing.userData.dur, 0, 1);
    destRing.material.opacity = 0.9 * (1 - k);
    const s = 1 + 0.6 * k;
    destRing.scale.set(s, 1, s);
    if (k >= 1) { destRing.visible = false; return false; }
    return true;
  }

  /* ------------------------------------------------------------ tour */
  function stopById(id) { return stops.find((s) => s.id === id) || null; }

  function eyeInside(p) {
    if (!walkable) return true;
    const x = p.x; const y = -p.z;
    return x > walkable.min[0] - 0.5 && x < walkable.max[0] + 0.5 && y > walkable.min[1] - 0.5 && y < walkable.max[1] + 0.5;
  }

  function applyTourCamera() {
    camera.position.copy(tour.eye);
    tmpE.set(tour.pitch, tour.yaw, 0, 'YXZ');
    camera.quaternion.setFromEuler(tmpE);
  }

  function setYawPitchFromQuat(q) {
    tmpV.set(0, 0, -1).applyQuaternion(q);
    tour.yaw = Math.atan2(-tmpV.x, -tmpV.z);
    tour.pitch = clamp(Math.asin(clamp(tmpV.y, -1, 1)), -1.2, 1.3);
  }

  function stopPose(s) {
    const eye = toThree(s.eye, new T.Vector3());
    const target = toThree(s.target, new T.Vector3());
    const dir = tmpV2.copy(target).sub(eye).normalize();
    const pitch = clamp(Math.asin(clamp(dir.y, -1, 1)), -1.2, 1.3);
    const yaw = Math.atan2(-dir.x, -dir.z);
    tmpE.set(pitch, yaw, 0, 'YXZ');
    const quat = new T.Quaternion().setFromEuler(tmpE);
    return { eye, quat, yaw, pitch };
  }

  function flyToStop(s, durMs, onDone) {
    const pose = stopPose(s);
    const dist = camera.position.distanceTo(pose.eye);
    const d = durMs != null ? durMs : clamp(700 + dist * 60, 800, 1800);
    tour.inertia = false;
    fly(pose.eye, pose.quat, tour.fov, d, () => {
      tour.eye.copy(pose.eye);
      tour.yaw = pose.yaw;
      tour.pitch = pose.pitch;
      setStop(s.id);
      if (onDone) onDone();
    });
  }

  function setStop(id) {
    if (tour.stopId === id) return;
    tour.stopId = id;
    for (const b of stopsBar.children) {
      const on = b.dataset.id === id;
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      if (on && stopsBar.scrollWidth > stopsBar.clientWidth) {
        const left = b.offsetLeft - (stopsBar.clientWidth - b.offsetWidth) / 2;
        try { stopsBar.scrollTo({ left, behavior: reducedMotion() ? 'auto' : 'smooth' }); } catch (_) { stopsBar.scrollLeft = left; }
      }
    }
    updateRings();
    try { o.onStopChange(id); } catch (_) { /* app */ }
  }

  function firstInteriorStop() {
    return stopById('entrance') || stops.find((s) => walkable && s.eye[0] > walkable.min[0] && s.eye[0] < walkable.max[0] && s.eye[1] > walkable.min[1] && s.eye[1] < walkable.max[1]) || stops[0] || null;
  }

  function beginTourMode() {
    if (mode === 'orbit') orbitSaved = { target: controls.target.clone(), pos: camera.position.clone() };
    mode = 'tour';
    intro = null;
    controls.enabled = false;
    controls.autoRotate = false;
    camera.near = 0.05;
    tourGroup.visible = true;
    stopsBar.hidden = false;
    container.classList.add('is-tour');
    showHint();
    try { o.onModeChange('tour'); } catch (_) { /* app */ }
  }

  function enterTour() {
    if (!stops.length) return false;
    beginTourMode();
    const back = tour.stopId && stopById(tour.stopId);
    const first = back || firstInteriorStop();
    const approach = stopById('approach') || stops.find((s) => walkable && !(s.eye[0] > walkable.min[0] && s.eye[0] < walkable.max[0] && s.eye[1] > walkable.min[1] && s.eye[1] < walkable.max[1]));
    if (!back && approach && approach !== first && !reducedMotion()) {
      flyToStop(approach, 1300, () => { setTimeout(() => { if (mode === 'tour' && !tween && tour.stopId === approach.id) flyToStop(first, 1400); }, 250); });
    } else {
      flyToStop(first, 1400);
    }
    return true;
  }

  function enterOrbit() {
    mode = 'orbit';
    tour.inertia = false;
    stopsBar.hidden = true;
    hint.hidden = true;
    container.classList.remove('is-tour');
    const hm = orbitHome;
    const target = orbitSaved ? orbitSaved.target : hm.target;
    const pos = orbitSaved ? orbitSaved.pos : orbitPos(hm, hm.theta, hm.phi, hm.dist, new T.Vector3());
    const q = lookQuat(pos, target, new T.Quaternion());
    fly(pos, q, ORBIT_FOV, 1200, () => {
      tourGroup.visible = false;
      camera.near = 0.2;
      applyProjection();
      controls.target.copy(target);
      controls.enabled = true;
      controls.update();
      invalidate(2);
    });
    try { o.onModeChange('orbit'); } catch (_) { /* app */ }
  }

  function buildStopsUI() {
    const sig = stops.map((s) => s.id + ':' + s.label).join('|');
    if (stopsBar.dataset.sig === sig) return;
    stopsBar.dataset.sig = sig;
    stopsBar.textContent = '';
    for (const s of stops) {
      const b = h('button', 'v3d-stop', s.label);
      b.type = 'button';
      b.dataset.id = s.id;
      b.setAttribute('aria-pressed', tour.stopId === s.id ? 'true' : 'false');
      b.addEventListener('click', () => api.goToStop(s.id));
      stopsBar.appendChild(b);
    }
  }

  let rings = [];
  function buildRings() {
    for (const r of rings) { tourGroup.remove(r.mesh); for (const c of r.mesh.children) c.geometry.dispose(); }
    rings = [];
    if (!stops.length) return;
    if (!buildRings.mat) {
      buildRings.mat = new T.MeshBasicMaterial({ color: 0xFFFFFF, transparent: true, opacity: 0.92, depthWrite: false, fog: false, toneMapped: false });
      buildRings.disc = new T.MeshBasicMaterial({ color: 0xFFFFFF, transparent: true, opacity: 0.28, depthWrite: false, fog: false, toneMapped: false });
    }
    for (const s of stops) {
      const inside = walkable && s.eye[0] > walkable.min[0] - 0.3 && s.eye[0] < walkable.max[0] + 0.3 && s.eye[1] > walkable.min[1] - 0.3 && s.eye[1] < walkable.max[1] + 0.3;
      const fz = inside ? floorZ : Math.max(footprint.min[2] - 0.3, s.eye[2] - EYE_H);
      const g = new T.Group();
      const ring = new T.Mesh(new T.RingGeometry(0.34, 0.46, 48).rotateX(-Math.PI / 2), buildRings.mat);
      const disc = new T.Mesh(new T.CircleGeometry(0.34, 32).rotateX(-Math.PI / 2), buildRings.disc);
      ring.renderOrder = 10; disc.renderOrder = 10;
      g.add(ring, disc);
      g.position.set(s.eye[0], fz + 0.03, -s.eye[1]);
      tourGroup.add(g);
      rings.push({ id: s.id, mesh: g });
    }
    updateRings();
  }

  function updateRings() {
    for (const r of rings) {
      const near = tour.eye && r.mesh.position.distanceTo(tmpV.set(tour.eye.x, r.mesh.position.y, tour.eye.z)) < 0.9;
      r.mesh.visible = r.id !== tour.stopId && !near;
    }
  }

  function showHint() {
    if (showHint.done) return;
    showHint.done = true;
    hint.hidden = false;
    hint.classList.add('is-on');
    setTimeout(() => { hint.classList.remove('is-on'); setTimeout(() => { hint.hidden = true; }, 400); }, 4200);
  }

  /* pointer input (tour mode; OrbitControls handles orbit) */
  const ptrs = new Map();
  let drag = null; let pinch = null; let lastMoveT = 0;
  function onPointerDown(e) {
    if (mode === 'orbit') { pauseIdle(); return; }
    if (tween && tween.kind === 'fly') return;
    try { canvas.setPointerCapture(e.pointerId); } catch (_) { /* old browsers */ }
    ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    tour.inertia = false;
    if (ptrs.size === 1) {
      drag = { id: e.pointerId, t0: now(), lx: e.clientX, ly: e.clientY, moved: 0 };
    } else if (ptrs.size === 2) {
      const [a, b] = [...ptrs.values()];
      pinch = { d0: Math.hypot(a.x - b.x, a.y - b.y) || 1, fov0: tour.fov };
      drag = null;
    }
    if (!hint.hidden) hint.classList.remove('is-on');
  }
  function onPointerMove(e) {
    if (mode !== 'tour' || !ptrs.has(e.pointerId)) return;
    ptrs.set(e.pointerId, { x: e.clientX, y: e.clientY });
    if (pinch && ptrs.size >= 2) {
      const [a, b] = [...ptrs.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y) || 1;
      tour.fov = clamp((pinch.fov0 * pinch.d0) / d, 38, 85);
      if (!tween) { fovBase = tour.fov; applyProjection(); }
      invalidate();
      return;
    }
    if (drag && e.pointerId === drag.id) {
      const dx = e.clientX - drag.lx; const dy = e.clientY - drag.ly;
      drag.lx = e.clientX; drag.ly = e.clientY;
      drag.moved += Math.abs(dx) + Math.abs(dy);
      if (drag.moved < 6) return;
      const k = (tour.fov * DEG) / Math.max(200, viewH);
      tour.yaw += dx * k;
      tour.pitch = clamp(tour.pitch + dy * k, -1.2, 1.3);
      const t = now();
      const dtm = Math.max(8, t - lastMoveT);
      lastMoveT = t;
      tour.vyaw = 0.6 * tour.vyaw + 0.4 * ((dx * k) / dtm);
      tour.vpitch = 0.6 * tour.vpitch + 0.4 * ((dy * k) / dtm);
      if (tween && tween.kind === 'fly') return;
      invalidate();
    }
  }
  function onPointerUp(e) {
    if (!ptrs.has(e.pointerId)) return;
    ptrs.delete(e.pointerId);
    if (pinch) { if (ptrs.size < 2) pinch = null; drag = null; return; }
    if (drag && e.pointerId === drag.id) {
      const tap = drag.moved < 10 && now() - drag.t0 < 450;
      if (tap && e.type === 'pointerup') handleTap(e.clientX, e.clientY);
      else if (!tap && !reducedMotion() && now() - lastMoveT < 80) { tour.inertia = true; invalidate(); }
      drag = null;
    }
  }
  function onWheel(e) {
    if (mode !== 'tour') return;
    e.preventDefault();
    tour.fov = clamp(tour.fov * (1 + Math.sign(e.deltaY) * 0.08), 38, 85);
    if (!tween) { fovBase = tour.fov; applyProjection(); }
    invalidate();
  }

  function stepInertia(dt) {
    if (!tour.inertia || mode !== 'tour') return false;
    const ms = dt * 1000;
    tour.yaw += tour.vyaw * ms;
    tour.pitch = clamp(tour.pitch + tour.vpitch * ms, -1.2, 1.3);
    const decay = Math.exp(-ms / 160);
    tour.vyaw *= decay; tour.vpitch *= decay;
    if (Math.abs(tour.vyaw) < 2e-5 && Math.abs(tour.vpitch) < 2e-5) { tour.inertia = false; }
    return true;
  }

  function handleTap(cx, cy) {
    const rect = canvas.getBoundingClientRect();
    const x = cx - rect.left; const y = cy - rect.top;
    // hotspot rings first (screen space, forgiving)
    let best = null; let bestD = 38;
    for (const r of rings) {
      if (!r.mesh.visible) continue;
      tmpV.copy(r.mesh.position).project(camera);
      if (tmpV.z > 1) continue;
      const sx = ((tmpV.x + 1) / 2) * viewW; const sy = ((1 - tmpV.y) / 2) * viewH;
      const d = Math.hypot(sx - x, sy - y);
      if (d < bestD) { bestD = d; best = r; }
    }
    if (best) { api.goToStop(best.id); return; }
    // floor under the tap
    tmpV.set((x / viewW) * 2 - 1, -(y / viewH) * 2 + 1, 0.5).unproject(camera);
    const dir = tmpV.sub(camera.position).normalize();
    if (dir.y > -0.03) return;
    const eye = camera.position;
    const tFloor = (floorZ - eye.y) / dir.y;
    if (!(tFloor > 0)) return;
    // model coordinates for the occluder test
    const ox = eye.x; const oy = -eye.z; const oz = eye.y;
    const dx = dir.x; const dy = -dir.z; const dz = dir.y;
    const tHit = rayFirstHit(occ, ox, oy, oz, dx, dy, dz, tFloor - 0.08);
    const tt = tHit < Infinity ? Math.max(0, tHit - 0.45) : tFloor;
    let px = ox + dx * tt; let py = oy + dy * tt;
    const m = 0.35;
    px = clamp(px, walkable.min[0] + m, walkable.max[0] - m);
    py = clamp(py, walkable.min[1] + m, walkable.max[1] - m);
    // never end inside a chair, desk or column: step back toward the eye, else search around
    const free = freeSpot(px, py, ox, oy);
    if (!free) return;
    px = free[0]; py = free[1];
    const to = new T.Vector3(px, floorZ + EYE_H, -py);
    const dist = to.distanceTo(eye);
    if (dist < 0.3) return;
    tween = {
      kind: 'glide', t0: now(), dur: reducedMotion() ? 0 : clamp((dist / 3.2) * 1000, 450, 1800), ease: easeInOut,
      fromPos: eye.clone(), toPos: to,
      onDone: () => {
        const near = stops.find((s) => Math.hypot(s.eye[0] - px, s.eye[1] - py) < 0.5);
        setStop(near ? near.id : null);
        updateRings();
      },
    };
    destRing.position.set(px, floorZ + 0.035, -py);
    destRing.userData = { t0: now(), dur: Math.max(600, tween.dur + 200) };
    destRing.visible = true;
    setStop(null);
    invalidate();
  }

  // Is (x, y) (model coordinates) clear of every obstacle footprint, with a body radius margin?
  function clearAt(x, y, r = 0.28) {
    const O = obstacles;
    for (let i = 0; i < O.length; i += 4) {
      if (x > O[i] - r && x < O[i + 2] + r && y > O[i + 1] - r && y < O[i + 3] + r) return false;
    }
    return true;
  }
  function inWalk(x, y) {
    return x >= walkable.min[0] + 0.3 && x <= walkable.max[0] - 0.3 && y >= walkable.min[1] + 0.3 && y <= walkable.max[1] - 0.3;
  }
  function freeSpot(px, py, ex, ey) {
    if (clearAt(px, py)) return [px, py];
    const dx = ex - px; const dy = ey - py; const d = Math.hypot(dx, dy);
    for (let s = 0.15; s < d; s += 0.15) { // back along the line toward the eye
      const x = px + (dx / d) * s; const y = py + (dy / d) * s;
      if (inWalk(x, y) && clearAt(x, y)) return [x, y];
    }
    for (let rad = 0.3; rad <= 1.5; rad += 0.3) { // then around the tapped point
      for (let k = 0; k < 12; k++) {
        const a = (k / 12) * Math.PI * 2;
        const x = px + Math.cos(a) * rad; const y = py + Math.sin(a) * rad;
        if (inWalk(x, y) && clearAt(x, y)) return [x, y];
      }
    }
    return null;
  }

  /* ------------------------------------------------------------ pins */
  function pinSet() {
    if (tween && tween.kind === 'fly') return null;
    if (mode === 'orbit') return 'exterior';
    return eyeInside(camera.position) ? 'interior' : 'exterior';
  }

  function makePinEl(q) {
    const wrap = h('div', 'v3d-pin-wrap is-off');
    const btn = h('button', 'v3d-pin');
    btn.type = 'button';
    btn.dataset.q = q;
    const badge = h('span', 'v3d-pin-badge');
    const text = h('span', 'v3d-pin-text');
    const label = h('b', 'v3d-pin-label');
    const value = h('span', 'v3d-pin-value');
    const tag = h('span', 'v3d-pin-tag');
    tag.hidden = true;
    text.append(label, value);
    btn.append(badge, text, tag);
    const dot = h('span', 'v3d-pin-dot');
    wrap.append(dot, btn);
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      try { o.onPinTap(q); } catch (_) { /* app */ }
    });
    btn.addEventListener('pointerdown', (e) => e.stopPropagation());
    pinLayer.appendChild(wrap);
    return { q, wrap, btn, badge, label, value, tag, w: 0, hgt: 0, on: false, x: -1, y: -1, lift: 0, dx: 0, kind: 'above', slot: null, cand: -1, set: null, sig: '' };
  }

  function renderPinContent(rec, item) {
    const c = pinCands.get(rec.q);
    const label = item.label || (c && c.label) || rec.q;
    const state = ['todo', 'answered', 'active', 'changed'].includes(item.state) ? item.state : 'todo';
    const sig = JSON.stringify([label, item.value, state, item.num, item.tag, item.aria]);
    if (sig === rec.sig) return;
    rec.sig = sig;
    rec.btn.dataset.state = state;
    rec.wrap.dataset.state = state;
    rec.label.textContent = label;
    rec.value.textContent = item.value != null ? String(item.value) : '';
    rec.badge.textContent = '';
    if (state === 'answered') rec.badge.appendChild(checkSvg());
    else rec.badge.textContent = item.num != null ? String(item.num) : '';
    rec.tag.hidden = state !== 'changed';
    rec.tag.textContent = state === 'changed' ? String(item.tag || 'Changed') : '';
    const stateWord = { todo: 'not set yet', answered: 'set', active: 'open', changed: String(item.tag || 'changed').toLowerCase() }[state];
    rec.btn.setAttribute('aria-label', item.aria || `${label}: ${item.value || ''}, ${stateWord}`);
    if (state === 'active') rec.btn.setAttribute('aria-expanded', 'true'); else rec.btn.removeAttribute('aria-expanded');
    rec.w = 0;
    sizesDirty = true;
  }

  function hidePinsNow() {
    for (const rec of pinEls.values()) {
      if (rec.on) { rec.on = false; rec.wrap.classList.add('is-off'); }
    }
  }

  function pinOccluded(c) {
    if (!occ) return false;
    const cam = camera.position;
    const ox = cam.x; const oy = -cam.z; const oz = cam.y;
    const dx = c.model[0] - ox; const dy = c.model[1] - oy; const dz = c.model[2] - oz;
    const len = Math.hypot(dx, dy, dz);
    if (len < 0.4) return false;
    const maxT = 1 - 0.12 / len;
    return rayFirstHit(occ, ox, oy, oz, dx, dy, dz, maxT) !== Infinity;
  }

  function updatePins(moving) {
    if (!pinEls.size) return;
    const set = isReady ? pinSet() : null;
    for (const rec of pinEls.values()) {
      if (!rec.w) { rec.w = rec.btn.offsetWidth; rec.hgt = rec.btn.offsetHeight; }
    }
    sizesDirty = false;
    const doOcc = occDirty || !moving || frameNo % 4 === 0;
    const topSafe = Math.max(0, Number(o.insetTop) || 0);
    const botSafe = mode === 'tour' ? 64 : 0;
    const placed = [];
    for (const rec of pinEls.values()) {
      const c = set ? pinCands.get(rec.q) : null;
      const list = c ? c[set] : null;
      let best = -1; let bestScore = -Infinity; let bx = 0; let by = 0;
      if (list && list.length) {
        for (let i = 0; i < list.length; i++) {
          const cd = list[i];
          tmpV.copy(cd.pos).project(camera);
          if (tmpV.z < -1 || tmpV.z > 1) continue;
          const sx = ((tmpV.x + 1) / 2) * viewW; const sy = ((1 - tmpV.y) / 2) * viewH;
          if (sx < 4 || sx > viewW - 4 || sy < topSafe + 4 || sy > viewH - botSafe - 6) continue;
          tmpV2.copy(camera.position).sub(cd.pos);
          const dist = tmpV2.length();
          const facing = cd.nrm.lengthSq() > 0 ? cd.nrm.dot(tmpV2.divideScalar(dist || 1)) : 1;
          if (facing < -0.05) continue;
          if (doOcc) cd.occ = pinOccluded(cd);
          if (cd.occ) continue;
          const score = facing - dist * 0.01 + (i === rec.cand && set === rec.set ? 0.35 : 0);
          if (score > bestScore) { bestScore = score; best = i; bx = sx; by = sy; }
        }
      }
      if (best < 0) {
        if (rec.on) { rec.on = false; rec.wrap.classList.add('is-off'); }
        continue;
      }
      rec.cand = best;
      rec.set = set;
      placed.push({ rec, x: bx, y: by, lift: 0, dx: 0, kind: 'above' });
    }
    layoutPins(placed, topSafe, botSafe);
    for (const p of placed) {
      const rec = p.rec;
      const x = Math.round(p.x * 2) / 2; const y = Math.round(p.y * 2) / 2;
      const lift = Math.round(p.lift); const dx = Math.round(p.dx);
      if (x !== rec.x || y !== rec.y) {
        rec.wrap.style.transform = `translate3d(${x}px, ${y}px, 0)`;
        rec.x = x; rec.y = y;
      }
      if (lift !== rec.lift) { rec.wrap.style.setProperty('--lift', lift + 'px'); rec.lift = lift; }
      if (dx !== rec.dx) { rec.wrap.style.setProperty('--dx', dx + 'px'); rec.dx = dx; }
      if (p.kind !== rec.kind) {
        rec.wrap.classList.toggle('is-below', p.kind === 'below');
        rec.wrap.classList.toggle('is-left', p.kind === 'left');
        rec.wrap.classList.toggle('is-right', p.kind === 'right');
        rec.kind = p.kind;
      }
      if (!rec.on) { rec.on = true; rec.wrap.classList.remove('is-off'); }
    }
    if (doOcc) occDirty = false;
  }

  // Greedy label placement: each chip goes above, beside or below its anchor (shifted sideways when
  // useful), wherever it covers no other chip, stem or anchor dot. Keeps the previous slot while it
  // still fits (no flicker). Last resort: lift it above the chips it collides with.
  const SLOTS = [['above', 0], ['above', -1], ['above', 1], ['right', 0], ['left', 0], ['below', 0], ['below', -1], ['below', 1]];
  function layoutPins(placed, topSafe, botSafe) {
    const GAP = 12; const M = 4;
    const rank = { active: 0, todo: 1, changed: 2, answered: 3 };
    placed.sort((a, b) => (rank[a.rec.btn.dataset.state] - rank[b.rec.btn.dataset.state]) || (b.y - a.y));
    const boxes = []; const stems = [];
    const dots = placed.map((p) => [p.x - 8, p.y - 8, p.x + 8, p.y + 8, p.rec]);
    const rectFor = (p, w, hh, slot, lift) => {
      const [kind, sh] = slot;
      if (kind === 'left' || kind === 'right') {
        const x0 = kind === 'right' ? p.x + GAP : p.x - GAP - w;
        return {
          x0, x1: x0 + w, y0: p.y - hh / 2, y1: p.y + hh / 2, dx: 0, kind,
          stem: kind === 'right' ? [p.x, p.y - 1, p.x + GAP, p.y + 1] : [p.x - GAP, p.y - 1, p.x, p.y + 1],
          side: true,
        };
      }
      let dx = sh * (w / 2 - 16);
      if (p.x + dx - w / 2 < 6) dx = 6 - (p.x - w / 2);
      else if (p.x + dx + w / 2 > viewW - 6) dx = viewW - 6 - (p.x + w / 2);
      const top = kind === 'below' ? p.y + GAP : p.y - GAP - lift - hh;
      return {
        x0: p.x + dx - w / 2, y0: top, x1: p.x + dx + w / 2, y1: top + hh, dx, kind,
        stem: kind === 'below' ? [p.x - 1, p.y, p.x + 1, p.y + GAP] : [p.x - 1, p.y - GAP - lift, p.x + 1, p.y],
      };
    };
    const ov = (a0, a1, a2, a3, b0, b1, b2, b3, m) => {
      const ox = Math.min(a2, b2 + m) - Math.max(a0, b0 - m);
      const oy = Math.min(a3, b3 + m) - Math.max(a1, b1 - m);
      return ox > 0 && oy > 0 ? ox * oy : 0;
    };
    const cost = (r, self) => {
      if (r.y0 < topSafe + 4 || r.y1 > viewH - botSafe - 4 || r.x0 < 4 || r.x1 > viewW - 4) return Infinity;
      let c = 0;
      const st = r.stem;
      for (const b of boxes) {
        c += ov(r.x0, r.y0, r.x1, r.y1, b.x0, b.y0, b.x1, b.y1, M);
        c += 3 * ov(st[0], st[1], st[2], st[3], b.x0, b.y0, b.x1, b.y1, 2);
      }
      for (const t of stems) c += 2 * ov(r.x0, r.y0, r.x1, r.y1, t[0], t[1], t[2], t[3], 2);
      for (const d of dots) {
        if (d[4] === self) continue;
        c += 4 * ov(r.x0, r.y0, r.x1, r.y1, d[0], d[1], d[2], d[3], 0); // covering an anchor is worse
      }
      if (r.side) c += 1; // prefer above/below when all else is equal
      return c;
    };
    for (const p of placed) {
      const rec = p.rec;
      const w = rec.w || 110; const hh = rec.hgt || 44;
      let best = null; let bestCost = Infinity;
      const order = rec.slot != null ? [rec.slot, ...SLOTS.keys()] : [...SLOTS.keys()];
      for (const si of order) {
        const r = rectFor(p, w, hh, SLOTS[si], 0);
        const c = cost(r, rec);
        if (c < bestCost) { bestCost = c; best = { r, si, lift: 0 }; }
        if (c <= 1) break;
      }
      if (bestCost > 1) {
        for (let lift = 12; lift <= 120; lift += 12) { // lift above whatever it overlaps
          const r = rectFor(p, w, hh, SLOTS[0], lift);
          const c = cost(r, rec);
          if (c < bestCost) { bestCost = c; best = { r, si: 0, lift }; }
          if (c === 0) break;
        }
      }
      if (!best) best = { r: rectFor(p, w, hh, SLOTS[0], 0), si: 0, lift: 0 };
      rec.slot = best.si;
      p.dx = best.r.dx;
      p.kind = best.r.kind;
      p.lift = best.r.kind === 'above' ? best.lift : 0;
      boxes.push(best.r);
      stems.push(best.r.stem);
    }
  }

  /* ------------------------------------------------------------ focus */
  function focusQuestion(q) {
    const c = pinCands.get(q);
    if (!c || !isReady) return;
    if (mode === 'orbit') {
      const list = c.exterior.length ? c.exterior : c.interior;
      if (!list.length || !orbitHome) return;
      // the candidate that faces the camera best
      let cd = list[0]; let bestF = -Infinity;
      for (const x of list) {
        tmpV2.copy(camera.position).sub(x.pos).normalize();
        const f = x.nrm.dot(tmpV2);
        if (f > bestF) { bestF = f; cd = x; }
      }
      const hn = Math.hypot(cd.nrm.x, cd.nrm.z);
      const cur = tmpV.copy(camera.position).sub(controls.target);
      const dist = clamp(cur.length(), controls.minDistance, orbitHome.dist * 1.05);
      let theta = Math.atan2(cur.x, cur.z);
      let phi = Math.acos(clamp(cur.y / (cur.length() || 1), -1, 1));
      if (hn > 0.3) {
        const want = Math.atan2(cd.nrm.x, cd.nrm.z);
        // turn toward the element, but keep a three-quarter view (35 degrees off its normal)
        let dth = want - theta;
        while (dth > Math.PI) dth -= 2 * Math.PI;
        while (dth < -Math.PI) dth += 2 * Math.PI;
        const off = 0.6;
        if (Math.abs(dth) > off) theta += dth - Math.sign(dth) * off;
      }
      if (cd.nrm.y > 0.5) phi = Math.min(phi, 0.95); else phi = clamp(phi, 1.0, 1.32);
      const target = orbitHome.target.clone().lerp(cd.pos, 0.35);
      target.y = clamp(target.y, orbitHome.target.y - 1, orbitHome.target.y + 2);
      const pos = new T.Vector3().setFromSphericalCoords(dist, phi, theta).add(target);
      if (pos.distanceTo(camera.position) < 1.2 && target.distanceTo(controls.target) < 0.6) return;
      intro = null;
      userMovedOrbit = true;
      controls.enabled = false;
      const quat = lookQuat(pos, target, new T.Quaternion());
      fly(pos, quat, ORBIT_FOV, 900, () => {
        controls.target.copy(target);
        controls.enabled = true;
        controls.update();
        invalidate(2);
      });
    } else {
      const inside = eyeInside(camera.position);
      const list = (inside ? c.interior : c.exterior).length ? (inside ? c.interior : c.exterior) : c.interior.concat(c.exterior);
      if (!list.length) return;
      let cd = list[0]; let bestD = Infinity;
      for (const x of list) { const d = x.pos.distanceTo(camera.position); if (d < bestD) { bestD = d; cd = x; } }
      const quat = lookQuat(camera.position, cd.pos, new T.Quaternion());
      tmpV.set(0, 0, -1).applyQuaternion(quat);
      const pitch = clamp(Math.asin(clamp(tmpV.y, -1, 1)), -1.2, 1.3);
      const yaw = Math.atan2(-tmpV.x, -tmpV.z);
      tmpE.set(pitch, yaw, 0, 'YXZ');
      const q2 = new T.Quaternion().setFromEuler(tmpE);
      tour.eye.copy(camera.position);
      fly(camera.position.clone(), q2, tour.fov, 700, () => { tour.yaw = yaw; tour.pitch = pitch; });
    }
  }

  /* ------------------------------------------------------------ lifecycle */
  function onVisibility() {
    if (document.hidden) cancelFrame(); else invalidate(2);
  }
  function onContextLost(e) {
    e.preventDefault();
    contextLost = true;
    cancelFrame();
    clearTimeout(lostTimer);
    lostTimer = setTimeout(() => { if (contextLost) fail('context-lost'); }, 4000);
  }
  function onContextRestored() {
    contextLost = false;
    clearTimeout(lostTimer);
    if (renderer) renderer.shadowMap.needsUpdate = true;
    for (const { mat } of matCache.values()) mat.needsUpdate = true;
    invalidate(2);
  }

  function teardown() {
    disposed = true;
    cancelFrame();
    clearTimeout(lostTimer);
    if (ro) ro.disconnect();
    if (io) io.disconnect();
    document.removeEventListener('visibilitychange', onVisibility);
    if (document.fonts && document.fonts.removeEventListener) document.fonts.removeEventListener('loadingdone', onFonts);
    try {
      if (controls) controls.dispose();
      for (const mesh of meshes.values()) mesh.geometry.dispose();
      meshes.clear();
      for (const r of rings) for (const c of r.mesh.children) c.geometry.dispose();
      for (const { mat } of matCache.values()) mat.dispose();
      matCache.clear();
      for (const t of texCache.values()) if (t) t.dispose();
      texCache.clear();
      if (envRT) envRT.dispose();
      for (const m of [skyMesh, groundMesh, blobMesh, destRing]) {
        if (!m) continue;
        m.geometry.dispose();
        if (m.material.map && m !== groundMesh) m.material.map.dispose();
        m.material.dispose();
      }
      if (buildRings.mat) { buildRings.mat.dispose(); buildRings.disc.dispose(); }
      if (renderer) { renderer.dispose(); renderer.forceContextLoss(); }
    } catch (_) { /* best effort */ }
    renderer = null;
    container.classList.remove('v3d', 'is-ready', 'is-tour');
    for (const n of [canvas, pinLayer, stopsBar, hint]) n.remove();
  }

  /* ------------------------------------------------------------ API */
  const api = {
    ready,
    setModel(m, { keepCamera = true } = {}) {
      if (disposed || failed || !m || typeof m !== 'object') return;
      pending = { model: m, keepCamera: keepCamera && !firstModel };
      if (!renderer) return; // applied by start()
      if (!model) {
        const p = pending; pending = null;
        applyModel(p.model, p.keepCamera);
        if (revealPending) reveal();
        return;
      }
      invalidate();
    },
    setMode(md) {
      if (!isReady || disposed) return;
      if (md === 'tour' && mode !== 'tour') enterTour();
      else if (md === 'orbit' && mode !== 'orbit') enterOrbit();
    },
    getMode() { return mode; },
    resetView() {
      if (!isReady || !orbitHome) return;
      if (mode !== 'orbit') { orbitSaved = null; enterOrbit(); return; }
      const pos = orbitPos(orbitHome, orbitHome.theta, orbitHome.phi, orbitHome.dist, new T.Vector3());
      const quat = lookQuat(pos, orbitHome.target, new T.Quaternion());
      controls.enabled = false;
      intro = null;
      fly(pos, quat, ORBIT_FOV, 900, () => {
        controls.target.copy(orbitHome.target);
        controls.enabled = true;
        controls.update();
        userMovedOrbit = false;
        invalidate(2);
      });
    },
    goToStop(id) {
      if (!isReady) return;
      const s = stopById(id);
      if (!s) return;
      if (mode !== 'tour') beginTourMode();
      flyToStop(s);
    },
    setPins(list) {
      pinList = Array.isArray(list) ? list.filter((p) => p && typeof p.question === 'string') : [];
      const keep = new Set();
      for (const item of pinList) {
        keep.add(item.question);
        let rec = pinEls.get(item.question);
        if (!rec) { rec = makePinEl(item.question); pinEls.set(item.question, rec); }
        renderPinContent(rec, item);
      }
      for (const [q, rec] of pinEls) {
        if (!keep.has(q)) { rec.wrap.remove(); pinEls.delete(q); }
      }
      // keep DOM order = list order (tab order); move nodes only when the order differs (keeps focus)
      const want = pinList.map((it) => pinEls.get(it.question).wrap);
      const have = [...pinLayer.children];
      if (want.some((n, i) => have[i] !== n)) for (const n of want) pinLayer.appendChild(n);
      invalidate();
    },
    highlight(q) {
      const key = q || null;
      if (key === hlKey) return;
      hlKey = key;
      const idx = hlKey ? qKeys.indexOf(hlKey) + 1 : 0;
      HL.uHL.value = idx > 0 ? idx : 0;
      invalidate();
    },
    focus(q) { focusQuestion(q); },
    setViewInset(px) {
      const v = Math.max(0, Math.round(Number(px) || 0));
      if (v === inset.to) return;
      inset.from = inset.cur; inset.to = v; inset.t0 = now();
      invalidate();
    },
    setIdleRotation(on) {
      if (!!on === idleOn) return;
      idleOn = !!on;
      if (idleOn) idleUntil = now() + IDLE_MAX_MS;
      else if (controls) controls.autoRotate = false;
      invalidate();
    },
    stats() {
      return {
        ...st, tier: tierIdx, mode, stop: tour.stopId, highlight: hlKey, ready: isReady, failed,
        loopRunning: !!raf, dpr: renderer ? renderer.getPixelRatio() : 0, shadows: !!(renderer && renderer.shadowMap.enabled && sun && sun.castShadow),
        size: [viewW, viewH], pinsVisible: [...pinEls.values()].filter((r) => r.on).map((r) => r.q),
        idle: idleOn, three: THREE_VERSION, obstacles: obstacles.length / 4,
        eye: camera ? [+camera.position.x.toFixed(2), +(-camera.position.z).toFixed(2), +camera.position.y.toFixed(2)] : null,
      };
    },
    dispose() { if (!disposed) teardown(); },
    /** Test aid: why each pin candidate is shown or hidden in the current view. */
    debugPins() {
      if (!camera) return [];
      const set = pinSet();
      const out = [];
      for (const [q, c] of pinCands) {
        for (const md of ['exterior', 'interior']) {
          for (const cd of c[md]) {
            tmpV.copy(cd.pos).project(camera);
            const sx = ((tmpV.x + 1) / 2) * viewW; const sy = ((1 - tmpV.y) / 2) * viewH;
            tmpV2.copy(camera.position).sub(cd.pos).normalize();
            const cam = camera.position;
            const ox = cam.x; const oy = -cam.z; const oz = cam.y;
            const dx = cd.model[0] - ox; const dy = cd.model[1] - oy; const dz = cd.model[2] - oz;
            const len = Math.hypot(dx, dy, dz);
            const tHit = rayFirstHit(occ, ox, oy, oz, dx, dy, dz, 1 - 0.12 / len);
            out.push({ q, md, active: md === set, sx: Math.round(sx), sy: Math.round(sy), z: +tmpV.z.toFixed(3), facing: +cd.nrm.dot(tmpV2).toFixed(2), hitAt: tHit === Infinity ? null : +(tHit * len).toFixed(2), dist: +len.toFixed(2) });
          }
        }
      }
      return out;
    },
  };
  return api;
}
