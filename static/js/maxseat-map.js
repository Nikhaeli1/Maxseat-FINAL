/**
 * MaxSeat Alert — shared map helpers (Leaflet)
 *
 * Every map page (admin, enforcer, cooperative, driver) uses these so they all behave the same:
 *   • Leaflet is served from /static/vendor (no CDN that can be blocked or slow).
 *   • Tiles come from CARTO when CARTO_API_KEY is set, else OpenStreetMap; if they fail, backups are tried.
 *   • The map re-measures itself when its box changes size (phones, rotating, sidebar),
 *     which is what caused grey/blank maps before.
 *   • Bad coordinates (null, text, 0,0 from a GPS with no fix) never break the map.
 *     Vehicles with no GPS fix yet are drawn grey at the CDO center and left out of the zoom.
 *   • fit() zooms to where the vehicles actually are; a "show all vehicles" button does it again.
 *   • Scrolling the page over a map scrolls the PAGE. To zoom with the wheel: hold Ctrl (⌘ on a Mac),
 *     or click the map first. The + / − buttons and pinch always work.
 *   • A fullscreen button (top right) opens the map on the whole screen; Esc or the button closes it.
 *
 *   const map = MaxSeatMap.create('map', { fitAll: () => currentPuvs });
 *   MaxSeatMap.create('side-map', { background: true });   // still, decorative map (login page)
 *   MaxSeatMap.fit(map, puvs);
 *   L.circleMarker(MaxSeatMap.position(p), MaxSeatMap.style(p, colorFn, 12))
 */
const MaxSeatMap = (() => {
  const CDO = [8.4822, 124.6472];          // Cagayan de Oro city center
  const DEFAULT_ZOOM = 13;

  // Map backgrounds, tried in order; if one fails to load, the map switches to the next.
  //   1. CARTO Voyager, only when the server has CARTO_API_KEY set (CARTO now requires a key;
  //      without one it draws an "API KEY REQUIRED" watermark)
  //   2-4. Free servers that need no key
  const CARTO_KEY = (window.MAXSEAT_CARTO_KEY || '').trim();
  const TILE_SOURCES = (CARTO_KEY ? [{
      url: 'https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png?key=' + encodeURIComponent(CARTO_KEY),
      opts: { maxZoom: 20,
              attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>' },
  }] : []).concat([
    {
      url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
      opts: { maxZoom: 19,
              attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' },
    },
    {
      url: 'https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png',
      opts: { subdomains: 'abc', maxZoom: 19,
              attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, tiles by HOT' },
    },
    {
      url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}',
      opts: { maxZoom: 19, attribution: 'Tiles &copy; Esri' },
    },
  ]);

  // ── Coordinates ─────────────────────────────────────────────────────────
  function toLatLng(lat, lng) {
    const a = parseFloat(lat), b = parseFloat(lng);
    if (!isFinite(a) || !isFinite(b)) return null;
    if (Math.abs(a) > 90 || Math.abs(b) > 180) return null;
    if (Math.abs(a) < 0.001 && Math.abs(b) < 0.001) return null;   // 0,0 = GPS has no fix
    return [a, b];
  }

  function hasGps(p) {
    if (!p) return false;
    if (p.has_gps === false) return false;
    return !!toLatLng(p.lat, p.lng);
  }

  function position(p) {
    return (p && toLatLng(p.lat, p.lng)) || CDO;
  }

  // Marker look: solid color when tracked, grey dashed when there's no GPS fix yet
  function style(p, colorFn, radius) {
    const r = radius || 11;
    if (hasGps(p)) {
      return { radius: r, fillColor: colorFn ? colorFn(p) : '#2F6F5A', color: '#fff',
               weight: 3, fillOpacity: 1, opacity: 1, dashArray: null };
    }
    return { radius: r - 2, fillColor: '#9BA8A1', color: '#4A6258', weight: 2,
             fillOpacity: 0.55, opacity: 0.9, dashArray: '3 3' };
  }

  function noGpsNote(p) {
    return hasGps(p) ? '' :
      '<div style="margin-top:4px;font-size:11px;color:#5E6F67;">No GPS fix yet, shown at CDO center</div>';
  }

  // ── Map creation ────────────────────────────────────────────────────────
  function showError(el, text) {
    el.innerHTML = '<div style="height:100%;min-height:160px;display:flex;align-items:center;justify-content:center;' +
      'text-align:center;padding:16px;color:#5E6F67;font-size:13px;background:#F5F9F7;">' + text + '</div>';
  }

  function addTiles(map) {
    let index = 0, layer = null, loaded = 0, failed = 0;
    function use(i) {
      if (layer) map.removeLayer(layer);
      index = i; loaded = 0; failed = 0;
      const src = TILE_SOURCES[i];
      layer = L.tileLayer(src.url, src.opts).addTo(map);
      layer.on('tileload', () => { loaded++; });
      layer.on('load', () => { if (loaded > 0) map.fire('maxseat:tilesready'); });   // tiles in view have drawn
      layer.on('tileerror', () => {
        failed++;
        // Several failures before anything loaded: this tile server is blocked or down
        if (loaded === 0 && failed >= 4 && index + 1 < TILE_SOURCES.length) use(index + 1);
      });
    }
    use(0);
  }

  function keepSized(map, el) {
    const fix = () => { try { map.invalidateSize({ pan: false }); } catch (e) { /* map removed */ } };
    if (window.ResizeObserver) new ResizeObserver(fix).observe(el);
    window.addEventListener('resize', fix);
    window.addEventListener('orientationchange', () => setTimeout(fix, 300));
    document.addEventListener('visibilitychange', () => { if (!document.hidden) fix(); });
    setTimeout(fix, 250);
    setTimeout(fix, 1000);
  }

  // Small square map buttons (same look as Leaflet's + / −)
  const ICONS = {
    expand:   '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>',
    compress: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5"/></svg>',
    target:   '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><circle cx="12" cy="12" r="6.5"/><circle cx="12" cy="12" r="1.8" fill="currentColor"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3"/></svg>',
  };

  function mapButton(title, icon, onClick) {
    const b = L.DomUtil.create('a', 'leaflet-bar maxseat-map-btn');
    b.href = '#';
    b.title = title;
    b.setAttribute('role', 'button');
    b.setAttribute('aria-label', title);
    b.innerHTML = icon;
    b.style.cssText = 'display:flex;align-items:center;justify-content:center;width:34px;height:34px;' +
                      'background:#fff;color:#14211B;text-decoration:none;margin-bottom:8px;';
    L.DomEvent.disableClickPropagation(b);
    L.DomEvent.on(b, 'click', (e) => { L.DomEvent.preventDefault(e); onClick(b); });
    return b;
  }

  function addControl(map, build) {
    const Ctl = L.Control.extend({ options: { position: 'topright' }, onAdd: build });
    map.addControl(new Ctl());
  }

  function addFitButton(map, getPuvs) {
    addControl(map, () => mapButton('Show all vehicles', ICONS.target, () => fit(map, getPuvs())));
  }

  // Fullscreen: the map fills the whole screen. Hidden where the browser can't do it (iPhone).
  function fullscreenEl() { return document.fullscreenElement || document.webkitFullscreenElement || null; }
  function addFullscreenButton(map, el) {
    const can = el.requestFullscreen || el.webkitRequestFullscreen;
    if (!can || !(document.fullscreenEnabled || document.webkitFullscreenEnabled)) return;
    let btn;
    const sync = () => {
      const on = fullscreenEl() === el;
      btn.innerHTML = on ? ICONS.compress : ICONS.expand;
      btn.title = on ? 'Exit fullscreen' : 'Fullscreen';
      btn.setAttribute('aria-label', btn.title);
      setTimeout(() => { try { map.invalidateSize({ pan: false }); } catch (e) { /* map removed */ } }, 120);
    };
    addControl(map, () => {
      btn = mapButton('Fullscreen', ICONS.expand, () => {
        if (fullscreenEl() === el) (document.exitFullscreen || document.webkitExitFullscreen).call(document);
        else (el.requestFullscreen || el.webkitRequestFullscreen).call(el);
      });
      return btn;
    });
    document.addEventListener('fullscreenchange', sync);
    document.addEventListener('webkitfullscreenchange', sync);
  }

  // Mouse wheel: scrolls the page unless Ctrl/⌘ is held, the map was clicked, or it is fullscreen.
  function wheelScrollsPage(el) {
    const mac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
    const hint = document.createElement('div');
    hint.textContent = mac ? 'Hold ⌘ and scroll to zoom the map' : 'Hold Ctrl and scroll to zoom the map';
    hint.style.cssText = 'position:absolute;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center;' +
      'pointer-events:none;background:rgba(20,33,27,.45);color:#fff;font:700 15px/1.3 inherit;text-align:center;' +
      'padding:16px;opacity:0;transition:opacity .25s;';
    el.appendChild(hint);
    let active = false, timer = null;
    el.addEventListener('mousedown', () => { active = true; });
    el.addEventListener('mouseleave', () => { active = false; });
    el.addEventListener('wheel', (e) => {
      if (e.ctrlKey || e.metaKey || active || fullscreenEl() === el) return;   // let the map zoom
      e.stopImmediatePropagation();            // the map ignores it, so the page scrolls as normal
      e.stopPropagation();
      hint.style.opacity = '1';
      clearTimeout(timer);
      timer = setTimeout(() => { hint.style.opacity = '0'; }, 1100);
    }, { capture: true });
  }

  function create(elOrId, opts) {
    const el = typeof elOrId === 'string' ? document.getElementById(elOrId) : elOrId;
    if (!el) return null;
    if (typeof L === 'undefined') {
      showError(el, "The map couldn't load. Check your internet connection and refresh the page.");
      return null;
    }
    opts = opts || {};
    // background: true = a still map used as decoration (login page): no buttons, no dragging or zooming
    const bg = !!opts.background;
    const map = L.map(el, Object.assign({
      zoomControl:        !bg && opts.zoomControl !== false,
      attributionControl: !bg,                    // the still login map credits the map in the page text instead
      center:             opts.center || CDO,
      zoom:               opts.zoom || DEFAULT_ZOOM,
    }, bg ? { dragging: false, touchZoom: false, scrollWheelZoom: false, doubleClickZoom: false,
              boxZoom: false, keyboard: false, tap: false } : {}));
    if (map.attributionControl) map.attributionControl.setPrefix(false);
    addTiles(map);
    keepSized(map, el);
    if (bg) return map;
    if (opts.fullscreen !== false) addFullscreenButton(map, el);
    if (typeof opts.fitAll === 'function') addFitButton(map, opts.fitAll);
    if (opts.wheelScrollsPage !== false) wheelScrollsPage(el);
    return map;
  }

  // Zoom to the vehicles that have a real position
  function fit(map, puvs, maxZoom) {
    if (!map) return;
    const pts = (puvs || []).filter(hasGps).map((p) => toLatLng(p.lat, p.lng));
    if (!pts.length)      map.setView(CDO, DEFAULT_ZOOM);
    else if (pts.length === 1) map.setView(pts[0], Math.min(maxZoom || 16, 16));
    else                  map.fitBounds(pts, { padding: [36, 36], maxZoom: maxZoom || 16 });
  }

  return { CDO, create, fit, toLatLng, hasGps, position, style, noGpsNote };
})();
