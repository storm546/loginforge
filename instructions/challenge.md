# Challenge run — warmed session

A profile that a previous run authenticated exists at `/profile/chrome`, and its
cookies are restored from `/out/session/storage_state.json` at boot. Your job is
to act, not to re-authenticate.

1. `page_info`. If the session is still valid (account name / feed / success
   selector visible), call `login_succeeded` with that evidence.
2. If the session expired, run the first-time login procedure: fill the form with
   the configured credentials, then clear the interstitials.
3. Any captcha → `solve_captcha` (2captcha clears reCAPTCHA v2/v3, hCaptcha,
   Cloudflare Turnstile, Arkose/FunCaptcha and image captchas).
4. If a challenge survives the solver, take it over yourself: `screenshot`, click
   or drag the widget, `wait`, retry differently. Escalate to `request_handoff`
   only as a last resort.
5. Never reset the profile, never clear cookies, never open a private window.
6. Finish with `login_succeeded` or `give_up`.
