"""Captcha detection + 2captcha solving.

Strategy:
  * Detect what the page is asking for.
  * Everything the solver supports goes to 2captcha and the token is injected
    into the page: reCAPTCHA v2/v3, hCaptcha, Cloudflare Turnstile, Arkose Labs
    / FunCaptcha (funcaptcha method, needs public key + surl) and image captchas.
  * Vendors no solver supports (DataDome, PerimeterX) raise UnsolvableCaptcha so
    the caller can escalate to the agent's own interaction or, as a last resort,
    to a human.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import requests
from playwright.async_api import Page

DEFAULT_BASE = os.environ.get("TWOCAPTCHA_BASE_URL", "https://2captcha.com").rstrip("/")
POLL_INTERVAL = int(os.environ.get("TWOCAPTCHA_POLL_INTERVAL", "5"))
SUBMIT_TIMEOUT = int(os.environ.get("TWOCAPTCHA_TIMEOUT", "180"))

UNSOLVABLE_MARKERS = {
    "datadome": ["datadome", "dd_cookie"],
    "perimeterx": ["perimeterx", "px-captcha", "_pxhd"],
    "hcaptcha_enterprise_audio": ["hcaptcha-enterprise"],
}

# Arkose Labs / FunCaptcha is solvable through 2captcha's funcaptcha method,
# so these markers route to the solver instead of to a human.
FUNCAPTCHA_MARKERS = ["arkoselabs", "funcaptcha", "fc/api", "matchkey", "arkose"]


class UnsolvableCaptcha(RuntimeError):
    """Challenge that automated solvers cannot clear - needs a human."""


class TwoCaptchaError(RuntimeError):
    pass


@dataclass
class Captcha:
    kind: str          # recaptcha_v2 | recaptcha_v3 | hcaptcha | turnstile | funcaptcha | image | unsolvable
    sitekey: str = ""
    pageurl: str = ""
    reason: str = ""
    image_b64: str = ""
    action: str = ""
    surl: str = ""
    user_agent: str = ""
    blob: str = ""     # Arkose __cci blob (Facebook's fbsbx wrapper only)


_DETECT_JS = r"""
(() => {
  const sitekeyOf = (el) => el ? (el.getAttribute('data-sitekey') || '') : '';
  const q = (s) => document.querySelector(s);
  const html = document.documentElement.innerHTML;

  // --- Arkose Labs / FunCaptcha: solvable, needs public key + surl -----------
  const ark = q('iframe[src*="arkoselabs"], iframe[src*="funcaptcha"], #arkose, div[data-sitekey][id*="arkose"], div[data-sitekey][class*="arkose"]');
  if (ark || /arkoselabs|funcaptcha|matchkey/i.test(html)) {
    let key = ark ? sitekeyOf(ark) : '';
    let surl = ark ? (ark.getAttribute('data-surl') || '') : '';
    if (ark && ark.tagName.toLowerCase() === 'iframe') {
      try {
        const u = new URL(ark.getAttribute('src'), location.href);
        key = key || u.searchParams.get('public_key') || u.searchParams.get('pk') || '';
        surl = surl || u.searchParams.get('surl') || '';
      } catch (e) {}
    }
    return { kind: 'funcaptcha', sitekey: key, surl: surl || 'https://client-api.arkoselabs.com' };
  }

  // --- solver-proof vendors --------------------------------------------------
  const unsolvable = [
    ['datadome', ['datadome', 'dd_cookie']],
    ['perimeterx', ['perimeterx', 'px-captcha']],
  ];
  for (const [kind, needles] of unsolvable) {
    for (const n of needles) {
      if (html.toLowerCase().includes(n)) {
        return { kind: 'unsolvable', reason: kind + ' (' + n + ')' };
      }
    }
  }

  // --- cloudflare turnstile --------------------------------------------------
  const ts = q('.cf-turnstile[data-sitekey], [data-sitekey][data-callback], div[class*="turnstile"]');
  if (q('iframe[src*="challenges.cloudflare.com"]') || (ts && sitekeyOf(ts))) {
    return { kind: 'turnstile', sitekey: sitekeyOf(ts) };
  }

  // --- hcaptcha --------------------------------------------------------------
  const hc = q('div[data-sitekey][class*="h-captcha"], div.h-captcha[data-sitekey], div[data-hcaptcha-sitekey]');
  if (q('iframe[src*="hcaptcha.com"]') || hc) {
    return { kind: 'hcaptcha', sitekey: sitekeyOf(hc) || (hc ? hc.getAttribute('data-hcaptcha-sitekey') : '') };
  }

  // --- recaptcha -------------------------------------------------------------
  const rc = q('div.g-recaptcha[data-sitekey], div[data-sitekey][class*="g-recaptcha"], iframe[src*="recaptcha/api2/anchor"], iframe[src*="recaptcha/enterprise"]');
  if (rc) {
    let key = sitekeyOf(rc);
    if (!key) {
      const f = q('iframe[src*="recaptcha"]');
      if (f) { const m = f.getAttribute('src').match(/[?&]k=([^&]+)/); if (m) key = decodeURIComponent(m[1]); }
    }
    const isV3 = !!(window.grecaptcha && window.grecaptcha.execute && !q('iframe[src*="recaptcha/api2/anchor"]') && !q('.g-recaptcha'));
    let action = '';
    if (window.___grecaptcha_cfg) {
      try {
        const clients = window.___grecaptcha_cfg.clients || {};
        for (const k of Object.keys(clients)) {
          const walk = (o, d) => {
            if (d > 4 || !o || typeof o !== 'object') return;
            for (const kk of Object.keys(o)) {
              if (kk === 'action' && typeof o[kk] === 'string') action = o[kk];
              walk(o[kk], d + 1);
            }
          };
          walk(clients[k], 0);
        }
      } catch (e) {}
    }
    return { kind: isV3 ? 'recaptcha_v3' : 'recaptcha_v2', sitekey: key, action: action };
  }

  // --- image captcha ---------------------------------------------------------
  const img = q('img[src*="captcha" i], img[id*="captcha" i], img[class*="captcha" i]');
  if (img && img.src) {
    return { kind: 'image', image_b64: img.src };
  }

  return null;
})()
"""


def detect_sync_html(html: str) -> str | None:
    """Cheap offline marker scan - used by the unit tests."""
    low = html.lower()
    for kind, needles in UNSOLVABLE_MARKERS.items():
        if any(n in low for n in needles):
            return kind
    if any(n in low for n in FUNCAPTCHA_MARKERS):
        return "funcaptcha"
    return None


async def detect(page: Page) -> Captcha | None:
    data = await page.evaluate(_DETECT_JS)
    if not data:
        return None
    kind = data.get("kind", "")
    if kind == "unsolvable":
        return Captcha(kind="unsolvable", reason=data.get("reason", "unknown"), pageurl=page.url)
    cap = Captcha(
        kind=kind,
        sitekey=data.get("sitekey", ""),
        pageurl=page.url,
        image_b64=data.get("image_b64", ""),
        action=data.get("action", ""),
        surl=data.get("surl", ""),
    )
    if kind == "funcaptcha":
        # Facebook's Arkose iframe is served from fbsbx.com and carries NO
        # public_key / surl, so the DOM scan above yields an empty key. The real
        # GUID only appears in the Arkose requests made from inside the frame.
        guid, host, blob = arkose_params_from_urls(
            getattr(page, "_arkose_urls", None) or []
        )
        if guid:
            cap.sitekey = guid
        if host:
            cap.surl = host
        if blob:
            cap.blob = blob
    return cap


def arkose_params_from_urls(urls: list[str]) -> tuple[str, str, str]:
    """Extract (public_key_guid, surl, blob) from observed Arkose request URLs.

    Facebook Arkose traffic looks like:
      https://meta-api.arkoselabs.com/fc/gt2/public_key/2BF0FE95-9FB3-45E0-9CB9-3F5B5A8465B1
      https://meta-api.arkoselabs.com/v2/4.5.0/enforcement...
      https://www.fbsbx.com/captcha/arkose/iframe/?...&__cci=<blob>
    """
    guid = host = blob = ""
    for u in urls or []:
        if not guid and "/fc/gt2/public_key/" in u:
            guid = u.rsplit("/", 1)[-1].split("?")[0]
        if not guid:
            m = re.search(r"arkoselabs\.com/v2/([0-9A-Fa-f-]{36})/", u)
            if m:
                guid = m.group(1)
        if not host and "arkoselabs" in u:
            try:
                netloc = urlparse(u).netloc
                if netloc:
                    host = f"https://{netloc}"
            except Exception:
                pass
        if not blob and "__cci=" in u:
            try:
                blob = parse_qs(urlparse(u).query).get("__cci", [""])[0]
            except Exception:
                pass
        if guid and host and blob:
            break
    return guid, host, blob


# ------------------------------------------------------------------ 2captcha
class TwoCaptcha:
    def __init__(self, api_key: str, base_url: str = DEFAULT_BASE) -> None:
        if not api_key:
            raise TwoCaptchaError("TWOCAPTCHA_API_KEY is not set")
        self.key = api_key
        self.base = base_url.rstrip("/")

    def balance(self) -> float:
        r = requests.get(
            f"{self.base}/res.php",
            params={"key": self.key, "action": "getbalance", "json": 1},
            timeout=30,
        )
        data = r.json()
        if data.get("status") != 1:
            raise TwoCaptchaError(f"2captcha error: {data.get('request')}")
        return float(data["request"])

    def _submit(self, payload: dict) -> str:
        body = {"key": self.key, "json": 1, **payload}
        r = requests.post(f"{self.base}/in.php", data=body, timeout=45)
        data = r.json()
        if str(data.get("status")) != "1":
            raise TwoCaptchaError(f"2captcha submit failed: {data.get('request')}")
        return str(data["request"])

    def _poll(self, request_id: str, timeout: int = SUBMIT_TIMEOUT) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL)
            r = requests.get(
                f"{self.base}/res.php",
                params={"key": self.key, "action": "get", "id": request_id, "json": 1},
                timeout=30,
            )
            data = r.json()
            status = str(data.get("status"))
            request = str(data.get("request"))
            if status == "1":
                return request
            if request != "CAPCHA_NOT_READY":
                raise TwoCaptchaError(f"2captcha solve failed: {request}")
        raise TwoCaptchaError("2captcha timeout")

    @staticmethod
    def _funcaptcha_payload(captcha: Captcha) -> dict:
        """Build the 2captcha `funcaptcha` task body.

        Sites that wrap Arkose in their own iframe (Facebook serves it from
        fbsbx.com) do not expose the public key or subdomain in the DOM, so the
        caller passes the values captured from the network stream. Without the
        blob and the real `api_server`, 2captcha rejects the task outright.
        """
        payload = {
            "method": "funcaptcha",
            "publickey": captcha.sitekey,
            "surl": captcha.surl or "https://client-api.arkoselabs.com",
            "pageurl": captcha.pageurl,
        }
        if captcha.user_agent:
            payload["useragent"] = captcha.user_agent
        if captcha.blob:
            payload["data"] = json.dumps({"blob": captcha.blob})
        if captcha.surl:
            host = urlparse(captcha.surl).netloc
            if host:
                payload["api_server"] = host
        return payload

    def solve(self, captcha: Captcha) -> str:
        """Return a solver token for a captcha description."""
        if captcha.kind == "recaptcha_v2":
            rid = self._submit({
                "method": "userrecaptcha",
                "googlekey": captcha.sitekey,
                "pageurl": captcha.pageurl,
            })
        elif captcha.kind == "recaptcha_v3":
            rid = self._submit({
                "method": "userrecaptcha",
                "googlekey": captcha.sitekey,
                "pageurl": captcha.pageurl,
                "version": "v3",
                "action": captcha.action or "verify",
                "min_score": "0.5",
            })
        elif captcha.kind == "hcaptcha":
            rid = self._submit({
                "method": "hcaptcha",
                "sitekey": captcha.sitekey,
                "pageurl": captcha.pageurl,
            })
        elif captcha.kind == "turnstile":
            rid = self._submit({
                "method": "turnstile",
                "sitekey": captcha.sitekey,
                "pageurl": captcha.pageurl,
            })
        elif captcha.kind == "funcaptcha":
            rid = self._submit(self._funcaptcha_payload(captcha))
        elif captcha.kind == "image":
            b64 = captcha.image_b64
            if b64.startswith("http"):
                b64 = base64.b64encode(requests.get(b64, timeout=30).content).decode()
            elif "," in b64:
                b64 = b64.split(",", 1)[1]
            rid = self._submit({"method": "base64", "body": b64})
        else:
            raise UnsolvableCaptcha(f"cannot solve kind={captcha.kind} ({captcha.reason})")
        return self._poll(rid)


# ------------------------------------------------------------------ injection
_APPLY_JS = r"""
(token) => {
  const set = (el) => {
    el.value = token;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  let injected = false;

  // reCAPTCHA / hCaptcha / Turnstile / FunCaptcha response fields
  document.querySelectorAll(
    'textarea#g-recaptcha-response, textarea[name="g-recaptcha-response"], textarea#h-captcha-response, textarea[name="h-captcha-response"], textarea[name="cf-turnstile-response"], input[name="cf-turnstile-response"], input#fc-token, input[name="fc-token"], textarea#fc-token, textarea[name="fc-token"], #FunCaptcha-Token, input[name="verification-token"]'
  ).forEach((el) => { set(el); injected = true; });

  // Hidden textarea may not exist yet - create it so server-side checks pass.
  if (!document.querySelector('textarea[name="g-recaptcha-response"]')) {
    const ta = document.createElement('textarea');
    ta.name = 'g-recaptcha-response'; ta.id = 'g-recaptcha-response';
    ta.style.display = 'none'; document.body.appendChild(ta); set(ta); injected = true;
  }

  // Fire vendor callbacks if present.
  try {
    if (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {
      const clients = window.___grecaptcha_cfg.clients;
      for (const k of Object.keys(clients)) {
        const walk = (o, d) => {
          if (d > 5 || !o || typeof o !== 'object') return;
          for (const kk of Object.keys(o)) {
            const v = o[kk];
            if (typeof v === 'function' && (kk === 'callback' || kk === 'promise-callback')) {
              try { v(token); } catch (e) {}
            }
            walk(v, d + 1);
          }
        };
        walk(clients[k], 0);
      }
    }
    if (window.turnstile && window.turnstile.getResponse === undefined) { /* noop */ }
  } catch (e) {}
  return injected;
}
"""


async def apply_token(page: Page, token: str) -> bool:
    return bool(await page.evaluate(_APPLY_JS, token))


async def solve_with_2captcha(page: Page, api_key: str, base_url: str = DEFAULT_BASE) -> dict:
    """Detect, solve and inject. Raises UnsolvableCaptcha when the vendor is not
    supported by the solver (DataDome, PerimeterX)."""
    spec = await detect(page)
    if spec is None:
        return {"status": "none"}
    if spec.kind == "unsolvable":
        raise UnsolvableCaptcha(spec.reason or "unsolvable challenge")

    try:
        spec.user_agent = await page.evaluate("navigator.userAgent")
    except Exception:
        pass

    solver = TwoCaptcha(api_key, base_url)
    token = solver.solve(spec)
    injected = await apply_token(page, token)

    # Best-effort: click the vendor checkbox / submit button.
    for sel in ('button[type="submit"]', 'input[type="submit"]', '#submit',
                '#verify-submit', '.cf-turnstile'):
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click(timeout=3000)
                break
        except Exception:
            continue
    return {"status": "solved", "kind": spec.kind, "sitekey": spec.sitekey,
            "surl": spec.surl, "injected": injected, "token_prefix": token[:12]}
