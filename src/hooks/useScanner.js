import { useState, useEffect, useRef, useCallback } from 'react'

const INITIAL_STATE = {
  sol_price: 0,
  total_seen: 0,
  tokens: [],
  buy_signals: [],
  stats: { pump: 0, other: 0, errors: 0 },
}

export function useScanner() {
  const [state, setState] = useState(INITIAL_STATE)
  const [wsStatus, setWsStatus] = useState('connecting') // connecting | open | closed
  const wsRef = useRef(null)
  const retryRef = useRef(null)
  const retryDelay = useRef(1000)

  const connect = useCallback(() => {
    const host = window.location.hostname
    const port = import.meta.env.DEV ? '8000' : window.location.port || '8000'
    const url = `ws://${host}:${port}/ws`

    const ws = new WebSocket(url)
    wsRef.current = ws
    setWsStatus('connecting')

    ws.onopen = () => {
      setWsStatus('open')
      retryDelay.current = 1000
    }

    ws.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data)
        setState(data)
      } catch (e) {
        console.error('WS parse error', e)
      }
    }

    ws.onclose = () => {
      setWsStatus('closed')
      retryRef.current = setTimeout(() => {
        retryDelay.current = Math.min(retryDelay.current * 2, 30000)
        connect()
      }, retryDelay.current)
    }

    ws.onerror = () => {
      ws.close()
    }
  }, [])

  useEffect(() => {
    connect()
    return () => {
      clearTimeout(retryRef.current)
      wsRef.current?.close()
    }
  }, [connect])

  return { ...state, wsStatus }
}
