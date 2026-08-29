"""
Gemini API key checker.

Answers one question: does this key actually work, right now, on the free tier?

It does four things, in order, and stops at the first real failure:
  1. AUTH      — can the key talk to Google at all?
  2. MODELS    — which models can THIS key actually use? (no guessing model IDs)
  3. TEXT      — a real generateContent call, the thing your agent will do
  4. AUDIO     — optional multimodal test, for Hinglish voice notes

Every failure is translated into a plain-language diagnosis, because Google's
raw errors do not tell you which of the five possible problems you have.

Usage:
    pip install google-generativeai python-dotenv
    python scripts/check_gemini_key.py
    python scripts/check_gemini_key.py --audio path\\to\\voice.mp3
    python scripts/check_gemini_key.py --key AIza...        # test without .env
"""
from __future__ import annotations

import argparse
import os
import re
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import google.generativeai as genai
except ImportError:
    sys.exit("Missing dependency. Run:  pip install google-generativeai python-dotenv")


OK, BAD, WARN, INFO = "[ OK ]", "[FAIL]", "[WARN]", "[ .. ]"


def diagnose(err: Exception) -> str:
    """Translate Google's errors into something actionable."""
    msg = str(err)
    low = msg.lower()

    if "api_key_invalid" in low or "api key not valid" in low:
        return (
            "The key itself is wrong or was revoked.\n"
            "     -> Regenerate at aistudio.google.com/apikey and copy the FULL string."
        )
    if "permission_denied" in low or "403" in msg:
        return (
            "Key is valid but not permitted on this project.\n"
            "     -> The Generative Language API isn't enabled, or the key has\n"
            "        HTTP-referrer/IP restrictions. Remove restrictions while testing."
        )
    if "free_tier" in low and "limit: 0" in low:
        return (
            "THE BILLING TRAP. This project has billing enabled, which DELETES\n"
            "     the free tier — every call is billable from the first token.\n"
            "     -> Use a different project that has never had billing enabled."
        )
    if "429" in msg or "resource_exhausted" in low or "quota" in low:
        return (
            "Quota exhausted — but the key WORKS. This is a limits problem,\n"
            "     not an auth problem.\n"
            "     -> Free daily quota resets at 00:00 Pacific. Or switch to a\n"
            "        Flash / Flash-Lite model, which have the largest free quotas."
        )
    if "404" in msg or "not found" in low:
        return (
            "That model ID doesn't exist for this key.\n"
            "     -> Use one from the MODELS list printed above."
        )
    if "billing" in low or "prepay" in low:
        return (
            "Google wants billing activated on this project.\n"
            "     -> Use a fresh Google account whose project has no billing attached."
        )
    return "Unrecognised error. Full text below."


def _pick_model(candidates: list[str]) -> str:
    """
    Choose a model that is actually alive.

    Models get retired for new users, so listing a model does NOT guarantee you
    may call it. Preference order:
      1. a "-latest" alias  — never goes stale, Google repoints it
      2. the highest version number, excluding previews
      3. the highest version number including previews
      4. whatever is first
    """
    if not candidates:
        return ""

    latest = [m for m in candidates if m.endswith("-latest")]
    if latest:
        # prefer plain flash-latest over flash-lite-latest
        plain = [m for m in latest if "lite" not in m]
        return (plain or latest)[0]

    def version_of(name: str) -> float:
        nums = re.findall(r"(\d+\.?\d*)", name)
        return float(nums[0]) if nums else 0.0

    stable = [m for m in candidates if "preview" not in m and "exp" not in m]
    pool = stable or candidates
    return max(pool, key=version_of)


def _suggested_model(err: Exception) -> str | None:
    """Google's 404 usually names the replacement. Extract it and retry."""
    m = re.search(r"use\s+models/([\w.\-]+)", str(err))
    return m.group(1) if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Check whether a Gemini API key works.")
    ap.add_argument("--key", help="API key to test (otherwise reads GEMINI_API_KEY from .env)")
    ap.add_argument("--audio", help="optional audio file to test Hinglish transcription")
    ap.add_argument("--model", help="force a specific model id")
    args = ap.parse_args()

    key = args.key or os.getenv("GEMINI_API_KEY", "")
    if not key:
        print(f"{BAD} No key found.")
        print("       Add GEMINI_API_KEY=... to your .env, or pass --key AIza...")
        return 1

    print(f"{INFO} Key loaded: {key[:8]}...{key[-4:]}  (length {len(key)})")
    if not key.startswith("AIza"):
        print(f"{WARN} AI Studio keys normally start with 'AIza'. Check you copied the right value.")

    genai.configure(api_key=key)

    # ---- 1 & 2: AUTH + MODELS ------------------------------------------------
    print(f"\n{INFO} Checking auth and listing usable models...")
    try:
        usable = [
            m.name.replace("models/", "")
            for m in genai.list_models()
            if "generateContent" in m.supported_generation_methods
        ]
    except Exception as e:
        print(f"{BAD} Auth failed.\n     {diagnose(e)}\n\n     Raw: {e}")
        return 1

    if not usable:
        print(f"{BAD} Key authenticates but exposes no usable models.")
        return 1

    print(f"{OK} Auth works. {len(usable)} models available.")
    flash = [m for m in usable if "flash" in m.lower()]
    for m in (flash or usable)[:8]:
        print(f"       - {m}")

    target = args.model or _pick_model(flash or usable)
    print(f"\n{INFO} Testing generation with: {target}")

    # ---- 3: TEXT -------------------------------------------------------------
    def _try(model_id: str):
        model = genai.GenerativeModel(model_id)
        return model.generate_content(
            "Reply with exactly one word: WORKING",
            generation_config={"max_output_tokens": 10, "temperature": 0},
        )

    try:
        resp = _try(target)
        print(f"{OK} Text generation works ({target}). Replied: {resp.text.strip()!r}")
    except Exception as e:
        # A retired model returns 404 and names its replacement. Retry once.
        alt = _suggested_model(e)
        if alt and alt != target:
            print(f"{WARN} {target} is retired. Google suggests {alt} — retrying...")
            try:
                resp = _try(alt)
                target = alt
                print(f"{OK} Text generation works ({target}). Replied: {resp.text.strip()!r}")
            except Exception as e2:
                print(f"{BAD} Generation failed.\n     {diagnose(e2)}\n\n     Raw: {e2}")
                return 1
        else:
            print(f"{BAD} Generation failed.\n     {diagnose(e)}\n\n     Raw: {e}")
            return 1

    # ---- 4: AUDIO (optional) -------------------------------------------------
    if args.audio:
        if not os.path.exists(args.audio):
            print(f"{WARN} Audio file not found: {args.audio}")
        else:
            print(f"\n{INFO} Testing Hinglish audio understanding...")
            try:
                uploaded = genai.upload_file(args.audio)
                resp = model.generate_content([
                    "Transcribe this Hinglish audio. Then on a new line, give the "
                    "speaker's intent as one of: promise_to_pay, already_paid, "
                    "opt_out, pay_now, dispute, unclear.",
                    uploaded,
                ])
                print(f"{OK} Audio works. Response:\n{resp.text.strip()}")
            except Exception as e:
                print(f"{WARN} Audio failed (text still works).\n     {diagnose(e)}\n\n     Raw: {e}")

    print(f"\n{OK} KEY IS USABLE. Put it in .env as GEMINI_API_KEY=...")
    print(f"{INFO} Free-tier quota resets daily at 00:00 Pacific (12:30 PM IST).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())