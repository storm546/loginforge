# loginforge

Containerised first-login + captcha automation.

One container = one virtual desktop (Xvfb + x11vnc + noVNC) + one real Chromium
with a **persistent profile** + an agent loop driven by **OpenRouter** that drives
the browser through tools. When the page demands something only a human can do
(Arkose/FunCaptcha device check, SMS/2FA, an "unusual activity" block) the agent
parks the session and pings you with a noVNC URL. You clear it in the same
browser window, the agent resumes by itself, exports the session, exits, and the
container is removed. Every later run reuses the saved profile and clears
captchas automatically through **2captcha**.

```
forge login <target> <identity>     # first run: human gate, session saved
forge run   <target> <identity>     # later runs: warmed session + 2captcha
```

## Layout

```
loginforge/
├── Dockerfile              agent image: Xvfb, x11vnc, noVNC, Chromium, Playwright
├── entrypoint.sh           boots display/VNC, then hands over to the agent
├── forge.sh                host CLI (build/login/run/ps/url/logs/kill/shell/test)
├── agent/
│   ├── main.py             run orchestration + session export
│   ├── loop.py             LLM tool-calling loop, JSONL trace to /out/run.jsonl
│   ├── tools.py            browser tools + handoff/login_succeeded/give_up
│   ├── captcha.py          captcha detection + 2captcha client + token injection
│   ├── handoff.py          park session -> noVNC -> auto-resume
│   ├── browser.py          persistent Chromium, page digest, login/handoff checks
│   ├── llm.py              OpenRouter client, free model auto-pick
│   └── config.py           target yaml + ${ENV} credential expansion
├── instructions/           exact per-mode procedures given to the model
├── targets/                per-site target profiles (selectors, success/handoff rules)
├── testsite/               local fixture (login -> human gate -> captcha -> dashboard)
├── tests/e2e.sh            full E2E suite (real browser, real LLM, real containers)
├── profiles/<identity>/    THE SESSION — host-side, survives container death
└── out/<identity>/         run.jsonl, result.json, handoff.json, session/, screenshots
```

## First run

```bash
cd /root/loginforge
cp .env.example .env && chmod 600 .env      # already done on this box
$EDITOR .env                                # OPENROUTER_API_KEY, TWOCAPTCHA_API_KEY
cp targets/example.yaml targets/mysite.yaml # then fill in url/selectors/rules
./forge.sh build
./forge.sh login mysite myuser
```

The CLI prints a noVNC URL. When the handoff banner appears, open it
(`http://192.168.0.114:6080/vnc.html?...`), solve the challenge, done — the agent
notices and continues. See `HANDOFF.md`.

## Adding a captcha key

`TWOCAPTCHA_API_KEY` in `.env`. Without it, `solve_captcha` returns
`solver error` and the agent escalates to a handoff instead of failing. Balance
check: `curl "https://2captcha.com/res.php?key=$KEY&action=getbalance&json=1"`.

## Solvable vs handoff

The agent clears everything it can by itself. Escalation ladder: **solver →
the agent's own interaction (click/drag/vision) → human handoff**.

| Challenge | Path |
|---|---|
| reCAPTCHA v2/v3, hCaptcha, Cloudflare Turnstile, image captcha | 2captcha → token injected into the page |
| Arkose Labs / FunCaptcha (MatchKey) | 2captcha `funcaptcha` (public key + surl from the widget) → token injected, form submitted |
| DataDome, PerimeterX (no solver supports them) | agent interacts with it itself; handoff only if that fails |
| SMS/email code, 2FA prompt, device check, "unusual activity" | agent clicks through what it can; handoff as the fallback |

`handoff_mode` in the target yaml decides how eager the human path is:

* `auto` (default) — solve/interact first, handoff only when the agent is stuck.
* `never` — no human at all: the `request_handoff` tool is removed from the model.
* `eager` — a matching `handoff_url_patterns`/`handoff_selectors` rule parks the
  live session for a human immediately (the old behaviour, kept for targets that
  must never be fought by a bot).

## Verified (this box, 2026-09-11)

```
./forge.sh test                     -> 28 passed, 0 failed
podman run ... pytest tests/        -> 9 passed
```

Real containers, real Chromium, real LLM, real 2captcha wire format (local mock
endpoint, same API shape):

* PHASE 0 — free OpenRouter model drives the whole login with **zero human
  input**: 11 steps, Arkose/FunCaptcha + reCAPTCHA both cleared through 2captcha,
  no handoff.
* PHASE 1 — deterministic pass, no human: guard solves `funcaptcha` and
  `recaptcha_v2`, session exported, container removed, 184-file profile kept.
* PHASE 2 — new container on the warmed session: `already authenticated`, 0 steps.
* PHASE 3 — fallback proof: a target with `handoff_mode: eager` parks the session,
  a human click over noVNC resumes it, run finishes, container removed.

## Notes / constraints

* Docker on this LXC needs `--security-opt apparmor=unconfined` (the forge CLI
  adds it); elsewhere plain `docker` works.
* Containers run with `--rm`: the container dies on its own after handing the
  session back. `forge kill <id>` is only for aborting early.
* The profile is the crown jewel — `profiles/<identity>/` (Chromium user data)
  plus `out/<identity>/session/storage_state.json`.
* E2E: `./forge.sh test` (spins the fixture, simulates the human with xdotool
  over the live X display, drives a mock 2captcha endpoint). Unit tests:
  `podman run --rm --security-opt apparmor=unconfined -v $PWD:/w -w /w
  --entrypoint python loginforge -m pytest tests/test_units.py -q`.
* OpenRouter free models share a 20 requests/minute cap; the client paces itself
  (`FORGE_LLM_MIN_INTERVAL`, default 3.2s) and backs off on 429. Adding a few
  credits to the OpenRouter key lifts the free-model daily cap.
