/**
 * MaxSeat Alert — shared map helpers (Leaflet)
 *
 * Every map page (admin, enforcer, cooperative, driver) uses these so they all behave the same:
 *   • Leaflet is served from /static/vendor (no CDN that can be blocked or slow).
 *   • Tiles come from OpenStreetMap (no API key); if they fail to load, it tries backup servers.
 *   • The map re-measures itself when its box changes size (phones, rotating, sidebar),
 *     which is what caused grey/blank maps before.
 *   • Bad coordinates (null, text, 0,0 from a GPS with no fix) never break the map.
 *     Vehicles with no GPS fix yet are drawn grey at the CDO center and left out of the zoom.
 *   • fit() zooms to where the vehicles actually are; a "show all" button does it again.
 *
 *   const map = MaxSeatMap.create('map', { fitAll: () => currentPuvs });
 *   MaxSeatMap.fit(map, puvs);
 *   L.circleMarker(MaxSeatMap.position(p), MaxSeatMap.style(p, colorFn, 12))
 */
const MaxSeatMap = (() => {
  const CDO = [8.4822, 124.6472];          // Cagayan de Oro city center
  const DEFAULT_ZOOM = 13;

  // Free map backgrounds that need no API key. (CARTO's free tiles now show an
  // "API KEY REQUIRED" watermark, so they are no longer used.)
  // If one server fails to load, the map switches to the next one.
  const TILE_SOURCES = [
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
  ];

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
      return { radius: r, fillColor: colorFn ? colorFn(p) : '#2563eb', color: '#fff',
               weight: 3, fillOpacity: 1, opacity: 1, dashArray: null };
    }
    return { radius: r - 2, fillColor: '#94a3b8', color: '#475569', weight: 2,
             fillOpacity: 0.55, opacity: 0.9, dashArray: '3 3' };
  }

  function noGpsNote(p) {
    return hasGps(p) ? '' :
      '<div style="margin-top:4px;font-size:11px;color:#64748b;">No GPS fix yet, shown at CDO center</div>';
  }

  // ── Map creation ────────────────────────────────────────────────────────
  function showError(el, text) {
    el.innerHTML = '<div style="height:100%;min-height:160px;display:flex;align-items:center;justify-content:center;' +
      'text-align:center;padding:16px;color:#64748b;font-size:13px;background:#f8fafc;">' + text + '</div>';
  }

  function addTiles(map) {
    let index = 0, layer = null, loaded = 0, failed = 0;
    function use(i) {
      if (layer) map.removeLayer(layer);
      index = i; loaded = 0; failed = 0;
      const src = TILE_SOURCES[i];
      layer = L.tileLayer(src.url, src.opts).addTo(map);
      layer.on('tileload', () => { loaded++; });
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

  function addFitButton(map, getPuvs) {
    const Ctl = L.Control.extend({
      options: { position: 'topright' },
      onAdd() {
        const b = L.DomUtil.create('a', 'leaflet-bar maxseat-fit-btn');
        b.href = '#';
        b.title = 'Show all vehicles';
        b.setAttribute('role', 'button');
        b.innerHTML = '&#x2922;';
        b.style.cssText = 'display:block;width:34px;height:34px;line-height:32px;text-align:center;' +
                          'background:#fff;color:#0f172a;font-size:18px;font-weight:700;text-decoration:none;';
        L.DomEvent.disableClickPropagation(b);
        L.DomEvent.on(b, 'click', (e) => { L.DomEvent.preventDefault(e); fit(map, getPuvs()); });
        return b;
      },
    });
    map.addControl(new Ctl());
  }

  function create(elOrId, opts) {
    const el = typeof elOrId === 'string' ? document.getElementById(elOrId) : elOrId;
    if (!el) return null;
    if (typeof L === 'undefined') {
      showError(el, "The map couldn't load. Check your internet connection and refresh the page.");
      return null;
    }
    opts = opts || {};
    const map = L.map(el, {
      zoomControl:        opts.zoomControl !== false,
      attributionControl: true,
      center:             opts.center || CDO,
      zoom:               opts.zoom || DEFAULT_ZOOM,
    });
    map.attributionControl.setPrefix(false);
    addTiles(map);
    keepSized(map, el);
    if (typeof opts.fitAll === 'function') addFitButton(map, opts.fitAll);
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
