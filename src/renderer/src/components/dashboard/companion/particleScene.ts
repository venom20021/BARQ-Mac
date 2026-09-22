/**
 * particleScene.ts — BARQ companion particle scene.
 *
 * Ported from the Lovable "particle AI companion" reference and adapted to BARQ:
 *  - the canvas is sized to its CONTAINER, not the window, so it can live inside a
 *    dashboard panel instead of owning the whole viewport
 *  - pointer gaze is normalised against the canvas rect (same reason)
 *  - a `barq` theme using BARQ's own tokens is the default
 *  - devicePixelRatio is capped by the caller (bloom + ~64k points is expensive)
 *
 * Everything else — the seven visual modes, the five reference themes, the contour
 * banding shader, the additive bloom stack — is preserved from the reference, because
 * those are its signature traits.
 */
import * as THREE from 'three'
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js'
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js'
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js'
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js'
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js'

export type SceneLevels = { mic: number; voice: number; mode: number }
export type VisualKind =
  | 'sphere'
  | 'node'
  | 'vortex'
  | 'iris'
  | 'reactor'
  | 'singularity'
  | 'gyro'
export type ThemeKind = 'barq' | 'aura' | 'ultron' | 'jarvis' | 'matrix' | 'eve'

export type Theme = {
  core: number
  edge: number
  deep: number
  accent: number
  ring: number
  bg: number
  dustA: number
  dustB: number
  peak: number
}

export const THEMES: Record<ThemeKind, Theme> = {
  /* BARQ house theme — mapped 1:1 to tailwind.config.ts tokens.
   * core   = plasma  #FF6B35   edge = cyan-300 #00F0FF
   * deep   = cyan-700 #006666  accent = holographic #A855F7 (thinking role only)
   * ring   = cyan-400 #00E6E6  bg = void-900 #0A0A0F */
  barq: {
    core: 0xff6b35, edge: 0x00f0ff, deep: 0x006666, accent: 0xa855f7,
    ring: 0x00e6e6, bg: 0x0a0a0f, dustA: 0x00b3b3, dustB: 0x80faff, peak: 0xff6b35,
  },
  aura: {
    core: 0xff7a0a, edge: 0x40b8ff, deep: 0x0f52d9, accent: 0x8c6bff,
    ring: 0x2bb6ff, bg: 0x050a14, dustA: 0x2a8cff, dustB: 0xb3ebff, peak: 0xff8c1f,
  },
  ultron: {
    core: 0xff2a12, edge: 0xff7a45, deep: 0x4a0d06, accent: 0xffb066,
    ring: 0xff5a2a, bg: 0x0b0503, dustA: 0xff4a20, dustB: 0xffc9a0, peak: 0xff3a10,
  },
  jarvis: {
    core: 0x8fe4ff, edge: 0x2f8bff, deep: 0x0a2f8c, accent: 0xcdf1ff,
    ring: 0x49a8ff, bg: 0x03070f, dustA: 0x2f8bff, dustB: 0xdff4ff, peak: 0x7fd6ff,
  },
  matrix: {
    core: 0x9dff5c, edge: 0x14ff7a, deep: 0x063a1c, accent: 0xd6ff9e,
    ring: 0x1cff86, bg: 0x000a05, dustA: 0x11cc66, dustB: 0xc8ffba, peak: 0x8cff4a,
  },
  eve: {
    core: 0xffffff, edge: 0x7fe4ff, deep: 0x2f6fb5, accent: 0xd8f6ff,
    ring: 0x9ae8ff, bg: 0x070d18, dustA: 0x8fd8ff, dustB: 0xffffff, peak: 0xbfefff,
  },
}

const rand = (a: number, b: number) => a + Math.random() * (b - a)

/* ------------------------------------------------------------------ *
 * Audio-reactive sphere — a high-density sphere whose surface spikes
 * and ripples with voice level, rendered as horizontal contour bands.
 * ------------------------------------------------------------------ */

const NOISE_GLSL = /* glsl */ `
vec3 mod289(vec3 x){ return x - floor(x * (1.0/289.0)) * 289.0; }
vec4 mod289(vec4 x){ return x - floor(x * (1.0/289.0)) * 289.0; }
vec4 permute(vec4 x){ return mod289(((x*34.0)+1.0)*x); }
vec4 taylorInvSqrt(vec4 r){ return 1.79284291400159 - 0.85373472095314 * r; }
float snoise(vec3 v){
  const vec2 C = vec2(1.0/6.0, 1.0/3.0);
  const vec4 D = vec4(0.0, 0.5, 1.0, 2.0);
  vec3 i  = floor(v + dot(v, C.yyy));
  vec3 x0 = v - i + dot(i, C.xxx);
  vec3 g = step(x0.yzx, x0.xyz);
  vec3 l = 1.0 - g;
  vec3 i1 = min(g.xyz, l.zxy);
  vec3 i2 = max(g.xyz, l.zxy);
  vec3 x1 = x0 - i1 + C.xxx;
  vec3 x2 = x0 - i2 + C.yyy;
  vec3 x3 = x0 - D.yyy;
  i = mod289(i);
  vec4 p = permute(permute(permute(
      i.z + vec4(0.0, i1.z, i2.z, 1.0))
    + i.y + vec4(0.0, i1.y, i2.y, 1.0))
    + i.x + vec4(0.0, i1.x, i2.x, 1.0));
  float n_ = 0.142857142857;
  vec3 ns = n_ * D.wyz - D.xzx;
  vec4 j = p - 49.0 * floor(p * ns.z * ns.z);
  vec4 x_ = floor(j * ns.z);
  vec4 y_ = floor(j - 7.0 * x_);
  vec4 x = x_ * ns.x + ns.yyyy;
  vec4 y = y_ * ns.x + ns.yyyy;
  vec4 h = 1.0 - abs(x) - abs(y);
  vec4 b0 = vec4(x.xy, y.xy);
  vec4 b1 = vec4(x.zw, y.zw);
  vec4 s0 = floor(b0)*2.0 + 1.0;
  vec4 s1 = floor(b1)*2.0 + 1.0;
  vec4 sh = -step(h, vec4(0.0));
  vec4 a0 = b0.xzyw + s0.xzyw*sh.xxyy;
  vec4 a1 = b1.xzyw + s1.xzyw*sh.zzww;
  vec3 p0 = vec3(a0.xy, h.x);
  vec3 p1 = vec3(a0.zw, h.y);
  vec3 p2 = vec3(a1.xy, h.z);
  vec3 p3 = vec3(a1.zw, h.w);
  vec4 norm = taylorInvSqrt(vec4(dot(p0,p0), dot(p1,p1), dot(p2,p2), dot(p3,p3)));
  p0 *= norm.x; p1 *= norm.y; p2 *= norm.z; p3 *= norm.w;
  vec4 m = max(0.6 - vec4(dot(x0,x0), dot(x1,x1), dot(x2,x2), dot(x3,x3)), 0.0);
  m = m * m;
  return 42.0 * dot(m*m, vec4(dot(p0,x0), dot(p1,x1), dot(p2,x2), dot(p3,x3)));
}
`

const BUST_VERT = /* glsl */ `
uniform float uTime;
uniform float uBreath;
uniform float uMic;
uniform float uVoice;
uniform float uMorph;
uniform float uDeform;
uniform float uIntensity;
uniform float uSpikeGain;
uniform float uPixelRatio;
uniform float uIdle;
uniform float uListen;
uniform float uThinkS;
uniform float uSpeak;
varying vec3 vPos;
varying vec3 vNormalV;
varying vec3 vViewDir;
varying float vDisp;
varying float vRipple;
varying float vSpike;

${NOISE_GLSL}

void main() {
  vec3 dir = normalize(position);
  vec3 n = normalize(normal);

  // ---- state-specific deformation ----------------------------------
  // idle: slow breathing swell
  float slow = snoise(dir * 1.4 + vec3(0.0, uTime * 0.22, 0.0));
  float calm = slow * 0.09 + uBreath * 0.025;

  // listening (mic): concentric ripples sweeping pole -> pole
  float ripple = sin(dir.y * 9.0 - uTime * 3.4) * uMic * 0.30
               + sin(dir.y * 18.0 - uTime * 5.2) * uMic * uMic * 0.18;
  vRipple = ripple;

  // thinking: slow rotating turbulence, no audio
  float swirl = snoise(dir * 2.2 + vec3(sin(uTime * 0.35), uTime * 0.5, cos(uTime * 0.35))) * 0.16;

  // speaking (AI voice): jagged high-frequency spikes + pulse
  float fast  = snoise(dir * 3.8 + vec3(uTime * 1.4, 0.0, uTime * 0.9));
  float jag   = snoise(dir * 9.0 + vec3(0.0, uTime * 3.4, 0.0));
  float spike = (fast * uVoice * 0.45 + jag * uVoice * uVoice * 0.95) * uSpikeGain;
  vSpike = abs(jag) * uVoice * uSpikeGain;
  float pulse = sin(uTime * 6.0) * uVoice * 0.06;

  float disp =
    calm * (0.5 + uIdle * 0.9) +
    ripple * uListen +
    swirl * uThinkS +
    (spike + pulse) * uSpeak;

  disp *= uDeform * uIntensity;
  vDisp = disp;

  float breathe = 1.0 + uIdle * sin(uTime * 0.9) * 0.015;
  vec3 p = (position + n * disp * uMorph * 2.0)
         * (1.0 + uVoice * 0.05 + uMic * 0.03) * breathe;

  vPos = p;
  vec4 mv = modelViewMatrix * vec4(p, 1.0);
  vNormalV = normalize(normalMatrix * n);
  vViewDir = normalize(-mv.xyz);
  gl_PointSize = (1.05 + uVoice * 0.7 + uMic * 0.5) * uPixelRatio * (9.0 / -mv.z);
  gl_Position = projectionMatrix * mv;
}
`

const BUST_FRAG = /* glsl */ `
precision highp float;
uniform float uTime;
uniform float uMorph;
uniform float uMic;
uniform float uVoice;
uniform float uThink;
uniform float uMode;
uniform float uIdle;
uniform float uListen;
uniform float uThinkS;
uniform float uSpeak;
uniform float uBright;
uniform float uSpeakBright;
uniform vec3 uColCore;
uniform vec3 uColEdge;
uniform vec3 uColDeep;
uniform vec3 uColAccent;
varying vec3 vPos;
varying vec3 vNormalV;
varying vec3 vViewDir;
varying float vDisp;
varying float vRipple;
varying float vSpike;

void main() {
  float lvl = max(uMic, uVoice);

  // ---- round particle sprite ----
  vec2 uv = gl_PointCoord - 0.5;
  float d = length(uv);
  if (d > 0.5) discard;
  float sprite = smoothstep(0.5, 0.06, d);

  // ---- horizontal contour banding modulates particle density ----
  float bands = 30.0 + uListen * 8.0 + uVoice * 12.0;
  float scroll = uTime * (0.3 + uListen * 0.9 + uThinkS * 0.2 + uSpeak * 1.4);
  float wave = sin(vPos.y * bands - scroll + vDisp * 6.0);
  float line = 0.22 + smoothstep(0.1, 0.95, wave) * 0.78;

  // emergence: particles resolve outward from the equator
  float reveal = smoothstep(0.0, 2.4, abs(vPos.y));
  if (reveal > uMorph * 1.3) discard;

  // ---- palette per state ----
  vec3 amber  = mix(uColCore, min(uColCore * 1.2 + 0.1, vec3(1.0)), 0.25 + uVoice * 0.3);
  vec3 cyan   = uColEdge;
  vec3 deep   = uColDeep;
  vec3 violet = uColAccent;
  vec3 ice    = mix(uColEdge, vec3(1.0), 0.35);

  // facing the camera = molten core, silhouette edges = cool
  float facing = abs(dot(normalize(vNormalV), normalize(vViewDir)));
  float t = smoothstep(0.95, 0.25, facing);

  vec3 coreCol = mix(amber, uColCore, uSpeak * 0.5);
  coreCol = mix(coreCol, ice, uListen * 0.5);
  coreCol = mix(coreCol, violet, uThinkS * 0.7);

  vec3 edgeCol = mix(cyan, deep, t * 0.5);
  edgeCol = mix(edgeCol, violet, uThinkS * 0.5);

  vec3 col = mix(coreCol, edgeCol, t);

  // ---- fresnel rim ----
  float fres = pow(1.0 - facing, 2.6);
  col += cyan * fres * 0.32;

  // mic ripples: cool crests racing across the surface
  float rip = smoothstep(0.08, 0.34, abs(vRipple)) * uListen;
  col += ice * rip * 0.35;

  // AI speech: warm amber crests, kept restrained
  float crest = smoothstep(0.24, 0.62, vSpike) * uSpeak;
  col += mix(uColCore, vec3(1.0), 0.12) * crest * 0.25;

  // thinking: slow violet shimmer
  col += violet * uThinkS * (0.08 + 0.05 * sin(vPos.y * 6.0 + uTime * 1.6));

  float core = 1.0 - t;
  float speakDim = (1.0 - uSpeak * 0.35) * mix(1.0, uSpeakBright, uSpeak);
  float glow = (0.24 + fres * 0.16 + lvl * 0.08 + crest * 0.07 + rip * 0.1) * speakDim * uBright;
  col += coreCol * core * core * (0.3 - uSpeak * 0.1);
  float alpha = sprite * line
             * (0.17 + fres * 0.16 + core * core * 0.26 + lvl * 0.05 + crest * 0.05 + rip * 0.07)
             * uMorph * speakDim * uBright;
  gl_FragColor = vec4(col * glow, alpha);
}
`

const DUST_VERT = /* glsl */ `
uniform float uTime;
uniform float uPixelRatio;
uniform float uLevel;
attribute float aSeed;
varying float vSeed;
void main() {
  vSeed = aSeed;
  vec3 p = position;
  p.y += sin(uTime * (0.2 + aSeed * 0.3) + aSeed * 40.0) * 0.5;
  p.x += cos(uTime * 0.16 + aSeed * 22.0) * 0.4;
  vec4 mv = modelViewMatrix * vec4(p, 1.0);
  gl_PointSize = (1.4 + aSeed * 2.2 + uLevel * 2.0) * uPixelRatio * (9.0 / -mv.z);
  gl_Position = projectionMatrix * mv;
}
`

const DUST_FRAG = /* glsl */ `
precision highp float;
uniform float uTime;
uniform float uLevel;
uniform vec3 uColA;
uniform vec3 uColB;
varying float vSeed;
void main() {
  vec2 uv = gl_PointCoord - 0.5;
  float d = length(uv);
  if (d > 0.5) discard;
  float falloff = smoothstep(0.5, 0.0, d);
  float twinkle = 0.6 + 0.4 * sin(uTime * 2.0 + vSeed * 60.0);
  vec3 col = mix(uColA, uColB, vSeed);
  gl_FragColor = vec4(col, falloff * (0.10 + vSeed * 0.16 + uLevel * 0.12) * twinkle);
}
`

export type SceneOptions = {
  /** Cap for devicePixelRatio. Bloom over ~64k points is expensive; 1.5 is a good
   *  compromise for a dashboard panel. */
  maxPixelRatio?: number
  /** Initial theme. Defaults to BARQ's house theme. */
  theme?: ThemeKind
  /** Initial visual mode. */
  visual?: VisualKind
}

export function createParticleScene(canvas: HTMLCanvasElement, options: SceneOptions = {}) {
  const maxPixelRatio = options.maxPixelRatio ?? 1.5
  const renderer = new THREE.WebGLRenderer({
    canvas,
    antialias: true,
    alpha: false,
    powerPreference: 'high-performance',
  })
  renderer.setClearColor(THEMES.barq.bg, 1)

  const scene = new THREE.Scene()
  scene.fog = new THREE.FogExp2(THEMES.barq.bg, 0.028)
  const camera = new THREE.PerspectiveCamera(46, 1, 0.1, 120)

  /* ------------- central figure (swappable, contour shader) ------------- */
  const makeGeometry = (kind: VisualKind): THREE.BufferGeometry => {
    switch (kind) {
      case 'node':
        return new THREE.IcosahedronGeometry(2.15, 5)
      case 'vortex':
        return new THREE.TorusKnotGeometry(1.35, 0.42, 900, 90, 2, 3)
      case 'iris': {
        const rings: THREE.BufferGeometry[] = []
        for (let i = 0; i < 9; i++) {
          const r = 0.45 + i * 0.24
          const g = new THREE.RingGeometry(r, r + 0.15, 420, 4)
          g.translate(0, 0, (i % 2 ? -1 : 1) * i * 0.07)
          rings.push(g)
        }
        return mergeGeometries(rings, false) ?? new THREE.SphereGeometry(2.05, 160, 100)
      }
      case 'reactor':
        return new THREE.SphereGeometry(1.0, 220, 140)
      case 'singularity':
        // dark, dense event-horizon sphere
        return new THREE.SphereGeometry(1.55, 300, 190)
      case 'gyro':
        // tiny bright core at the centre of the rings
        return new THREE.SphereGeometry(0.34, 120, 80)
      case 'sphere':
      default:
        return new THREE.SphereGeometry(2.05, 320, 200)
    }
  }

  let visual: VisualKind = options.visual ?? 'sphere'
  let bustGeo = makeGeometry(visual)

  const uniforms = {
    uTime: { value: 0 },
    uMorph: { value: 0 },
    uMic: { value: 0 },
    uVoice: { value: 0 },
    uMode: { value: 0 },
    uThink: { value: 0 },
    uBreath: { value: 0 },
    uIdle: { value: 1 },
    uListen: { value: 0 },
    uThinkS: { value: 0 },
    uSpeak: { value: 0 },
    uDeform: { value: 1 },
    uBright: { value: 1 },
    uIntensity: { value: 1 },
    uSpikeGain: { value: 1 },
    uSpeakBright: { value: 1 },
    uPixelRatio: { value: 1 },
    uColCore: { value: new THREE.Color(THEMES.barq.core) },
    uColEdge: { value: new THREE.Color(THEMES.barq.edge) },
    uColDeep: { value: new THREE.Color(THEMES.barq.deep) },
    uColAccent: { value: new THREE.Color(THEMES.barq.accent) },
  }

  const bustMat = new THREE.ShaderMaterial({
    uniforms,
    vertexShader: BUST_VERT,
    fragmentShader: BUST_FRAG,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  })

  const bust = new THREE.Points(bustGeo, bustMat)
  const bustGroup = new THREE.Group()
  bustGroup.add(bust)

  // crystalline lattice, shown for the neural node
  const latticeGeo = new THREE.WireframeGeometry(new THREE.IcosahedronGeometry(2.18, 2))
  const latticeMat = new THREE.LineBasicMaterial({
    color: 0x00f0ff,
    transparent: true,
    opacity: 0.22,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  })
  const lattice = new THREE.LineSegments(latticeGeo, latticeMat)
  lattice.visible = false
  bustGroup.add(lattice)

  /* ------- singularity: thin accretion ring bending around the sphere ------- */
  const extraGeos: THREE.BufferGeometry[] = []
  const trackGeo = <T extends THREE.BufferGeometry>(g: T): T => {
    extraGeos.push(g)
    return g
  }

  const accretionRig = new THREE.Group()
  {
    const disc = trackGeo(new THREE.TorusGeometry(2.5, 0.075, 12, 720))
    const ring = new THREE.Points(disc, bustMat)
    ring.rotation.x = Math.PI / 2 - 0.22
    accretionRig.add(ring)

    // lensed arc: the light bent up and over the event horizon
    const arcGeo = trackGeo(new THREE.TorusGeometry(2.5, 0.05, 10, 520, Math.PI))
    const arc = new THREE.Points(arcGeo, bustMat)
    arc.rotation.x = Math.PI / 2 - 0.22
    arc.rotation.y = Math.PI / 2
    arc.scale.set(1, 0.82, 1)
    accretionRig.add(arc)

    const halo = trackGeo(new THREE.TorusGeometry(1.75, 0.012, 8, 480))
    accretionRig.add(new THREE.Points(halo, bustMat))
  }
  accretionRig.visible = false
  bustGroup.add(accretionRig)

  /* ---------------- gyroscope: three rings on different axes ---------------- */
  const gyroRig = new THREE.Group()
  const gyroRings: THREE.Points[] = []
  {
    const radii = [2.3, 1.85, 1.4]
    const tilts: [number, number, number][] = [
      [Math.PI / 2, 0, 0],
      [0, Math.PI / 2, 0.35],
      [0.6, 0.4, Math.PI / 2],
    ]
    radii.forEach((r, i) => {
      const g = trackGeo(new THREE.TorusGeometry(r, 0.045, 10, 600))
      const pts = new THREE.Points(g, bustMat)
      const t = tilts[i]!
      pts.rotation.set(t[0], t[1], t[2])
      gyroRings.push(pts)
      gyroRig.add(pts)
    })
  }
  gyroRig.visible = false
  bustGroup.add(gyroRig)

  /* ---------------- arc reactor (solid built assembly) ---------------- */
  const reactorGeos: THREE.BufferGeometry[] = []
  const reactorMats: THREE.Material[] = []
  const track = <G extends THREE.BufferGeometry>(g: G) => {
    reactorGeos.push(g)
    return g
  }
  const shellMat = new THREE.MeshBasicMaterial({
    color: 0x9fd8ff,
    transparent: true,
    opacity: 0.4,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  })
  const coilMat = new THREE.MeshBasicMaterial({
    color: 0x6fc4ff,
    transparent: true,
    opacity: 0.32,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  })
  const coreMat = new THREE.MeshBasicMaterial({
    color: 0xffe7b0,
    transparent: true,
    opacity: 0.55,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  })
  const haloUniforms = {
    uTime: { value: 0 },
    uLevel: { value: 0 },
    uBright: { value: 1 },
    uColCore: { value: new THREE.Color(THEMES.barq.core) },
    uColEdge: { value: new THREE.Color(THEMES.barq.edge) },
  }
  const haloMat = new THREE.ShaderMaterial({
    uniforms: haloUniforms,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    side: THREE.DoubleSide,
    vertexShader: /* glsl */ `
      varying vec2 vUv;
      void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }
    `,
    fragmentShader: /* glsl */ `
      precision highp float;
      uniform float uTime; uniform float uLevel; uniform float uBright;
      uniform vec3 uColCore; uniform vec3 uColEdge;
      varying vec2 vUv;
      void main(){
        vec2 p = vUv - 0.5;
        float d = length(p) * 2.0;
        if (d > 1.0) discard;
        float ang = atan(p.y, p.x);
        float rays = 0.5 + 0.5 * sin(ang * 12.0 + uTime * 1.2);
        float core = smoothstep(0.55, 0.0, d);
        float glow = smoothstep(1.0, 0.25, d);
        vec3 col = mix(uColEdge, mix(uColCore, vec3(1.0), 0.55), core);
        float a = (core * (0.3 + uLevel * 0.25) + glow * 0.1 * (0.7 + rays * 0.5)) * uBright;
        gl_FragColor = vec4(col * (0.7 + uLevel * 0.5), a);
      }
    `,
  })
  reactorMats.push(shellMat, coilMat, coreMat, haloMat)

  const reactorRig = new THREE.Group()
  const reactorSpin = new THREE.Group() // coils + spokes rotate
  const reactorCore = new THREE.Group()

  // outer housing rings
  ;[
    { r: 2.3, t: 0.045 },
    { r: 2.12, t: 0.022 },
    { r: 1.62, t: 0.03 },
    { r: 1.12, t: 0.036 },
  ].forEach(({ r, t }) => {
    reactorRig.add(new THREE.Mesh(track(new THREE.TorusGeometry(r, t, 16, 160)), shellMat))
  })

  // outer housing plate (thin ring band)
  const plateMat = new THREE.MeshBasicMaterial({
    color: 0x2f6f9f,
    transparent: true,
    opacity: 0.16,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  })
  reactorMats.push(plateMat)
  const plate = new THREE.Mesh(track(new THREE.RingGeometry(2.12, 2.3, 160, 1)), plateMat)
  plate.position.z = -0.02
  reactorRig.add(plate)

  // copper coils around the ring
  const COILS = 10
  for (let i = 0; i < COILS; i++) {
    const a = (i / COILS) * Math.PI * 2
    const cx = Math.cos(a) * 1.87
    const cy = Math.sin(a) * 1.87

    const coil = new THREE.Group()
    for (let w = 0; w < 7; w++) {
      const ring = new THREE.Mesh(track(new THREE.TorusGeometry(0.19, 0.018, 10, 30)), coilMat)
      ring.position.set(0, 0, 0)
      ring.rotation.y = Math.PI / 2
      ring.position.x = (w - 3) * 0.045
      coil.add(ring)
    }
    coil.position.set(cx, cy, 0)
    coil.rotation.z = a
    reactorSpin.add(coil)

    // spoke from inner ring to coil
    const spoke = new THREE.Mesh(track(new THREE.BoxGeometry(0.52, 0.028, 0.028)), shellMat)
    spoke.position.set(Math.cos(a) * 1.4, Math.sin(a) * 1.4, 0)
    spoke.rotation.z = a
    reactorSpin.add(spoke)
  }
  reactorRig.add(reactorSpin)

  // triangular core cage
  const cage = new THREE.Mesh(track(new THREE.TorusGeometry(0.82, 0.045, 3, 3)), shellMat)
  cage.rotation.z = Math.PI / 2
  reactorCore.add(cage)

  // glowing core discs
  const halo = new THREE.Mesh(track(new THREE.PlaneGeometry(2.6, 2.6)), haloMat)
  reactorCore.add(halo)
  const coreDisc = new THREE.Mesh(track(new THREE.CircleGeometry(0.62, 64)), coreMat)
  coreDisc.position.z = 0.02
  reactorCore.add(coreDisc)
  reactorRig.add(reactorCore)

  reactorRig.visible = false
  bustGroup.add(reactorRig)
  scene.add(bustGroup)

  /* ---------------- ambient dust ---------------- */
  const DUST = 9000
  const dustPos = new Float32Array(DUST * 3)
  const dustSeed = new Float32Array(DUST)
  for (let i = 0; i < DUST; i++) {
    const r = rand(3.5, 18)
    const th = rand(0, Math.PI * 2)
    dustPos[i * 3] = Math.cos(th) * r
    dustPos[i * 3 + 1] = rand(-5.5, 6.5)
    dustPos[i * 3 + 2] = Math.sin(th) * r * 0.6 - 2
    dustSeed[i] = Math.random()
  }
  const dustGeo = new THREE.BufferGeometry()
  dustGeo.setAttribute('position', new THREE.BufferAttribute(dustPos, 3))
  dustGeo.setAttribute('aSeed', new THREE.BufferAttribute(dustSeed, 1))
  const dustUniforms = {
    uTime: { value: 0 },
    uPixelRatio: { value: 1 },
    uLevel: { value: 0 },
    uColA: { value: new THREE.Color(THEMES.barq.dustA) },
    uColB: { value: new THREE.Color(THEMES.barq.dustB) },
  }
  const dustMat = new THREE.ShaderMaterial({
    uniforms: dustUniforms,
    vertexShader: DUST_VERT,
    fragmentShader: DUST_FRAG,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  })
  scene.add(new THREE.Points(dustGeo, dustMat))

  /* ---------------- concentric dashed rings ---------------- */
  const ringGroup = new THREE.Group()
  const ringMats: THREE.LineDashedMaterial[] = []
  const ringGeos: THREE.BufferGeometry[] = []
  for (let r = 2.3; r <= 4.4; r += 0.42) {
    const pts: THREE.Vector3[] = []
    const segs = 220
    for (let s = 0; s <= segs; s++) {
      const th = (s / segs) * Math.PI * 2
      pts.push(new THREE.Vector3(Math.cos(th) * r, Math.sin(th) * r * 0.98, 0))
    }
    const g = new THREE.BufferGeometry().setFromPoints(pts)
    const m = new THREE.LineDashedMaterial({
      color: THEMES.barq.ring,
      dashSize: 0.22,
      gapSize: 0.16,
      transparent: true,
      opacity: 0.16 + (4.4 - r) * 0.05,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    })
    ringGeos.push(g)
    ringMats.push(m)
    const line = new THREE.Line(g, m)
    line.computeLineDistances()
    ringGroup.add(line)
  }
  ringGroup.position.set(0, -0.1, -1.6)
  scene.add(ringGroup)

  /* ---------------- wireframe digital mountains ---------------- */
  const terrainGeo = new THREE.PlaneGeometry(46, 20, 130, 60)
  const terrainUniforms = {
    uTime: { value: 0 },
    uLevel: { value: 0 },
    uColLow: { value: new THREE.Color(THEMES.barq.deep) },
    uColHigh: { value: new THREE.Color(THEMES.barq.peak) },
  }
  const terrainMat = new THREE.ShaderMaterial({
    uniforms: terrainUniforms,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
    wireframe: true,
    vertexShader: /* glsl */ `
      uniform float uTime;
      uniform float uLevel;
      varying float vH;
      void main() {
        vec3 p = position;
        float h =
          sin(p.x * 0.34 + uTime * 0.45) * 0.85 +
          sin(p.x * 0.11 + p.y * 0.3 - uTime * 0.3) * 1.25 +
          cos(p.y * 0.42 + uTime * 0.2) * 0.55;
        float edge = smoothstep(2.0, 9.0, abs(p.x));
        h *= 0.35 + edge;
        h += uLevel * 1.6 * sin(p.x * 0.75 - uTime * 3.2) * edge;
        vH = h;
        p.z += h;
        gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
      }
    `,
    fragmentShader: /* glsl */ `
      precision highp float;
      uniform float uLevel;
      uniform vec3 uColLow;
      uniform vec3 uColHigh;
      varying float vH;
      void main() {
        float t = clamp(vH * 0.35 + 0.5, 0.0, 1.0);
        vec3 col = mix(uColLow, uColHigh, smoothstep(0.55, 1.0, t));
        gl_FragColor = vec4(col * (0.35 + uLevel * 0.9), 0.14 + uLevel * 0.18);
      }
    `,
  })
  const terrain = new THREE.Mesh(terrainGeo, terrainMat)
  terrain.rotation.x = -Math.PI / 2
  terrain.position.set(0, -4.6, -4.0)
  scene.add(terrain)

  /* ---------------- bloom ---------------- */
  const composer = new EffectComposer(renderer)
  composer.addPass(new RenderPass(scene, camera))
  const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), 1.0, 0.6, 0.22)
  composer.addPass(bloom)
  composer.addPass(new OutputPass())

  /* ---------------- interaction ---------------- */
  let targetAzimuth = 0
  let targetPolar = 0
  let azimuth = 0
  let polar = 0
  let targetDist = 8.0
  let dist = 14
  let dragging = false
  let lastX = 0
  let lastY = 0
  let gazeX = 0
  let gazeY = 0

  const onPointerDown = (e: PointerEvent) => {
    dragging = true
    lastX = e.clientX
    lastY = e.clientY
  }
  const onPointerUp = () => {
    dragging = false
  }
  const onPointerMove = (e: PointerEvent) => {
    // Normalise against the canvas rect, not the window — the scene is a panel.
    const rect = canvas.getBoundingClientRect()
    const w = rect.width || 1
    const h = rect.height || 1
    const nx = (e.clientX - rect.left) / w - 0.5
    const ny = (e.clientY - rect.top) / h - 0.5
    gazeX = nx
    gazeY = ny
    if (dragging) {
      targetAzimuth += (e.clientX - lastX) * 0.004
      targetPolar = Math.max(-0.45, Math.min(0.45, targetPolar - (e.clientY - lastY) * 0.003))
      lastX = e.clientX
      lastY = e.clientY
    } else {
      targetAzimuth = targetAzimuth * 0.92 + nx * 0.24 * 0.08
      targetPolar = targetPolar * 0.92 + -ny * 0.18 * 0.08
    }
  }
  const onWheel = (e: WheelEvent) => {
    targetDist = Math.max(5.5, Math.min(15, targetDist + e.deltaY * 0.004))
  }

  canvas.addEventListener('pointerdown', onPointerDown)
  window.addEventListener('pointerup', onPointerUp)
  window.addEventListener('pointermove', onPointerMove)
  canvas.addEventListener('wheel', onWheel, { passive: true })

  const resize = () => {
    // Container-relative: the canvas fills whatever box the dashboard gives it.
    const parent = canvas.parentElement
    const w = Math.max(1, parent?.clientWidth || canvas.clientWidth || window.innerWidth)
    const h = Math.max(1, parent?.clientHeight || canvas.clientHeight || window.innerHeight)
    const pr = Math.min(window.devicePixelRatio || 1, maxPixelRatio)
    renderer.setPixelRatio(pr)
    renderer.setSize(w, h, false)
    camera.aspect = w / h
    camera.updateProjectionMatrix()
    dustUniforms.uPixelRatio.value = pr
    uniforms.uPixelRatio.value = pr
    composer.setPixelRatio(pr)
    composer.setSize(w, h)
    bloom.setSize(w * pr, h * pr)
  }
  resize()
  window.addEventListener('resize', resize)
  // The dashboard panel can resize without a window resize (side panels, layout shifts).
  const observer =
    typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => resize()) : null
  if (observer && canvas.parentElement) observer.observe(canvas.parentElement)

  /* ---------------- loop ---------------- */
  const levels: SceneLevels = { mic: 0, voice: 0, mode: 0 }
  let smoothMic = 0
  let smoothVoice = 0
  let morph = 0
  let start = performance.now()
  let raf = 0
  let nodPhase = 0
  let pendingVisual: VisualKind | null = null
  let brightness = 1
  const calibration = { micGain: 1, spike: 1, speakBright: 1, intensity: 1 }
  let glanceX = 0
  let glanceTimer = 0

  const damp = (cur: number, target: number, k: number, dt: number) =>
    cur + (target - cur) * Math.min(1, dt * k)

  let prev = performance.now()

  const loop = () => {
    raf = requestAnimationFrame(loop)
    const now = performance.now()
    const dt = Math.min((now - prev) / 1000, 0.05)
    prev = now
    const elapsed = (now - start) / 1000

    if (pendingVisual) {
      // crossfade out before swapping geometry
      morph += (0 - morph) * Math.min(1, dt * 6)
      if (morph < 0.03) {
        swapGeometry(pendingVisual)
        pendingVisual = null
        start = now
        morph = 0
      }
    } else {
      const target = Math.min(1, Math.max(0, (elapsed - 0.15) / 2.2))
      morph += (target - morph) * Math.min(1, dt * 2.4)
    }

    smoothMic = damp(smoothMic, Math.min(1.5, levels.mic * calibration.micGain), 12, dt)
    smoothVoice = damp(smoothVoice, Math.min(1.5, levels.voice), 12, dt)

    const mode = levels.mode
    const listening = mode === 1 ? 1 : 0
    const thinking = mode === 2 ? 1 : 0
    const speaking = mode === 3 ? 1 : 0

    // head/shoulder motion
    glanceTimer -= dt
    if (glanceTimer <= 0) {
      glanceTimer = rand(1.6, 3.4)
      glanceX = thinking ? rand(-0.3, 0.3) : rand(-0.09, 0.09)
    }
    nodPhase += dt * (speaking ? 2.2 + smoothVoice * 3.2 : listening ? 0.9 : 0.55)

    const yawTarget =
      Math.sin(elapsed * 0.23) * 0.06 + glanceX * (thinking ? 1 : 0.45) - (listening ? gazeX * 0.3 : 0)
    const pitchTarget =
      Math.sin(nodPhase) * 0.05 * (speaking ? 1.4 : 0.6) +
      (listening ? 0.04 + gazeY * 0.15 : 0) -
      thinking * 0.06
    const rollTarget = listening * 0.05 - thinking * 0.04 + Math.sin(elapsed * 0.19) * 0.025

    bustGroup.rotation.y = damp(bustGroup.rotation.y, yawTarget, 2.6, dt)
    bustGroup.rotation.x = damp(bustGroup.rotation.x, pitchTarget, 4.0, dt)
    bustGroup.rotation.z = damp(bustGroup.rotation.z, rollTarget, 2.2, dt)
    bustGroup.position.y = Math.sin(elapsed * 0.6) * 0.06

    // per-shape idle spin
    if (visual === 'node') {
      bust.rotation.y += dt * 0.18
      bust.rotation.x += dt * 0.07
      lattice.rotation.copy(bust.rotation)
    } else if (visual === 'vortex') {
      bust.rotation.y += dt * 0.32
      bust.rotation.z += dt * 0.12
    } else if (visual === 'reactor') {
      const lv = Math.max(smoothMic, smoothVoice)
      reactorSpin.rotation.z += dt * (0.25 + lv * 0.9) * calibration.intensity
      reactorRig.rotation.z -= dt * 0.05
      const pulse = 1 + Math.sin(elapsed * 2.1) * 0.03 + lv * 0.12
      reactorCore.scale.setScalar(pulse)
      coreMat.opacity = (0.4 + lv * 0.25) * calibration.speakBright * brightness
      haloUniforms.uTime.value = elapsed
      haloUniforms.uLevel.value = lv
      reactorRig.scale.setScalar(0.85 + morph * 0.15)
    } else if (visual === 'singularity') {
      const lv = Math.max(smoothMic, smoothVoice)
      accretionRig.rotation.y += dt * (0.22 + lv * 0.5) * calibration.intensity
      accretionRig.rotation.z = Math.sin(elapsed * 0.18) * 0.06
      bust.rotation.y += dt * 0.05
      bust.scale.setScalar(1 + lv * 0.05)
    } else if (visual === 'gyro') {
      const lv = Math.max(smoothMic, smoothVoice)
      const k = calibration.intensity
      gyroRings[0]!.rotation.z += dt * (0.35 + lv * 0.7) * k
      gyroRings[1]!.rotation.x += dt * (0.26 + lv * 0.5) * k
      gyroRings[2]!.rotation.y += dt * (0.44 + lv * 0.9) * k
      gyroRig.rotation.y += dt * 0.06
      bust.scale.setScalar(1 + Math.sin(elapsed * 1.6) * 0.05 + lv * 0.25)
    } else if (visual === 'iris') {
      bust.rotation.z += dt * (0.1 + Math.max(smoothMic, smoothVoice) * 0.5)
      bust.rotation.x = Math.sin(elapsed * 0.25) * 0.12
    }

    const breathRate = speaking ? 1.25 : listening ? 0.9 : 0.72
    uniforms.uBreath.value = damp(
      uniforms.uBreath.value,
      Math.sin(elapsed * breathRate) * (0.6 + smoothVoice * 0.3),
      8,
      dt,
    )

    uniforms.uTime.value = elapsed
    uniforms.uMorph.value = morph * morph * (3 - 2 * morph)
    uniforms.uMic.value = smoothMic
    uniforms.uVoice.value = smoothVoice
    uniforms.uMode.value = damp(uniforms.uMode.value, mode, 4, dt)
    uniforms.uThink.value = damp(uniforms.uThink.value, thinking, 3, dt)

    // state presets — crossfaded so each mode has its own motion + palette
    const idle = mode === 0 ? 1 : 0
    uniforms.uIdle.value = damp(uniforms.uIdle.value, idle, 2.2, dt)
    uniforms.uListen.value = damp(uniforms.uListen.value, listening, 2.8, dt)
    uniforms.uThinkS.value = damp(uniforms.uThinkS.value, thinking, 2.4, dt)
    uniforms.uSpeak.value = damp(uniforms.uSpeak.value, speaking, 3.2, dt)

    azimuth += (targetAzimuth - azimuth) * Math.min(1, dt * 3)
    polar += (targetPolar - polar) * Math.min(1, dt * 3)
    dist += (targetDist - dist) * Math.min(1, dt * 1.4)

    camera.position.set(
      Math.sin(azimuth) * Math.cos(polar) * dist,
      Math.sin(polar) * dist + Math.sin(elapsed * 0.4) * 0.08,
      Math.cos(azimuth) * Math.cos(polar) * dist,
    )
    camera.lookAt(0, 0, 0)

    const level = Math.max(smoothMic, smoothVoice)
    dustUniforms.uTime.value = elapsed
    dustUniforms.uLevel.value = level
    terrainUniforms.uTime.value = elapsed
    terrainUniforms.uLevel.value = level
    ringGroup.rotation.z = elapsed * 0.05
    bloom.strength =
      (0.2 + level * 0.18 - (speaking ? 0.05 : 0) + (thinking ? 0.03 : 0)) *
      brightness *
      (speaking ? calibration.speakBright : 1)

    composer.render()
  }
  loop()

  function swapGeometry(kind: VisualKind) {
    visual = kind
    const next = makeGeometry(kind)
    bust.geometry = next
    bustGeo.dispose()
    bustGeo = next
    bust.rotation.set(0, 0, 0)
    lattice.rotation.set(0, 0, 0)
    lattice.visible = kind === 'node'
    reactorRig.visible = kind === 'reactor'
    accretionRig.visible = kind === 'singularity'
    accretionRig.rotation.set(0, 0, 0)
    gyroRig.visible = kind === 'gyro'
    gyroRig.rotation.set(0, 0, 0)
    bust.visible = kind !== 'reactor'
    uniforms.uDeform.value =
      kind === 'iris' ? 0.5 : kind === 'singularity' ? 0.22 : kind === 'gyro' ? 0.3 : 1
  }

  const applyTheme = (t: Theme) => {
    uniforms.uColCore.value.setHex(t.core)
    uniforms.uColEdge.value.setHex(t.edge)
    uniforms.uColDeep.value.setHex(t.deep)
    uniforms.uColAccent.value.setHex(t.accent)
    dustUniforms.uColA.value.setHex(t.dustA)
    dustUniforms.uColB.value.setHex(t.dustB)
    terrainUniforms.uColLow.value.setHex(t.deep)
    terrainUniforms.uColHigh.value.setHex(t.peak)
    ringMats.forEach((m) => m.color.setHex(t.ring))
    latticeMat.color.setHex(t.edge)
    shellMat.color.setHex(t.edge)
    coilMat.color.setHex(t.ring)
    coreMat.color.setHex(t.core)
    haloUniforms.uColCore.value.setHex(t.core)
    haloUniforms.uColEdge.value.setHex(t.edge)
    renderer.setClearColor(t.bg, 1)
    ;(scene.fog as THREE.FogExp2).color.setHex(t.bg)
  }

  // Apply the initial theme/visual before the first frame is user-visible.
  applyTheme(THEMES[options.theme ?? 'barq'] ?? THEMES.barq)
  swapGeometry(visual)

  return {
    levels,
    setVisual(kind: VisualKind) {
      if (kind === visual && !pendingVisual) return
      pendingVisual = kind
    },
    setBrightness(value: number) {
      brightness = Math.max(0.2, Math.min(2, value))
      uniforms.uBright.value = brightness
      haloUniforms.uBright.value = brightness
    },
    setTheme(kind: ThemeKind) {
      applyTheme(THEMES[kind] ?? THEMES.barq)
    },
    setCustomTheme(partial: Partial<Theme>, base: ThemeKind = 'barq') {
      applyTheme({ ...(THEMES[base] ?? THEMES.barq), ...partial })
    },
    setCalibration(next: Partial<typeof calibration>) {
      Object.assign(calibration, next)
      uniforms.uIntensity.value = calibration.intensity
      uniforms.uSpikeGain.value = calibration.spike
      uniforms.uSpeakBright.value = calibration.speakBright
    },
    restartEmergence() {
      start = performance.now()
      morph = 0
    },
    dispose() {
      cancelAnimationFrame(raf)
      observer?.disconnect()
      window.removeEventListener('resize', resize)
      window.removeEventListener('pointerup', onPointerUp)
      window.removeEventListener('pointermove', onPointerMove)
      canvas.removeEventListener('pointerdown', onPointerDown)
      canvas.removeEventListener('wheel', onWheel)
      bustGeo.dispose()
      bustMat.dispose()
      latticeGeo.dispose()
      latticeMat.dispose()
      reactorGeos.forEach((g) => g.dispose())
      extraGeos.forEach((g) => g.dispose())
      reactorMats.forEach((m) => m.dispose())
      dustGeo.dispose()
      dustMat.dispose()
      ringGeos.forEach((g) => g.dispose())
      ringMats.forEach((m) => m.dispose())
      terrainGeo.dispose()
      terrainMat.dispose()
      composer.dispose()
      renderer.dispose()
    },
  }
}

export type ParticleScene = ReturnType<typeof createParticleScene>
