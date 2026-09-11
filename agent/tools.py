"""Tool definitions + dispatch for the browser agent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.browser import Browser
from agent.captcha import UnsolvableCaptcha, solve_with_2captcha
from agent.config import Target
from agent.handoff import request_handoff

OUT = Path("/out")

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "page_info",
            "description": "Current page: url, title, visible text and every interactive element with a usable selector. Call this before clicking or typing.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "goto",
            "description": "Navigate the browser to an absolute URL.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click an element by CSS selector, or by visible text with text=<text>.",
            "parameters": {
                "type": "object",
                "properties": {"selector": {"type": "string"}},
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_text",
            "description": "Fill a field. Set submit=true to press Enter afterwards (use for login forms).",
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string"},
                    "text": {"type": "string"},
                    "submit": {"type": "boolean"},
                },
                "required": ["selector", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "press_key",
            "description": "Press a keyboard key, e.g. Enter, Tab, Escape.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Wait N seconds for the page to settle.",
            "parameters": {
                "type": "object",
                "properties": {"seconds": {"type": "number"}},
                "required": ["seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Look at the page as an image (only if the text view is not enough, e.g. visual puzzles).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "solve_captcha",
            "description": "Detect a captcha/challenge on the page and clear it with 2captcha (reCAPTCHA, hCaptcha, Turnstile, image). Fails fast if the challenge is human-only.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_handoff",
            "description": "Park the live session and hand it to the human operator. Use when the page demands something only a human can do (device check, Arkose/FunCaptcha, phone/SMS confirmation, 2FA prompt, 'suspicious activity' block).",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "login_succeeded",
            "description": "Report that the session is authenticated and the job is done. Include the evidence you saw.",
            "parameters": {
                "type": "object",
                "properties": {"evidence": {"type": "string"}},
                "required": ["evidence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "give_up",
            "description": "Stop and report a blocker you cannot clear with the available tools.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
]


class Toolbox:
    def __init__(
        self,
        browser: Browser,
        target: Target,
        twocaptcha_key: str,
        handoff_timeout: int,
        vision: bool,
    ) -> None:
        self.b = browser
        self.target = target
        self.twocaptcha_key = twocaptcha_key
        self.handoff_timeout = handoff_timeout
        self.vision = vision
        # fingerprints of challenges we already spent a solver attempt on
        self.solved_attempts: set[str] = set()
        # hard-zone URLs we already nudged the model about
        self.nudged_zones: set[str] = set()

    @property
    def handoff_allowed(self) -> bool:
        return self.target.handoff_mode != "never"

    async def dispatch(self, name: str, args: dict[str, Any]) -> tuple[str, bool, dict]:
        """Return (text_result, is_terminal, extra)."""
        page = self.b.page
        assert page is not None

        if name == "page_info":
            return await self.b.digest(), False, {}

        if name == "goto":
            url = args["url"]
            await self.b.goto(url)
            return f"navigated -> {page.url}", False, {}

        if name == "click":
            sel = args["selector"].strip()
            if sel.startswith("text="):
                await page.get_by_text(sel[5:], exact=False).first.click(timeout=15000)
            else:
                await page.click(sel, timeout=15000)
            await page.wait_for_timeout(1500)
            return f"clicked {sel} -> {page.url}", False, {}

        if name == "type_text":
            sel = args["selector"].strip()
            text = args["text"]
            target = page.locator(sel).first
            await target.click(timeout=10000)
            await target.fill("")
            await target.type(text, delay=60)
            if args.get("submit"):
                await target.press("Enter")
                await page.wait_for_timeout(2500)
            return f"typed into {sel} -> {page.url}", False, {}

        if name == "press_key":
            await page.keyboard.press(args["key"])
            await page.wait_for_timeout(1200)
            return f"pressed {args['key']} -> {page.url}", False, {}

        if name == "wait":
            secs = float(args.get("seconds", 2))
            await page.wait_for_timeout(int(min(secs, 30) * 1000))
            return f"waited {secs}s -> {page.url}", False, {}

        if name == "screenshot":
            if not self.vision:
                await self.b.save_screenshot(OUT / "last_screenshot.png")
                return (
                    "screenshot saved to /out/last_screenshot.png (this model has no vision; "
                    "use page_info instead)",
                    False,
                    {},
                )
            b64 = await self.b.screenshot_b64()
            return "screenshot attached", False, {"image_b64": b64}

        if name == "solve_captcha":
            try:
                res = await solve_with_2captcha(page, self.twocaptcha_key)
            except UnsolvableCaptcha as exc:
                return (
                    f"UNSOLVABLE_BY_SOLVER: {exc}. Call request_handoff now.",
                    False,
                    {"unsolvable": str(exc)},
                )
            except Exception as exc:  # solver outage, no key, etc.
                return f"solver error: {exc}", False, {"error": str(exc)}
            await page.wait_for_timeout(2000)
            # Remember the challenge so the harness never pays for it twice.
            try:
                spec = res if isinstance(res, dict) else {}
                self.solved_attempts.add(
                    f"{spec.get('kind', 'unknown')}:{spec.get('sitekey', '')}:{page.url}"
                )
            except Exception:
                pass
            return f"solver result: {json.dumps(res)} -> {page.url}", False, res

        if name == "request_handoff":
            if not self.handoff_allowed:
                return (
                    "handoff is disabled for this target (handoff_mode=never) - you must clear "
                    "the challenge yourself.",
                    False,
                    {},
                )
            payload = await request_handoff(
                self.b, self.target, args["reason"], timeout=self.handoff_timeout
            )
            return f"handoff result: {json.dumps(payload)}", False, payload

        if name == "login_succeeded":
            return f"SUCCESS: {args['evidence']}", True, {"status": "success"}

        if name == "give_up":
            return f"GAVE UP: {args['reason']}", True, {"status": "failed", "reason": args["reason"]}

        return f"unknown tool {name}", False, {}
