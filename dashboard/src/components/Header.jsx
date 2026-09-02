import { NavLink } from 'react-router-dom'
import BatchSelector from './BatchSelector'

const NAV = [
  { to: '/', label: 'Pipeline', end: true },
  { to: '/metrics', label: 'Metrics' },
]

// A single clean row: title, the compact batch selector, nav. The batch
// explainer used to live here too but a one-line paragraph next to the pills
// kept breaking this row's vertical alignment -- it now renders on its own
// line on the Pipeline screen instead (PipelineBoard.jsx), where there's
// room for it without disrupting anything.
export default function Header() {
  return (
    <header className="border-b border-line px-6 py-4 flex items-center gap-4">
      <h1 className="text-xl font-semibold tracking-tight">
        वापसी <span className="text-muted font-normal">· Revenue Recovery AI Agent</span>
      </h1>

      <BatchSelector />

      <nav className="ml-auto flex gap-1">
        {NAV.map(({ to, label, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              `rounded-t-md border px-3 py-1.5 text-sm font-medium transition-colors ${
                isActive
                  ? 'border-x-line border-t-line border-b-2 border-b-accent bg-panel text-accent'
                  : 'border-transparent border-b-2 border-b-transparent text-muted hover:border-b-line hover:bg-panel/50 hover:text-white'
              }`
            }
          >
            {label}
          </NavLink>
        ))}
      </nav>
    </header>
  )
}
