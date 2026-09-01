import { Mic, MessageSquare, Send } from 'lucide-react'
import { EmptyPanel } from './AttemptsList'
import { formatDateTime } from '../lib/format'
import { replyChannel, replyTranscript } from '../lib/auditEvents'

// Right-rail panel: outbound outreach (the Hinglish message the agent sent)
// interleaved with inbound replies -- reconstructed from REPLY_RECEIVED audit
// rows, since that's where a voice reply's transcript actually lives (see
// lib/auditEvents.js). Chronological, oldest first, like a chat log.
export default function MessageThread({ outreach, replyEvents }) {
  const outbound = (outreach || []).map((o) => ({
    kind: 'out',
    ts: o.sent_at,
    channel: o.channel,
    text: o.message,
    key: `out-${o.id}`,
  }))
  const inbound = (replyEvents || [])
    .filter((r) => replyTranscript(r))
    .map((r) => ({
      kind: 'in',
      ts: r.ts,
      channel: replyChannel(r),
      text: replyTranscript(r),
      intent: r.decision,
      key: `in-${r.id}`,
    }))

  const thread = [...outbound, ...inbound].sort((a, b) => new Date(a.ts) - new Date(b.ts))

  if (thread.length === 0) {
    return <EmptyPanel icon={MessageSquare} text="No messages yet." />
  }

  return (
    <div className="flex flex-col gap-2">
      {thread.map((m) => (
        <div
          key={m.key}
          className={`rounded-lg border p-3 ${m.kind === 'out' ? 'border-line bg-panel' : 'border-promise/30 bg-promise/10'}`}
        >
          <div className="flex items-center justify-between gap-2">
            <span className="flex items-center gap-1.5 text-xs font-medium text-muted">
              {m.kind === 'out' ? (
                <>
                  <Send className="h-3 w-3" /> Sent · {m.channel}
                </>
              ) : (
                <>
                  {m.channel === 'voice' ? <Mic className="h-3 w-3 text-promise" /> : <MessageSquare className="h-3 w-3 text-promise" />}
                  <span className="text-promise">Customer reply · {m.channel}</span>
                </>
              )}
            </span>
            <span className="font-mono text-[11px] text-muted">{formatDateTime(m.ts)}</span>
          </div>
          <p className="mt-1.5 text-sm text-white">{m.text}</p>
          {m.intent && (
            <span className="mt-1.5 inline-block rounded bg-line px-1.5 py-0.5 font-mono text-[11px] text-muted">
              intent: {m.intent}
            </span>
          )}
        </div>
      ))}
    </div>
  )
}
