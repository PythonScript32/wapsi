/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        // Design tokens — see PRD §12 (Design spec). Use these, not ad-hoc hexes.
        ink:    '#0B0F14',   // page background
        panel:  '#141A22',   // cards / surfaces
        line:   '#222C38',   // borders
        muted:  '#8A97A8',   // secondary text
        recovered: '#22C55E',
        atrisk:    '#F59E0B',
        lost:      '#EF4444',
        promise:   '#6366F1',
        accent:    '#14B8A6',
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'ui-monospace', 'monospace'],
      },
    },
  },
  plugins: [],
}
