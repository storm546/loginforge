"""The agent loop: LLM (OpenRouter) drives the browser through tools.

Deterministic guards run after every step so the model cannot wander:
  * a page matching the target's handoff rules -> handoff, always.
  * a captcha on the page -> solved through 2captcha, always (once per
    fingerprint); if it persists, or it is human-only, -> handoff.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.browser import Browser
from agent.captcha import detect as detect_captcha
from agent.config import Target
from agent.llm import LLM, parse_args
from agent.tools import TOOL_SPECS, Toolbox

OUT = Path("/out")
INSTRUCTIONS_DIR = Path("/app/instructions")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Logger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = self.path.open("a")

    def event(self, kind: str, **fields: Any) -> None:
        rec = {"ts": _now(), "event": kind, **fields}
        self.fh.write(json.dumps(rec, default=str) + "\n")
        self.fh.flush()
        if kind == "step":
            print(
                f"[step {fields.get('n')}] {fields.get('tool')} "
                f"-> {str(fields.get('result'))[:200]}",
                flush=True,
            )
        elif kind in ("assistant", "boot", "result", "error", "guard"):
            print(f"[{kind}] {json.dumps(fields, default=str)[:600]}", flush=True)

    def close(self) -> None:
        self.fh.close()


async def guard(browser: Browser, toolbox: Toolbox, logger: Logger, step: int) -> str | None:
    """Hard-wired reactions to the page state. Returns a note for the model.

    Escalation ladder: solver -> the agent's own interaction -> human handoff.
    A handoff only happens when the target asks for it eagerly, when the agent
    calls request_handoff itself, or when a target is configured handoff_mode:
    never is not set and everything else has failed.
    """
    if browser.page is None:
        return None
    target = toolbox.target
    mode = target.handoff_mode

    hard, why = await browser.handoff_pending(target)
    if hard and mode == "eager":
        text, _, _ = await toolbox.dispatch("request_handoff", {"reason": f"trigger matched: {why}"})
        logger.event("guard", step=step, action="handoff", trigger=why, result=text[:400])
        return f"[harness] the page matched a human-required rule ({why}). Result: {text}"

    try:
        cap = await detect_captcha(browser.page)
    except Exception as exc:
        logger.event("guard", step=step, action="detect_error", error=str(exc))
        return None

    if not cap:
        if hard and mode != "never" and browser.page.url not in toolbox.nudged_zones:
            toolbox.nudged_zones.add(browser.page.url)
            logger.event("guard", step=step, action="hard_zone", url=browser.page.url)
            return (
                f"[harness] hard zone ({why}): this part of the flow normally trips a human "
                "check. Work through it yourself - inspect the page, click through the "
                "challenge, use screenshot if it is visual. Only call request_handoff if you "
                "have genuinely run out of options."
            )
        return None

    if cap.kind == "unsolvable":
        logger.event("guard", step=step, action="unsolvable", challenge=cap.reason, mode=mode)
        return (
            f"[harness] {cap.reason} detected - no solver supports this vendor. Clear it "
            "yourself: interact with the page (click, drag, wait for it to settle) and use "
            "screenshot to see it. request_handoff is a last resort."
        )

    fp = f"{cap.kind}:{cap.sitekey}:{cap.pageurl}"
    if not toolbox.twocaptcha_key:
        return (
            f"[harness] captcha detected ({cap.kind}) but TWOCAPTCHA_API_KEY is unset. Try to "
            "clear it yourself with clicks; call request_handoff only if you cannot."
        )
    if fp in toolbox.solved_attempts:
        logger.event("guard", step=step, action="persisted", challenge=cap.kind)
        return (
            f"[harness] the solver already tried this {cap.kind} and it is still there. Take it "
            "over yourself: screenshot the page, click the challenge widgets, wait, retry. "
            "request_handoff only if it is truly stuck."
        )

    toolbox.solved_attempts.add(fp)
    text, _, _ = await toolbox.dispatch("solve_captcha", {})
    logger.event("guard", step=step, action="solve", challenge=cap.kind, sitekey=cap.sitekey, result=text[:400])
    return f"[harness] captcha detected ({cap.kind}) and handled automatically. Result: {text}"


def build_system_prompt(target: Target, mode: str, instructions_file: str) -> str:
    path = INSTRUCTIONS_DIR / instructions_file
    base = path.read_text() if path.exists() else ""
    mode_line = (
        "MODE: first-time login. The profile may be empty - you must authenticate from scratch."
        if mode == "first-login"
        else "MODE: challenge. A warmed profile exists; assume it may already be authenticated."
    )
    autonomy = {
        "never": "You must clear every challenge yourself. Human handoff is switched off for this target.",
        "auto": ("You must clear challenges yourself first (solver, then your own clicking and "
                 "vision). request_handoff exists but is a LAST RESORT after you have genuinely "
                 "tried everything."),
        "eager": "Human handoff triggers are active for this target: rules may park the session for a human.",
    }[target.handoff_mode]
    return f"""You are a browser operator agent. You control one real Chromium window
through tools. You are literal, terse and methodical: one tool call per turn.

{mode_line}

AUTONOMY
{autonomy}

TARGET CONFIG
{target.describe()}

RULES
1. Call page_info after every navigation or click before deciding the next step.
2. Never invent selectors - only use selectors that appear in page_info output.
3. Credentials are given above. Do not ask for them and do not print them elsewhere.
4. A captcha or "confirm it is you" page is your problem, not the human's. Solve it:
   call solve_captcha once; if it comes back UNSOLVABLE_BY_SOLVER or "persisted", look at
   the page (screenshot) and interact with it directly - click the widget, drag the
   slider, wait for it to settle, retry. Never repeat an identical failing action.
5. Interstitials ("Save your login info?", notifications, cookie banners) -> dismiss them
   with the "Not now" / "Accept all" / "Skip" control and continue.
6. Only when you have exhausted steps 4-5 and are truly stuck may you call
   request_handoff with a one-line reason. After it resumes, page_info and continue.
7. Finish with login_succeeded (evidence: the url/selector you saw) or give_up.
   Never end your turn without a tool call.

TASK INSTRUCTIONS
{base}
"""


async def run_loop(
    browser: Browser,
    target: Target,
    llm: LLM,
    toolbox: Toolbox,
    logger: Logger,
    mode: str,
    instructions_file: str,
    max_steps: int = 40,
) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(target, mode, instructions_file)},
        {"role": "user", "content": "Start now. Call page_info on the current page."},
    ]

    # Bootstrap: let the harness clear whatever is already on screen (handoff
    # triggers, captchas) before the model gets a turn. Bounded so a stubborn
    # page can't loop us forever.
    result: dict[str, Any] = {"status": "step_limit", "steps": 0}
    started = time.time()
    for _ in range(4):
        ok, why = await browser.is_logged_in(target)
        if ok:
            result = {"status": "success", "steps": 0, "evidence": f"harness detected login ({why})"}
            result["duration_seconds"] = round(time.time() - started, 1)
            logger.event("result", **result)
            return result
        note = await guard(browser, toolbox, logger, step=0)
        if not note:
            break
        messages.append({"role": "user", "content": note})

    for step in range(1, max_steps + 1):
        tools = TOOL_SPECS if toolbox.handoff_allowed else [
            t for t in TOOL_SPECS if t["function"]["name"] != "request_handoff"
        ]
        try:
            msg = llm.chat(messages, tools)
        except Exception as exc:
            logger.event("error", where="llm", error=str(exc))
            result = {"status": "llm_error", "error": str(exc), "steps": step - 1}
            break

        messages.append(LLM.dump(msg))
        calls = getattr(msg, "tool_calls", None) or []

        if not calls:
            logger.event("assistant", n=step, content=(msg.content or "")[:400])
            messages.append({
                "role": "user",
                "content": "You must answer with a tool call. Call page_info and continue.",
            })
            continue

        terminal = False
        for call in calls:
            name = call.function.name
            args = parse_args(call.function.arguments)
            logger.event("step", n=step, tool=name, args=args)

            try:
                text, terminal, extra = await toolbox.dispatch(name, args)
            except Exception as exc:
                text, terminal, extra = f"tool error: {type(exc).__name__}: {exc}", False, {}

            logger.event("step", n=step, tool=name, result=text[:600])

            tool_msg: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": call.id,
                "name": name,
                "content": text[:4000],
            }
            if extra.get("image_b64"):
                tool_msg["content"] = [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{extra['image_b64']}"}},
                ]
            messages.append(tool_msg)

            if terminal:
                result = {"status": extra.get("status", "success"), "steps": step, **extra}
                if extra.get("status") == "success":
                    result["evidence"] = args.get("evidence", "")
                break

        if terminal or result["status"] in ("success", "failed"):
            break

        note = await guard(browser, toolbox, logger, step=step)
        if note:
            messages.append({"role": "user", "content": note})
            ok, why = await browser.is_logged_in(target)
            if ok:
                result = {"status": "success", "steps": step, "evidence": f"harness detected login ({why})"}
                logger.event("result", **result)
                break

    result["duration_seconds"] = round(time.time() - started, 1)
    logger.event("result", **result)
    return result
