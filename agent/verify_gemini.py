"""Step 6 verification: confirm the Gemini API key is live and correctly scoped.

Deliberately trivial. No browser, no screenshots, no Metabase, no agent loop -- this only
answers "does the key work, and can it reach a computer-use-capable model?".

Two checks:
  1. A single plain-text generate_content call. Proves the key is valid at all.
  2. The same call with the ComputerUse tool declared (but never executed). Proves the key
     is scoped for computer use specifically, which a plain-text call would not catch.

Run:
    cd agent && ./.venv/bin/python verify_gemini.py
"""

import os
import pathlib
import sys

from dotenv import load_dotenv

# .env lives at the project root, one level above agent/
PROJECT_ROOT = pathlib.Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")

from google import genai  # noqa: E402
from google.genai import types  # noqa: E402

# The repo's default model (main.py --model default).
DEFAULT_MODEL = "gemini-3.6-flash"

# Every computer-use-capable model the README lists, to see what this key can actually reach.
CANDIDATE_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3-flash-preview",
    "gemini-2.5-computer-use-preview-10-2025",
]


def main() -> int:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key or key == "PASTE_YOUR_KEY_HERE":
        print("FAIL: GEMINI_API_KEY missing or still the placeholder in .env")
        return 1
    print(f"key loaded: {key[:6]}...{key[-4:]} (len {len(key)})")
    print(f"USE_VERTEXAI: {os.environ.get('USE_VERTEXAI')}")
    print()

    client = genai.Client(api_key=key)

    # ---- Check 1: plain text call ----
    print(f"[1] plain generate_content -> {DEFAULT_MODEL}")
    try:
        resp = client.models.generate_content(
            model=DEFAULT_MODEL,
            contents="Reply with exactly the word: OK",
        )
        print("    response text :", repr(resp.text))
        if resp.usage_metadata:
            print("    tokens        :",
                  f"prompt={resp.usage_metadata.prompt_token_count}",
                  f"total={resp.usage_metadata.total_token_count}")
        check1 = True
    except Exception as e:
        print(f"    ERROR: {type(e).__name__}: {e}")
        check1 = False
    print()

    # ---- Check 2: same call, ComputerUse tool declared but never executed ----
    print(f"[2] generate_content WITH ComputerUse tool -> {DEFAULT_MODEL}")
    try:
        resp2 = client.models.generate_content(
            model=DEFAULT_MODEL,
            contents="What tools do you have available? Answer in one sentence.",
            config=types.GenerateContentConfig(
                tools=[
                    types.Tool(
                        computer_use=types.ComputerUse(
                            environment=types.Environment.ENVIRONMENT_BROWSER,
                        ),
                    )
                ],
            ),
        )
        text = resp2.text
        print("    accepted the ComputerUse tool: True")
        print("    response text :", repr(text[:200] if text else text))
        check2 = True
    except Exception as e:
        print(f"    ERROR: {type(e).__name__}: {e}")
        check2 = False
    print()

    # ---- Check 3: which computer-use models does this key reach? ----
    # The ComputerUse tool must be declared on every probe. Some models (notably
    # gemini-2.5-computer-use-preview-10-2025) hard-reject any request without it:
    #   400 INVALID_ARGUMENT "This model requires the use of the Computer Use tool."
    # Probing without the tool reports a false failure that looks like a key-scope problem.
    cu_config = types.GenerateContentConfig(
        tools=[
            types.Tool(
                computer_use=types.ComputerUse(
                    environment=types.Environment.ENVIRONMENT_BROWSER,
                ),
            )
        ],
    )
    print("[3] model reachability (ComputerUse tool declared on each)")
    reachable = []
    for model in CANDIDATE_MODELS:
        try:
            client.models.generate_content(
                model=model, contents="Say OK.", config=cu_config
            )
            print(f"    {model:<45} OK")
            reachable.append(model)
        except Exception as e:
            msg = str(e).replace("\n", " ")[:90]
            print(f"    {model:<45} FAILED  {type(e).__name__}: {msg}")
    print()

    ok = check1 and check2
    print("RESULT:", "PASS" if ok else "FAIL")
    print(f"reachable computer-use models: {len(reachable)}/{len(CANDIDATE_MODELS)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
