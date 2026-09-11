"""Playwright session: persistent profile, DOM digest, login/handoff detection."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

from agent.config import Target

PROFILE_DIR = Path("/profile/chrome")
STATE_FILE = Path("/out/session/storage_state.json")

# Arkose/FunCaptcha traffic (Facebook serves the widget from fbsbx.com and its
# API from a per-tenant arkoselabs.com subdomain).
ARKOSE_URL_HINTS = ("arkoselabs", "fbsbx.com/captcha", "funcaptcha")

_DIGEST_JS = r"""
(() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    const s = window.getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const sel = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    if (el.getAttribute('name')) return `${el.tagName.toLowerCase()}[name="${el.getAttribute('name')}"]`;
    if (el.getAttribute('type') && el.tagName === 'INPUT') return `input[type="${el.getAttribute('type')}"]`;
    if (el.getAttribute('data-testid')) return `[data-testid="${el.getAttribute('data-testid')}"]`;
    const al = el.getAttribute('aria-label');
    if (al) return `${el.tagName.toLowerCase()}[aria-label="${al}"]`;
    return null;
  };
  const label = (el) => (
    el.getAttribute('aria-label') || el.placeholder || el.value || el.textContent || ''
  ).trim().replace(/\s+/g, ' ').slice(0, 70);

  const out = [];
  document.querySelectorAll('input, textarea, select, button, a, [role="button"], [data-sitekey], iframe, h1, h2, label').forEach((el) => {
    if (!vis(el)) return;
    const t = el.tagName.toLowerCase();
    if (t === 'iframe') {
      const src = (el.getAttribute('src') || '').slice(0, 120);
      out.push(`iframe src="${src}"`);
      return;
    }
    if (el.hasAttribute('data-sitekey')) {
      out.push(`${t} ${sel(el) || ''} [data-sitekey="${el.getAttribute('data-sitekey')}"]`);
      return;
    }
    if (t === 'h1' || t === 'h2' || t === 'label') {
      const txt = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 90);
      if (txt) out.push(`${t}: ${txt}`);
      return;
    }
    out.push(`${t} ${sel(el) || '(no stable selector)'} :: ${label(el)}`);
  });
  return {
    url: location.href,
    title: document.title,
    text: (document.body ? document.body.innerText : '').replace(/\s+/g, ' ').slice(0, 900),
    elements: out.slice(0, 70),
  };
})()
"""


class Browser:
    def __init__(self, headless: bool = False) -> None:
        self.headless = headless
        self._pw: Playwright | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._pending_origins: list[dict] = []

    @staticmethod
    def _clear_stale_locks(profile: Path) -> int:
        """A killed container leaves Chromium singleton locks behind; the next
        run then dies with TargetClosedError. One container per profile at a
        time means these are always stale."""
        removed = 0
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            path = profile / name
            try:
                if path.is_symlink() or path.exists():
                    path.unlink()
                    removed += 1
            except Exception:
                continue
        return removed

    async def _launch(self) -> BrowserContext:
        assert self._pw is not None
        return await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=self.headless,
            viewport={"width": 1420, "height": 860},
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
            ],
            ignore_default_args=["--enable-automation"],
        )

    async def start(self) -> Page:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        freed = self._clear_stale_locks(PROFILE_DIR)
        if freed:
            print(f"[browser] cleared {freed} stale profile lock(s)", flush=True)
        self._pw = await async_playwright().start()
        try:
            self.context = await self._launch()
        except Exception as exc:
            print(f"[browser] launch failed ({type(exc).__name__}), clearing locks and retrying", flush=True)
            self._clear_stale_locks(PROFILE_DIR)
            await asyncio.sleep(1)
            self.context = await self._launch()
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.set_default_timeout(20000)
        self._watch_arkose()
        restored = await self._restore_state()
        if restored:
            print(f"[browser] restored {restored} cookie(s) from {STATE_FILE}", flush=True)
        return self.page

    def _watch_arkose(self) -> None:
        """Record Arkose/FunCaptcha request URLs for the captcha layer.

        Sites that wrap Arkose in their own iframe (Facebook serves the widget
        from fbsbx.com) do not expose the public key or subdomain in the DOM -
        the key arrives on a request made from inside a cross-origin frame, so a
        request listener is the only place it can be observed.
        """
        if self.page is None:
            return
        seen: list[str] = []
        setattr(self.page, "_arkose_urls", seen)

        def _record(request) -> None:
            try:
                url = request.url
            except Exception:
                return
            if any(h in url for h in ARKOSE_URL_HINTS):
                seen.append(url)

        try:
            self.page.on("request", _record)
        except Exception:
            pass

    async def _restore_state(self) -> int:
        """Replay the exported storage state on boot.

        Chromium drops non-persistent (session) cookies when it exits, so the
        exported state - not the profile alone - is the reliable hand-back
        channel between runs."""
        if not STATE_FILE.exists() or self.context is None:
            return 0
        try:
            data = json.loads(STATE_FILE.read_text())
        except Exception:
            return 0
        cookies = data.get("cookies") or []
        if cookies:
            try:
                await self.context.add_cookies(cookies)
            except Exception:
                pass
        self._pending_origins = list(data.get("origins") or [])
        return len(cookies)

    async def _apply_local_storage(self) -> None:
        if not self._pending_origins or self.page is None:
            return
        try:
            origin = await self.page.evaluate("location.origin")
        except Exception:
            return
        for entry in list(self._pending_origins):
            if entry.get("origin") == origin:
                items = entry.get("localStorage") or []
                try:
                    await self.page.evaluate(
                        "(items) => { for (const it of items) { try { localStorage.setItem(it.name, it.value); } catch (e) {} } }",
                        items,
                    )
                except Exception:
                    pass
                self._pending_origins.remove(entry)

    async def goto(self, url: str) -> str:
        assert self.page
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await self.page.wait_for_timeout(1200)
        await self._apply_local_storage()
        return self.page.url

    async def digest(self) -> str:
        assert self.page
        data = await self.page.evaluate(_DIGEST_JS)
        lines = [
            f"URL: {data['url']}",
            f"TITLE: {data['title']}",
            "VISIBLE TEXT: " + data["text"][:700],
            "INTERACTIVE:",
        ]
        lines += [f"  - {e}" for e in data["elements"]]
        return "\n".join(lines)

    async def screenshot_b64(self) -> str:
        assert self.page
        raw = await self.page.screenshot(full_page=False)
        return base64.b64encode(raw).decode()

    async def save_screenshot(self, path: Path) -> None:
        assert self.page
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.page.screenshot(path=str(path), full_page=False)

    # ------------------------------------------------------------------ checks
    async def is_logged_in(self, target: Target) -> tuple[bool, str]:
        assert self.page
        url = self.page.url
        for pat in target.success_url_patterns:
            if pat in url:
                return True, f"url matches {pat}"
        for sel in target.success_selectors:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    return True, f"selector visible {sel}"
            except Exception:
                continue
        return False, ""

    async def handoff_pending(self, target: Target) -> tuple[bool, str]:
        """True when the page shows something only a human can clear."""
        assert self.page
        url = self.page.url
        for pat in target.handoff_url_patterns:
            if pat in url:
                return True, f"url matches {pat}"
        for sel in target.handoff_selectors:
            try:
                el = await self.page.query_selector(sel)
                if el and await el.is_visible():
                    return True, f"handoff marker {sel} visible"
            except Exception:
                continue
        return False, ""

    async def export_state(self, out_path: Path) -> Path:
        assert self.context
        out_path.parent.mkdir(parents=True, exist_ok=True)
        await self.context.storage_state(path=str(out_path))
        return out_path

    async def cookies(self) -> list[dict]:
        assert self.context
        return await self.context.cookies()

    async def close(self) -> None:
        try:
            if self.context:
                await self.context.close()
        finally:
            if self._pw:
                await self._pw.stop()


def profile_summary(path: Path = PROFILE_DIR) -> dict:
    if not path.exists():
        return {"exists": False}
    files = sum(1 for _ in path.rglob("*") if _.is_file())
    size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return {"exists": True, "files": files, "bytes": size}
