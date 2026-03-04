import { useState, useRef, useEffect, useCallback } from 'react'

function CaCell({ ca }) {
  const [copied, setCopied] = useState(false)
  const copy = useCallback(() => {
    if (!ca) return
    navigator.clipboard.writeText(ca).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }, [ca])
  if (!ca) return <span className="text-gray-600">—</span>
  const short = `${ca.slice(0, 4)}…${ca.slice(-4)}`
  return (
    <button
      onClick={copy}
      title={ca}
      className="text-gray-500 hover:text-cyan-400 transition-colors font-mono text-xs cursor-pointer"
    >
      {copied ? <span className="text-green-400">✓ copied</span> : short}
    </button>
  )
}

function scoreColor(s) {
  if (s >= 85) return 'text-green-300 font-bold'
  if (s >= 75) return 'text-green-400 font-bold'
  if (s >= 65) return 'text-green-500'
  if (s >= 50) return 'text-yellow-400'
  if (s >= 35) return 'text-orange-400'
  return 'text-red-400'
}

function SignalBadge({ signal, creatorSells, bundled, bundleWallets }) {
  if (bundled && bundleWallets >= 5) return (
    <span className="text-orange-400 font-bold whitespace-nowrap" title={`${bundleWallets} wallets bought at launch`}>⚠ BNDL</span>
  )
  if (signal === 'STRONG BUY') return (
    <span className="text-green-300 font-bold whitespace-nowrap">⬆ SBUY</span>
  )
  if (signal === 'NEAR GRAD') return (
    <span className="text-yellow-300 font-bold whitespace-nowrap">◎ GRAD</span>
  )
  if (signal === 'MIGRATED') return (
    <span className="text-cyan-400 font-bold whitespace-nowrap">↗ MIG</span>
  )
  if (signal === 'BUY') return (
    <span className="text-green-500 font-bold whitespace-nowrap">↑ BUY</span>
  )
  if (bundled) return (
    <span className="text-orange-500 font-bold whitespace-nowrap" title={`${bundleWallets} wallets bought at launch`}>~ BNDL</span>
  )
  if (creatorSells >= 3) return (
    <span className="text-red-400 font-bold whitespace-nowrap">⚠ DEV</span>
  )
  if (creatorSells >= 1) return (
    <span className="text-yellow-500 font-bold whitespace-nowrap">! DEV</span>
  )
  return <span className="text-gray-600">—</span>
}

function fmtMc(usd) {
  if (!usd) return '—'
  if (usd >= 1_000_000) return `$${(usd / 1_000_000).toFixed(2)}M`
  if (usd >= 1_000) return `$${(usd / 1_000).toFixed(1)}K`
  return `$${usd.toFixed(0)}`
}

function EntryMcCell({ entryMc, currentMc }) {
  if (!entryMc) return <span className="text-gray-600">—</span>
  const gain = entryMc > 0 ? currentMc / entryMc : null
  const gainColor = gain >= 2 ? 'text-green-400' : gain >= 1.2 ? 'text-green-600' : gain < 0.9 ? 'text-red-400' : 'text-gray-500'
  return (
    <span className="whitespace-nowrap">
      <span className="text-gray-400">{fmtMc(entryMc)}</span>
      {gain !== null && (
        <span className={`ml-1 text-xs ${gainColor}`}>
          {gain.toFixed(1)}x
        </span>
      )}
    </span>
  )
}

function McCell({ mcUsd, trend }) {
  const formatted = mcUsd >= 1_000_000
    ? `$${(mcUsd / 1_000_000).toFixed(1)}M`
    : mcUsd >= 1_000
    ? `$${(mcUsd / 1_000).toFixed(0)}K`
    : `$${mcUsd.toFixed(0)}`

  const arrow = trend > 0
    ? <span className="text-green-400 ml-0.5">▲</span>
    : trend < 0
    ? <span className="text-red-400 ml-0.5">▼</span>
    : null

  return <span className="whitespace-nowrap">{formatted}{arrow}</span>
}

function HoldersCell({ pct }) {
  if (pct == null) return <span className="text-gray-600">…</span>
  if (pct >= 80) return <span className="text-red-400 font-bold">{pct.toFixed(0)}%</span>
  if (pct >= 70) return <span className="text-red-500">{pct.toFixed(0)}%</span>
  if (pct >= 60) return <span className="text-yellow-400">{pct.toFixed(0)}%</span>
  return <span className="text-green-400">{pct.toFixed(0)}%</span>
}

function LiqCell({ liq }) {
  if (!liq) return <span className="text-gray-600">—</span>
  if (liq >= 10_000) return <span className="text-green-400">${(liq/1000).toFixed(0)}K</span>
  if (liq >= 3_000) return <span className="text-yellow-400">${(liq/1000).toFixed(1)}K</span>
  return <span className="text-red-400">${liq.toFixed(0)}</span>
}

function BpmCell({ bpm }) {
  if (bpm >= 5) return <span className="text-green-400 font-bold">{bpm.toFixed(1)}</span>
  if (bpm >= 1) return <span className="text-yellow-400">{bpm.toFixed(1)}</span>
  return <span className="text-gray-500">{bpm.toFixed(1)}</span>
}

function CurveCell({ curvePct, source }) {
  if (source !== 'websocket') return <span className="text-gray-600">n/a</span>
  const filled = Math.round((curvePct / 100) * 7)
  const bar = '█'.repeat(filled) + '░'.repeat(7 - filled)
  const color = curvePct < 40 ? 'text-green-400' : curvePct < 75 ? 'text-yellow-400' : 'text-red-400'
  return <span className={`${color} font-mono`}>{bar} {curvePct.toFixed(0)}%</span>
}

function AgeCell({ ageS }) {
  if (ageS < 60) return <span>{ageS}s</span>
  if (ageS < 3600) return <span>{Math.floor(ageS/60)}m{(ageS%60).toString().padStart(2,'0')}s</span>
  return <span>{Math.floor(ageS/3600)}h{Math.floor((ageS%3600)/60)}m</span>
}

function PlatformBadge({ platform }) {
  const colors = {
    'Pump.fun':    'text-green-300',
    'PumpSwap':    'text-green-400',
    'Moonshot':    'text-yellow-300',
    'LaunchLab':   'text-blue-400',
    'Meteora DBC': 'text-purple-400',
    'Meteora v2':  'text-purple-300',
    'Boop.fun':    'text-cyan-400',
    'TokenMill':   'text-cyan-300',
    'Heaven':      'text-gray-300',
    'Daos.fun':    'text-white',
    'Virtuals':    'text-pink-400',
  }
  return <span className={colors[platform] || 'text-gray-300'}>{platform}</span>
}

const COLUMNS = [
  { key: 'signal',        label: 'Signal',      sortable: true  },
  { key: 'score',         label: 'Score',       sortable: true  },
  { key: 'platform',      label: 'Platform',    sortable: true  },
  { key: 'symbol',        label: 'Symbol',      sortable: false },
  { key: 'ca',            label: 'CA',          sortable: false, hideOnMobile: true },
  { key: 'mc_usd',        label: 'MC▲▼',        sortable: true  },
  { key: 'first_seen_mc', label: 'Entry MC',    sortable: true  },
  { key: 'liq_usd',       label: 'Liq',         sortable: true  },
  { key: 'init_sol',      label: 'Init SOL',    sortable: true  },
  { key: 'buys',          label: 'Buys',        sortable: true  },
  { key: 'sells',         label: 'Sells',       sortable: true  },
  { key: 'bpm',           label: 'B/min',       sortable: true  },
  { key: 'top10_pct',     label: 'Hold%',       sortable: true  },
  { key: 'social',        label: 'Social',      sortable: false },
  { key: 'curve_pct',     label: 'Curve',       sortable: false, hideOnMobile: true },
  { key: 'age_s',         label: 'Age',         sortable: true  },
]

function signalRank(signal) {
  if (signal === 'STRONG BUY') return 0
  if (signal === 'BUY') return 1
  return 2
}

export default function TokenTable({ tokens }) {
  const [sortCol, setSortCol] = useState('signal')
  const [sortAsc, setSortAsc] = useState(true)
  const prevIds = useRef(new Set())
  const [pulseIds, setPulseIds] = useState(new Set())

  useEffect(() => {
    const incoming = new Set(tokens.map(t => t.id))
    const newOnes = [...incoming].filter(id => !prevIds.current.has(id))
    if (newOnes.length > 0) {
      setPulseIds(new Set(newOnes))
      setTimeout(() => setPulseIds(new Set()), 1200)
    }
    prevIds.current = incoming
  }, [tokens])

  const handleSort = (col) => {
    if (sortCol === col) setSortAsc(a => !a)
    else { setSortCol(col); setSortAsc(false) }
  }

  const sorted = [...tokens].sort((a, b) => {
    let av, bv
    if (sortCol === 'signal') {
      av = signalRank(a.signal); bv = signalRank(b.signal)
      if (av !== bv) return av - bv
      return b.score - a.score
    }
    av = a[sortCol] ?? -Infinity
    bv = b[sortCol] ?? -Infinity
    if (sortCol === 'symbol' || sortCol === 'platform') {
      return sortAsc
        ? String(av).localeCompare(String(bv))
        : String(bv).localeCompare(String(av))
    }
    return sortAsc ? av - bv : bv - av
  })

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs font-mono border-collapse min-w-max">
        <thead>
          <tr className="border-b border-gray-700 text-gray-400 uppercase tracking-wide">
            {COLUMNS.map(col => (
              <th
                key={col.key}
                className={`px-2 py-2 text-left whitespace-nowrap select-none
                  ${col.sortable ? 'cursor-pointer hover:text-gray-200' : ''}
                  ${col.hideOnMobile ? 'hidden lg:table-cell' : ''}`}
                onClick={() => col.sortable && handleSort(col.key)}
              >
                {col.label}
                {col.sortable && sortCol === col.key && (
                  <span className="ml-1 text-cyan-400">{sortAsc ? '↑' : '↓'}</span>
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.length === 0 && (
            <tr>
              <td colSpan={COLUMNS.length} className="px-4 py-8 text-center text-gray-600">
                Waiting for tokens...
              </td>
            </tr>
          )}
          {sorted.map(token => {
            const social = (token.has_twitter ? 'X ' : '') + (token.has_telegram ? 'TG' : '')
            const isPulsing = pulseIds.has(token.id)
            return (
              <tr
                key={token.id}
                className={`border-b border-gray-800 hover:bg-gray-800/40 transition-colors
                  ${isPulsing ? 'row-pulse' : ''}
                  ${token.signal === 'STRONG BUY' ? 'bg-green-950/20' : ''}
                  ${token.signal === 'NEAR GRAD' ? 'bg-yellow-950/20' : ''}
                  ${token.signal === 'MIGRATED' ? 'bg-cyan-950/20' : ''}
                  ${token.bundled && token.bundle_wallets >= 5 ? 'bg-orange-950/20' : ''}`}
              >
                <td className="px-2 py-1.5">
                  <SignalBadge
                    signal={token.signal}
                    creatorSells={token.creator_sells}
                    bundled={token.bundled}
                    bundleWallets={token.bundle_wallets}
                  />
                </td>
                <td className={`px-2 py-1.5 ${scoreColor(token.score)}`}>
                  {token.score}
                </td>
                <td className="px-2 py-1.5">
                  <PlatformBadge platform={token.platform} />
                </td>
                <td className="px-2 py-1.5">
                  <a
                    href={token.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-white hover:text-cyan-300 font-bold transition-colors"
                  >
                    {token.symbol}
                    {token.graduated && <span className="text-green-400 ml-1 text-xs">G</span>}
                  </a>
                </td>
                <td className="px-2 py-1.5 hidden lg:table-cell">
                  <CaCell ca={token.ca} />
                </td>
                <td className="px-2 py-1.5 text-right">
                  <McCell mcUsd={token.mc_usd} trend={token.mc_trend} />
                </td>
                <td className="px-2 py-1.5 text-right">
                  <EntryMcCell entryMc={token.first_seen_mc} currentMc={token.mc_usd} />
                </td>
                <td className="px-2 py-1.5 text-right">
                  <LiqCell liq={token.liq_usd} />
                </td>
                <td className="px-2 py-1.5 text-right text-gray-300">
                  {token.init_sol ? token.init_sol.toFixed(2) : <span className="text-gray-600">—</span>}
                </td>
                <td className="px-2 py-1.5 text-right text-gray-300">{token.buys}</td>
                <td className="px-2 py-1.5 text-right text-gray-400">{token.sells}</td>
                <td className="px-2 py-1.5 text-right">
                  <BpmCell bpm={token.bpm} />
                </td>
                <td className="px-2 py-1.5 text-right">
                  <HoldersCell pct={token.top10_pct} />
                </td>
                <td className="px-2 py-1.5 text-center text-gray-400">
                  {social || <span className="text-gray-700">—</span>}
                </td>
                <td className="px-2 py-1.5 hidden lg:table-cell">
                  <CurveCell curvePct={token.curve_pct} source={token.source} />
                </td>
                <td className="px-2 py-1.5 text-right text-gray-400">
                  <AgeCell ageS={token.age_s} />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
