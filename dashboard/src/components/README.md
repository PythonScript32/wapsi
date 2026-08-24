# Components to build (in this order)

1. `StatCard.jsx`      — one metric: label, big value, delta vs baseline.
2. `CaseCard.jsx`      — compact case tile: customer, ₹ amount, reason chip, state.
3. `PipelineBoard.jsx` — columns per state, live via subscribeToCases().
4. `AuditTrail.jsx`    — vertical timeline; each row = ts, actor, decision,
                         reasoning, action, result. Gate blocks in red, allows in green.
5. `MetricsPanel.jsx`  — headline metrics + recovery-by-reason bar chart (recharts)
                         + exception list table.
6. `ReasonChip.jsx`    — coloured pill per reason_category.

Design tokens are in tailwind.config.js — use `ink/panel/line/muted/recovered/
atrisk/lost/promise/accent`, never raw hex.
