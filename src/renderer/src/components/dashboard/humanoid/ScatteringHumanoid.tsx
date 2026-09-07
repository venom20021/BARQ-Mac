// ─── ScatteringHumanoid.tsx ───────────────────────────────────────────
// Smooth holographic bust: horizontal scanlines with enough density
// to form continuous visible lines, not sparse dots.

import { useRef, useMemo, useEffect } from 'react'
import { useFrame } from '@react-three/fiber'
import type { Points } from 'three'
import * as THREE from 'three'
import { scatteringVertexShader, scatteringFragmentShader } from './scatteringShaders'

interface Props {
  progress: number
  audioLevel: number
}

// ── Bust profile: radius at each Y height ────────────────────────────
function radius(y: number): number {
  // Head: y=0.95..1.55
  if (y >= 0.95 && y <= 1.55) {
    const t = (y - 0.95) / 0.6
    return 0.12 + 0.22 * Math.sin(t * Math.PI) - 0.04 * t * t
  }
  // Neck: y=0.7..0.95
  if (y >= 0.7 && y < 0.95) return 0.07 + ((y - 0.7) / 0.25) * 0.03
  // Shoulder transition: y=0.55..0.7
  if (y >= 0.55 && y < 0.7) return 0.09 + ((y - 0.55) / 0.15) * 0.01
  // Shoulders (widest): y=0.3..0.55
  if (y >= 0.3 && y < 0.55) {
    const t = (y - 0.3) / 0.25
    return 0.32 + Math.sin(t * Math.PI * 0.5) * 0.3
  }
  // Upper torso: y=0.0..0.3
  if (y >= 0.0 && y < 0.3) return 0.30 + (y / 0.3) * 0.04
  // Lower torso: y=-0.5..0.0
  if (y >= -0.5 && y < 0.0) return 0.26 - ((-y) / 0.5) * 0.02
  return 0
}

function genCoords(count: number) {
  const targets = new Float32Array(count * 3)
  const randoms = new Float32Array(count * 3)
  const flows = new Float32Array(count)

  // Define rings: Y position and particles per ring
  // More rings in head/shoulders for smooth curves
  const rings: Array<{ y: number; n: number }> = []

  // Lower torso: 10 rings
  for (let i = 0; i < 10; i++) rings.push({ y: -0.5 + i * 0.05, n: 150 })
  // Upper torso: 6 rings
  for (let i = 0; i < 6; i++) rings.push({ y: 0.0 + i * 0.05, n: 180 })
  // Shoulders: 5 rings (wider, more particles)
  for (let i = 0; i < 5; i++) rings.push({ y: 0.3 + i * 0.05, n: 220 })
  // Neck transition: 3 rings
  for (let i = 0; i < 3; i++) rings.push({ y: 0.55 + i * 0.05, n: 80 })
  // Neck: 5 rings
  for (let i = 0; i < 5; i++) rings.push({ y: 0.7 + i * 0.05, n: 60 })
  // Head: 12 rings (most detail)
  for (let i = 0; i < 12; i++) rings.push({ y: 0.95 + i * 0.05, n: 200 })

  let cursor = 0

  for (const ring of rings) {
    const r = radius(ring.y)
    if (r <= 0) continue
    const n = Math.min(ring.n, count - cursor)

    for (let i = 0; i < n && cursor < count; i++) {
      const theta = (i / n) * Math.PI * 2
      const idx = cursor * 3

      // Particles sit ON the surface with slight jitter
      targets[idx] = r * Math.cos(theta)
      targets[idx + 1] = ring.y
      targets[idx + 2] = r * Math.sin(theta)

      randoms[idx] = (Math.random() - 0.5) * 1.5
      randoms[idx + 1] = Math.random() * 1.5
      randoms[idx + 2] = (Math.random() - 0.5) * 1.5

      flows[cursor] = 0.3 + (1.0 - (ring.y + 0.5) / 2.1) * 0.7
      cursor++
    }
  }

  // Fill remaining
  while (cursor < count) {
    const y = -0.5 + Math.random() * 2.1
    const r = radius(y) * 0.9
    const theta = Math.random() * Math.PI * 2
    const idx = cursor * 3
    targets[idx] = r * Math.cos(theta)
    targets[idx + 1] = y
    targets[idx + 2] = r * Math.sin(theta)
    randoms[idx] = (Math.random() - 0.5) * 1.5
    randoms[idx + 1] = Math.random() * 1.5
    randoms[idx + 2] = (Math.random() - 0.5) * 1.5
    flows[cursor] = 0.5
    cursor++
  }

  return { targets, randoms, flows }
}

export function ScatteringHumanoid({ progress, audioLevel }: Props): JSX.Element {
  const pointsRef = useRef<Points>(null!)
  const progressRef = useRef(0)
  const audioLevelRef = useRef(0)
  const COUNT = 8000

  const coords = useMemo(() => genCoords(COUNT), [])

  const uniforms = useMemo(() => ({
    uTime: { value: 0 },
    uProgress: { value: 0 },
    uAudio: { value: 0 },
  }), [])

  useEffect(() => { progressRef.current = progress }, [progress])
  useEffect(() => { audioLevelRef.current = audioLevel }, [audioLevel])

  useFrame(({ clock }) => {
    if (!pointsRef.current) return
    const mat = pointsRef.current.material as THREE.ShaderMaterial
    if (!mat?.uniforms) return
    mat.uniforms.uTime.value = clock.elapsedTime
    mat.uniforms.uProgress.value = THREE.MathUtils.lerp(mat.uniforms.uProgress.value, progressRef.current, 0.04)
    mat.uniforms.uAudio.value = THREE.MathUtils.lerp(mat.uniforms.uAudio.value, audioLevelRef.current, 0.1)
  })

  return (
    <points ref={pointsRef}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" count={COUNT} array={coords.randoms} itemSize={3} />
        <bufferAttribute attach="attributes-aTargetPosition" count={COUNT} array={coords.targets} itemSize={3} />
        <bufferAttribute attach="attributes-aRandomOffset" count={COUNT} array={coords.randoms} itemSize={3} />
        <bufferAttribute attach="attributes-aFlowSpeed" count={COUNT} array={coords.flows} itemSize={1} />
      </bufferGeometry>
      <shaderMaterial
        vertexShader={scatteringVertexShader}
        fragmentShader={scatteringFragmentShader}
        uniforms={uniforms}
        transparent
        depthWrite={false}
        blending={THREE.NormalBlending}
      />
    </points>
  )
}
