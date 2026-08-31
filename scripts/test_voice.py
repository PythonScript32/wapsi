"""
Demo tool for Feature C (inbound) -- run a recorded customer reply through
parse_reply() and print what the pipeline understood.

Voice samples are committed at data/voice_samples/*.ogg. Accepts .ogg, .m4a,
.mp3, or .wav -- the MIME type is guessed from the extension, never assumed.

Usage:
    python scripts/test_voice.py data/voice_samples/promise.ogg
    python scripts/test_voice.py data/voice_samples/promise_date.ogg
"""
from __future__ import annotations

import sys
from pathlib import Path

# Scripts run standalone (python scripts/test_voice.py ...), so the repo root
# -- not just this file's directory -- needs to be on sys.path to import app.*.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.voice import inbound  # noqa: E402

_ACCEPTED_EXTS = {".ogg", ".m4a", ".mp3", ".wav"}


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python scripts/test_voice.py <audio file>", file=sys.stderr)
        return 1

    path = Path(sys.argv[1])
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 1
    if path.suffix.lower() not in _ACCEPTED_EXTS:
        print(f"warning: unrecognised extension {path.suffix!r}, treating as OGG", file=sys.stderr)

    audio_bytes = path.read_bytes()
    mime_type = inbound.mime_type_for_path(str(path))

    result = inbound.parse_reply(
        audio_bytes=audio_bytes,
        ctx={"mime_type": mime_type, "case_id": f"demo:{path.stem}"},
    )

    print(f"file            : {path}")
    print(f"mime type       : {mime_type}")
    print(f"transcript      : {result['transcript']}")
    print(f"intent          : {result['intent']}")
    print(f"raw date phrase : {result['raw_date_phrase']}")
    print(f"promised date   : {result['promised_date']}")
    print(f"confidence      : {result['confidence']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
