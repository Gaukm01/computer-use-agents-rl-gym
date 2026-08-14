"""Step 7 verification: prove the browser harness can see the Metabase gym.

Deliberately NOT an agent loop. No Gemini call, no model, no action-taking. This only
answers: can computer-use-preview's own PlaywrightComputer open http://localhost:3000,
render the real Metabase login page, and hand back a screenshot in the format the model
expects?

It uses the repo's PlaywrightComputer rather than a hand-rolled Playwright script on
purpose -- verifying a harness we won't actually use would prove nothing.

Run:
    cd agent && PLAYWRIGHT_HEADLESS=1 ./.venv/bin/python verify_harness.py
"""

import pathlib
import sys

REPO = pathlib.Path(__file__).parent / "computer-use-preview"
sys.path.insert(0, str(REPO))

from computers import PlaywrightComputer  # noqa: E402

# Same screen size main.py uses, so this matches real agent runs.
SCREEN_SIZE = (1440, 900)

# NOTE: the scheme is mandatory. PlaywrightComputer.navigate() prepends "https://" to any
# URL without one, so a bare "localhost:3000" would silently become https:// and fail.
GYM_URL = "http://localhost:3000"

OUT_DIR = pathlib.Path(__file__).parent / "artifacts"
OUT_DIR.mkdir(exist_ok=True)


def describe_png(data: bytes) -> tuple[int, int]:
    """Read width/height straight from the PNG IHDR chunk -- no image lib needed."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


def main() -> int:
    print(f"Opening {GYM_URL} at {SCREEN_SIZE[0]}x{SCREEN_SIZE[1]} ...")

    with PlaywrightComputer(
        screen_size=SCREEN_SIZE,
        initial_url=GYM_URL,
    ) as computer:
        state = computer.current_state()

        png_w, png_h = describe_png(state.screenshot)
        viewport = computer.screen_size()

        out = OUT_DIR / "step7_login_page.png"
        out.write_bytes(state.screenshot)

        print()
        print("  url reported       :", state.url)
        print("  screenshot bytes   :", len(state.screenshot))
        print("  PNG magic          : OK (valid PNG)")
        print("  PNG dimensions     :", f"{png_w}x{png_h}")
        print("  harness screen_size:", f"{viewport[0]}x{viewport[1]}")
        print("  match?             :", (png_w, png_h) == tuple(viewport))
        print("  saved to           :", out)

        # Prove we're on the real Metabase login page and not an error/blank page.
        # Reading the DOM is a *verification* affordance for this step only -- the agent
        # itself stays visual-only and never inspects the DOM.
        title = computer._page.title()
        has_password_field = computer._page.locator("input[type=password]").count()
        print("  page <title>       :", repr(title))
        print("  password inputs    :", has_password_field)

        ok = (
            state.url.startswith(GYM_URL)
            and (png_w, png_h) == tuple(viewport)
            and has_password_field >= 1
        )
        print()
        print("RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
