# Findings — verified against live Facebook, 2026-09-11

Written by whoever ran this against the real site (not the fixture). Everything
below is measured output, not inference. Where something is unverified it says so.

## Changes made

1. **Arkose public key is now captured from the network stream.**
   Facebook serves the Arkose widget through its own wrapper —
   `https://www.fbsbx.com/captcha/arkose/iframe/?...&__cci=<blob>` — which carries
   **no `public_key` and no `surl`**, and there are **zero** `arkoselabs`/`funcaptcha`
   iframes on the page. The previous DOM scan therefore produced an empty
   `publickey` and defaulted `surl` to `client-api.arkoselabs.com`.
   The real values only appear in requests made from inside the cross-origin
   frame:
   - public key GUID: `.../fc/gt2/public_key/2BF0FE95-9FB3-45E0-9CB9-3F5B5A8465B1`
   - host: `https://meta-api.arkoselabs.com`
   - blob: the `__cci` query parameter
   `agent/browser.py` now records Arkose request URLs
   (`Browser._watch_arkose`), and `agent/captcha.py` enriches the funcaptcha
   task with `arkose_params_from_urls()` plus `data.blob` and `api_server`.

2. **`targets/facebook.yaml` now treats `/two_step_verification` as a handoff zone.**
   Meta's human check lands on
   `/two_step_verification/authentication/?...flow=pre_authentication`, **not** on
   `/checkpoint`/`/recover`/`/login/device-based`. Without the pattern the agent
   keeps retrying a solver instead of escalating. The Arkose iframe selector
   (`iframe[src*='captcha/arkose']`) was added too.

## Verified evidence

**Warm session on real Facebook — 4/4 runs**

```
forge login facebook fb1  -> success, 0 steps, "already authenticated (url matches facebook.com/home)"
forge run   facebook fb1  -> success, 0 steps, 11s
forge run   facebook fb1  -> success, 0 steps, 11s
forge run   facebook fb1  -> success, 0 steps, 10s
```

Session hand-back is real: `out/<id>/session/storage_state.json` holds 7 cookies
for `.facebook.com` (`c_user`, `xs`, `fr`, `datr`, `sb`, `wd`, `presence`) plus one
localStorage origin, and `profiles/<id>/chrome` persists across container death.

**Unit tests: 13/13** (`tests/test_units.py`). Four new tests assert the Arkose
parser against the real URLs captured off live Facebook.

**PHASE 1 + PHASE 3 run standalone: 16/16 PASS.** PHASE 1 clears Arkose/FunCaptcha
*and* reCAPTCHA through 2captcha with zero human input, hands the session back and
keeps a 185-file profile. PHASE 3 parks the session for a human over noVNC, resumes
on its own and finishes.

## Limits — read these before trusting the suite

- **`./forge.sh test` will fail PHASE 0 whenever the OpenRouter free-model daily
  cap is exhausted** (`429 ... free-models-per-day`, limit 50/day). The LLM agent
  then retries for ~460s and reports `status: llm_error`. Adding ~$10 of OpenRouter
  credit raises the cap to 1000 free requests/day. This was the cause of a
  15-passed/9-failed run — **not** a code defect.
- **The E2E suite is not isolated between phases.** PHASE 0's container (`forge-e2e-llm`)
  can still be alive holding `:6080` when PHASE 1 (`forge-e2e`) starts, giving
  `bind: address already in use` and cascading failures (no noVNC URL, no
  result.json, no solver rounds, no storage_state, empty profile). Fix: run the
  `cleanup` function at each phase start, or wait for the port to be free.
- **Assisted solvers cannot clear Meta's Arkose MatchKey.** 2captcha accepts the
  task and returns valid-looking tokens (373–374 chars, verified with the real GUID
  and blob), and the token is rejected server-side — the page stays on
  `/two_step_verification/authentication/`. MatchKey binds the token to the
  challenge session and client telemetry, so a token minted by a third-party worker
  fails validation. **The reliable path for Facebook is the human handoff**, which is
  why the pattern above was added. Do not expect `solve_captcha` to rescue a cold
  Facebook login.
- **CapSolver has retired Arkose/FunCaptcha.** `FunCaptchaTaskProxyLess` returns
  `ERROR_INVALID_TASK_DATA: unsupported service`; the type name is not even
  recognised (`ArkoseLabsTaskProxyLess` -> `ERROR_TYPE_NOT_SUPPORTED`). Any code
  written against CapSolver's old FunCaptcha surface cannot work.
- **A cold Facebook login has not been run end to end**, deliberately: it risks
  flagging the account whose warm session works, and the solver limitation above
  means the expected outcome is a handoff.
- The project's original "Arkose cleared through 2captcha" verification ran against
  the **local fixture with a mock 2captcha endpoint**, which accepts any token. It is
  not evidence about Facebook.

## Getting a working Facebook session

1. Authenticate once in a real browser window (headed — the challenge does not
   appear for a normal desktop fingerprint the way it does for a cold headless one).
2. Put that Chromium user-data directory at `profiles/<identity>/chrome/`.
3. `forge run facebook <identity>` then reuses it: `0 steps`, no challenge.
