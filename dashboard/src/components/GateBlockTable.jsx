import { gateDescription } from '../lib/gateInfo'

// Each gate that actually fired in this batch (gate_block_counts, from
// app/governance/policy_gate.py via app/metrics/compute.py), its count, and
// a plain-English one-liner for what it enforces. Only gates present in the
// snapshot are shown -- a gate that never blocked anything isn't a data
// point to fabricate a zero row for.
export default function GateBlockTable({ gateBlockCounts }) {
  const rows = Object.entries(gateBlockCounts || {}).sort((a, b) => b[1] - a[1])

  if (rows.length === 0) {
    return (
      <div className="rounded-lg border border-line bg-panel p-8 text-center text-sm text-muted">
        No gate blocks in this batch.
      </div>
    )
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-line bg-panel">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-muted">
            <th className="px-4 py-2 font-medium">Gate</th>
            <th className="px-4 py-2 font-medium">Blocks</th>
            <th className="px-4 py-2 font-medium">What it enforces</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([gate, count]) => (
            <tr key={gate} className="border-b border-line last:border-0">
              <td className="px-4 py-2.5 font-mono text-sm font-semibold text-white">{gate}</td>
              <td className="px-4 py-2.5 font-mono text-sm text-white">{count}</td>
              <td className="px-4 py-2.5 text-muted">{gateDescription(gate)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
