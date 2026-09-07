// ─── NexusCore.tsx ────────────────────────────────────────────────────
// Bottom emitter at [0, -0.5, 0], normal blending.

import { useRef, useMemo } from 'react'
import { useFrame } from '@react-three/fiber'
import type { Group, Points } from 'three'
import { NormalBlending } from 'three'

export function NexusCore({ audioLevel, progress }: { audioLevel: number; progress: number }): JSX.Element {
  const coreRef = useRef<Points>(null!)
  const ring1Ref = useRef<Group>(null!)
  const ring2Ref = useRef<Group>(null!)
  const tRef = useRef(0)
  const alRef = useRef(0)
  alRef.current = audioLevel

  const pos = useMemo(() => {
    const arr = new Float32Array(80 * 3)
    for (let i = 0; i < 80; i++) {
      const th = Math.random() * Math.PI * 2
      const ph = Math.acos(2 * Math.random() - 1)
      const r = 0.04 + Math.random() * 0.06
      arr[i * 3] = r * Math.sin(ph) * Math.cos(th)
      arr[i * 3 + 1] = r * Math.sin(ph) * Math.sin(th) - 0.5
      arr[i * 3 + 2] = r * Math.cos(ph)
    }
    return arr
  }, [])

  useFrame((_, dt) => {
    tRef.current += dt
    const t = tRef.current
    const al = alRef.current
    if (ring1Ref.current) {
      ring1Ref.current.rotation.y -= dt * (1.5 + al * 0.5)
      ring1Ref.current.rotation.x = Math.sin(t * 0.8) * 0.15
    }
    if (ring2Ref.current) {
      ring2Ref.current.rotation.y += dt * (1.2 + al * 0.3)
    }
    if (coreRef.current) {
      coreRef.current.scale.setScalar(1 + Math.sin(t * 3) * 0.1 + al * 0.15)
    }
  })

  const vis = progress > 0.05 ? 1 : progress / 0.05

  return (
    <group position={[0, -0.5, 0]}>
      <points ref={coreRef} visible={vis > 0}>
        <bufferGeometry>
          <bufferAttribute attach="attributes-position" count={80} array={pos} itemSize={3} />
        </bufferGeometry>
        <pointsMaterial size={0.025} sizeAttenuation transparent opacity={0.4 * vis} color="#00aabb" blending={NormalBlending} depthWrite={false} />
      </points>

      <mesh scale={vis}><sphereGeometry args={[0.04, 8, 8]} /><meshBasicMaterial color="#00aabb" transparent opacity={0.5} blending={NormalBlending} depthWrite={false} /></mesh>
      <mesh scale={vis}><sphereGeometry args={[0.1, 8, 8]} /><meshBasicMaterial color="#004455" transparent opacity={0.1} blending={NormalBlending} depthWrite={false} /></mesh>

      <group ref={ring1Ref}>
        <mesh rotation={[Math.PI / 2, 0, 0]}><torusGeometry args={[0.25, 0.003, 4, 32]} /><meshBasicMaterial color="#007788" transparent opacity={0.3 * vis} blending={NormalBlending} depthWrite={false} /></mesh>
      </group>
      <group ref={ring2Ref}>
        <mesh rotation={[Math.PI / 1.8, 0.3, 0]}><torusGeometry args={[0.2, 0.002, 4, 32]} /><meshBasicMaterial color="#005566" transparent opacity={0.2 * vis} blending={NormalBlending} depthWrite={false} /></mesh>
      </group>
    </group>
  )
}
