export default function StatsBar({ solPrice, totalSeen, stats, wsStatus, tokenCount }) {
  const dot = wsStatus === 'open'
    ? 'bg-green-400 shadow-[0_0_6px_#4ade80]'
    : wsStatus === 'connecting'
    ? 'bg-yellow-400 animate-pulse'
    : 'bg-red-500'

  const label = wsStatus === 'open' ? 'LIVE' : wsStatus === 'connecting' ? 'CONNECTING' : 'DISCONNECTED'

  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-1 px-4 py-2 bg-gray-900 border-b border-gray-700 text-xs font-mono">
      <span className="text-cyan-400 font-bold text-sm">⚡ MEME SCANNER</span>

      <span className="text-gray-400">
        SOL <span className="text-white font-bold">${solPrice?.toFixed(0) ?? '—'}</span>
      </span>

      <span className="text-gray-400">
        Tracked <span className="text-yellow-300 font-bold">{tokenCount}</span>
      </span>

      <span className="text-gray-400">
        Seen <span className="text-white font-bold">{totalSeen?.toLocaleString()}</span>
      </span>

      <span className="text-gray-400">
        Pump <span className="text-green-400 font-bold">{stats?.pump ?? 0}</span>
        {' / '}
        Other <span className="text-blue-400 font-bold">{stats?.other ?? 0}</span>
      </span>

      {stats?.errors > 0 && (
        <span className="text-red-400">Errors: {stats.errors}</span>
      )}

      <span className="flex items-center gap-1.5 ml-auto">
        <span className={`w-2 h-2 rounded-full ${dot}`} />
        <span className={wsStatus === 'open' ? 'text-green-400' : 'text-gray-400'}>{label}</span>
      </span>
    </div>
  )
}
