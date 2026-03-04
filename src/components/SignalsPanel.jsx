import { useEffect, useRef } from 'react'

function timeAgo(isoStr) {
  const secs = Math.floor((Date.now() - new Date(isoStr)) / 1000)
  if (secs < 60) return `${secs}s ago`
  const mins = Math.floor(secs / 60)
  return `${mins}m ago`
}

function SignalCard({ signal }) {
  const cfg = {
    'STRONG BUY': { border: 'border-green-400', bg: 'bg-green-950/40', shadow: 'shadow-[0_0_8px_rgba(74,222,128,0.2)]', label: '🔥 STRONG BUY', color: 'text-green-300' },
    'NEAR GRAD':  { border: 'border-yellow-400', bg: 'bg-yellow-950/40', shadow: 'shadow-[0_0_8px_rgba(234,179,8,0.2)]', label: '◎ NEAR GRAD', color: 'text-yellow-300' },
    'MIGRATED':   { border: 'border-cyan-400', bg: 'bg-cyan-950/40', shadow: 'shadow-[0_0_8px_rgba(34,211,238,0.2)]', label: '↗ MIGRATED', color: 'text-cyan-300' },
    'BUY':        { border: 'border-green-700', bg: 'bg-green-950/20', shadow: '', label: '↑ BUY', color: 'text-green-400' },
  }
  const c = cfg[signal.signal] || cfg['BUY']
  return (
    <div className={`signal-card-enter flex-shrink-0 w-64 rounded-lg border p-3 text-xs font-mono ${c.border} ${c.bg} ${c.shadow}`}>
      <div className="flex items-center justify-between mb-1.5">
        <span className={`font-bold text-sm ${c.color}`}>
          {c.label}
        </span>
        <span className="text-gray-500 text-xs">{timeAgo(signal.time)}</span>
      </div>
      <div className="flex items-baseline gap-2 mb-1">
        <span className="text-white font-bold text-base">${signal.symbol}</span>
        <span className="text-gray-400 text-xs">{signal.platform}</span>
      </div>
      <div className="flex gap-3 mb-1.5 text-gray-300">
        <span>Score: <span className="text-green-400 font-bold">{signal.score}</span></span>
        <span>MC: <span className="text-yellow-300">${(signal.mc_usd / 1000).toFixed(1)}K</span></span>
      </div>
      <div className="text-gray-500 text-xs truncate mb-2">{signal.reason}</div>
      <a
        href={signal.url}
        target="_blank"
        rel="noopener noreferrer"
        className="text-blue-400 hover:text-blue-300 text-xs underline"
      >
        Chart →
      </a>
    </div>
  )
}

export default function SignalsPanel({ signals }) {
  const scrollRef = useRef(null)

  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollLeft = 0
    }
  }, [signals?.length])

  if (!signals?.length) {
    return (
      <div className="px-4 py-3 bg-gray-900/50 border-b border-gray-700 text-gray-500 text-xs font-mono">
        ⚡ BUY SIGNALS — waiting for qualifying tokens...
      </div>
    )
  }

  return (
    <div className="border-b border-gray-700 bg-gray-900/30">
      <div className="px-4 pt-2 pb-1 text-xs text-yellow-400 font-bold font-mono">
        ⚡ BUY SIGNALS ({signals.length})
      </div>
      <div
        ref={scrollRef}
        className="flex gap-3 overflow-x-auto pb-3 px-4"
        style={{ scrollbarWidth: 'thin' }}
      >
        {signals.map((sig, i) => (
          <SignalCard key={`${sig.symbol}-${sig.time}`} signal={sig} />
        ))}
      </div>
    </div>
  )
}
