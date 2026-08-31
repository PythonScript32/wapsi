"""
Run every voice sample through parse_reply and report a pass/fail table.

This is the Block F acceptance test and a demo tool in one: it proves the
Hinglish pipeline works across all six intents, in one command, using real
recorded audio.

Usage:
    python scripts/test_voice_all.py
    python scripts/test_voice_all.py --dir data/voice_samples
    python scripts/test_voice_all.py --verbose      # show full transcripts
"""
from __future__ import annotations 
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import argparse
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

# Windows consoles default to cp1252 and cannot print Devanagari.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from app.voice import inbound  # noqa: E402

# filename stem -> (expected intent, expected day offset from today or None)
EXPECTED: dict[str, tuple[str, int | None]] = {
    "promise":      ("promise_to_pay", 7),
    "promise_date": ("promise_to_pay", 3),
    "paid":         ("already_paid",   None),
    "optout":       ("opt_out",        None),
    "paynow":       ("pay_now",        None),
    "unclear":      ("unclear",        None),
}

AUDIO_EXTS = (".ogg", ".m4a", ".mp3", ".wav", ".flac", ".aac")


def _day_offset(promised: str | None) -> int | None:
    if not promised:
        return None
    try:
        d = datetime.fromisoformat(str(promised)).date()
    except ValueError:
        try:
            d = date.fromisoformat(str(promised))
        except ValueError:
            return None
    return (d - datetime.now(timezone.utc).date()).days


def _find(sample_dir: Path, stem: str) -> Path | None:
    for ext in AUDIO_EXTS:
        p = sample_dir / f"{stem}{ext}"
        if p.exists():
            return p
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Run all voice samples through parse_reply.")
    ap.add_argument("--dir", default="data/voice_samples")
    ap.add_argument("--verbose", action="store_true", help="print full transcripts")
    args = ap.parse_args()

    sample_dir = Path(args.dir)
    if not sample_dir.exists():
        print(f"No such directory: {sample_dir}")
        return 1

    rows, passed, failed, missing = [], 0, 0, 0

    for stem, (want_intent, want_offset) in EXPECTED.items():
        path = _find(sample_dir, stem)
        if path is None:
            print(f"[skip] no audio file found for '{stem}'")
            missing += 1
            continue

        print(f"[..] {path.name} ...", end=" ", flush=True)
        t0 = time.time()
        try:
            result = inbound.parse_reply(audio_bytes=path.read_bytes(), text=None, ctx={})
            err = None
        except Exception as exc:  # the module should degrade, not raise — but be safe
            result, err = {}, f"{type(exc).__name__}: {exc}"
        elapsed = time.time() - t0

        intent = result.get("intent", "ERROR")
        transcript = (result.get("transcript") or "").strip()
        promised = result.get("promised_date")
        phrase = result.get("raw_date_phrase")
        conf = result.get("confidence", 0.0)
        offset = _day_offset(promised)

        ok_intent = intent == want_intent
        ok_date = (want_offset is None) or (offset == want_offset)
        ok = ok_intent and ok_date and err is None

        print("PASS" if ok else "FAIL", f"({elapsed:.1f}s)")
        if ok:
            passed += 1
        else:
            failed += 1

        rows.append({
            "file": path.name,
            "want": want_intent,
            "got": intent,
            "date": f"{promised} ({offset:+d}d)" if offset is not None else str(promised),
            "want_date": f"{want_offset:+d}d" if want_offset is not None else "-",
            "conf": conf,
            "phrase": phrase,
            "transcript": transcript,
            "err": err,
            "ok": ok,
        })

    # ---- report ------------------------------------------------------------
    print("\n" + "=" * 78)
    print("VOICE SAMPLE RESULTS")
    print("=" * 78)
    print(f"{'file':<18}{'expected':<16}{'got':<16}{'date':<22}{'conf':>5}")
    print("-" * 78)
    for r in rows:
        mark = " " if r["ok"] else "!"
        print(f"{mark}{r['file']:<17}{r['want']:<16}{r['got']:<16}{r['date']:<22}{r['conf']:>5.2f}")

    if args.verbose or failed:
        print("\n--- detail ---")
        for r in rows:
            print(f"\n{r['file']}")
            print(f"  transcript : {r['transcript'] or '(empty)'}")
            print(f"  date phrase: {r['phrase']}")
            if r["want_date"] != "-":
                print(f"  wanted date: today {r['want_date']}")
            if r["err"]:
                print(f"  ERROR      : {r['err']}")

    print("\n" + "=" * 78)
    print(f"{passed} passed, {failed} failed" + (f", {missing} missing" if missing else ""))
    print("=" * 78)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
