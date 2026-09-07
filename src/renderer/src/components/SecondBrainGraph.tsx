import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { Loader2, AlertCircle, RefreshCw, Search, X } from 'lucide-react'
import ForceGraph2D from 'react-force-graph-2d'
import { api } from '../utils/api'

// ─── Types ──────────────────────────────────────────────────────────────

interface SBNode {
  id: string
  label?: string
  title?: string
  node_type?: string
  item_type?: string
  content_preview?: string
  tags?: string[]
  size?: number
  x?: number
  y?: number
}

interface SBLink {
  source: string | SBNode
  target: string | SBNode
  weight?: number
  confidence?: string
  shared_tags?: string[]
}

interface SBGraphData {
  nodes: SBNode[]
  links: SBLink[]
  communities?: Record<string, string[]>
  stats?: {
    total_nodes: number
    total_edges: number
    total_communities: number
  }
}

interface SecondBrainGraphProps {
  height?: number
  showControls?: boolean
  onNodeClick?: (node: SBNode) => void
}

// ─── Color palette ──────────────────────────────────────────────────────

const TYPE_COLORS: Record<string, string> = {
  item: '#8b5cf6',
  tag: '#22d3ee',
  note: '#4ade80',
  code: '#22d3ee',
  bookmark: '#fbbf24',
  task: '#f87171',
}

function nodeColor(node: SBNode): string {
  if (node.node_type === 'tag') return TYPE_COLORS.tag
  return TYPE_COLORS[node.item_type || ''] || TYPE_COLORS.item
}

// ─── Component ──────────────────────────────────────────────────────────

export function SecondBrainGraph({
  height = 500,
  showControls = true,
  onNodeClick,
}: SecondBrainGraphProps): JSX.Element {
  const [graphData, setGraphData] = useState<SBGraphData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [highlightNodes, setHighlightNodes] = useState<Set<string>>(new Set())
  const [highlightLinks, setHighlightLinks] = useState<Set<string>>(new Set())
  const [hoveredNode, setHoveredNode] = useState<SBNode | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const [dimensions, setDimensions] = useState({ width: Math.max(400, 800), height: Math.max(300, height) })
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const graphRef = useRef<any>(null)

  // ── Fetch graph data ────────────────────────────────────────────────
  const fetchGraph = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const resp = await api<SBGraphData>('/api/second-brain/graph')
      if (resp && typeof resp === 'object' && 'nodes' in resp) {
        // Normalize: react-force-graph-2d expects source/target as strings
        const data = { ...resp } as SBGraphData
        // Backend returns 'edges', react-force-graph-2d expects 'links'
        const rawEdges = (data as unknown as { edges?: SBLink[] }).edges || data.links || []
        data.links = rawEdges.map((l) => ({
          ...l,
          source: typeof l.source === 'object' ? (l.source as SBNode).id : l.source,
          target: typeof l.target === 'object' ? (l.target as SBNode).id : l.target,
        }))
        setGraphData(data)
      } else {
        setError('No graph data returned — is Second Brain running?')
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load graph')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void fetchGraph()
  }, [fetchGraph])

  // ── Responsive container ────────────────────────────────────────────
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const w = Math.floor(entry.contentRect.width)
        const h = Math.floor(entry.contentRect.height)
        if (w > 0 && h > 0) {
          setDimensions({ width: w, height: h })
        }
      }
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  // ── Search ──────────────────────────────────────────────────────────
  const matchingNodeIds = useMemo(() => {
    if (!searchQuery.trim() || !graphData) return null
    const q = searchQuery.toLowerCase()
    const ids = new Set<string>()
    for (const n of graphData.nodes) {
      if (n.id.toLowerCase().includes(q) || (n.label || '').toLowerCase().includes(q)) {
        ids.add(n.id)
      }
    }
    return ids
  }, [searchQuery, graphData])

  // ── Highlight on search/hover ───────────────────────────────────────
  useEffect(() => {
    if (!matchingNodeIds || !graphData) {
      if (hoveredNode && graphData) {
        const nodeIds = new Set<string>([hoveredNode.id])
        const linkKeys = new Set<string>()
        for (const link of graphData.links) {
          const src = typeof link.source === 'string' ? link.source : (link.source as SBNode)?.id
          const tgt = typeof link.target === 'string' ? link.target : (link.target as SBNode)?.id
          if (src === hoveredNode.id || tgt === hoveredNode.id) {
            if (src) nodeIds.add(src)
            if (tgt) nodeIds.add(tgt)
            if (src && tgt) linkKeys.add(`${src}->${tgt}`)
          }
        }
        setHighlightNodes(nodeIds)
        setHighlightLinks(linkKeys)
      } else {
        setHighlightNodes(new Set())
        setHighlightLinks(new Set())
      }
      return
    }

    const nodeIds = new Set(matchingNodeIds)
    const linkKeys = new Set<string>()
    for (const link of graphData.links) {
      const src = typeof link.source === 'string' ? link.source : (link.source as SBNode)?.id
      const tgt = typeof link.target === 'string' ? link.target : (link.target as SBNode)?.id
      if (!src || !tgt) continue
      if (matchingNodeIds.has(src) && matchingNodeIds.has(tgt)) {
        linkKeys.add(`${src}->${tgt}`)
      } else if (matchingNodeIds.has(src)) {
        nodeIds.add(tgt)
        linkKeys.add(`${src}->${tgt}`)
      } else if (matchingNodeIds.has(tgt)) {
        nodeIds.add(src)
        linkKeys.add(`${src}->${tgt}`)
      }
    }
    setHighlightNodes(nodeIds)
    setHighlightLinks(linkKeys)
  }, [matchingNodeIds, graphData, hoveredNode])

  // ── Node hover ──────────────────────────────────────────────────────
  const handleNodeHover = useCallback((node: SBNode | null) => {
    setHoveredNode(node)
    if (!node || !graphData) {
      setHighlightNodes(new Set())
      setHighlightLinks(new Set())
      return
    }
    const nodeIds = new Set<string>([node.id])
    const linkKeys = new Set<string>()
    for (const link of graphData.links) {
      const src = typeof link.source === 'string' ? link.source : (link.source as SBNode)?.id
      const tgt = typeof link.target === 'string' ? link.target : (link.target as SBNode)?.id
      if (src === node.id || tgt === node.id) {
        if (src) nodeIds.add(src)
        if (tgt) nodeIds.add(tgt)
        if (src && tgt) linkKeys.add(`${src}->${tgt}`)
      }
    }
    setHighlightNodes(nodeIds)
    setHighlightLinks(linkKeys)
  }, [graphData])

  // ── Canvas node painter ─────────────────────────────────────────────
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const paintNode = useCallback((node: SBNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
    if (node.x == null || !Number.isFinite(node.x) || node.y == null || !Number.isFinite(node.y)) return
    const isTag = node.node_type === 'tag'
    const baseRadius = isTag ? 2.0 / globalScale : 2.8 / globalScale
    const isSearchMatch = matchingNodeIds?.has(node.id) ?? false
    const isHovered = highlightNodes.has(node.id)

    let fill: string
    if (isSearchMatch) fill = '#34d399'
    else if (isHovered) fill = '#ffffff'
    else if (highlightNodes.size > 0) fill = 'rgba(138,138,138,0.25)'
    else fill = nodeColor(node)

    const radius = (isHovered || isSearchMatch) ? baseRadius * 1.4 : baseRadius

    ctx.beginPath()
    ctx.arc(node.x, node.y, radius, 0, 2 * Math.PI)
    ctx.fillStyle = fill
    ctx.fill()

    if (isHovered || isSearchMatch) {
      ctx.beginPath()
      ctx.arc(node.x, node.y, radius + 1.5 / globalScale, 0, 2 * Math.PI)
      ctx.strokeStyle = '#ffffff'
      ctx.lineWidth = 0.8 / globalScale
      ctx.stroke()
    }

    // Label on hover/search
    const showLabel = isHovered || isSearchMatch
    if (showLabel) {
      const label = (node.label || node.id).slice(0, 24)
      const fontSize = Math.max(6, 10 / globalScale)
      ctx.font = `${fontSize}px Inter, sans-serif`
      ctx.textAlign = 'center'
      ctx.textBaseline = 'top'
      const labelY = node.y + radius + 2
      const textW = ctx.measureText(label).width
      ctx.fillStyle = 'rgba(0,0,0,0.5)'
      ctx.fillRect(node.x - textW / 2 - 3, labelY - 2, textW + 6, fontSize + 4)
      ctx.fillStyle = '#e6edf3'
      ctx.fillText(label, node.x, labelY)
    }
  }, [highlightNodes, matchingNodeIds])

  // ── Link painter ────────────────────────────────────────────────────
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const paintLink = useCallback((link: any, ctx: CanvasRenderingContext2D) => {
    const src = typeof link.source === 'object' ? link.source : null
    const tgt = typeof link.target === 'object' ? link.target : null
    if (!src || !tgt || src.x == null || tgt.x == null) return

    const key = `${src.id}->${tgt.id}`
    const isHighlighted = highlightLinks.has(key)

    ctx.beginPath()
    ctx.moveTo(src.x, src.y)
    ctx.lineTo(tgt.x, tgt.y)

    if (highlightNodes.size > 0 && !isHighlighted) {
      ctx.strokeStyle = 'rgba(150,150,150,0.08)'
      ctx.lineWidth = 0.3
    } else if (isHighlighted) {
      ctx.strokeStyle = 'rgba(139,92,246,0.7)'
      ctx.lineWidth = 0.8
    } else {
      ctx.strokeStyle = 'rgba(150,150,150,0.2)'
      ctx.lineWidth = 0.4
    }
    ctx.stroke()
  }, [highlightLinks, highlightNodes])

  // ── Zoom to fit on load ─────────────────────────────────────────────
  useEffect(() => {
    if (graphData && graphRef.current) {
      setTimeout(() => {
        try { graphRef.current.zoomToFit(400, 50) } catch { /* ignore */ }
      }, 500)
    }
  }, [graphData])

  // ── Stats ───────────────────────────────────────────────────────────
  const nodeCount = graphData?.nodes.length ?? 0
  const edgeCount = graphData?.links.length ?? 0

  return (
    <div className="flex flex-col h-full">
      {/* Controls bar */}
      {showControls && (
        <div className="flex items-center gap-3 px-3 py-2 border-b border-zinc-800/60 shrink-0">
          <div className="relative flex-1 max-w-xs">
            <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3 h-3 text-zinc-500" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search nodes..."
              className="w-full pl-8 pr-8 py-1.5 text-[10px] font-mono rounded-lg bg-zinc-900/70 border border-zinc-700/60 text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-purple-500/40"
            />
            {searchQuery && (
              <button onClick={() => setSearchQuery('')} className="absolute right-2 top-1/2 -translate-y-1/2 text-zinc-500 hover:text-zinc-300">
                <X className="w-3 h-3" />
              </button>
            )}
          </div>
          <span className="text-[9px] font-mono text-zinc-500">
            {nodeCount} nodes · {edgeCount} edges
          </span>
          <button
            onClick={fetchGraph}
            disabled={loading}
            className="p-1.5 rounded-lg text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800/50 transition-colors disabled:opacity-40"
            title="Refresh"
          >
            <RefreshCw className={`w-3 h-3 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      )}

      {/* Graph canvas */}
      <div ref={containerRef} className="flex-1 relative overflow-hidden" style={{ minHeight: height }}>
        {/* Background */}
        <div className="absolute inset-0 bg-[#0d1117]" />

        {/* Loading */}
        {loading && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-[#0d1117]/75">
            <div className="flex flex-col items-center gap-2">
              <Loader2 className="w-5 h-5 animate-spin text-purple-400" />
              <span className="text-[10px] font-mono text-zinc-500">Loading Second Brain graph...</span>
            </div>
          </div>
        )}

        {/* Error */}
        {error && !loading && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-[#0d1117]/75">
            <div className="flex flex-col items-center gap-2 px-6 py-5 rounded-xl bg-zinc-900/60 border border-red-900/40 text-center">
              <AlertCircle className="w-6 h-6 text-red-400" />
              <p className="text-xs font-mono text-zinc-400 max-w-xs">{error}</p>
              <button
                onClick={fetchGraph}
                className="px-3 py-1 text-[10px] font-mono rounded-lg bg-purple-500/15 text-purple-400 border border-purple-500/30 hover:bg-purple-500/25 transition-colors"
              >
                Retry
              </button>
            </div>
          </div>
        )}

        {/* Empty */}
        {!loading && !error && graphData && nodeCount === 0 && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-[#0d1117]/55">
            <div className="text-center">
              <p className="text-xs font-mono text-zinc-500">Second Brain is empty</p>
              <p className="text-[10px] font-mono text-zinc-600 mt-1">Add items in Second Brain to see the graph</p>
            </div>
          </div>
        )}

        {/* ForceGraph2D */}
        {graphData && !loading && (
          <ForceGraph2D
            ref={graphRef}
            graphData={graphData}
            width={dimensions.width}
            height={dimensions.height}
            backgroundColor="rgba(13,17,23,1)"
            nodeRelSize={2.5}
            nodeCanvasObject={paintNode}
            nodeCanvasObjectMode={() => 'replace'}
            nodePointerAreaPaint={(node, color, ctx) => {
              if (node.x == null || !Number.isFinite(node.x) || node.y == null || !Number.isFinite(node.y)) return
              const r = Math.max(6, 8 + Math.sqrt((node as SBNode).size || 8))
              ctx.beginPath()
              ctx.arc(node.x, node.y, r, 0, 2 * Math.PI)
              ctx.fillStyle = color
              ctx.fill()
            }}
            linkCanvasObject={paintLink}
            onNodeHover={handleNodeHover}
            onNodeClick={(node) => onNodeClick?.(node as SBNode)}
            onBackgroundClick={() => {
              setHighlightNodes(new Set())
              setHighlightLinks(new Set())
            }}
            enableNodeDrag={true}
            enableZoomInteraction={true}
            enablePanInteraction={true}
            d3AlphaDecay={0.03}
            d3VelocityDecay={0.3}
          />
        )}
      </div>
    </div>
  )
}

export default SecondBrainGraph
