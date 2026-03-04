import { useScanner } from './hooks/useScanner'
import StatsBar from './components/StatsBar'
import SignalsPanel from './components/SignalsPanel'
import TokenTable from './components/TokenTable'

export default function App() {
  const { sol_price, total_seen, tokens, buy_signals, stats, wsStatus } = useScanner()

  return (
    <div className="min-h-screen bg-[#0f1117] text-gray-100 flex flex-col">
      <StatsBar
        solPrice={sol_price}
        totalSeen={total_seen}
        stats={stats}
        wsStatus={wsStatus}
        tokenCount={tokens.length}
      />
      <SignalsPanel signals={buy_signals} />
      <div className="flex-1 overflow-auto">
        <TokenTable tokens={tokens} />
      </div>
    </div>
  )
}
