"""Human handoff: park the live session, hand it to Adrian over noVNC, resume.

Contract with the host CLI (`forge`):
  * when the agent needs a human it writes /out/handoff.json and touches
    /out/handoff.READY  -> the CLI prints the noVNC URL and a bell.
  * the human solves it in the very same Chromium window (same profile, same
    cookies), optionally pressing the on-screen "HUMAN DONE" file trigger.
  * the agent resumes as soon as it sees the logged-in state (auto-resume) or
    finds /out/resume (manual escape hatch).
  * /out/handoff.DONE is written when the session is back.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from agent.browser import Browser
from agent.config import Target

OUT = Path("/out")


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def request_handoff(
    browser: Browser,
    target: Target,
    reason: str,
    timeout: int = 1200,
    poll: int = 3,
) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    shot = OUT / "handoff.png"
    try:
        await browser.save_screenshot(shot)
    except Exception:
        shot = None

    url = ""
    url_file = OUT / "novnc_url"
    if url_file.exists():
        url = url_file.read_text().strip()

    payload = {
        "status": "handoff",
        "reason": reason,
        "target": target.name,
        "page_url": browser.page.url if browser.page else "",
        "novnc_url": url,
        "screenshot": str(shot) if shot else None,
        "started_at": _stamp(),
        "timeout_seconds": timeout,
    }
    (OUT / "handoff.json").write_text(json.dumps(payload, indent=2))
    (OUT / "handoff.READY").write_text(_stamp())

    banner = f"""
================================================================
  HUMAN HANDOFF REQUIRED
  reason : {reason}
  page   : {payload['page_url']}
  open   : {url}
  drop   : touch /out/resume   (or just finish the login - auto-resume)
  budget : {timeout}s
================================================================
"""
    print(banner, flush=True)

    deadline = time.time() + timeout
    has_marker = bool((await browser.handoff_pending(target))[0])
    while time.time() < deadline:
        if (OUT / "resume").exists():
            payload.update(status="resumed", how="manual", ended_at=_stamp())
            break
        ok, why = await browser.is_logged_in(target)
        if ok:
            payload.update(status="resumed", how="auto", evidence=why, ended_at=_stamp())
            break
        if has_marker:
            still_pending, _ = await browser.handoff_pending(target)
            if not still_pending:
                # The human cleared the blocker even though we are not "logged in"
                # yet (e.g. the challenge step just moved the flow forward).
                payload.update(status="resumed", how="cleared", ended_at=_stamp())
                break
        await browser.page.wait_for_timeout(poll * 1000)

    else:
        payload.update(status="timeout", ended_at=_stamp())

    (OUT / "handoff.json").write_text(json.dumps(payload, indent=2))
    (OUT / "handoff.DONE").write_text(_stamp())
    print(f"[handoff] {payload['status']} ({payload.get('how', '-')})", flush=True)
    return payload
