"""Deterministic login pass.

The known part of a first login (open the login page, fill the two fields,
submit) does not need a language model - selectors from the target config do
that reliably. The LLM is kept for the messy part: interstitials, odd layouts,
recovery after a challenge.
"""

from __future__ import annotations

from agent.browser import Browser
from agent.config import Target

EMAIL_FALLBACKS = [
    "#email", "input[type=email]", "input[name=email]", "input[name=login]",
    "input[name=username]", "#username", "input[autocomplete=username]",
]
PASSWORD_FALLBACKS = ["#pass", "input[type=password]", "input[name=password]"]
SUBMIT_FALLBACKS = [
    "button[type=submit]", "input[type=submit]", "#submit",
    "button[name=login]", "button:has-text('Log in')", "button:has-text('Sign in')",
]


def _candidates(explicit: str | None, fallbacks: list[str]) -> list[str]:
    out: list[str] = []
    if explicit:
        out.append(explicit)
    for f in fallbacks:
        if f not in out:
            out.append(f)
    return out


async def _fill_first(browser: Browser, selectors: list[str], value: str) -> str | None:
    page = browser.page
    assert page is not None
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            if not await loc.is_visible():
                continue
            await loc.click(timeout=5000)
            await loc.fill("")
            await loc.type(value, delay=50)
            return sel
        except Exception:
            continue
    return None


async def _click_first(browser: Browser, selectors: list[str]) -> str | None:
    page = browser.page
    assert page is not None
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0 or not await loc.is_visible():
                continue
            await loc.click(timeout=8000)
            return sel
        except Exception:
            continue
    return None


async def deterministic_login(browser: Browser, target: Target) -> dict:
    """Returns {'status': ..., 'email_selector': ..., ...}."""
    page = browser.page
    assert page is not None

    url = target.login_url or target.url
    await browser.goto(url)
    await page.wait_for_timeout(1500)

    ok, why = await browser.is_logged_in(target)
    if ok:
        return {"status": "already_in", "evidence": why}

    email = target.credentials.get("email") or target.credentials.get("user") or ""
    password = target.credentials.get("password") or ""
    if not email or not password:
        return {"status": "no_credentials"}

    email_sel = await _fill_first(browser, _candidates(target.login_selectors.get("email"), EMAIL_FALLBACKS), email)
    pass_sel = await _fill_first(browser, _candidates(target.login_selectors.get("password"), PASSWORD_FALLBACKS), password)
    if not email_sel or not pass_sel:
        return {"status": "no_form", "email_selector": email_sel, "password_selector": pass_sel}

    sub_sel = await _click_first(browser, _candidates(target.login_selectors.get("submit"), SUBMIT_FALLBACKS))
    if not sub_sel:
        await page.keyboard.press("Enter")
        sub_sel = "Enter"

    await page.wait_for_timeout(4000)
    return {
        "status": "submitted",
        "email_selector": email_sel,
        "password_selector": pass_sel,
        "submit": sub_sel,
        "url": page.url,
    }
