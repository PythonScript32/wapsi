import { createContext, useContext, useEffect, useState } from 'react'
import { DEFAULT_BATCH_ID, batchById } from './batches'

const STORAGE_KEY = 'wapsi:batchId'
const BatchContext = createContext(null)

function readStoredBatchId() {
  try {
    return localStorage.getItem(STORAGE_KEY) || DEFAULT_BATCH_ID
  } catch {
    return DEFAULT_BATCH_ID // private/blocked storage -- fall back silently
  }
}

// One selected batch (dev/holdout), shared by the Pipeline and Metrics
// screens so switching tabs doesn't reset it -- persisted to localStorage so
// a reload doesn't silently fall back to a different batch than what's on
// screen.
export function BatchProvider({ children }) {
  const [batchId, setBatchId] = useState(readStoredBatchId)

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, batchId)
    } catch {
      // ignore -- persistence is a convenience, not a requirement
    }
  }, [batchId])

  return (
    <BatchContext.Provider value={{ batchId, setBatchId, batch: batchById(batchId) }}>
      {children}
    </BatchContext.Provider>
  )
}

export function useBatch() {
  const ctx = useContext(BatchContext)
  if (!ctx) throw new Error('useBatch must be used within a BatchProvider')
  return ctx
}
