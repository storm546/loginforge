# First-time login — exact procedure

You do the work. The human is a fallback, not a step.

1. `goto <start url>` (the start url is in TARGET CONFIG).
2. `page_info` — confirm you are on the login page. If the page already shows an
   authenticated state (account name, logout control, feed), call
   `login_succeeded`.
3. Find the login form. Preferred selectors, in this order:
   - the selectors under TARGET CONFIG (`selector[email]`, `selector[password]`,
     `selector[submit]`);
   - otherwise what page_info reports (`input[type=email]`, `input[name=email]`,
     `input[name=login]`, `#email`, `#username` for the account field, and the
     visible submit control).
   If no form is visible, click the visible "Log in" / "Sign in" / "Вход" link,
   then `page_info` again.
4. `type_text` the email/username into the identified field.
5. `type_text` the password into the password field; set `submit=true` when the
   password field is the last field and Enter submits the form, otherwise click
   the submit control.
6. `wait` 3, then `page_info`.
7. Clear the interstitials yourself, in this order of preference:
   1. captcha / "confirm it is you" → `solve_captcha` **once**. If it answers
      `UNSOLVABLE_BY_SOLVER`, or the harness says the challenge persisted, then
      take it over: `screenshot`, click the widget / drag the slider, `wait`, and
      retry with a *different* action.
   2. cookie/consent banner → click "Accept all" / "Accept" / "Allow all".
   3. "Save your login info?" / "Remember this browser?" → "Not now" / "Later".
   4. notifications prompt → "Not now" / "Block".
   5. "unusual activity" / verification page with no widget → click the primary
      button, follow the on-screen path, `wait`, re-check.
8. `request_handoff` is the **last resort**: only when you have genuinely tried
   7.1–7.5, the page cannot be advanced by any control you can see, or it demands
   something only a human holds (SMS code, an emailed code, a 2FA device you do
   not have). Then re-check with `page_info` and continue.
9. Confirm authentication: the URL moves off the login path and/or TARGET CONFIG's
   success selector is visible. Then `login_succeeded` with that evidence.
10. If the server rejects the credentials (visible error text), `give_up` once —
   never retry the same wrong credentials.

Hard rules:
- One tool call per turn. `page_info` after every click or navigation.
- Never repeat an identical failing action; change selector, wait, or approach.
- Never type credentials into a field you have not verified with page_info.
