/* ------------------------------------------------------------------
   Decorative "Tom & Jerry" chase near the workspace divider.

   Purely ornamental: appends TWO aria-hidden elements to #main-view,
   touches no application state, renders no task/activity data, and is
   pointer-events:none so it can never intercept a click. Jerry wanders
   along the divider band; Tom chases him with a lag so he's always a
   step behind. Movement is a single requestAnimationFrame loop using
   transforms only; it pauses when the tab is hidden and when the user
   prefers reduced motion, and fully tears itself down on pagehide (or
   via window.__jerryWander.destroy).
   ------------------------------------------------------------------ */
(function () {
  if (window.__jerryWander) return; // guard against double-init

  var host = document.getElementById('main-view');
  if (!host) return;

  /* Jerry — a small brown mouse, mid-run. Named limb groups are animated
     by CSS (.leg-front / .leg-back / .arm). */
  var JERRY_SVG =
    '<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">' +
    '<path d="M14 50C2 44 6 30 15 34" fill="none" stroke="#b06a2a" stroke-width="3.4" stroke-linecap="round"/>' +
    '<g class="leg-back"><ellipse cx="30" cy="47" rx="4" ry="7" fill="#a9611f"/><ellipse cx="30" cy="53" rx="5" ry="2.6" fill="#c8823c"/></g>' +
    '<g class="leg-front"><ellipse cx="40" cy="47" rx="4" ry="7" fill="#c8823c"/><ellipse cx="41" cy="53" rx="5" ry="2.6" fill="#e0a35f"/></g>' +
    '<ellipse cx="35" cy="40" rx="14" ry="16" fill="#c8823c"/>' +
    '<ellipse cx="36" cy="43" rx="8" ry="10" fill="#f4e2c6"/>' +
    '<g class="arm"><ellipse cx="26" cy="30" rx="3.4" ry="7" fill="#c8823c"/></g>' +
    '<circle cx="37" cy="22" r="13" fill="#c8823c"/>' +
    '<circle cx="27" cy="11" r="7.5" fill="#c8823c"/><circle cx="27" cy="11" r="4" fill="#e9a9b8"/>' +
    '<circle cx="46" cy="12" r="6.5" fill="#c8823c"/><circle cx="46" cy="12" r="3.4" fill="#e9a9b8"/>' +
    '<ellipse cx="41" cy="26" rx="6.5" ry="5" fill="#f4e2c6"/>' +
    '<circle cx="35" cy="19" r="2.4" fill="#241a12"/><circle cx="41.5" cy="19" r="2.4" fill="#241a12"/>' +
    '<circle cx="35.7" cy="18.2" r=".8" fill="#fff"/><circle cx="42.2" cy="18.2" r=".8" fill="#fff"/>' +
    '<circle cx="44" cy="25" r="1.8" fill="#3a2a1a"/>' +
    '<path d="M30 28q6 4 12 0" fill="none" stroke="#7a4a1c" stroke-width="1.4" stroke-linecap="round"/>' +
    '<path d="M20 24l-9-3M21 27l-9 0M22 30l-8 4" stroke="#5a3714" stroke-width="1" stroke-linecap="round"/>' +
    '</svg>';

  /* Tom — a bigger blue-grey cat, lunging forward with one paw reaching. */
  var TOM_SVG =
    '<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg">' +
    '<path d="M8 52C-4 44 4 26 14 32" fill="none" stroke="#7d8ea3" stroke-width="4" stroke-linecap="round"/>' +
    '<g class="leg-back"><ellipse cx="26" cy="48" rx="5" ry="9" fill="#7d8ea3"/><ellipse cx="26" cy="56" rx="6" ry="3" fill="#e9edf2"/></g>' +
    '<g class="leg-front"><ellipse cx="40" cy="48" rx="5" ry="9" fill="#93a2b5"/><ellipse cx="42" cy="56" rx="6" ry="3" fill="#fff"/></g>' +
    '<ellipse cx="33" cy="38" rx="17" ry="19" fill="#93a2b5"/>' +
    '<ellipse cx="35" cy="42" rx="10" ry="13" fill="#e9edf2"/>' +
    '<g class="arm"><ellipse cx="49" cy="26" rx="4.5" ry="10" fill="#93a2b5" transform="rotate(28 49 26)"/>' +
    '<circle cx="55" cy="16" r="4.6" fill="#fff"/>' +
    '<path d="M52 13l1.5-4M55 12l0-4M58 13l1.5-3.5" stroke="#c7d0db" stroke-width="1.2" stroke-linecap="round"/></g>' +
    '<circle cx="33" cy="20" r="15" fill="#93a2b5"/>' +
    '<path d="M20 10l-3-9 10 5zM46 10l3-9-10 5z" fill="#93a2b5"/>' +
    '<path d="M22 9l-1.5-5 6 3zM44 9l1.5-5-6 3z" fill="#f0b6c0"/>' +
    '<ellipse cx="33" cy="24" rx="10" ry="7" fill="#fff"/>' +
    '<ellipse cx="27" cy="17" rx="3.4" ry="4.2" fill="#fff"/><ellipse cx="39" cy="17" rx="3.4" ry="4.2" fill="#fff"/>' +
    '<circle cx="27.5" cy="17.6" r="2.1" fill="#2b2f22"/><circle cx="38.5" cy="17.6" r="2.1" fill="#2b2f22"/>' +
    '<circle cx="28.2" cy="16.8" r=".7" fill="#fff"/><circle cx="39.2" cy="16.8" r=".7" fill="#fff"/>' +
    '<path d="M31 23h4l-2 2z" fill="#e88b98"/>' +
    '<path d="M33 25v3M33 28q-3 1-5 0M33 28q3 1 5 0" fill="none" stroke="#5c6470" stroke-width="1.3" stroke-linecap="round"/>' +
    '<path d="M20 22l-9-2M20 25l-9 1M21 28l-9 4M46 22l9-2M46 25l9 1M45 28l9 4" stroke="#c7d0db" stroke-width="1" stroke-linecap="round"/>' +
    '</svg>';

  var reduceMQ = window.matchMedia('(prefers-reduced-motion: reduce)');

  function make(cls, svg) {
    var el = document.createElement('div');
    el.className = 'toon-sprite ' + cls;
    el.setAttribute('aria-hidden', 'true');
    el.innerHTML = '<div class="toon-inner">' + svg + '</div>';
    host.appendChild(el);
    return { el: el, inner: el.firstChild, dir: 1 };
  }

  var jerry = make('toon-jerry', JERRY_SVG);
  var tom = make('toon-tom', TOM_SVG);

  var DIVIDER_Y = 58;   // just below the topbar's faded divider
  var BAND = 26;        // vertical half-range of the wander area
  var TOM_GAP = 74;     // how far behind Jerry the cat trails, in px

  var bounds = { xMin: 0, xMax: 0, yMin: 0, yMax: 0 };
  function measure() {
    var w = host.clientWidth || 0;
    bounds.xMin = 30;
    bounds.xMax = Math.max(bounds.xMin + 1, w - 60);
    bounds.yMin = DIVIDER_Y - BAND;
    bounds.yMax = DIVIDER_Y + BAND;
  }
  measure();

  function rand(a, b) { return a + Math.random() * (b - a); }
  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }
  function easeInOutSine(t) { return -(Math.cos(Math.PI * t) - 1) / 2; }

  // Jerry's motion state (point-to-point hops).
  var cur = { x: rand(bounds.xMin, bounds.xMax), y: DIVIDER_Y };
  var from = { x: cur.x, y: cur.y };
  var to = { x: cur.x, y: cur.y };
  var phase = 'hold', phaseStart = 0, phaseDur = 600, moving = false;

  // Tom follows a lagged copy of Jerry's position.
  var tomPos = { x: cur.x - TOM_GAP, y: cur.y };

  var rafId = 0, running = false;

  function place(sprite, x, y, bob) {
    sprite.el.style.transform = 'translate(' + x.toFixed(1) + 'px,' + y.toFixed(1) + 'px)';
    sprite.inner.style.transform = 'translateY(' + (bob || 0).toFixed(2) + 'px) scaleX(' + sprite.dir + ')';
  }

  function startMove(now) {
    from.x = cur.x; from.y = cur.y;
    to.x = rand(bounds.xMin, bounds.xMax);
    to.y = rand(bounds.yMin, bounds.yMax);
    var dist = Math.hypot(to.x - from.x, to.y - from.y);
    phase = 'move'; phaseStart = now;
    phaseDur = 1400 + dist * 6 + rand(0, 900); // brisk — it's a chase
    if (Math.abs(to.x - from.x) > 4) jerry.dir = to.x > from.x ? 1 : -1;
    moving = true;
  }
  function startHold(now) {
    phase = 'hold'; phaseStart = now;
    phaseDur = rand(180, 700); // short pauses only
    moving = false;
  }

  function frame(now) {
    if (!running) return;
    var t = phaseDur > 0 ? (now - phaseStart) / phaseDur : 1;
    if (phase === 'move') {
      if (t >= 1) { cur.x = to.x; cur.y = to.y; startHold(now); }
      else {
        var e = easeInOutSine(clamp(t, 0, 1));
        cur.x = from.x + (to.x - from.x) * e;
        cur.y = from.y + (to.y - from.y) * e;
      }
    } else if (t >= 1) {
      startMove(now);
    }

    // Tom eases toward a point TOM_GAP behind Jerry along his facing.
    var targetX = cur.x - TOM_GAP * jerry.dir;
    var targetY = cur.y + 4;
    var prevTomX = tomPos.x;
    tomPos.x += (targetX - tomPos.x) * 0.06;
    tomPos.y += (targetY - tomPos.y) * 0.06;
    if (Math.abs(tomPos.x - prevTomX) > 0.15) tom.dir = tomPos.x > prevTomX ? 1 : -1;

    var bob = Math.sin(now / 160) * 3;           // fast running bob
    place(jerry, cur.x, cur.y, moving ? bob : bob * 0.3);
    place(tom, tomPos.x, tomPos.y, Math.sin(now / 150 + 1) * 3.4);

    rafId = requestAnimationFrame(frame);
  }

  function start() {
    if (running || reduceMQ.matches || document.hidden) return;
    running = true;
    phaseStart = performance.now();
    startMove(phaseStart);
    rafId = requestAnimationFrame(frame);
  }
  function stop() {
    running = false;
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
  }
  function placeStatic() {
    cur.x = (bounds.xMin + bounds.xMax) / 2; cur.y = DIVIDER_Y; jerry.dir = 1;
    tomPos.x = cur.x - TOM_GAP; tomPos.y = cur.y + 4; tom.dir = 1;
    place(jerry, cur.x, cur.y, 0);
    place(tom, tomPos.x, tomPos.y, 0);
  }

  var resizeRAF = 0;
  function onResize() {
    if (resizeRAF) return;
    resizeRAF = requestAnimationFrame(function () {
      resizeRAF = 0;
      measure();
      cur.x = clamp(cur.x, bounds.xMin, bounds.xMax);
      cur.y = clamp(cur.y, bounds.yMin, bounds.yMax);
      if (!running) placeStatic();
    });
  }
  function onVisibility() { if (document.hidden) stop(); else start(); }
  function onMotionPref() {
    if (reduceMQ.matches) { stop(); placeStatic(); } else start();
  }

  window.addEventListener('resize', onResize);
  document.addEventListener('visibilitychange', onVisibility);
  if (reduceMQ.addEventListener) reduceMQ.addEventListener('change', onMotionPref);
  else if (reduceMQ.addListener) reduceMQ.addListener(onMotionPref);

  function destroy() {
    stop();
    if (resizeRAF) { cancelAnimationFrame(resizeRAF); resizeRAF = 0; }
    window.removeEventListener('resize', onResize);
    document.removeEventListener('visibilitychange', onVisibility);
    if (reduceMQ.removeEventListener) reduceMQ.removeEventListener('change', onMotionPref);
    else if (reduceMQ.removeListener) reduceMQ.removeListener(onMotionPref);
    [jerry, tom].forEach(function (s) { if (s.el.parentNode) s.el.parentNode.removeChild(s.el); });
    window.__jerryWander = null;
  }
  window.addEventListener('pagehide', destroy, { once: true });
  window.__jerryWander = { destroy: destroy };

  placeStatic();
  if (!reduceMQ.matches) start();
})();
