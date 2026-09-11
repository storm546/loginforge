"""Local E2E fixture: a login site with a solver-style challenge and a captcha.

Flow (default gate):
  /            landing
  /login       email+password form      (POST sets session cookie)
  /verify      Arkose/FunCaptcha gate   (agent must clear it via 2captcha funcaptcha)
  /captcha     reCAPTCHA v2 test sitekey (agent clears it via 2captcha)
  /dashboard   success marker #dashboard

Alternate gate (`/login?gate=checkpoint`) exercises the human-handoff fallback:
  /checkpoint  "click anywhere" gate - only a real human interaction clears it
"""

from __future__ import annotations

import os
import secrets
from datetime import timedelta

from flask import Flask, make_response, redirect, request, session

app = Flask(__name__)
app.secret_key = os.environ.get("FIXTURE_SECRET", "loginforge-fixture")
# Real sites issue persistent auth cookies; mimic that so a warmed profile
# survives the container it was created in.
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

EMAIL = os.environ.get("SITE_EMAIL", "fixture@example.com")
PASSWORD = os.environ.get("SITE_PASSWORD", "fixture-pass")
TEST_SITEKEY = "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI"
TEST_PUBLICKEY = "TESTPUBKEY-LOGINFORGE"

PAGE = """<!doctype html><html><head><title>{title}</title>
<style>body{{font-family:system-ui;margin:60px;max-width:720px}}
input,button{{font-size:18px;padding:8px;margin:6px 0;display:block}}
.card{{border:1px solid #ccc;padding:24px;border-radius:8px}}</style></head>
<body><div class="card"><h1>{title}</h1>{body}</div>{extra}</body></html>"""


@app.get("/")
def index():
    if session.get("verified"):
        return redirect("/dashboard")
    return PAGE.format(
        title="Fixture Site",
        body='<p id="welcome">Welcome. Please continue.</p>'
        '<a id="login-link" href="/login">Log in</a>',
        extra="",
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.args.get("gate"):
        session["gate"] = request.args["gate"]
    if session.get("verified"):
        return redirect("/dashboard")
    if request.method == "POST":
        if request.form.get("email") == EMAIL and request.form.get("password") == PASSWORD:
            session["session"] = secrets.token_hex(8)
            session.permanent = True
            return redirect("/" + session.get("gate", "verify"))
        return PAGE.format(
            title="Login",
            body=_login_form() + '<p id="error" style="color:crimson">Wrong email or password</p>',
            extra="",
        )
    return PAGE.format(title="Login", body=_login_form(), extra="")


def _login_form() -> str:
    return (
        '<form id="login-form" method="post" action="/login">'
        '<label for="email">Email</label><input id="email" name="email" type="email" placeholder="Email">'
        '<label for="pass">Password</label><input id="pass" name="password" type="password" placeholder="Password">'
        '<button id="submit" type="submit">Log in</button></form>'
    )


# --------------------------------------------------------------- solver gate
@app.route("/verify", methods=["GET", "POST"])
def verify():
    """Arkose/FunCaptcha style gate: the agent clears it with 2captcha funcaptcha."""
    if not session.get("session"):
        return redirect("/login")
    if session.get("human"):
        return redirect("/captcha")
    if request.method == "POST":
        token = (request.form.get("fc-token") or "").strip()
        if token:
            session["human"] = True
            session.permanent = True
            return redirect("/captcha")
        return PAGE.format(title="Security check", body=_arkose_body() + '<p id="error">No token</p>', extra="")
    return PAGE.format(title="Security check", body=_arkose_body(), extra="")


def _arkose_body() -> str:
    return (
        "<p>Confirm it is really you.</p>"
        f'<iframe src="https://cdn.arkoselabs.com/fc/api/?public_key={TEST_PUBLICKEY}'
        f'&amp;surl=https://client-api.arkoselabs.com" width="1" height="1" style="opacity:0"></iframe>'
        f'<form id="verify-form" method="post" action="/verify">'
        f'<div id="arkose" data-sitekey="{TEST_PUBLICKEY}" data-surl="https://client-api.arkoselabs.com"></div>'
        f'<input id="fc-token" name="fc-token" type="hidden" value="">'
        f'<button id="verify-submit" type="submit">Continue</button></form>'
    )


# --------------------------------------------------------------- human gate
@app.get("/checkpoint")
def checkpoint():
    """Human-only gate used by the handoff-fallback target."""
    if not session.get("session"):
        return redirect("/login")
    if session.get("human"):
        return redirect("/captcha")
    return PAGE.format(
        title="Suspicious activity",
        body='<p>Press and hold to confirm it is you.</p>'
        '<p id="hint">Click anywhere on this page to prove you are human.</p>'
        '<button id="human-btn">I am human</button>',
        extra="""<script>
        function pass() { fetch('/checkpoint/ok', {method:'POST'}).then(() => location.href='/captcha'); }
        document.addEventListener('click', function (e) { if (e.isTrusted) pass(); }, {once: true});
        document.getElementById('human-btn').addEventListener('click', function (e) { e.stopPropagation(); pass(); });
        </script>""",
    )


@app.post("/checkpoint/ok")
def checkpoint_ok():
    session["human"] = True
    session.permanent = True
    return {"ok": True}


# ------------------------------------------------------------------ captcha
@app.route("/captcha", methods=["GET", "POST"])
def captcha():
    if not session.get("human"):
        return redirect("/verify")
    if request.method == "POST":
        token = (request.form.get("g-recaptcha-response") or "").strip()
        if token:
            session["verified"] = True
            session.permanent = True
            return redirect("/dashboard")
        return PAGE.format(title="Security check", body=_captcha_body() + '<p id="error">No captcha token</p>', extra=_captcha_script())
    return PAGE.format(title="Security check", body=_captcha_body(), extra=_captcha_script())


def _captcha_body() -> str:
    return (
        '<p>Please complete the challenge to continue.</p>'
        f'<form id="captcha-form" method="post" action="/captcha">'
        f'<div class="g-recaptcha" data-sitekey="{TEST_SITEKEY}"></div>'
        f'<textarea id="g-recaptcha-response" name="g-recaptcha-response" style="display:none"></textarea>'
        f'<button id="captcha-submit" type="submit">Continue</button></form>'
    )


def _captcha_script() -> str:
    # Test fixture only: this mimics a third-party site loading the real reCAPTCHA
    # widget. SRI is not applicable here (simulated vendor script, no real asset).
    return '<script src="https://www.google.com/recaptcha/api.js" async defer></script>'


@app.get("/dashboard")
def dashboard():
    if not session.get("verified"):
        return redirect("/login")
    return PAGE.format(
        title="Dashboard",
        body=f'<div id="dashboard">You are logged in as {EMAIL}</div>'
        '<a id="logout" href="/logout">Log out</a>',
        extra="",
    )


@app.get("/logout")
def logout():
    session.clear()
    return make_response(redirect("/"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("FIXTURE_PORT", "5000")), threaded=True)
