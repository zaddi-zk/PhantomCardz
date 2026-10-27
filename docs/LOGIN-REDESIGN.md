# PhantomCardz — Login Page Redesign (Dark Fantasy)

Scope: **`/auth` only** (the SPA also renders it at `/` for guests). The shop, account,
admin, seller and post-login experience are the existing app and untouched.

## What changed

The frontend is a pre-built Vite bundle (`frontend/assets/index-*.js`), so the redesign is
a **skin layer** over the existing markup rather than a rebuild:

```
frontend/index.html
  ├─ auth-layout.css          (existing overrides)
  ├─ auth-phantom.css         (new — theme, background, card, form, reaper)
  └─ auth-phantom.js          (new — smoke, cards, lightning, reaper, ripple, welcome)
```

All selectors are scoped with `body:has(.spider-entry-page)`, so no other route changes.
The form, captcha and `POST /api/login` call are **untouched**; the skin restyles them and
listens to the bundle's existing `phantomcardz:signup-complete` window event.

### Visuals

- Deep-purple palette (`#6B21A8` / `#9333EA` / `#A855F7`) on `#0a0a0a`, white text.
- Blackletter wordmark (`UnifrakturMaguntia` + `Cinzel Decorative`) replacing the old JPEG logo.
- Canvas smoke (rAF, additive gradients), 7 holographic floating cards, subtle lightning.
- Glassmorphism card, purple focus states, gradient CTA with sheen + click ripple.
- Reaper: inline SVG (no external assets) — slides in from the left, swings the scythe,
  glowing eyes, energy trail, exits right after a successful login.
- "Welcome back" confirmation below the button **and** a body-level pill that survives the
  SPA's instant route swap to `/` (the Telegram modal and its backdrop sit at
  `z-index: 2147483000`, so the pill sits at `2147483001`).
- Responsive down to 390 px, `prefers-reduced-motion` honoured, ARIA labels and
  keyboard focus states preserved.

## TODO

- [x] Audit the existing `/auth` DOM, CSS hooks and `/api/login`
- [x] Build `auth-phantom.css`: tokens, layered background, glass card, inputs, captcha,
      CTA (gradient/sheen/ripple/disabled), welcome states, footer, responsive,
      reduced motion
- [x] Build `auth-phantom.js`: canvas smoke, holographic cards, lightning scheduler,
      reaper entrance/swing/trail/exit, ripple, parallax, welcome, MutationObserver
- [x] Replace the raster logo with an accessible blackletter wordmark
- [x] Wire fonts + assets into `index.html` with a cache-busting `?v=` query
- [x] Verify desktop 1280×900 and tablet 834×1112
- [x] Fix mobile 390×844: true device-emulated viewport; scope the captcha image rule to
      `.spider-captcha > svg` so the refresh icon is no longer forced to 104×36 px
- [x] Register mode (no reaper exit / no welcome) verified
- [x] Verify the real login end-to-end: captcha accepted, `/api/login` 200, session cookie,
      route → `/`, reaper exit, "Welcome back" pill visible above the Telegram modal
- [x] Verify `prefers-reduced-motion` (all animations disabled) and focus rings
- [x] Remove the temporary probe page and the test account used for verification

## Verification notes

- Screenshots and measurements were taken through ChromeDriver device emulation
  (`deviceMetrics 390×844`), not `--window-size`; headless Chromium enforces a ~500 px
  minimum window width, which made early mobile captures misleading.
- The login test typed the rendered captcha text, submitted, and asserted the redirect,
  the reaper's `pcz-reaper-out` state and the confirmation pill. The test user and its
  tokens were deleted afterwards.

## Kept / not changed

- Bot verification (client-side captcha): untouched, restyled only.
- `/api/login`, cookie handling and rate limiting: untouched.
- Existing routes, top nav, product grid and post-login home: untouched.

## Known notes (pre-existing, out of scope)

- `rememberMe` is sent by the SPA but ignored server-side (the cookie is always 7 days).
- The captcha is compared in the client bundle, so it is cosmetic rather than a real bot
  defence. Kept as-is on request.
- After login the SPA unmounts the auth route immediately; the in-form "Welcome back"
  therefore relies on the body-level pill for the transition.
