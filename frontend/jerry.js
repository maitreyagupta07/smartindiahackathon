/* ------------------------------------------------------------------
   Decorative wandering "Jerry" near the workspace divider.

   Purely ornamental: it appends ONE aria-hidden element to #main-view,
   touches no application state, renders no task/activity data, and is
   pointer-events:none so it can never intercept a click. Movement is a
   single requestAnimationFrame loop using transforms only; it pauses
   when the tab is hidden and when the user prefers reduced motion, and
   fully tears itself down on pagehide (or via window.__jerryWander.destroy).
   ------------------------------------------------------------------ */
(function () {
  if (window.__jerryWander) return; // guard against double-init

  var host = document.getElementById('main-view');
  if (!host) return;

  var JERRY_SVG =
    '<svg viewBox="0 0 64 64" width="34" height="34" xmlns="http://www.w3.org/2000/svg">' +
    '<path d="M20 52C6 50 8 38 16 40" fill="none" stroke="#a9762f" stroke-width="3" stroke-linecap="round"/>' +
    '<path d="M22 44q-6 3-7 9M46 44q6 3 7 9" fill="none" stroke="#c8823c" stroke-width="4" stroke-linecap="round"/>' +
    '<ellipse cx="28" cy="60" rx="4" ry="2.5" fill="#c8823c"/>' +
    '<ellipse cx="40" cy="60" rx="4" ry="2.5" fill="#c8823c"/>' +
    '<ellipse cx="34" cy="44" rx="13" ry="15" fill="#c8823c"/>' +
    '<ellipse cx="35" cy="47" rx="7" ry="9" fill="#f4e2c6"/>' +
    '<circle cx="35" cy="24" r="13" fill="#c8823c"/>' +
    '<circle cx="24" cy="13" r="7.5" fill="#c8823c"/>' +
    '<circle cx="24" cy="13" r="4" fill="#e9a9b8"/>' +
    '<circle cx="44" cy="14" r="6" fill="#c8823c"/>' +
    '<circle cx="44" cy="14" r="3" fill="#e9a9b8"/>' +
    '<ellipse cx="38" cy="28" rx="6" ry="4.5" fill="#f4e2c6"/>' +
    '<circle cx="33" cy="21" r="2.2" fill="#2a2018"/>' +
    '<circle cx="39" cy="21" r="2.2" fill="#2a2018"/>' +
    '<circle cx="41" cy="27" r="1.6" fill="#3a2a1a"/>' +
    '</svg>';

  var SIZE = 34;
  var DIVIDER_Y = 56; // bottom edge of .topbar — the faded horizontal divider
  var BAND = 24;      // vertical half-range of the wander area, centered on the divider

  var el = document.createElement('div');
  el.className = 'jerry-wander';
  el.setAttribute('aria-hidden', 'true');
  el.innerHTML = '<div class="jerry-inner">' + JERRY_SVG + '</div>';
  host.appendChild(el);
  var inner = el.firstChild;

  var reduceMQ = window.matchMedia('(prefers-reduced-motion: reduce)');

  var bounds = { xMin: 0, xMax: 0, yMin: 0, yMax: 0 };
  function measure() {
    var w = host.clientWidth || 0;
    bounds.xMin = 24;
    bounds.xMax = Math.max(bounds.xMin + 1, w - SIZE - 24);
    bounds.yMin = DIVIDER_Y - BAND;
    bounds.yMax = DIVIDER_Y + BAND;
  }
  measure();

  function rand(a, b) { return a + Math.random() * (b - a); }
  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }
  function easeInOutSine(t) { return -(Math.cos(Math.PI * t) - 1) / 2; }

  var cur = { x: rand(bounds.xMin, bounds.xMax), y: rand(bounds.yMin, bounds.yMax) };
  var from = { x: cur.x, y: cur.y };
  var to = { x: cur.x, y: cur.y };
  var phase = 'hold';
  var phaseStart = 0;
  var phaseDur = 600;
  var dir = 1;
  var rafId = 0;
  var running = false;

  function render(bob) {
    el.style.transform = 'translate(' + cur.x.toFixed(1) + 'px,' + cur.y.toFixed(1) + 'px)';
    inner.style.transform = 'translateY(' + (bob || 0).toFixed(2) + 'px) scaleX(' + dir + ')';
  }

  function startMove(now) {
    from.x = cur.x; from.y = cur.y;
    to.x = rand(bounds.xMin, bounds.xMax);
    to.y = rand(bounds.yMin, bounds.yMax);
    var dist = Math.hypot(to.x - from.x, to.y - from.y);
    phase = 'move';
    phaseStart = now;
    phaseDur = 2600 + dist * 10 + rand(0, 1600); // longer hops take proportionally longer
    if (Math.abs(to.x - from.x) > 4) dir = to.x > from.x ? 1 : -1;
  }

  function startHold(now) {
    phase = 'hold';
    phaseStart = now;
    phaseDur = rand(500, 2000);
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
    render(Math.sin(now / 420) * 2.5); // slight vertical bob
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
    cur.x = (bounds.xMin + bounds.xMax) / 2;
    cur.y = DIVIDER_Y;
    dir = 1;
    render(0);
  }

  var resizeRAF = 0;
  function onResize() {
    if (resizeRAF) return;
    resizeRAF = requestAnimationFrame(function () {
      resizeRAF = 0;
      measure();
      cur.x = clamp(cur.x, bounds.xMin, bounds.xMax);
      cur.y = clamp(cur.y, bounds.yMin, bounds.yMax);
      if (!running) render(0);
    });
  }
  function onVisibility() { if (document.hidden) stop(); else start(); }
  function onMotionPref() {
    if (reduceMQ.matches) { stop(); placeStatic(); }
    else start();
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
    if (el.parentNode) el.parentNode.removeChild(el);
    window.__jerryWander = null;
  }
  window.addEventListener('pagehide', destroy, { once: true });

  window.__jerryWander = { destroy: destroy };

  render(0);
  if (reduceMQ.matches) placeStatic();
  else start();
})();
