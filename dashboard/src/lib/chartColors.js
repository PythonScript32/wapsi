// recharts renders SVG fill/stroke attributes directly -- it cannot consume
// Tailwind classes, so it needs literal colour values. These MUST match
// tailwind.config.js's design tokens exactly; this file exists so a chart is
// the ONE place a hex value is spelled out, kept in lockstep with the
// tokens by hand rather than invented ad hoc.
export const CHART_COLORS = {
  muted: '#8A97A8',
  recovered: '#22C55E',
  atrisk: '#F59E0B',
  lost: '#EF4444',
  promise: '#6366F1',
  accent: '#14B8A6',
  line: '#222C38',
  panel: '#141A22',
}
