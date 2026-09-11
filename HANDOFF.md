# HANDOFF — only when the agent genuinely cannot clear something

By default (`handoff_mode: auto`) the agent solves everything itself: 2captcha
clears reCAPTCHA, hCaptcha, Turnstile, Arkose/FunCaptcha and image captchas, and
for anything else it clicks/drags its way through on its own. A handoff is the
exception — you'll see it when a challenge survives the solver, or on a target
deliberately set to `handoff_mode: eager` (e.g. a Facebook checkpoint you do not
want a bot poking at).

The agent then prints:

```
🔔 HANDOFF READY — a human is needed for identity <identity>
   open this on your phone/laptop:
       http://192.168.0.114:6080/vnc.html?autoconnect=1&resize=scale&password=<pw>
```

Exact steps:

1. Open that URL (phone browser or laptop — noVNC, nothing to install).
   The password is already in the link (`password=<pw>`, also in
   `out/<identity>/vnc_password`).
2. Tap **Connect**. You are now looking at the live Chromium window the agent is
   driving — same cookies, same session, nothing has been lost.
3. Do the human-only bit:
   - Arkose / "confirm it's you" puzzle → solve it exactly as you would by hand.
   - SMS or emailed code → type it in.
   - 2FA prompt → enter the code from your authenticator.
   - "Unusual activity / locked" → follow the on-screen recovery.
4. Do not close the tab and do not press Ctrl+W. If the page shows a "not now"
   prompt after the challenge, dismiss it.
5. That's it — **do nothing else**. The agent polls the page; as soon as the
   challenge is gone or the logged-in state appears, it resumes on its own and
   writes `out/<identity>/handoff.DONE`.
6. Escape hatches:
   - stuck agent → `docker exec forge-<identity> touch /out/resume`
   - abort the whole run → `./forge.sh kill <identity>`
   - watch what it does next → `./forge.sh logs <identity> 200`
   - handoff times out after `FORGE_HANDOFF_TIMEOUT` seconds (default 20 min);
     the run then exits with `status: timeout` and the profile is still saved.

After the run: the container is gone, `profiles/<identity>/` holds the warmed
session, and `./forge.sh run <target> <identity>` uses it next time — captchas
from there on go to 2captcha, no human needed.

Re-handoff later (session got flagged) → do the same thing again; nothing to
reconfigure.
