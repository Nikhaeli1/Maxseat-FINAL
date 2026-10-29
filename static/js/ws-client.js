/**
 * MaxSeat Alert — real-time client
 *
 * Loaded on every page by layout.html. Two parts:
 *
 * 1. MaxSeatWS  — the live feed. Two modes, picked by the server (window.MAXSEAT_LIVE_MODE):
 *      "ws"   one WebSocket to /ws with automatic reconnection (local / regular servers)
 *      "poll" asks /api/live every few seconds (Vercel, which has no WebSockets). Each
 *             vehicle whose reading changed is passed to listeners in the same shape the
 *             WebSocket sends, and {type: "data_changed"} is sent when vehicles are added/
 *             removed or other fleet data changes, so pages work the same in both modes.
 *      MaxSeatWS.init(fn)       listen for messages (can be called by several scripts)
 *      MaxSeatWS.isConnected()  true while the feed is working
 *
 * 2. MaxSeatLive — keeps pages current without a manual refresh.
 *      • Any element with  data-live="some-unique-key"  is re-rendered by the server
 *        in the background after a sensor update and swapped in place. Because the
 *        server renders it, colors, badges, counts and violation lists all stay correct.
 *      • MaxSeatLive.trackMarker(puv, leafletMarker, popupFn, colorFn) moves/recolors a
 *        map marker instantly when that vehicle's reading changes.
 *      • MaxSeatLive.syncMarkers(puvList, createFn) adds markers for new vehicles and
 *        removes markers for deleted ones (call it after a refresh).
 *      • The server also sends {type: "data_changed"} when a vehicle is added/removed/
 *        reassigned, a capacity changes or a citation is issued; pages refresh on that too.
 *      • Regions containing a focused input/select/textarea are left alone until the
 *        user is done, so typing is never interrupted.
 *      • If the live feed is down, regions are refreshed by polling every 15 s instead.
 */

const MaxSeatWS = (() => {
  const MODE              = window.MAXSEAT_LIVE_MODE === 'poll' ? 'poll' : 'ws';
  const POLL_URL          = '/api/live';
  const POLL_MS           = 4000;   // how often open pages ask for new readings (poll mode)
  const WS_PROTOCOL       = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
  const WS_URL            = WS_PROTOCOL + window.location.host + '/ws';
  const RECONNECT_BASE_MS = 1000;   // first retry after 1 s
  const RECONNECT_MAX_MS  = 30000;  // cap at 30 s
  const RECONNECT_FACTOR  = 2;      // exponential backoff multiplier

  let socket       = null;
  let retryCount   = 0;
  let retryTimeout = null;
  let manualClose  = false;
  let started      = false;
  const listeners  = [];

  // Updates every element marked id="ws-status" or [data-ws-status]
  function setStatus(state) {
    const labels = { connected: 'Live tracking on', connecting: 'Connecting…', disconnected: 'Reconnecting…' };
    document.querySelectorAll('#ws-status, [data-ws-status]').forEach((el) => {
      el.setAttribute('data-state', state);
      const txt = el.querySelector('.ws-status-text');
      if (txt) txt.textContent = labels[state] || labels.disconnected;
    });
  }

  function emit(data) {
    listeners.forEach((fn) => {
      try { fn(data); } catch (e) { console.warn('[MaxSeatWS] listener error:', e); }
    });
  }

  // ── Poll mode ────────────────────────────────────────────────────────────
  let pollTimer   = null;
  let pollOk      = false;
  let pollBusy    = false;
  let lastVersion = null;
  let lastPuvs    = {};      // puv_id -> JSON string of its last payload

  async function pollOnce() {
    if (pollBusy || manualClose) return;
    pollBusy = true;
    try {
      const res = await fetch(POLL_URL, { cache: 'no-store', credentials: 'same-origin' });
      if (res.status === 401) { pollOk = false; setStatus('disconnected'); return; }  // logged out
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      if (!pollOk) { pollOk = true; setStatus('connected'); }

      const fresh = {};
      (data.puvs || []).forEach((p) => { fresh[p.puv_id] = JSON.stringify(p); });

      if (lastVersion !== null && data.version !== lastVersion) {
        let anyPuvChanged = false;
        (data.puvs || []).forEach((p) => {
          if (lastPuvs[p.puv_id] !== fresh[p.puv_id]) { anyPuvChanged = true; emit(p); }
        });
        const before = Object.keys(lastPuvs).sort().join(',');
        const after  = Object.keys(fresh).sort().join(',');
        // Vehicles added/removed, or something not in the readings changed (e.g. a citation)
        if (before !== after || !anyPuvChanged) emit({ type: 'data_changed', scope: 'poll' });
      }
      lastVersion = data.version;
      lastPuvs    = fresh;
    } catch (e) {
      if (pollOk) { pollOk = false; setStatus('disconnected'); }
    } finally {
      pollBusy = false;
    }
  }

  function startPolling() {
    if (pollTimer) return;
    setStatus('connecting');
    pollOnce();
    pollTimer = setInterval(() => { if (!document.hidden) pollOnce(); }, POLL_MS);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    pollOk = false;
  }

  // ── WebSocket mode ───────────────────────────────────────────────────────
  function connect() {
    if (MODE === 'poll') { startPolling(); return; }
    if (socket && (socket.readyState === WebSocket.OPEN ||
                   socket.readyState === WebSocket.CONNECTING)) return;

    setStatus('connecting');
    try {
      socket = new WebSocket(WS_URL);
    } catch (e) {
      scheduleReconnect();
      return;
    }

    socket.onopen = () => {
      retryCount   = 0;
      retryTimeout = null;
      setStatus('connected');
    };

    socket.onmessage = (event) => {
      let data;
      try { data = JSON.parse(event.data); } catch (e) { return; }
      emit(data);
    };

    socket.onclose = () => {
      setStatus('disconnected');
      if (!manualClose) scheduleReconnect();
    };

    socket.onerror = () => {
      try { socket.close(); } catch (e) { /* already closed */ }
    };
  }

  function scheduleReconnect() {
    if (retryTimeout) return;
    const delay = Math.min(RECONNECT_BASE_MS * Math.pow(RECONNECT_FACTOR, retryCount), RECONNECT_MAX_MS);
    retryCount++;
    retryTimeout = setTimeout(() => { retryTimeout = null; connect(); }, delay);
  }

  function init(onMessage) {
    if (typeof onMessage === 'function' && !listeners.includes(onMessage)) listeners.push(onMessage);
    manualClose = false;
    started     = true;
    connect();
  }

  function disconnect() {
    manualClose = true;
    stopPolling();
    if (retryTimeout) { clearTimeout(retryTimeout); retryTimeout = null; }
    if (socket)       { socket.close(); socket = null; }
    setStatus('disconnected');
  }

  function send(data) {
    if (MODE === 'poll') return;   // nothing to send to in poll mode
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(data));
  }

  function isConnected() {
    if (MODE === 'poll') return pollOk;
    return !!socket && socket.readyState === WebSocket.OPEN;
  }

  // Reconnect when the tab regains focus (mobile sleep / network switch)
  document.addEventListener('visibilitychange', () => {
    if (MODE === 'poll') {
      if (started && !document.hidden && !manualClose) pollOnce();   // catch up right away
      return;
    }
    if (started && !document.hidden && !manualClose && !isConnected()) {
      if (retryTimeout) { clearTimeout(retryTimeout); retryTimeout = null; }
      connect();
    }
  });

  return { init, disconnect, send, isConnected, mode: MODE };
})();


const MaxSeatLive = (() => {
  const MIN_GAP_MS    = 2000;   // at most one background refresh every 2 s
  const SETTLE_MS     = 400;    // wait for a burst of sensor messages to settle
  const FALLBACK_POLL = 15000;  // refresh interval while the WebSocket is down

  const markers = {};           // puv_id -> { marker, data, popupFn, colorFn }
  let lastRefresh = 0;
  let timer       = null;
  let inFlight    = false;
  let pending     = false;   // another refresh is needed right after this one
  let deferred    = false;   // a region was skipped (user typing / dialog open)

  // ── Map markers ──────────────────────────────────────────────────────────
  function trackMarker(puv, marker, popupFn, colorFn) {
    if (!puv || puv.id === undefined || !marker) return;
    markers[puv.id] = { marker, data: Object.assign({}, puv), popupFn, colorFn };
  }

  function updateMarker(msg) {
    const entry = markers[msg.puv_id];
    if (!entry) return;
    const d = entry.data;
    ['passengers', 'capacity', 'temp', 'lat', 'lng', 'loc_name', 'last_update',
     'is_violator', 'is_overheating', 'company', 'plate', 'driver'].forEach((k) => {
      if (msg[k] !== undefined && msg[k] !== null && msg[k] !== '') d[k] = msg[k];
    });
    if (typeof d.lat === 'number' && typeof d.lng === 'number') entry.marker.setLatLng([d.lat, d.lng]);
    if (entry.colorFn && entry.marker.setStyle) entry.marker.setStyle({ fillColor: entry.colorFn(d) });
    if (entry.popupFn && entry.marker.setPopupContent) entry.marker.setPopupContent(entry.popupFn(d));
  }

  // Add markers for vehicles that are new, drop markers for vehicles that were removed
  function syncMarkers(puvs, createFn) {
    if (!Array.isArray(puvs)) return;
    const seen = new Set();
    puvs.forEach((p) => {
      seen.add(String(p.id));
      if (markers[p.id]) updateMarker(Object.assign({}, p, { puv_id: p.id }));
      else if (typeof createFn === 'function') createFn(p);
    });
    Object.keys(markers).forEach((id) => {
      if (!seen.has(id)) { markers[id].marker.remove(); delete markers[id]; }
    });
  }

  // ── Server-rendered live regions ────────────────────────────────────────
  function regions() {
    return document.querySelectorAll('[data-live]');
  }

  function isBusy(el) {
    const a = document.activeElement;
    return !!a && a !== document.body && el.contains(a) && /^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName);
  }

  function syncAttributes(target, source) {
    // Keep class/style in step too (e.g. a card's red/green border lives on the region itself)
    ['class', 'style'].forEach((name) => {
      const v = source.getAttribute(name);
      if (v === null) target.removeAttribute(name);
      else if (target.getAttribute(name) !== v) target.setAttribute(name, v);
    });
  }

  async function refreshRegions() {
    if (inFlight) { pending = true; return; }
    if (document.hidden) { pending = true; return; }       // catch up when the tab is visible again
    if (window.Swal && Swal.isVisible && Swal.isVisible()) { deferred = true; return; }
    if (!regions().length) return;

    inFlight    = true;
    lastRefresh = Date.now();
    try {
      const res = await fetch(window.location.href, {
        headers: { 'X-MaxSeat-Live': '1' },
        cache: 'no-store',
        credentials: 'same-origin',
      });
      // Session expired → server redirected to /login; leave the page as it is
      if (!res.ok || new URL(res.url).pathname !== window.location.pathname) return;
      const doc = new DOMParser().parseFromString(await res.text(), 'text/html');

      let changed = false;
      regions().forEach((el) => {
        const key   = el.getAttribute('data-live');
        const fresh = doc.querySelector('[data-live="' + CSS.escape(key) + '"]');
        if (!fresh) return;
        if (isBusy(el)) { deferred = true; return; }
        if (el.innerHTML !== fresh.innerHTML) { el.innerHTML = fresh.innerHTML; changed = true; }
        syncAttributes(el, fresh);
      });
      if (changed) document.dispatchEvent(new CustomEvent('maxseat:live-updated'));
    } catch (e) {
      pending = true;   // network hiccup: try again on the next trigger
    } finally {
      inFlight = false;
      if (pending) { pending = false; scheduleRefresh(); }
    }
  }

  function scheduleRefresh() {
    if (timer) return;
    const wait = Math.max(SETTLE_MS, MIN_GAP_MS - (Date.now() - lastRefresh));
    timer = setTimeout(() => { timer = null; refreshRegions(); }, wait);
  }

  function onMessage(msg) {
    if (!msg) return;
    if (msg.type === 'data_changed') { scheduleRefresh(); return; }
    if (msg.puv_id === undefined) return;
    updateMarker(msg);
    scheduleRefresh();
  }

  function start() {
    if (!regions().length && !Object.keys(markers).length) return;   // nothing live on this page
    MaxSeatWS.init(onMessage);
    setInterval(() => { if (!MaxSeatWS.isConnected()) scheduleRefresh(); }, FALLBACK_POLL);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) scheduleRefresh(); });
    // Retry a region that was skipped because the user was typing in it
    document.addEventListener('focusout', () => { if (deferred) { deferred = false; scheduleRefresh(); } });
  }

  // Page scripts run before DOMContentLoaded, so markers are registered by then
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else setTimeout(start, 0);

  return { trackMarker, syncMarkers, refresh: scheduleRefresh };
})();
