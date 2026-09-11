"""loginforge agent entrypoint: boot, run the loop, hand the session back, exit."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from agent.browser import PROFILE_DIR, Browser, profile_summary
from agent.config import Target, env, env_int, load_target
from agent.llm import LLM, credits, resolve_model
from agent.loop import Logger, run_loop
from agent.tools import Toolbox

OUT = Path("/out")
SESSION_DIR = OUT / "session"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="loginforge-agent")
    p.add_argument("--mode", choices=["first-login", "challenge"], default="first-login")
    p.add_argument("--target", default=env("FORGE_TARGET", "example"))
    p.add_argument("--identity", default=env("FORGE_IDENTITY", "default"))
    p.add_argument("--model", default=env("OPENROUTER_MODEL") or None)
    p.add_argument("--steps", type=int, default=env_int("FORGE_MAX_STEPS", 40))
    p.add_argument("--vision", action="store_true", default=env("FORGE_VISION") == "1")
    p.add_argument("--handoff-timeout", type=int, default=env_int("FORGE_HANDOFF_TIMEOUT", 1200))
    p.add_argument("--headless", action="store_true", default=env("FORGE_HEADLESS") == "1")
    return p.parse_args()


async def amain() -> int:
    args = parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    logger = Logger(OUT / "run.jsonl")

    target: Target = load_target(args.target)
    api_key = env("OPENROUTER_API_KEY")
    twocaptcha_key = env("TWOCAPTCHA_API_KEY")

    if not api_key:
        logger.event("error", where="config", error="OPENROUTER_API_KEY missing")
        return 2

    model = resolve_model(api_key, want_vision=args.vision, override=args.model)
    try:
        bal = credits(api_key)
        logger.event("boot", **{
            "mode": args.mode,
            "target": target.name,
            "identity": args.identity,
            "model": model,
            "vision": args.vision,
            "twocaptcha": bool(twocaptcha_key),
            "openrouter_credits": bal.get("total_credits"),
            "openrouter_usage": bal.get("total_usage"),
            "profile": profile_summary(),
        })
    except Exception as exc:
        logger.event("boot", mode=args.mode, model=model, credits_error=str(exc))

    instructions = "first_login.md" if args.mode == "first-login" else "challenge.md"
    llm = LLM(api_key=api_key, model=model, vision=args.vision)
    browser = Browser(headless=args.headless)

    result: dict = {"status": "error"}
    try:
        await browser.start()
        await browser.goto(target.url)

        # A warmed profile that is already authenticated needs no fight.
        ok, why = await browser.is_logged_in(target)
        if ok:
            result = {"status": "success", "steps": 0, "evidence": f"already authenticated ({why})"}
            logger.event("result", **result)
        else:
            if env("FORGE_DETERMINISTIC", "1") == "1":
                from agent.presets import deterministic_login
                det = await deterministic_login(browser, target)
                logger.event("deterministic_login", **det)
                ok, why = await browser.is_logged_in(target)
                if ok:
                    result = {"status": "success", "steps": 0, "evidence": f"deterministic login ({why})"}
                    logger.event("result", **result)

        if not result.get("status") == "success":
            toolbox = Toolbox(
                browser=browser,
                target=target,
                twocaptcha_key=twocaptcha_key,
                handoff_timeout=args.handoff_timeout,
                vision=args.vision,
            )
            result = await run_loop(
                browser=browser,
                target=target,
                llm=llm,
                toolbox=toolbox,
                logger=logger,
                mode=args.mode,
                instructions_file=instructions,
                max_steps=args.steps,
            )

        # --- hand the session back -------------------------------------------
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        state_path = await browser.export_state(SESSION_DIR / "storage_state.json")
        cookies = await browser.cookies()
        names = sorted({c.get("name", "") for c in cookies})
        await browser.save_screenshot(OUT / "final.png")

        summary = {
            "identity": args.identity,
            "target": target.name,
            "mode": args.mode,
            "status": result.get("status"),
            "evidence": result.get("evidence", ""),
            "steps": result.get("steps"),
            "duration_seconds": result.get("duration_seconds"),
            "profile_dir": str(PROFILE_DIR),
            "profile": profile_summary(),
            "storage_state": str(state_path),
            "cookies": len(cookies),
            "cookie_names": names[:40],
            "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (OUT / "result.json").write_text(json.dumps(summary, indent=2))
        logger.event("session", **{k: summary[k] for k in ("cookies", "cookie_names", "profile")})
        print("\n=== RESULT ===")
        print(json.dumps(summary, indent=2))
        return 0 if result.get("status") == "success" else 1
    except Exception as exc:
        logger.event("error", where="main", error=f"{type(exc).__name__}: {exc}")
        print(f"[fatal] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 3
    finally:
        try:
            await browser.close()
        finally:
            logger.close()


def main() -> None:
    sys.exit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
