// ─── SonarRings.tsx ──────────────────────────────────────────────────
// Concentric rings behind head, normal blending.

import { useRef } from 'react'
import { useFrame } from '@react-three/fiber'
import type { Group } from 'three'
import { NormalBlending } from 'three'

export function SonarRings({ audioLevel }: { audioLevel: number }): JSX.Element {
  const r0 = useRef<Group>(null!)
  const r1 = useRef<Group>(null!)
  const r2 = useRef<Group>(null!)
  const tRef = useRef(0)
  const alRef = useRef(0)
  alRef.current = audioLevel

  const refs = [r0, r1, r2]
  const radii = [0.4, 0.6, 0.8]
  const speeds = [0.8, 0.6, 0.4]

  useFrame((_, dt) => {
    tRef.current += dt
    const t = tRef.current
    const al = alRef.current
    for (let i = 0; i < 3; i++) {
      const r = refs[i].current
      if (!r) continue
      r.scale.setScalar(1 + Math.sin(t * speeds[i] + i * 1.2) * 0.15 + al * 0.1)
    }
  })

  return (
    <group position={[0, 1.2, -0.2]}>
      {refs.map((ref, i) => (
        <group key={i} ref={ref}>
          <mesh><torusGeometry args={[radii[i], 0.003, 4, 48]} /><meshBasicMaterial color="#005566" transparent opacity={0.12} blending={NormalBlending} depthWrite={false} /></mesh>
        </group>
      ))}
      <mesh><ringGeometry args={[0.12, 0.14, 24]} /><meshBasicMaterial color="#003344" transparent opacity={0.06} blending={NormalBlending} depthWrite={false} side={2} /></mesh>
    </group>
  )
}
