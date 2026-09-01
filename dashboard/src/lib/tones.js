// Tailwind's JIT scanner only picks up class names it can see as literal
// text in a source file -- `bg-${tone}` at runtime would never generate the
// CSS. So every tone this app uses is spelled out here, once, as complete
// class strings; components look up a tone key instead of interpolating one.
export const TONE_CLASSES = {
  muted: { text: 'text-muted', bg: 'bg-muted/10', border: 'border-muted/30', dot: 'bg-muted' },
  recovered: { text: 'text-recovered', bg: 'bg-recovered/10', border: 'border-recovered/30', dot: 'bg-recovered' },
  atrisk: { text: 'text-atrisk', bg: 'bg-atrisk/10', border: 'border-atrisk/30', dot: 'bg-atrisk' },
  lost: { text: 'text-lost', bg: 'bg-lost/10', border: 'border-lost/30', dot: 'bg-lost' },
  promise: { text: 'text-promise', bg: 'bg-promise/10', border: 'border-promise/30', dot: 'bg-promise' },
  accent: { text: 'text-accent', bg: 'bg-accent/10', border: 'border-accent/30', dot: 'bg-accent' },
}

export function toneClasses(tone) {
  return TONE_CLASSES[tone] || TONE_CLASSES.muted
}
