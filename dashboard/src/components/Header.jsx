import { NavLink } from 'react-router-dom'
import { useBatch } from '../lib/batchContext'
import BatchSelector from './BatchSelector'

const NAV = [
  { to: '/', label: 'Pipeline', end: true },
  { to: '/metrics', label: 'Metrics' },
]

// The batch explainer lives here (not BatchSelector itself) because it
// reads the current selection from context -- App.jsx can't do that inline
// since it's the component that renders BatchProvider itself, one level
// above where the context is actually available.
export default function Header() {
  const { batch } = useBatch()

  return (
    <header className="border-b border-line px-6 py-4">
      <div className="flex items-start gap-4">
        <h1 className="text-xl font-semibold tracking-tight">
          वापसी <span className="text-muted font-normal">· Revenue Recovery AI Agent</span>
        </h1>

        <div className="flex flex-col gap-1">
          <BatchSelector />
          <p className="max-w-xs text-xs text-muted">{batch.explainer}</p>
        </div>

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
      </div>
    </header>
  )
}
