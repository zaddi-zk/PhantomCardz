/* ==========================================================================
   PhantomCardz — dark-fantasy login skin (behaviour)
   Layers atmospheric effects over the existing SPA login form.
   The form, captcha and /api/login call are left untouched.
   ========================================================================== */
(function () {
  'use strict';

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  var state = {
    bg: null,
    cards: null,
    lightning: null,
    reaper: null,
    smoke: null,
    raf: 0,
    lightningTimer: 0,
    observer: null,
    parallaxQueued: false,
    pointer: { x: 0, y: 0 },
    started: false
  };

  var CARD_SLOTS = [
    { top: '9%', left: '4%', rot: '-14deg', depth: 1.6, dur: '6.4s' },
    { top: '58%', left: '7%', rot: '9deg', depth: 2.2, dur: '7.6s' },
    { top: '72%', left: '19%', rot: '-6deg', depth: 1.2, dur: '5.8s' },
    { top: '13%', right: '5%', rot: '12deg', depth: 1.9, dur: '7.1s' },
    { top: '63%', right: '7%', rot: '-9deg', depth: 2.4, dur: '6.7s' },
    { top: '36%', left: '2%', rot: '17deg', depth: 0.9, dur: '8.2s' },
    { top: '31%', right: '2%', rot: '-16deg', depth: 1.0, dur: '7.9s' }
  ];

  var GLYPHS = ['PHANTOM', 'VAULT', 'RARE', 'HOLO', 'GHOST', 'ELITE', 'MYTHIC'];

  var REAPER_SVG = [
    '<svg viewBox="0 0 420 660" xmlns="http://www.w3.org/2000/svg" aria-hidden="true" focusable="false">',
    '<defs>',
    '<filter id="pczGlow" x="-60%" y="-60%" width="220%" height="220%">',
    '<feGaussianBlur stdDeviation="4" result="b"/>',
    '<feMerge><feMergeNode in="b"/><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>',
    '</filter>',
    '<filter id="pczSoft" x="-50%" y="-50%" width="200%" height="200%">',
    '<feGaussianBlur stdDeviation="18"/>',
    '</filter>',
    '<linearGradient id="pczBlade" x1="0%" y1="100%" x2="100%" y2="0%">',
    '<stop offset="0%" stop-color="#6b21a8"/><stop offset="50%" stop-color="#a855f7"/><stop offset="100%" stop-color="#f3e8ff"/>',
    '</linearGradient>',
    '<linearGradient id="pczTrail" x1="100%" y1="0%" x2="0%" y2="100%">',
    '<stop offset="0%" stop-color="#e9d5ff" stop-opacity="0.9"/><stop offset="45%" stop-color="#a855f7" stop-opacity="0.45"/><stop offset="100%" stop-color="#7c3aed" stop-opacity="0"/>',
    '</linearGradient>',
    '<linearGradient id="pczCloak" x1="10%" y1="0%" x2="90%" y2="100%">',
    '<stop offset="0%" stop-color="#2a2038"/><stop offset="26%" stop-color="#191224"/><stop offset="62%" stop-color="#0d0913"/><stop offset="100%" stop-color="#050308"/>',
    '</linearGradient>',
    '<linearGradient id="pczRim" x1="0%" y1="0%" x2="100%" y2="0%">',
    '<stop offset="0%" stop-color="#c084fc" stop-opacity="0.7"/><stop offset="100%" stop-color="#a855f7" stop-opacity="0"/>',
    '</linearGradient>',
    '<radialGradient id="pczHalo"><stop offset="0%" stop-color="#a855f7" stop-opacity="0.3"/><stop offset="100%" stop-color="#a855f7" stop-opacity="0"/></radialGradient>',
    '</defs>',

    '<ellipse class="pcz-reaper-halo" cx="208" cy="380" rx="185" ry="250" fill="url(#pczHalo)" filter="url(#pczSoft)"/>',

    /* ---- scythe ---- */
    '<g class="pcz-scythe">',
    '<path d="M306 150 L306 636" stroke="#1d1626" stroke-width="11" stroke-linecap="round"/>',
    '<path d="M306 150 L306 636" stroke="#433454" stroke-width="3.5" stroke-linecap="round" opacity="0.75"/>',
    '<path d="M306 152 Q372 120 398 56 Q403 39 392 30 Q384 46 370 62 Q334 116 300 146" fill="none" stroke="url(#pczBlade)" stroke-width="9" stroke-linecap="round" filter="url(#pczGlow)"/>',
    '<path d="M306 152 Q372 120 398 56 Q403 39 392 30" fill="none" stroke="#faf5ff" stroke-width="2" stroke-linecap="round" opacity="0.8"/>',
    '<circle cx="306" cy="150" r="7" fill="#2a1f38" stroke="#a855f7" stroke-width="2" opacity="0.9"/>',
    '</g>',

    /* ---- energy trail from the blade ---- */
    '<path class="pcz-trail" d="M392 32 Q322 132 268 196 Q206 268 152 300 Q100 330 46 318" fill="none" stroke="url(#pczTrail)" stroke-width="5" stroke-linecap="round" filter="url(#pczGlow)"/>',
    '<path class="pcz-trail" d="M382 46 Q310 148 250 212 Q192 272 134 300" fill="none" stroke="#c084fc" stroke-width="1.8" stroke-linecap="round" opacity="0.6"/>',

    /* ---- cloak: one continuous silhouette from hood to hem ---- */
    '<path d="M210 118 C250 118 276 150 282 196 C288 232 280 262 272 286 C320 300 344 356 350 420 C358 490 360 566 352 622 L68 622 C60 566 62 490 70 420 C76 356 100 300 148 286 C140 262 132 232 138 196 C144 150 170 118 210 118 Z" fill="url(#pczCloak)"/>',

    /* rim light down the left edge */
    '<path d="M70 420 C62 490 60 566 68 622" fill="none" stroke="url(#pczRim)" stroke-width="2.6" opacity="0.9"/>',
    '<path d="M138 196 C132 232 140 262 148 286 C100 300 76 356 70 420" fill="none" stroke="url(#pczRim)" stroke-width="1.8" opacity="0.55"/>',

    /* ---- sleeves ---- */
    '<path d="M150 288 C120 306 104 344 100 386 C98 412 112 424 128 416 C146 406 158 372 164 342 Z" fill="#150f1f"/>',
    '<path d="M150 288 C120 306 104 344 100 386" fill="none" stroke="#3a2c4d" stroke-width="1.6" opacity="0.7"/>',
    '<path d="M270 288 C300 306 318 344 322 386 C324 412 310 424 294 416 C276 406 264 372 258 342 Z" fill="#150f1f"/>',
    '<path d="M270 288 C300 306 318 344 322 386" fill="none" stroke="#3a2c4d" stroke-width="1.6" opacity="0.7"/>',

    /* ---- robe folds ---- */
    '<path d="M180 340 C172 420 168 520 170 620" fill="none" stroke="#33264a" stroke-width="6" opacity="0.6"/>',
    '<path d="M226 336 C240 418 248 522 252 620" fill="none" stroke="#33264a" stroke-width="5" opacity="0.5"/>',
    '<path d="M132 356 C118 436 110 536 110 618" fill="none" stroke="#2a2038" stroke-width="4.5" opacity="0.55"/>',
    '<path d="M292 358 C306 438 312 536 312 618" fill="none" stroke="#2a2038" stroke-width="4.5" opacity="0.55"/>',

    /* ---- tattered hem ---- */
    '<path d="M68 622 L82 588 L96 620 L110 582 L124 618 L138 578 L152 616 L168 576 L182 614 L198 574 L212 612 L228 578 L242 616 L258 580 L272 618 L286 586 L302 620 L318 590 L352 622 Z" fill="#08050d"/>',

    /* ---- scythe (vertical staff on the left, blade sweeping up-left) ---- */
    '<g class="pcz-scythe">',
    '<path d="M300 648 L300 152" stroke="#1d1626" stroke-width="11" stroke-linecap="round"/>',
    '<path d="M300 648 L300 152" stroke="#4b3a5f" stroke-width="3.5" stroke-linecap="round" opacity="0.7"/>',
    '<g class="pcz-blade">',
    '<path d="M302 156 C258 150 210 118 188 68 C183 56 193 48 202 58 C232 86 268 110 302 120 C312 123 314 144 302 156 Z" fill="url(#pczBlade)" filter="url(#pczGlow)"/>',
    '<path d="M302 156 C258 150 210 118 188 68" fill="none" stroke="#faf5ff" stroke-width="2" stroke-linecap="round" opacity="0.85"/>',
    '</g>',
    '<circle cx="300" cy="154" r="7" fill="#2a1f38" stroke="#a855f7" stroke-width="2" opacity="0.9"/>',
    '</g>',

    /* ---- energy trail from the blade ---- */
    '<path class="pcz-trail" d="M190 68 Q150 150 130 220 Q108 300 120 380 Q130 448 178 496" fill="none" stroke="url(#pczTrail)" stroke-width="5" stroke-linecap="round" filter="url(#pczGlow)"/>',
    '<path class="pcz-trail" d="M200 82 Q164 156 146 222 Q126 296 138 372" fill="none" stroke="#c084fc" stroke-width="1.8" stroke-linecap="round" opacity="0.55"/>',

    /* ---- hood opening ---- */
    '<path d="M210 140 C232 140 246 160 248 186 C250 212 246 240 240 258 C228 244 192 244 180 258 C174 240 170 212 172 186 C174 160 188 140 210 140 Z" fill="#030206"/>',
    '<path d="M210 148 C228 148 240 166 242 188 C244 210 240 232 236 248 C226 236 194 236 184 248 C180 232 176 210 178 188 C180 166 192 148 210 148 Z" fill="#0a0710" opacity="0.85"/>',

    /* ---- eyes ---- */
    '<g filter="url(#pczGlow)">',
    '<ellipse class="pcz-eye pcz-eye-left" cx="196" cy="192" rx="8.5" ry="4" fill="#c084fc"/>',
    '<ellipse class="pcz-eye pcz-eye-right" cx="226" cy="192" rx="8.5" ry="4" fill="#c084fc"/>',
    '<ellipse cx="196" cy="192" rx="3.2" ry="1.6" fill="#fbf5ff"/>',
    '<ellipse cx="226" cy="192" rx="3.2" ry="1.6" fill="#fbf5ff"/>',
    '</g>',

    /* ---- hand gripping the staff ---- */
    '<path d="M186 356 C198 366 202 388 196 400 C190 412 174 410 170 398 C166 384 172 368 180 356 Z" fill="#1b1426" stroke="#4a3a5e" stroke-width="1.6"/>',
    '<path d="M176 372 C186 368 194 370 198 376" fill="none" stroke="#5a4870" stroke-width="2" stroke-linecap="round"/>',
    '<path d="M174 388 C184 384 192 386 196 392" fill="none" stroke="#5a4870" stroke-width="2" stroke-linecap="round"/>',

    /* ---- other hand emerging from the sleeve ---- */
    '<path d="M126 412 C118 422 116 438 124 444 C132 450 142 444 144 432 C146 422 140 412 134 408 Z" fill="#1b1426" stroke="#3f3152" stroke-width="1.6"/>',
    '</svg>'
  ].join('');

  /* ---------------------------------------------------------------------- */
  /* helpers                                                                */
  /* ---------------------------------------------------------------------- */

  function rand(min, max) {
    return min + Math.random() * (max - min);
  }

  function isAuthPage() {
    return !!document.querySelector('.spider-entry-page');
  }

  function isRegisterForm() {
    var form = document.querySelector('.spider-entry-page .spider-form');
    if (!form) return false;
    return form.querySelectorAll('input[type="password"]').length > 1;
  }

  /* ---------------------------------------------------------------------- */
  /* background                                                             */
  /* ---------------------------------------------------------------------- */

  function buildBackground() {
    if (state.bg) return;

    var bg = document.createElement('div');
    bg.id = 'pcz-bg';
    bg.setAttribute('aria-hidden', 'true');

    var canvas = document.createElement('canvas');
    canvas.id = 'pcz-smoke';
    bg.appendChild(canvas);

    var cards = document.createElement('div');
    cards.id = 'pcz-cards';
    cards.setAttribute('aria-hidden', 'true');

    var lightning = document.createElement('div');
    lightning.id = 'pcz-lightning';
    lightning.setAttribute('aria-hidden', 'true');

    document.body.appendChild(bg);
    document.body.appendChild(cards);
    document.body.appendChild(lightning);

    state.bg = bg;
    state.cards = cards;
    state.lightning = lightning;
    state.smoke = canvas;

    buildCards();
    startSmoke();
    scheduleLightning();
  }

  function buildCards() {
    if (!state.cards || state.cards.childElementCount) return;

    CARD_SLOTS.forEach(function (slot, i) {
      var card = document.createElement('div');
      card.className = 'pcz-card';
      card.style.top = slot.top;
      if (slot.left) card.style.left = slot.left;
      if (slot.right) card.style.right = slot.right;
      card.style.setProperty('--pcz-rot', slot.rot);
      card.style.setProperty('--pcz-dur', slot.dur);
      card.style.setProperty('--pcz-delay', (i * 0.42).toFixed(2) + 's');
      card.style.setProperty('--pcz-in-delay', (0.35 + i * 0.12).toFixed(2) + 's');

      var glyph = document.createElement('span');
      glyph.className = 'pcz-card-glyph';
      glyph.textContent = GLYPHS[i % GLYPHS.length];
      card.appendChild(glyph);

      state.cards.appendChild(card);
    });
  }

  /* ---------------------------------------------------------------------- */
  /* smoke                                                                  */
  /* ---------------------------------------------------------------------- */

  function makeSmokeSprite(rgb) {
    var size = 256;
    var sprite = document.createElement('canvas');
    sprite.width = sprite.height = size;
    var ctx = sprite.getContext('2d');
    var grad = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    grad.addColorStop(0, 'rgba(' + rgb + ',0.55)');
    grad.addColorStop(0.45, 'rgba(' + rgb + ',0.22)');
    grad.addColorStop(1, 'rgba(' + rgb + ',0)');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, size, size);
    return sprite;
  }

  function startSmoke() {
    var canvas = state.smoke;
    if (!canvas || !canvas.getContext) return;

    var ctx = canvas.getContext('2d');
    var sprites = [makeSmokeSprite('147,51,234'), makeSmokeSprite('168,85,247'), makeSmokeSprite('107,33,168')];
    var blobs = [];
    var width = 0;
    var height = 0;
    var dpr = 1;
    var last = 0;

    function resize() {
      dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      width = canvas.clientWidth || window.innerWidth;
      height = canvas.clientHeight || window.innerHeight;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }

    function seed() {
      blobs = [];
      var count = reduceMotion.matches ? 6 : 15;
      for (var i = 0; i < count; i++) {
        var radius = rand(150, 430);
        blobs.push({
          x: rand(-0.1, 1.1) * width,
          y: rand(-0.1, 1.1) * height,
          r: radius,
          vx: rand(-9, 9),
          vy: rand(-6, 4),
          alpha: rand(0.1, 0.3),
          phase: rand(0, Math.PI * 2),
          speed: rand(0.12, 0.4),
          sprite: sprites[i % sprites.length]
        });
      }
    }

    function frame(now) {
      state.raf = requestAnimationFrame(frame);
      if (document.hidden) return;
      if (!last) last = now;
      var dt = Math.min((now - last) / 1000, 0.05);
      last = now;
      var t = now / 1000;

      ctx.clearRect(0, 0, width, height);
      ctx.globalCompositeOperation = 'lighter';

      for (var i = 0; i < blobs.length; i++) {
        var b = blobs[i];
        b.x += b.vx * dt;
        b.y += b.vy * dt;

        if (b.x < -b.r) b.x = width + b.r;
        if (b.x > width + b.r) b.x = -b.r;
        if (b.y < -b.r) b.y = height + b.r;
        if (b.y > height + b.r) b.y = -b.r;

        var pulse = 0.72 + 0.28 * Math.sin(t * b.speed + b.phase);
        ctx.globalAlpha = b.alpha * pulse;
        ctx.drawImage(b.sprite, b.x - b.r, b.y - b.r, b.r * 2, b.r * 2);
      }

      ctx.globalAlpha = 1;
      ctx.globalCompositeOperation = 'source-over';
    }

    resize();
    seed();

    window.addEventListener('resize', function () {
      resize();
      seed();
    });

    if (!reduceMotion.matches) {
      state.raf = requestAnimationFrame(frame);
    } else {
      /* still render a single static frame */
      requestAnimationFrame(function () {
        frame(performance.now());
        cancelAnimationFrame(state.raf);
        state.raf = 0;
      });
    }
  }

  /* ---------------------------------------------------------------------- */
  /* lightning                                                              */
  /* ---------------------------------------------------------------------- */

  function boltPath(x, width) {
    var points = [];
    var y = 0;
    var step = 60;
    var jitter = 26;
    var cx = x;
    while (y < 620) {
      points.push([cx, y]);
      cx += rand(-jitter, jitter);
      cx = Math.max(8, Math.min(width - 8, cx));
      y += step;
      step *= 0.92;
    }
    return points.map(function (p, i) {
      return (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1);
    }).join(' ');
  }

  function flash() {
    if (!state.lightning || !isAuthPage()) return;

    var bolt = document.createElement('div');
    bolt.className = 'pcz-bolt';

    var w = window.innerWidth;
    var x = rand(w * 0.1, w * 0.9);
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 ' + w + ' 620');
    svg.setAttribute('preserveAspectRatio', 'none');

    var defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
    defs.innerHTML = '<filter id="pczBoltGlow"><feGaussianBlur stdDeviation="3" result="b"/>' +
      '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>';
    svg.appendChild(defs);

    var d = boltPath(x, w);

    var wide = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    wide.setAttribute('d', d);
    wide.setAttribute('fill', 'none');
    wide.setAttribute('stroke', '#7c3aed');
    wide.setAttribute('stroke-width', '9');
    wide.setAttribute('stroke-linecap', 'round');
    wide.setAttribute('opacity', '0.28');
    wide.setAttribute('filter', 'url(#pczBoltGlow)');

    var core = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    core.setAttribute('d', d);
    core.setAttribute('fill', 'none');
    core.setAttribute('stroke', '#e9d5ff');
    core.setAttribute('stroke-width', '1.6');
    core.setAttribute('stroke-linecap', 'round');
    core.setAttribute('opacity', '0.6');

    svg.appendChild(wide);
    svg.appendChild(core);

    var glow = document.createElement('div');
    glow.className = 'pcz-bolt-flash';

    bolt.appendChild(svg);
    bolt.appendChild(glow);
    state.lightning.appendChild(bolt);

    window.setTimeout(function () {
      if (bolt.parentNode) bolt.parentNode.removeChild(bolt);
    }, 760);
  }

  function scheduleLightning() {
    window.clearTimeout(state.lightningTimer);
    if (reduceMotion.matches) return;

    state.lightningTimer = window.setTimeout(function () {
      if (isAuthPage() && !document.hidden && Math.random() > 0.25) flash();
      scheduleLightning();
    }, rand(4200, 11000));
  }

  /* ---------------------------------------------------------------------- */
  /* reaper                                                                 */
  /* ---------------------------------------------------------------------- */

  function buildReaper() {
    if (state.reaper || !isAuthPage()) return;

    var el = document.createElement('div');
    el.id = 'pcz-reaper';
    el.setAttribute('aria-hidden', 'true');
    el.innerHTML = REAPER_SVG;
    document.body.appendChild(el);
    state.reaper = el;

    /* entrance */
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        if (!state.reaper) return;
        state.reaper.classList.add('pcz-reaper-in');
        window.setTimeout(function () {
          if (state.reaper) state.reaper.classList.add('pcz-reaper-swing');
        }, 520);
      });
    });
  }

  function swingReaper() {
    if (!state.reaper) return;
    state.reaper.classList.remove('pcz-reaper-swing');
    /* force reflow so the animation can restart */
    void state.reaper.offsetWidth;
    state.reaper.classList.add('pcz-reaper-swing');
  }

  function exitReaper() {
    if (!state.reaper) return;
    state.reaper.classList.remove('pcz-reaper-swing');
    state.reaper.classList.add('pcz-reaper-out');
  }

  /* ---------------------------------------------------------------------- */
  /* DOM decoration                                                         */
  /* ---------------------------------------------------------------------- */

  function decorate() {
    var page = document.querySelector('.spider-entry-page');
    var card = page && page.querySelector('.spider-entry-content');
    if (!page || !card) return;

    /* Wrap the card so the wordmark can sit above it, outside the glass panel. */
    var shell = page.querySelector('.pcz-auth-shell');
    if (!shell) {
      shell = document.createElement('div');
      shell.className = 'pcz-auth-shell';
      page.appendChild(shell);
      shell.appendChild(card);

      var brand = document.createElement('div');
      brand.className = 'pcz-brand-bar';
      brand.innerHTML = '<span class="pcz-wordmark">Phantom<span class="pcz-wordmark-accent">Cardz</span></span>';
      shell.insertBefore(brand, card);
    }

    /* the old raster logo lives inside the card; hide it, keep the link usable */
    var oldBrand = card.querySelector('.nexus-brand');
    if (oldBrand) oldBrand.setAttribute('aria-hidden', 'true');

    var form = card.querySelector('.spider-form');
    if (form && !form.querySelector('.pcz-welcome')) {
      var welcome = document.createElement('p');
      welcome.className = 'pcz-welcome';
      welcome.setAttribute('role', 'status');
      welcome.setAttribute('aria-live', 'polite');
      welcome.hidden = true;
      welcome.textContent = 'Welcome back';
      form.appendChild(welcome);
    }
  }

  function showWelcome() {
    var welcome = document.querySelector('.spider-entry-page .pcz-welcome');
    if (welcome) {
      welcome.hidden = false;
      /* restart the entrance animation */
      welcome.style.animation = 'none';
      void welcome.offsetWidth;
      welcome.style.animation = '';
    }
    showWelcomeFlash();
  }

  /* The SPA swaps straight to the home route, unmounting the form. This body-level
     flash keeps the confirmation visible across that transition. */
  function showWelcomeFlash() {
    var existing = document.getElementById('pcz-welcome-flash');
    if (existing && existing.parentNode) existing.parentNode.removeChild(existing);

    var flash = document.createElement('div');
    flash.id = 'pcz-welcome-flash';
    flash.className = 'pcz-welcome-flash';
    flash.setAttribute('role', 'status');
    flash.setAttribute('aria-live', 'polite');
    flash.textContent = 'Welcome back';
    document.body.appendChild(flash);

    var remove = function () {
      if (flash.parentNode) flash.parentNode.removeChild(flash);
    };
    flash.addEventListener('animationend', function (event) {
      if (event.animationName === 'pcz-flash-life') remove();
    });
    /* fallback for reduced motion / interrupted animations */
    window.setTimeout(remove, 4200);
  }

  /* ---------------------------------------------------------------------- */
  /* interactions                                                           */
  /* ---------------------------------------------------------------------- */

  function ripple(event) {
    var target = event.target.closest('.spider-primary-button');
    if (!target) return;
    if (reduceMotion.matches) return;

    var rect = target.getBoundingClientRect();
    var dot = document.createElement('span');
    dot.className = 'pcz-ripple';
    dot.style.left = (event.clientX - rect.left) + 'px';
    dot.style.top = (event.clientY - rect.top) + 'px';
    target.appendChild(dot);
    window.setTimeout(function () {
      if (dot.parentNode) dot.parentNode.removeChild(dot);
    }, 640);
  }

  function parallax(event) {
    if (reduceMotion.matches || !state.cards) return;
    state.pointer.x = event.clientX / window.innerWidth - 0.5;
    state.pointer.y = event.clientY / window.innerHeight - 0.5;
    if (state.parallaxQueued) return;
    state.parallaxQueued = true;
    requestAnimationFrame(function () {
      state.parallaxQueued = false;
      if (!state.cards) return;
      state.cards.style.transform =
        'translate3d(' + (state.pointer.x * -22).toFixed(2) + 'px,' +
        (state.pointer.y * -14).toFixed(2) + 'px,0)';
    });
  }

  function onPointerDown(event) {
    ripple(event);
    if (event.target.closest('.spider-primary-button')) swingReaper();
  }

  function onSubmitCapture() {
    swingReaper();
  }

  function onSignupComplete() {
    if (isRegisterForm()) return;
    showWelcome();
    exitReaper();
  }

  /* ---------------------------------------------------------------------- */
  /* lifecycle                                                              */
  /* ---------------------------------------------------------------------- */

  function teardown() {
    cancelAnimationFrame(state.raf);
    state.raf = 0;
    window.clearTimeout(state.lightningTimer);

    ['bg', 'cards', 'lightning', 'reaper'].forEach(function (key) {
      var el = state[key];
      if (el && el.parentNode) el.parentNode.removeChild(el);
      state[key] = null;
    });
    state.smoke = null;
  }

  function sync() {
    if (isAuthPage()) {
      buildBackground();
      buildReaper();
      decorate();
    } else if (state.bg) {
      teardown();
    }
  }

  function start() {
    if (state.started) return;
    state.started = true;

    document.addEventListener('pointerdown', onPointerDown, true);
    document.addEventListener('submit', onSubmitCapture, true);
    document.addEventListener('mousemove', parallax, { passive: true });
    window.addEventListener('phantomcardz:signup-complete', onSignupComplete);

    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) scheduleLightning();
    });

    reduceMotion.addEventListener('change', function () {
      scheduleLightning();
    });

    /* The SPA re-renders on login/register toggle and route changes. */
    var pending = 0;
    state.observer = new MutationObserver(function () {
      window.clearTimeout(pending);
      pending = window.setTimeout(sync, 90);
    });
    state.observer.observe(document.body, { childList: true, subtree: true });

    sync();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start, { once: true });
  } else {
    start();
  }
})();
