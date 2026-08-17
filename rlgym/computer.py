"""Browser environment for one attempt.

Subclasses the vendored `PlaywrightComputer` rather than reimplementing it -- upstream's
action methods (clicks, typing, scrolling, key handling) are correct and long, so they are
reused verbatim. What's added here is everything a rollout needs and a demo script doesn't:

  * **Pre-authentication** via Playwright `storage_state`, so an attempt starts on an
    authenticated page. This is the standard shape (WebArena ships per-site storage_state
    files for exactly this reason): login is a confound and a step tax, not the capability
    under test.
  * **Video recording** for the transcript viewer (Playwright tracing is wired through
    `trace_path` but no caller passes it, so no attempt records a trace).
  * **A domain lock** as an environment invariant.
  * **`prohibited_actions`**, empty by default -- nothing is blocked today.

On the domain lock: upstream `navigate()` prepends `https://` to any scheme-less URL and
will happily leave the gym, and the `search` action jumps straight to a search engine. Both
are refused here. Note the refusal is *returned to the model as an observation* rather than
the tool being removed from its action space -- the environment has a boundary, but we don't
hand-tune what the agent is allowed to attempt.
"""

from __future__ import annotations

import pathlib
import time
from urllib.parse import urlparse

# Classes, so imported directly. sys.path is set up by rlgym/__init__.py, so these no
# longer depend on import order.
from computers import EnvState  # type: ignore[import-not-found]
from computers.playwright.playwright import (  # type: ignore[import-not-found]
    PlaywrightComputer,
)

# Same viewport main.py uses, so attempts match the harness we verified in Phase 2.
DEFAULT_SCREEN_SIZE = (1440, 900)

class ActionRefused(Exception):
    """Raised when the domain lock or prohibited_actions blocks an action.

    The agent loop catches this, records it on the step, and hands the reason back to the
    model so it can adapt instead of silently repeating a no-op.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def same_origin(url: str, allowed_origin: str) -> bool:
    """True when `url` is on the allowed origin. Blank/about: URLs are permitted."""
    if not url or url.startswith(("about:", "chrome-error://")):
        return True
    parsed = urlparse(url)
    if not parsed.scheme and not parsed.netloc:
        return True  # relative URL -- resolves against the current origin
    allowed = urlparse(allowed_origin)
    return (parsed.scheme, parsed.netloc) == (allowed.scheme, allowed.netloc)


class GymComputer(PlaywrightComputer):
    """A recorded, domain-locked, pre-authenticated browser for one attempt."""

    def __init__(
        self,
        *,
        gym_url: str,
        screen_size: tuple[int, int] = DEFAULT_SCREEN_SIZE,
        storage_state: pathlib.Path | str | None = None,
        video_dir: pathlib.Path | str | None = None,
        trace_path: pathlib.Path | str | None = None,
        headless: bool = True,
        highlight_mouse: bool = True,
        prohibited_actions: tuple[str, ...] | list[str] = (),
    ) -> None:
        super().__init__(
            screen_size=screen_size,
            initial_url=gym_url,
            # search_engine_url is deliberately left at upstream's off-origin default.
            # Repointing it at the gym would turn `search` into a silent redirect home,
            # which looks to the model like a search that returned nothing useful. Letting
            # the domain lock refuse it instead gives an explicit, observable boundary:
            # the agent learns the environment is sealed rather than being quietly
            # teleported.
            highlight_mouse=highlight_mouse,
        )
        self.gym_url = gym_url.rstrip("/")
        self._storage_state = str(storage_state) if storage_state else None
        self._video_dir = pathlib.Path(video_dir) if video_dir else None
        self._trace_path = pathlib.Path(trace_path) if trace_path else None
        self._headless = headless
        self.prohibited_actions = set(prohibited_actions)

        # Filled in as the attempt runs: refusals recorded, video path resolved on exit.
        self.refusals: list[str] = []
        self.video_path: pathlib.Path | None = None

    # -- lifecycle ----------------------------------------------------------------------

    def __enter__(self) -> GymComputer:
        from playwright.sync_api import sync_playwright  # local import: heavy

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            args=[
                "--disable-extensions",
                "--disable-file-system",
                "--disable-plugins",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-sync",
            ],
            headless=self._headless,
        )

        context_kwargs: dict = {
            "viewport": {
                "width": self._screen_size[0],
                "height": self._screen_size[1],
            }
        }
        if self._storage_state:
            context_kwargs["storage_state"] = self._storage_state
        if self._video_dir:
            self._video_dir.mkdir(parents=True, exist_ok=True)
            context_kwargs["record_video_dir"] = str(self._video_dir)
            context_kwargs["record_video_size"] = {
                "width": self._screen_size[0],
                "height": self._screen_size[1],
            }

        self._context = self._browser.new_context(**context_kwargs)

        if self._trace_path:
            self._context.tracing.start(screenshots=True, snapshots=True)

        self._page = self._context.new_page()
        self._install_navigation_guard()
        self._context.on("page", self._handle_new_page)

        self._page.goto(self.gym_url)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Tear down in an order that actually produces a playable video.

        Playwright only finalizes the video file when the *context* closes, so this must
        run even when the attempt failed. The video path has to be read before closing,
        because `page.video` is gone afterwards.
        """
        video_handle = None
        try:
            if getattr(self, "_page", None) and self._video_dir:
                video_handle = self._page.video
        except Exception:
            video_handle = None

        try:
            if self._trace_path and getattr(self, "_context", None):
                self._trace_path.parent.mkdir(parents=True, exist_ok=True)
                self._context.tracing.stop(path=str(self._trace_path))
        except Exception:
            pass  # a missing trace must never mask the real failure

        try:
            if getattr(self, "_context", None):
                self._context.close()
        except Exception:
            pass

        if video_handle is not None:
            try:
                self.video_path = pathlib.Path(video_handle.path())
            except Exception:
                self.video_path = None

        try:
            self._browser.close()
        except Exception as exc:
            if "Connection closed while reading from the driver" not in str(exc):
                raise
        finally:
            try:
                self._playwright.stop()
            except Exception:
                pass

    # -- domain lock --------------------------------------------------------------------

    def _install_navigation_guard(self) -> None:
        """Abort off-origin *top-level* navigations; leave sub-resources alone.

        The route predicate only matches off-origin URLs, so same-origin traffic (all of
        Metabase) is never intercepted and pays no latency. Sub-resource loads from other
        hosts -- fonts, CDN scripts Metabase itself pulls in -- are explicitly allowed;
        only a main-frame navigation is blocked.
        """

        def is_off_origin(url: str) -> bool:
            return not same_origin(url, self.gym_url)

        def guard(route, request) -> None:
            try:
                is_top_level = (
                    request.is_navigation_request()
                    and request.frame == self._page.main_frame
                )
            except Exception:
                is_top_level = False

            if is_top_level:
                self.refusals.append(f"blocked navigation to {request.url}")
                route.abort()
            else:
                route.continue_()

        self._context.route(is_off_origin, guard)

    def _assert_allowed(self, url: str) -> None:
        if not same_origin(url, self.gym_url):
            reason = (
                f"navigation to {url!r} refused: this environment is restricted to "
                f"{self.gym_url}"
            )
            self.refusals.append(reason)
            raise ActionRefused(reason)

    def check_action_allowed(self, action_name: str) -> None:
        """Enforce `prohibited_actions`. Empty by default, so normally a no-op."""
        if action_name in self.prohibited_actions:
            reason = f"action {action_name!r} is not permitted in this run"
            self.refusals.append(reason)
            raise ActionRefused(reason)

    def navigate(self, url: str) -> EnvState:
        # Deliberately checked BEFORE upstream's https:// prepending, so the refusal
        # message shows the URL the model actually asked for.
        self._assert_allowed(url)
        return super().navigate(url)

    def _handle_new_page(self, new_page) -> None:
        """Upstream re-opens popups in the current tab; make sure that respects the lock."""
        url = new_page.url
        try:
            new_page.close()
        except Exception:
            pass
        if not same_origin(url, self.gym_url):
            self.refusals.append(f"blocked popup navigation to {url}")
            return
        self._page.goto(url)

    # -- readiness checks ---------------------------------------------------------------

    def is_authenticated(self) -> bool:
        """True unless we're sitting on Metabase's login page.

        Used as a pre-flight check: if injected `storage_state` has expired, that is an
        infrastructure failure (NOT_OBSERVED), not the agent failing its task.
        """
        try:
            self._page.wait_for_load_state()
            return "/auth/login" not in self._page.url
        except Exception:
            return False

    def wait_for_ready(self, timeout_ms: int = 30_000) -> None:
        """Let Metabase's SPA settle before the first screenshot."""
        try:
            self._page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            time.sleep(2)  # networkidle can never arrive on a polling SPA
