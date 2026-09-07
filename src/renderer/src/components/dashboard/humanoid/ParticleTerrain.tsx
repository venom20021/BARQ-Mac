// ─── ParticleTerrain.tsx ─────────────────────────────────────────────
// 40x40 particle grid (1,600) with mountain ridges, normal blending.

import { useRef, useMemo } from 'react'
import { useFrame } from '@react-three/fiber'
import type { Points } from 'three'
import { NormalBlending, Color } from 'three'

const GRID = 40
const TOTAL = GRID * GRID

export function ParticleTerrain({ audioLevel }: { audioLevel: number }): JSX.Element {
  const pointsRef = useRef<Points>(null!)
  const timeRef = useRef(0)
  const audioLevelRef = useRef(0)
  audioLevelRef.current = audioLevel

  const data = useMemo(() => {
    const pos = new Float32Array(TOTAL * 3)
    const col = new Float32Array(TOTAL * 3)
    const blue = new Color('#0c1e30')
    const cyan = new Color('#0a5060')
    const amber = new Color('#cc7700')

    for (let ix = 0; ix < GRID; ix++) {
      for (let iz = 0; iz < GRID; iz++) {
        const idx = ix * GRID + iz
        const i3 = idx * 3
        const x = ((ix / GRID) - 0.5) * 14
        const z = ((iz / GRID) - 0.5) * 8 - 3
        const ef = Math.abs(x) / 7
        const r1 = Math.sin(x * 1.2 + 0.5) * Math.exp(-Math.pow(x * 0.3, 2))
        const r2 = Math.cos(x * 0.8 - 0.3) * Math.exp(-Math.pow((x - 2) * 0.25, 2))
        const h = (r1 + r2) * 0.6 * ef
        pos[i3] = x; pos[i3 + 1] = h - 2.5; pos[i3 + 2] = z
        const n = Math.max(0, Math.min(1, (h + 0.5) * 2))
        const c = new Color()
        if (n < 0.5) c.lerpColors(blue, cyan, n / 0.5)
        else c.lerpColors(cyan, amber, (n - 0.5) / 0.5)
        col[i3] = c.r; col[i3 + 1] = c.g; col[i3 + 2] = c.b
      }
    }
    return { pos, col }
  }, [])

  useFrame((_, delta) => {
    if (!pointsRef.current) return
    timeRef.current += delta
    const attr = pointsRef.current.geometry.attributes.position as import('three').BufferAttribute
    if (!attr) return
    const arr = attr.array as Float32Array
    const t = timeRef.current
    const base = data.pos
    for (let i = 0; i < TOTAL; i++) {
      const i3 = i * 3
      arr[i3 + 1] = base[i3 + 1] + Math.sin(base[i3]*0.8+t*0.5)*0.04 + Math.cos(base[i3+2]*1.2+t*0.3)*0.02
    }
    attr.needsUpdate = true
  })

  return (
    <points ref={pointsRef}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" count={TOTAL} array={data.pos} itemSize={3} />
        <bufferAttribute attach="attributes-color" count={TOTAL} array={data.col} itemSize={3} />
      </bufferGeometry>
      <pointsMaterial size={0.04} sizeAttenuation transparent opacity={0.5} vertexColors depthWrite={false} blending={NormalBlending} />
    </points>
  )
}
