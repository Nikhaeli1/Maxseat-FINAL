/**
 * MaxSeat Alert — WebSocket Client
 * Handles real-time sensor data with automatic reconnection.
 *
 * Usage: include this script in any dashboard template, then call:
 *   MaxSeatWS.init(onMessage);
 *
 * onMessage(data) receives parsed JSON from the server.
 */

const MaxSeatWS = (() => {
  // ── Config ────────────────────────────────────────────────────────────────
  const WS_PROTOCOL  = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
  const WS_URL       = WS_PROTOCOL + window.location.host + '/ws';
  const RECONNECT_BASE_MS  = 1000;   // first retry after 1 s
  const RECONNECT_MAX_MS   = 30000;  // cap at 30 s
  const RECONNECT_FACTOR   = 2;      // exponential backoff multiplier

  // ── State ─────────────────────────────────────────────────────────────────
  let socket          = null;
  let retryCount      = 0;
  let retryTimeout    = null;
  let userCallback    = null;
  let manualClose     = false;

  // ── Status indicator helpers ──────────────────────────────────────────────
  function setStatus(state) {
    const el = document.getElementById('ws-status');
    if (!el) return;
    const map = {
      connected:    { text: '● Live',         cls: 'text-green-400'  },
      connecting:   { text: '◌ Connecting…',  cls: 'text-yellow-400' },
      disconnected: { text: '○ Disconnected', cls: 'text-red-400'    },
    };
    const s = map[state] || map.disconnected;
    el.textContent = s.text;
    el.className   = s.cls + ' text-xs font-bold';
  }

  // ── Connect ───────────────────────────────────────────────────────────────
  function connect() {
    if (socket && (socket.readyState === WebSocket.OPEN ||
                   socket.readyState === WebSocket.CONNECTING)) return;

    setStatus('connecting');
    socket = new WebSocket(WS_URL);

    socket.onopen = () => {
      console.log('[MaxSeatWS] Connected');
      retryCount   = 0;
      retryTimeout = null;
      setStatus('connected');
    };

    socket.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (typeof userCallback === 'function') userCallback(data);
      } catch (e) {
        console.warn('[MaxSeatWS] Could not parse message:', event.data);
      }
    };

    socket.onclose = (event) => {
      setStatus('disconnected');
      if (manualClose) return;
      scheduleReconnect();
    };

    socket.onerror = (err) => {
      console.warn('[MaxSeatWS] Error:', err);
      socket.close();
    };
  }

  // ── Exponential backoff reconnect ─────────────────────────────────────────
  function scheduleReconnect() {
    if (retryTimeout) return;
    const delay = Math.min(
      RECONNECT_BASE_MS * Math.pow(RECONNECT_FACTOR, retryCount),
      RECONNECT_MAX_MS
    );
    retryCount++;
    console.log(`[MaxSeatWS] Reconnecting in ${delay}ms (attempt ${retryCount})`);
    retryTimeout = setTimeout(() => {
      retryTimeout = null;
      connect();
    }, delay);
  }

  // ── Public API ────────────────────────────────────────────────────────────
  function init(onMessage) {
    userCallback = onMessage;
    manualClose  = false;
    connect();
  }

  function disconnect() {
    manualClose = true;
    if (retryTimeout) { clearTimeout(retryTimeout); retryTimeout = null; }
    if (socket)       { socket.close(); socket = null; }
    setStatus('disconnected');
  }

  function send(data) {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(data));
    } else {
      console.warn('[MaxSeatWS] Cannot send — socket not open');
    }
  }

  // Reconnect on tab regaining focus (handles mobile sleep / network switch)
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && (!socket || socket.readyState !== WebSocket.OPEN)) {
      if (!manualClose) connect();
    }
  });

  return { init, disconnect, send };
})();
