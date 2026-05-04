/* SKYWATCH — frontend application
 *
 * Connects to the WebSocket, accumulates aircraft state, renders the map,
 * the traffic list, the detail pane, the event ticker, and the RA timeline.
 */

(() => {
  'use strict';

  // ─── State ──────────────────────────────────────────────────────────

  const state = {
    aircraft: new Map(),     // icao -> aircraft data
    selectedIcao: null,
    receiver: null,
    raEvents: [],            // {icao, started_at, ended_at, summary, threat_icao}
    // Bounded backlog of every event the ticker has seen this session.
    // Kept as data (not just DOM) so the per-aircraft EVENTS block in
    // the detail pane can filter by ICAO.  Insertion-order (oldest at
    // index 0); the renderer reverses it for newest-first display.
    events: [],
    eventsMax: 500,
    stats: {},
    frameTimes: [],          // sliding window for rate calc
    filterText: '',
    detailMode: localStorage.getItem('skywatch.detailMode') || 'compact',
    config: { route_enrichment: false, route_enrichment_available: false },
    // Map-marker label fields.  Persisted per-browser.  Default mirrors
    // the original (callsign + FL + V/S + GS) so existing users see no
    // change after upgrading.
    labelFields: new Set(JSON.parse(
      localStorage.getItem('skywatch.labelFields') ||
      '["callsign","fl","vrate","gs"]')),
  };

  // ─── DOM ────────────────────────────────────────────────────────────

  const el = {
    list: document.getElementById('aircraft-list'),
    detail: document.getElementById('detail-pane'),
    detailContent: document.getElementById('detail-content'),
    eventLog: document.getElementById('event-log'),
    routeToggle: document.getElementById('route-toggle'),
    raTimeline: document.getElementById('ra-timeline'),
    raCount: document.getElementById('ra-count'),
    rxInfo: document.getElementById('rx-info'),
    filter: document.getElementById('filter'),
    statUptime: document.getElementById('stat-uptime'),
    statFrames: document.getElementById('stat-frames'),
    statRate: document.getElementById('stat-rate'),
    statDrop: document.getElementById('stat-drop'),
    statRx: document.getElementById('stat-rx'),
    statAircraft: document.getElementById('stat-aircraft'),
    connStat: document.getElementById('conn-stat'),
  };

  el.filter.addEventListener('input', () => {
    state.filterText = el.filter.value.trim().toUpperCase();
    renderList();
  });

  // ─── Pane resize splitters ──────────────────────────────────────────
  // Each .splitter sits in its own narrow grid track between two panes
  // (see CSS).  On drag, we update the matching CSS variable on the
  // layout element so the grid recomputes.  Sizes persist to localStorage.

  const layoutEl = document.getElementById('layout');

  function loadSplitterSize(varName, fallback) {
    const stored = localStorage.getItem('skywatch.size' + varName);
    if (stored) layoutEl.style.setProperty(varName, stored);
    else if (fallback) layoutEl.style.setProperty(varName, fallback);
  }
  loadSplitterSize('--col-list');
  loadSplitterSize('--col-detail');
  loadSplitterSize('--row-ra');

  function clamp(n, lo, hi) { return Math.max(lo, Math.min(hi, n)); }

  document.querySelectorAll('.splitter').forEach(sp => {
    sp.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      const axis = sp.dataset.axis;        // 'x' (column) or 'y' (row)
      const cssVar = sp.dataset.var;
      const startCoord = (axis === 'x') ? ev.clientX : ev.clientY;
      const startSize = parseFloat(
        getComputedStyle(layoutEl).getPropertyValue(cssVar)) || 0;
      sp.classList.add('dragging');
      sp.setPointerCapture(ev.pointerId);

      function onMove(e) {
        const cur = (axis === 'x') ? e.clientX : e.clientY;
        // Splitters live to the LEFT/TOP of the variable they control,
        // so dragging "into" the variable's side (right or down) shrinks
        // it.  i.e. delta and size move opposite.
        const newSize = startSize - (cur - startCoord);
        layoutEl.style.setProperty(cssVar, clamp(newSize, 120, 900) + 'px');
      }
      function onUp(e) {
        sp.removeEventListener('pointermove', onMove);
        sp.removeEventListener('pointerup', onUp);
        sp.removeEventListener('pointercancel', onUp);
        sp.classList.remove('dragging');
        try { sp.releasePointerCapture(e.pointerId); } catch (_) {}
        localStorage.setItem(
          'skywatch.size' + cssVar,
          layoutEl.style.getPropertyValue(cssVar));
        // Leaflet caches its container size — invalidate it so tiles
        // and markers redraw to the new dimensions.
        if (typeof map !== 'undefined' && map.invalidateSize) {
          map.invalidateSize();
        }
      }
      sp.addEventListener('pointermove', onMove);
      sp.addEventListener('pointerup', onUp);
      sp.addEventListener('pointercancel', onUp);
    });
  });

  // ─── Map-label field selector ───────────────────────────────────────
  // Wire the checkboxes to state.labelFields and re-render markers on
  // any change.  Initial state reflects what's stored.

  document.querySelectorAll('#label-fields input[data-field]').forEach(cb => {
    cb.checked = state.labelFields.has(cb.dataset.field);
    cb.addEventListener('change', () => {
      const f = cb.dataset.field;
      if (cb.checked) state.labelFields.add(f);
      else state.labelFields.delete(f);
      localStorage.setItem(
        'skywatch.labelFields',
        JSON.stringify([...state.labelFields]));
      // Re-render every marker with the new label set.
      for (const ac of state.aircraft.values()) updateMarker(ac);
    });
  });

  // Route-enrichment toggle (adsbdb.com).  Click sends the new state to
  // the backend over the WS; the backend echoes a config message that
  // updates this and any other connected client.
  el.routeToggle.addEventListener('click', () => {
    if (!state.config.route_enrichment_available) return;
    const next = !state.config.route_enrichment;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'set_route_enrichment', enabled: next }));
    }
  });

  function applyConfig(cfg) {
    if (!cfg) return;
    Object.assign(state.config, cfg);
    el.routeToggle.hidden = !state.config.route_enrichment_available;
    el.routeToggle.classList.toggle('on', !!state.config.route_enrichment);
    el.routeToggle.querySelector('.val').textContent =
      state.config.route_enrichment ? 'ON' : 'OFF';
  }

  // Detail-pane view toggle: compact vs verbose+source-provenance.
  // Choice persisted in localStorage so reloads remember it.
  document.querySelectorAll('.detail-mode-btn').forEach(btn => {
    if (btn.dataset.mode === state.detailMode) btn.classList.add('active');
    btn.addEventListener('click', () => {
      if (btn.dataset.mode === state.detailMode) return;
      state.detailMode = btn.dataset.mode;
      localStorage.setItem('skywatch.detailMode', state.detailMode);
      document.querySelectorAll('.detail-mode-btn').forEach(b =>
        b.classList.toggle('active', b.dataset.mode === state.detailMode));
      renderDetail();
    });
  });

  // ─── Map ────────────────────────────────────────────────────────────

  const map = L.map('map', {
    center: [51.4775, -0.4614],
    zoom: 8,
    zoomControl: true,
    attributionControl: true,
    preferCanvas: false,
    worldCopyJump: true,
  });

  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png', {
    attribution: '© OpenStreetMap, © CARTO',
    maxZoom: 19,
    subdomains: 'abcd',
  }).addTo(map);

  // Layers we manage
  const aircraftLayer = L.layerGroup().addTo(map);
  const trailLayer = L.layerGroup().addTo(map);
  const tcasLinkLayer = L.layerGroup().addTo(map);
  const rangeRingLayer = L.layerGroup().addTo(map);

  // Per-aircraft Leaflet objects we keep around
  const markers = new Map();   // icao -> L.Marker
  const trails = new Map();    // icao -> L.Polyline
  const vectors = new Map();   // icao -> L.Polyline (heading vector)

  // ─── Rendering helpers ──────────────────────────────────────────────

  function flBand(altFt) {
    if (altFt == null) return 2;
    if (altFt < 10000) return 0;
    if (altFt < 20000) return 1;
    if (altFt < 30000) return 2;
    if (altFt < 40000) return 3;
    return 4;
  }

  function fmtFL(altFt) {
    if (altFt == null) return '—';
    return 'FL' + String(Math.round(altFt / 100)).padStart(3, '0');
  }

  function fmtHeading(deg) {
    if (deg == null) return '—';
    return String(Math.round(deg)).padStart(3, '0') + '°';
  }

  function fmtSpeed(kt) {
    if (kt == null) return '—';
    return String(Math.round(kt));
  }

  function fmtVrate(fpm) {
    if (fpm == null) return '';
    if (Math.abs(fpm) < 100) return ' →';
    if (fpm > 0) return ` ↑${Math.round(fpm)}`;
    return ` ↓${Math.abs(Math.round(fpm))}`;
  }

  // "Time since" formatter used by the LAST column and detail panes.
  // Compact: "12s", "2m", "1h 03m".  Returns "—" when ts is null.
  function fmtAge(secsAgo) {
    if (secsAgo == null || !isFinite(secsAgo) || secsAgo < 0) return '—';
    const s = Math.floor(secsAgo);
    if (s < 60) return s + 's';
    const m = Math.floor(s / 60);
    if (m < 60) return m + 'm ' + String(s % 60).padStart(2, '0') + 's';
    const h = Math.floor(m / 60);
    return h + 'h ' + String(m % 60).padStart(2, '0') + 'm';
  }

  // Map-marker staleness thresholds (seconds since last frame).  Below
  // STALE_S the marker renders normally; between STALE_S and VERY_STALE_S
  // it dims; beyond VERY_STALE_S it's almost ghosted but kept on the map
  // until the engine prunes it (currently 10 minutes).
  const STALE_S = 60;
  const VERY_STALE_S = 180;

  function ageOf(ac) {
    if (!ac || ac.last_seen == null) return null;
    return (Date.now() / 1000) - ac.last_seen;
  }

  function staleLevel(age) {
    if (age == null) return '';
    if (age >= VERY_STALE_S) return 'very-stale';
    if (age >= STALE_S) return 'stale';
    return '';
  }

  function aircraftIconSvg(headingDeg) {
    // A simple stylized aircraft silhouette (tip points up by default)
    return `<svg viewBox="-10 -10 20 20" xmlns="http://www.w3.org/2000/svg" style="transform: rotate(${headingDeg || 0}deg)">
      <path class="ac-icon" d="M 0 -8 L 1.2 -2 L 8 1 L 8 2.5 L 1.2 1 L 1 5 L 3 6.5 L 3 7.5 L 0 6.5 L -3 7.5 L -3 6.5 L -1 5 L -1.2 1 L -8 2.5 L -8 1 L -1.2 -2 Z"/>
    </svg>`;
  }

  // Build the per-marker label HTML based on the user's selected fields.
  // Returns the full <div class="ac-label">…</div> wrapper, or '' when
  // nothing is selected (no label rendered).
  function aircraftLabelHtml(ac) {
    const f = state.labelFields;
    const lines = [];

    // Header row: callsign and/or ICAO and/or squawk
    const header = [];
    if (f.has('callsign') && ac.callsign) {
      header.push(`<span class="lbl-cs">${ac.callsign}</span>`);
    }
    if (f.has('icao')) {
      header.push(`<span class="lbl-icao">${ac.icao}</span>`);
    }
    if (f.has('squawk') && ac.squawk) {
      header.push(`<span class="lbl-sqk">${ac.squawk}</span>`);
    }
    if (header.length) lines.push(`<div>${header.join('')}</div>`);

    // Type code (from offline DB lookup)
    if (f.has('type')) {
      const t = ac.info && ac.info.type_code;
      if (t) lines.push(`<div class="lbl-typ">${t}</div>`);
    }

    // Altitude + V/S, combined to save vertical space
    if (f.has('fl') || f.has('vrate')) {
      const parts = [];
      if (f.has('fl')) parts.push(`<span class="lbl-alt">${fmtFL(ac.alt_baro_ft)}</span>`);
      if (f.has('vrate')) parts.push(`<span class="lbl-vrate">${fmtVrate(ac.vrate_fpm)}</span>`);
      lines.push(`<div>${parts.join('')}</div>`);
    }

    // Ground speed + track, combined
    if (f.has('gs') || f.has('hdg')) {
      const parts = [];
      if (f.has('gs')) parts.push(`<span class="lbl-spd">${fmtSpeed(ac.gs_kt)}kt</span>`);
      if (f.has('hdg')) {
        const h = ac.track_deg != null ? ac.track_deg : ac.heading_deg;
        parts.push(`<span class="lbl-hdg">${fmtHeading(h)}</span>`);
      }
      lines.push(`<div>${parts.join('')}</div>`);
    }

    // Origin → Destination (from adsbdb route enrichment)
    if (f.has('route') && ac.route) {
      const o = ac.route.origin || {};
      const d = ac.route.destination || {};
      const oCode = o.iata || o.icao;
      const dCode = d.iata || d.icao;
      if (oCode || dCode) {
        lines.push(`<div class="lbl-route">${oCode || '?'}→${dCode || '?'}</div>`);
      }
    }

    // Dominant receiver tag (the receiver currently hearing this
    // aircraft most strongly).  Only meaningful in multi-receiver
    // setups; degrades gracefully when by_receiver is empty.
    if (f.has('rx')) {
      const dom = dominantReceiver(ac);
      if (dom) {
        lines.push(`<div class="lbl-rx">${dom.rid}</div>`);
      }
    }

    if (!lines.length) return '';
    return `<div class="ac-label">${lines.join('')}</div>`;
  }

  function buildMarkerHtml(ac) {
    const heading = ac.heading_deg != null ? ac.heading_deg :
                    (ac.track_deg != null ? ac.track_deg : 0);
    return aircraftIconSvg(heading) + aircraftLabelHtml(ac);
  }

  function markerClass(ac) {
    const stale = staleLevel(ageOf(ac));
    return 'ac-marker fl-' + flBand(ac.alt_baro_ft) +
           (ac.tcas_ra_active ? ' tcas-ra' : '') +
           (ac.icao === state.selectedIcao ? ' selected' : '') +
           (stale ? ' ' + stale : '');
  }

  function makeMarker(ac) {
    const icon = L.divIcon({
      html: buildMarkerHtml(ac),
      className: markerClass(ac),
      iconSize: [0, 0],
      iconAnchor: [0, 0],
    });
    const m = L.marker([ac.lat, ac.lon], { icon, riseOnHover: true });
    m.on('click', () => { selectAircraft(ac.icao); });
    return m;
  }

  function updateMarker(ac) {
    if (ac.lat == null || ac.lon == null) return;
    const existing = markers.get(ac.icao);
    if (existing) {
      existing.setLatLng([ac.lat, ac.lon]);
      existing.setIcon(L.divIcon({
        html: buildMarkerHtml(ac),
        className: markerClass(ac),
        iconSize: [0, 0],
        iconAnchor: [0, 0],
      }));
    } else {
      const m = makeMarker(ac);
      markers.set(ac.icao, m);
      aircraftLayer.addLayer(m);
    }
  }

  function updateTrail(ac) {
    if (!ac.trail || ac.trail.length < 2) return;
    const latlngs = ac.trail.map(pt => [pt[1], pt[2]]);
    const existing = trails.get(ac.icao);
    if (existing) {
      existing.setLatLngs(latlngs);
    } else {
      const t = L.polyline(latlngs, {
        className: 'ac-trail',
        color: '#b8f5d0',
        weight: 1,
        opacity: 0.5,
        interactive: false,
      });
      trails.set(ac.icao, t);
      trailLayer.addLayer(t);
    }
  }

  function removeAircraft(icao) {
    const m = markers.get(icao);
    if (m) { aircraftLayer.removeLayer(m); markers.delete(icao); }
    const t = trails.get(icao);
    if (t) { trailLayer.removeLayer(t); trails.delete(icao); }
    const v = vectors.get(icao);
    if (v) { aircraftLayer.removeLayer(v); vectors.delete(icao); }
  }

  function updateTcasLinks() {
    tcasLinkLayer.clearLayers();
    const seen = new Set();
    for (const [icao, ac] of state.aircraft) {
      if (!ac.tcas_ra_active || !ac.tcas_threat_icao) continue;
      const a = ac;
      const b = state.aircraft.get(ac.tcas_threat_icao);
      if (!b || a.lat == null || b.lat == null) continue;
      const key = [icao, ac.tcas_threat_icao].sort().join('-');
      if (seen.has(key)) continue;
      seen.add(key);
      L.polyline(
        [[a.lat, a.lon], [b.lat, b.lon]],
        { className: 'tcas-link', color: '#ff4848', weight: 1.5, dashArray: '4 3', interactive: false },
      ).addTo(tcasLinkLayer);
    }
  }

  // ─── Aircraft list (left table) ─────────────────────────────────────

  function renderList() {
    // Sort by callsign, then ICAO
    const items = [...state.aircraft.values()]
      .filter(ac => {
        if (!state.filterText) return true;
        const t = state.filterText;
        return ac.icao.toUpperCase().includes(t) ||
               (ac.callsign || '').toUpperCase().includes(t);
      })
      .sort((a, b) => {
        // RAs to the top, then by callsign
        if (a.tcas_ra_active && !b.tcas_ra_active) return -1;
        if (b.tcas_ra_active && !a.tcas_ra_active) return 1;
        return (a.callsign || a.icao).localeCompare(b.callsign || b.icao);
      });

    el.list.innerHTML = items.map(ac => {
      const age = ageOf(ac);
      const stale = staleLevel(age);
      const cls = (ac.icao === state.selectedIcao ? 'selected ' : '') +
                  (ac.tcas_ra_active ? 'tcas-ra ' : '') +
                  stale;
      const typ = ac.info?.type_code || '—';
      // Route (origin → destination) inlined under the callsign when
      // route enrichment has resolved this callsign.
      let routeStr = '';
      if (ac.route) {
        const o = ac.route.origin || {};
        const d = ac.route.destination || {};
        const oCode = o.iata || o.icao;
        const dCode = d.iata || d.icao;
        if (oCode || dCode) {
          routeStr = `<span class="cs-route">${oCode || '?'}→${dCode || '?'}</span>`;
        }
      }
      return `<li class="${cls}" data-icao="${ac.icao}">
        <span class="icao">${ac.icao}</span>
        <span class="cs">
          <span class="cs-name">${ac.callsign || ''}</span>
          ${routeStr}
        </span>
        <span class="typ">${typ}</span>
        <span class="fl">${fmtFL(ac.alt_baro_ft)}</span>
        <span class="gs">${fmtSpeed(ac.gs_kt)}</span>
        <span class="last">${fmtAge(age)}</span>
      </li>`;
    }).join('');

    el.list.querySelectorAll('li').forEach(li => {
      li.addEventListener('click', () => selectAircraft(li.dataset.icao));
    });
  }

  // ─── Detail pane ────────────────────────────────────────────────────

  // One-line text summary of an event, for the per-aircraft EVENTS
  // block.  The global ticker has its own renderer in `pushEvent` that
  // also styles by class; here we keep it minimal and homogeneous so
  // the per-aircraft block reads as a clean log.
  function fmtAircraftEvent(ev) {
    const t = new Date((ev.t || Date.now() / 1000) * 1000)
      .toISOString().substr(11, 8);
    let cls = 'ev-row';
    let msg;
    if (ev.type === 'new_aircraft') {
      msg = 'CONTACT acquired';
    } else if (ev.type === 'tcas_ra_started') {
      cls += ' ev-tcas';
      msg = `RA · ${ev.summary || ''}` +
            (ev.threat_icao ? ` (vs ${ev.threat_icao})` : '');
    } else if (ev.type === 'tcas_ra_ended') {
      cls += ' ev-tcas';
      msg = `RA END · ${ev.summary || ''}`;
    } else if (ev.type === 'emergency') {
      cls += ' ev-emerg';
      msg = `EMERGENCY · ${ev.state || ''}`;
    } else if (ev.type === 'intent_change') {
      cls += ' ev-intent';
      const src = ev.source ? ` [${ev.source}]` : '';
      msg = `${ev.summary || ''}${src}`;
    } else {
      msg = ev.summary || ev.type || JSON.stringify(ev);
    }
    return `<div class="${cls}"><span class="ev-t">${t}</span><span class="ev-msg">${msg}</span></div>`;
  }

  // Per-aircraft event stream — filtered subset of the global ticker.
  // Includes events naming this aircraft directly (`ev.icao`) AND ones
  // where it appears as a TCAS threat (`ev.threat_icao`), so resolution
  // advisories show up on both ends of the encounter.  Newest first;
  // capped at 20 rows so the detail pane stays scannable.
  function aircraftEventsBlock(ac) {
    const matches = state.events.filter(ev =>
      ev.icao === ac.icao || ev.threat_icao === ac.icao);
    if (!matches.length) return `
      <div class="detail-section">
        <h4>EVENTS</h4>
        <div class="ev-empty">No events yet for ${ac.icao}.</div>
      </div>`;
    const rows = matches.slice(-20).reverse().map(fmtAircraftEvent).join('');
    return `
      <div class="detail-section">
        <h4>EVENTS <span class="ev-count">${matches.length}</span></h4>
        <div class="ev-list">${rows}</div>
      </div>`;
  }

  // Renders the active flags from a {vnav, alt_hold, approach, ...}
  // dict as "VNAV / ALT_HOLD / APPROACH" — or em-dash if all are
  // false/None.  Empty string when the dict is empty.
  function fmtModeFlags(modes) {
    if (!modes || !Object.keys(modes).length) return '';
    const active = Object.entries(modes)
      .filter(([, v]) => v)
      .map(([k]) => k.toUpperCase());
    return active.length ? active.join(' / ') : '—';
  }

  // RECEIVERS HEARING block: which receivers have heard this aircraft,
  // with their RSSI.  Skipped entirely when only one receiver exists
  // (single-receiver mode reads RSSI from the top-level field via the
  // existing surveillance-quality section).  Sorted strongest-first.
  function dominantReceiver(ac) {
    const by = ac.by_receiver || {};
    let best = null;
    for (const [rid, b] of Object.entries(by)) {
      if (!best || (b.rssi != null && b.rssi > best.rssi)) {
        best = { rid, rssi: b.rssi, last_seen: b.last_seen };
      }
    }
    return best;
  }

  function receiversBlock(ac) {
    const by = ac.by_receiver || {};
    const ids = Object.keys(by);
    if (ids.length < 2) return '';   // not interesting with one RX
    const rows = ids
      .map(rid => ({ rid, ...by[rid] }))
      .sort((a, b) => (b.rssi ?? -999) - (a.rssi ?? -999))
      .map(r => {
        const rssi = r.rssi != null ? r.rssi.toFixed(1) + ' dBFS' : '—';
        const frames = Object.values(r.msg_counts || {})
          .reduce((s, n) => s + n, 0);
        return `<div class="rx-row">
          <span class="rx-id">${r.rid}</span>
          <span class="rx-rssi">${rssi}</span>
          <span class="rx-frames">${frames} fr</span>
        </div>`;
      }).join('');
    return `
      <div class="detail-section">
        <h4>RECEIVERS HEARING</h4>
        <div class="rx-list">${rows}</div>
      </div>`;
  }

  // Route enrichment block (adsbdb.com origin/destination).  Returns ''
  // if the aircraft has no route data; both detail-pane modes render it
  // identically inside the header.
  function routeBlock(ac) {
    const r = ac.route;
    if (!r) return '';
    const o = r.origin || {};
    const d = r.destination || {};
    if (!o.iata && !o.icao && !d.iata && !d.icao) return '';
    const oCode = o.iata || o.icao || '???';
    const dCode = d.iata || d.icao || '???';
    const oName = o.municipality || o.name || '';
    const dName = d.municipality || d.name || '';
    const airline = r.airline ? `<div class="airline">${r.airline}</div>` : '';
    return `
      <div class="detail-route">
        <span class="ap" title="${o.name || ''}">${oCode}</span>
        ${oName ? `<span class="ap-name">${oName}</span>` : ''}
        <span class="arrow">→</span>
        <span class="ap" title="${d.name || ''}">${dCode}</span>
        ${dName ? `<span class="ap-name">${dName}</span>` : ''}
        <span class="src-pill">${(r.source || 'route').toUpperCase()}</span>
        ${airline}
      </div>`;
  }

  function fmtMaybe(v, suffix = '', precision = 0) {
    if (v == null) return '—';
    if (typeof v === 'number') {
      return precision ? v.toFixed(precision) + suffix : Math.round(v) + suffix;
    }
    return String(v) + suffix;
  }

  function renderDetail() {
    const ac = state.aircraft.get(state.selectedIcao);
    if (!ac) {
      el.detailContent.innerHTML = '<div class="detail-empty">Click an aircraft to inspect.<br><br>State observed live from 1090 MHz.</div>';
      return;
    }
    if (state.detailMode === 'compact') {
      renderDetailCompact(ac);
    } else {
      renderDetailVerbose(ac);
    }
  }

  function renderDetailVerbose(ac) {
    const info = ac.info || {};
    const op = info.operator;
    const t = info.type;

    // ─── Source attribution ──────────────────────────────────────────
    // Each Mode S field can in principle come from several different
    // downlink messages.  We render one source pill per candidate and
    // mark each one with a class indicating whether that source has
    // actually been heard from this aircraft this session:
    //
    //   .obs  = observed (BDS register actually decoded, or DF received)
    //   .cand = candidate (a possible source per the spec, not yet seen)
    //
    // The user can then see at a glance whether an MCP altitude shown
    // came from BDS 4,0 or from TC=29 — the distinction matters because
    // BDS 4,0 needs Mode S radar coverage and TC=29 needs ADS-B v2.
    //
    // Source key:
    //   TC=N      ADS-B Type Code N (DF17/18 broadcast)
    //   BDS X,Y   Comm-B register decoded from DF20/21
    //   DF4/5/N   Mode S surveillance / Comm-B reply
    //   DF16      ACAS coordination reply
    //   receiver  measured locally (e.g. RSSI)

    const observedBds = new Set(ac.bds_observed || []);
    const observedDfs = new Set(Object.keys(ac.msg_counts || {}).map(Number));

    function isObserved(source) {
      // Match on the source string.  Decide: have we actually heard
      // a frame from this aircraft that could carry this field?
      const s = source.replace(/\s+/g, '');
      // Comm-B register?
      const bdsMatch = s.match(/^BDS([\d,]+)$/);
      if (bdsMatch) return observedBds.has(bdsMatch[1]);
      // ADS-B Type Code?  TC requires DF17/18 to have been received.
      if (/^TC=/.test(s)) return observedDfs.has(17) || observedDfs.has(18);
      // Specific DF?  Match e.g. "DF4", "DF20", "DF4/5", "DF20/21"
      const dfMatch = s.match(/^DF([\d/]+)/);
      if (dfMatch) {
        const dfs = dfMatch[1].split('/').map(Number);
        return dfs.some(d => observedDfs.has(d));
      }
      // "receiver" is always observed
      if (s === 'receiver') return true;
      return false;
    }

    function srcTag(...sources) {
      const pills = sources.map(s => {
        const cls = isObserved(s) ? 'src-pill obs' : 'src-pill cand';
        return `<span class="${cls}">${s}</span>`;
      }).join('');
      return `<span class="src">${pills}</span>`;
    }

    let badges = '';
    if (info.is_military) badges += '<span class="badge badge-mil">MIL</span>';
    if (info.is_pia) badges += '<span class="badge badge-pia">PIA</span>';
    if (info.is_interesting) badges += '<span class="badge badge-int">SPECIAL</span>';

    const tcasBlock = ac.tcas_ra_active ? `
      <div class="detail-section">
        <h4>TCAS RESOLUTION ADVISORY ${srcTag('TC=28 ST=2', 'DF16')}</h4>
        <div class="tcas-summary">
          <div class="ra-cmd">${ac.tcas_ra_summary || ''}</div>
          ${ac.tcas_threat_icao ? `<div style="font-size:10px;color:var(--amber);margin-top:4px;">Threat: ${ac.tcas_threat_icao}</div>` : ''}
        </div>
      </div>` : '';

    el.detailContent.innerHTML = `
      <div class="detail-header">
        <div class="icao-line">
          <span class="icao">${ac.icao}</span>
          <span class="cs">${ac.callsign || '—'}</span>
        </div>
        <div class="reg">
          ${info.registration || '—'}
          <span class="reg-src">${info.registration_source ? '[' + info.registration_source.toUpperCase() + ']' : ''}</span>
        </div>
        <div class="descr">${info.description || (t ? t.manufacturer + ' ' + t.model : '')}</div>
        <div class="meta">
          <span class="country-flag">${info.country_code || '??'}</span>
          ${op ? '<span style="color:var(--fg-bright)">' + op.name + '</span>' : '<span style="color:var(--fg-dim)">unknown operator</span>'}
          ${badges}
        </div>
        ${routeBlock(ac)}
      </div>

      ${tcasBlock}

      <div class="detail-section">
        <h4>POSITION &amp; ALTITUDE</h4>
        <div class="detail-grid">
          <div class="detail-row"><span class="k">LAT</span><span class="v">${ac.lat ? ac.lat.toFixed(4) : '—'}</span>${srcTag('TC=9–22')}</div>
          <div class="detail-row"><span class="k">LON</span><span class="v">${ac.lon ? ac.lon.toFixed(4) : '—'}</span>${srcTag('TC=9–22')}</div>
          <div class="detail-row"><span class="k">BARO</span><span class="v">${fmtMaybe(ac.alt_baro_ft, ' ft')}</span>${srcTag('TC=9–18', 'DF4', 'DF20')}</div>
          <div class="detail-row"><span class="k">GNSS</span><span class="v">${fmtMaybe(ac.alt_gnss_ft, ' ft')}</span>${srcTag('TC=20–22')}</div>
          <div class="detail-row"><span class="k">GROUND</span><span class="v">${ac.on_ground ? 'YES' : 'NO'}</span>${srcTag('TC=31', 'DF4/5/20/21 FS')}</div>
          <div class="detail-row${ac.alert ? ' alert' : ''}"><span class="k">FS</span><span class="v">${ac.flight_status || '—'}</span>${srcTag('DF4/5/20/21')}</div>
          ${ac.squawk ? `<div class="detail-row${['7500','7600','7700'].includes(ac.squawk) ? ' alert' : ''}"><span class="k">SQK</span><span class="v">${ac.squawk}</span>${srcTag('DF5', 'DF21')}</div>` : ''}
          ${ac.spi ? `<div class="detail-row alert"><span class="k">SPI</span><span class="v">ACTIVE</span>${srcTag('DF4/5/20/21 FS')}</div>` : ''}
        </div>
      </div>

      <div class="detail-section">
        <h4>VELOCITY</h4>
        <div class="detail-grid">
          <div class="detail-row"><span class="k">GS</span><span class="v">${fmtMaybe(ac.gs_kt, ' kt')}</span>${srcTag('TC=19 ST=1/2', 'BDS 5,0')}</div>
          <div class="detail-row"><span class="k">TAS</span><span class="v">${fmtMaybe(ac.tas_kt, ' kt')}</span>${srcTag('TC=19 ST=3/4', 'BDS 5,0')}</div>
          <div class="detail-row"><span class="k">IAS</span><span class="v">${fmtMaybe(ac.ias_kt, ' kt')}</span>${srcTag('TC=19 ST=3/4', 'BDS 6,0')}</div>
          <div class="detail-row"><span class="k">MACH</span><span class="v">${ac.mach != null ? ac.mach.toFixed(3) : '—'}</span>${srcTag('BDS 6,0')}</div>
          <div class="detail-row"><span class="k">TRACK</span><span class="v">${fmtHeading(ac.track_deg)}</span>${srcTag('TC=19 ST=1/2', 'BDS 5,0')}</div>
          <div class="detail-row"><span class="k">HDG</span><span class="v">${fmtHeading(ac.heading_deg)}</span>${srcTag('TC=19 ST=3/4', 'BDS 6,0')}</div>
          <div class="detail-row"><span class="k">VRATE</span><span class="v">${fmtMaybe(ac.vrate_fpm, ' fpm')}</span>${srcTag('TC=19', 'BDS 6,0')}</div>
          <div class="detail-row"><span class="k">ROLL</span><span class="v">${ac.roll_deg != null ? ac.roll_deg.toFixed(1) + '°' : '—'}</span>${srcTag('BDS 5,0')}</div>
          <div class="detail-row"><span class="k">TRK RATE</span><span class="v">${ac.track_rate_dps != null ? ac.track_rate_dps.toFixed(2) + '°/s' : '—'}</span>${srcTag('BDS 5,0')}</div>
        </div>
      </div>

      ${(ac.sel_alt_mcp_ft || ac.sel_alt_fms_ft || ac.qnh_mb || ac.selected_heading_deg != null) ? `
      <div class="detail-section">
        <h4>AUTOPILOT INTENT</h4>
        <div class="detail-grid">
          <div class="detail-row"><span class="k">SEL ALT (MCP)</span><span class="v">${fmtMaybe(ac.sel_alt_mcp_ft, ' ft')}</span>${srcTag('TC=29', 'BDS 4,0')}</div>
          <div class="detail-row"><span class="k">SEL ALT (FMS)</span><span class="v">${fmtMaybe(ac.sel_alt_fms_ft, ' ft')}</span>${srcTag('TC=29', 'BDS 4,0')}</div>
          <div class="detail-row"><span class="k">QNH</span><span class="v">${ac.qnh_mb != null ? ac.qnh_mb.toFixed(1) + ' mb' : '—'}</span>${srcTag('TC=29', 'BDS 4,0')}</div>
          <div class="detail-row"><span class="k">SEL HDG</span><span class="v">${fmtHeading(ac.selected_heading_deg)}</span>${srcTag('TC=29')}</div>
        </div>
        ${ac.autopilot_modes && Object.keys(ac.autopilot_modes).length ? `
          <div class="modes-line">
            <span class="modes-lbl">Modes:</span>
            ${fmtModeFlags(ac.autopilot_modes) || '—'}
            ${srcTag('TC=29')}
          </div>` : ''}
        ${ac.autopilot_modes_bds && Object.keys(ac.autopilot_modes_bds).length ? `
          <div class="modes-line modes-bds">
            <span class="modes-lbl">Modes (BDS):</span>
            ${fmtModeFlags(ac.autopilot_modes_bds) || '—'}
            ${srcTag('BDS 4,0')}
          </div>` : ''}
      </div>` : ''}

      ${(ac.wind_speed_kt || ac.static_air_temp_c) ? `
      <div class="detail-section">
        <h4>METEOROLOGY ${srcTag('BDS 4,4')}</h4>
        <div class="detail-grid">
          <div class="detail-row"><span class="k">WIND</span><span class="v">${fmtHeading(ac.wind_direction_deg)} / ${fmtMaybe(ac.wind_speed_kt, 'kt')}</span></div>
          <div class="detail-row"><span class="k">SAT</span><span class="v">${ac.static_air_temp_c != null ? ac.static_air_temp_c.toFixed(1) + '°C' : '—'}</span></div>
        </div>
      </div>` : ''}

      <div class="detail-section">
        <h4>SURVEILLANCE QUALITY</h4>
        <div class="detail-grid">
          <div class="detail-row${staleLevel(ageOf(ac)) ? ' alert' : ''}"><span class="k">LAST SEEN</span><span class="v">${fmtAge(ageOf(ac))} ago</span>${srcTag('receiver')}</div>
          <div class="detail-row"><span class="k">RSSI</span><span class="v">${ac.rssi != null ? ac.rssi.toFixed(1) + ' dBFS' : '—'}</span>${srcTag('receiver')}</div>
          <div class="detail-row"><span class="k">ADS-B v</span><span class="v">${ac.adsb_version != null ? 'v' + ac.adsb_version : '—'}</span>${srcTag('TC=31')}</div>
          <div class="detail-row"><span class="k">NIC</span><span class="v">${fmtMaybe(ac.nic)}</span>${srcTag('TC=31')}</div>
          <div class="detail-row"><span class="k">NACp</span><span class="v">${fmtMaybe(ac.nac_p)}</span>${srcTag('TC=31')}</div>
          <div class="detail-row"><span class="k">SIL</span><span class="v">${fmtMaybe(ac.sil)}</span>${srcTag('TC=31')}</div>
          <div class="detail-row"><span class="k">CAT</span><span class="v">${ac.category || '—'}</span>${srcTag('TC=1–4')}</div>
        </div>
      </div>

      <div class="detail-section">
        <h4>BDS REGISTERS OBSERVED <span style="color:var(--fg-dim);font-size:9px;letter-spacing:0.05em;font-weight:400;text-transform:none;">(this session)</span></h4>
        <div class="bds-pills">
          ${(ac.bds_observed || []).length ? ac.bds_observed.map(b => `<span class="bds-pill">${b}</span>`).join('') : '<span style="color:var(--fg-dim);font-size:10px">none</span>'}
        </div>
      </div>

      <div class="detail-section">
        <h4>MESSAGE COUNTS BY DF</h4>
        <div class="df-counts">
          ${Object.entries(ac.msg_counts || {}).sort((a,b) => +a[0] - +b[0]).map(([df, n]) =>
            `<span class="df-pill">DF${df}=<span class="n">${n}</span></span>`).join('')}
        </div>
      </div>

      ${receiversBlock(ac)}
      ${aircraftEventsBlock(ac)}
    `;
  }

  // Compact detail renderer.  Same data as the verbose pane, but with
  // a denser two-column key/value grid per section and no per-field
  // source-provenance pills (those live in the VERBOSE view).
  function renderDetailCompact(ac) {
    const info = ac.info || {};
    const op = info.operator;
    const t = info.type;

    let badges = '';
    if (info.is_military) badges += '<span class="badge badge-mil">MIL</span>';
    if (info.is_pia) badges += '<span class="badge badge-pia">PIA</span>';
    if (info.is_interesting) badges += '<span class="badge badge-int">SPECIAL</span>';

    function row(k, v) {
      return `<div class="detail-row"><span class="k">${k}</span><span class="v">${v}</span></div>`;
    }
    function rowAlert(k, v, alertOn) {
      return `<div class="detail-row${alertOn ? ' alert' : ''}"><span class="k">${k}</span><span class="v">${v}</span></div>`;
    }

    const tcasBlock = ac.tcas_ra_active ? `
      <div class="detail-section">
        <h4>TCAS RESOLUTION ADVISORY</h4>
        <div class="tcas-summary">
          <div class="ra-cmd">${ac.tcas_ra_summary || ''}</div>
          ${ac.tcas_threat_icao ? `<div style="font-size:10px;color:var(--amber);margin-top:4px;">Threat: ${ac.tcas_threat_icao}</div>` : ''}
        </div>
      </div>` : '';

    // Combine vrate arrow into altitude line
    const altLine = ac.alt_baro_ft != null
      ? `${fmtFL(ac.alt_baro_ft)}${fmtVrate(ac.vrate_fpm)}`
      : '—';

    const positionRows = [
      row('LAT',    ac.lat != null ? ac.lat.toFixed(4) : '—'),
      row('LON',    ac.lon != null ? ac.lon.toFixed(4) : '—'),
      row('ALT',    altLine),
      row('GNSS',   fmtMaybe(ac.alt_gnss_ft, ' ft')),
      row('GROUND', ac.on_ground ? 'YES' : 'NO'),
      rowAlert('FS', ac.flight_status || '—', !!ac.alert),
    ];
    if (ac.squawk) {
      positionRows.push(rowAlert('SQK', ac.squawk, ['7500','7600','7700'].includes(ac.squawk)));
    }
    if (ac.spi) positionRows.push(rowAlert('SPI', 'ACTIVE', true));

    const velocityRows = [
      row('GS',       fmtMaybe(ac.gs_kt, ' kt')),
      row('TAS',      fmtMaybe(ac.tas_kt, ' kt')),
      row('IAS',      fmtMaybe(ac.ias_kt, ' kt')),
      row('MACH',     ac.mach != null ? ac.mach.toFixed(3) : '—'),
      row('TRACK',    fmtHeading(ac.track_deg)),
      row('HDG',      fmtHeading(ac.heading_deg)),
      row('VRATE',    fmtMaybe(ac.vrate_fpm, ' fpm')),
      row('ROLL',     ac.roll_deg != null ? ac.roll_deg.toFixed(1) + '°' : '—'),
    ];

    const hasAutopilot = ac.sel_alt_mcp_ft || ac.sel_alt_fms_ft || ac.qnh_mb || ac.selected_heading_deg != null;
    const apModesStr = fmtModeFlags(ac.autopilot_modes);
    const apModesBdsStr = fmtModeFlags(ac.autopilot_modes_bds);
    const apBlock = hasAutopilot ? `
      <div class="detail-section">
        <h4>AUTOPILOT INTENT</h4>
        <div class="detail-grid cols-2">
          ${row('SEL ALT (MCP)', fmtMaybe(ac.sel_alt_mcp_ft, ' ft'))}
          ${row('SEL ALT (FMS)', fmtMaybe(ac.sel_alt_fms_ft, ' ft'))}
          ${row('SEL HDG', fmtHeading(ac.selected_heading_deg))}
          ${row('QNH', ac.qnh_mb != null ? ac.qnh_mb.toFixed(1) + ' mb' : '—')}
        </div>
        ${apModesStr ? `<div class="modes-line"><span class="modes-lbl">Modes:</span> ${apModesStr}</div>` : ''}
        ${apModesBdsStr ? `<div class="modes-line modes-bds"><span class="modes-lbl">Modes (BDS):</span> ${apModesBdsStr}</div>` : ''}
      </div>` : '';

    const hasMet = ac.wind_speed_kt || ac.static_air_temp_c;
    const metBlock = hasMet ? `
      <div class="detail-section">
        <h4>METEOROLOGY</h4>
        <div class="detail-grid cols-2">
          ${row('WIND', `${fmtHeading(ac.wind_direction_deg)} / ${fmtMaybe(ac.wind_speed_kt, 'kt')}`)}
          ${row('SAT', ac.static_air_temp_c != null ? ac.static_air_temp_c.toFixed(1) + '°C' : '—')}
        </div>
      </div>` : '';

    const ageNow = ageOf(ac);
    const linkRows = [
      rowAlert('LAST', fmtAge(ageNow) + ' ago', !!staleLevel(ageNow)),
      row('RSSI',   ac.rssi != null ? ac.rssi.toFixed(1) + ' dBFS' : '—'),
      row('ADS-B',  ac.adsb_version != null ? 'v' + ac.adsb_version : '—'),
      row('NIC',    fmtMaybe(ac.nic)),
      row('NACp',   fmtMaybe(ac.nac_p)),
      row('SIL',    fmtMaybe(ac.sil)),
      row('CAT',    ac.category || '—'),
    ];

    const bdsCount = (ac.bds_observed || []).length;
    const dfCount = Object.values(ac.msg_counts || {}).reduce((a,b) => a + b, 0);

    el.detailContent.innerHTML = `
      <div class="detail-header">
        <div class="icao-line">
          <span class="icao">${ac.icao}</span>
          <span class="cs">${ac.callsign || '—'}</span>
        </div>
        <div class="reg">
          ${info.registration || '—'}
          <span class="reg-src">${info.registration_source ? '[' + info.registration_source.toUpperCase() + ']' : ''}</span>
        </div>
        <div class="descr">${info.description || (t ? t.manufacturer + ' ' + t.model : '')}</div>
        <div class="meta">
          <span class="country-flag">${info.country_code || '??'}</span>
          ${op ? '<span style="color:var(--fg-bright)">' + op.name + '</span>' : '<span style="color:var(--fg-dim)">unknown operator</span>'}
          ${badges}
        </div>
        ${routeBlock(ac)}
      </div>

      ${tcasBlock}

      <div class="detail-section">
        <h4>POSITION &amp; ALTITUDE</h4>
        <div class="detail-grid cols-2">${positionRows.join('')}</div>
      </div>

      <div class="detail-section">
        <h4>VELOCITY</h4>
        <div class="detail-grid cols-2">${velocityRows.join('')}</div>
      </div>

      ${apBlock}
      ${metBlock}

      <div class="detail-section">
        <h4>LINK QUALITY</h4>
        <div class="detail-grid cols-2">${linkRows.join('')}</div>
      </div>

      ${receiversBlock(ac)}
      ${aircraftEventsBlock(ac)}

      <div class="detail-section detail-footer">
        <span class="detail-footer-item">BDS <span class="n">${bdsCount}</span></span>
        <span class="detail-footer-item">FRAMES <span class="n">${dfCount}</span></span>
      </div>
    `;
  }

  function selectAircraft(icao) {
    state.selectedIcao = icao;
    renderList();
    renderDetail();
    // Mark on map
    for (const [k, m] of markers) {
      const ac = state.aircraft.get(k);
      if (ac) updateMarker(ac);
    }
    const ac = state.aircraft.get(icao);
    if (ac && ac.lat != null && ac.lon != null) {
      map.panTo([ac.lat, ac.lon], { animate: true, duration: 0.5 });
    }
  }

  // ─── Event log ──────────────────────────────────────────────────────

  function pushEvent(ev) {
    const li = document.createElement('li');
    const t = new Date((ev.t || Date.now()/1000) * 1000)
      .toISOString().substr(11, 8);
    let cls = '';
    let msg = '';
    if (ev.type === 'new_aircraft') {
      msg = `NEW ${ev.icao}`;
    } else if (ev.type === 'tcas_ra_started') {
      cls = 'ev-tcas';
      msg = `RA ${ev.callsign || ev.icao}: ${ev.summary}` +
            (ev.threat_icao ? ` (threat ${ev.threat_icao})` : '') +
            ` [${ev.source}]`;
      // Also push to RA timeline
      state.raEvents.push({
        icao: ev.icao,
        callsign: ev.callsign,
        started_at: ev.t,
        summary: ev.summary,
        threat_icao: ev.threat_icao,
        source: ev.source,
        ended_at: null,
      });
      renderRaTimeline();
    } else if (ev.type === 'tcas_ra_ended') {
      cls = 'ev-tcas';
      msg = `RA END ${ev.icao}: ${ev.summary || ''}`;
      // Mark the latest matching RA as ended
      for (let i = state.raEvents.length - 1; i >= 0; i--) {
        if (state.raEvents[i].icao === ev.icao && state.raEvents[i].ended_at == null) {
          state.raEvents[i].ended_at = ev.t;
          break;
        }
      }
      renderRaTimeline();
    } else if (ev.type === 'emergency') {
      cls = 'ev-emerg';
      msg = `EMERGENCY ${ev.icao}: ${ev.state}`;
    } else if (ev.type === 'intent_change') {
      cls = 'ev-intent ev-intent-' + (ev.subtype || 'misc');
      const who = ev.callsign || ev.icao;
      const src = ev.source ? ` <span class="ev-src">[${ev.source}]</span>` : '';
      msg = `${who} · ${ev.summary || ''}${src}`;
    } else {
      msg = JSON.stringify(ev);
    }
    // Make rows clickable when they reference a known aircraft, so the
    // user can jump straight from a ticker entry to the detail pane.
    if (ev.icao) {
      cls += (cls ? ' ' : '') + 'ev-clickable';
      li.dataset.icao = ev.icao;
      li.addEventListener('click', () => selectAircraft(li.dataset.icao));
    }
    li.className = cls;
    li.innerHTML = `<span class="ev-t">${t}</span><span class="ev-msg">${msg}</span>`;
    el.eventLog.insertBefore(li, el.eventLog.firstChild);
    while (el.eventLog.children.length > 200) {
      el.eventLog.removeChild(el.eventLog.lastChild);
    }
    // Mirror into state.events so the per-aircraft EVENTS block in the
    // detail pane can filter by ICAO.  Trim to the configured cap.
    state.events.push(ev);
    if (state.events.length > state.eventsMax) {
      state.events.splice(0, state.events.length - state.eventsMax);
    }
    // If the event references the currently-selected aircraft, refresh
    // the detail pane so the per-aircraft EVENTS section picks it up.
    // (A normal `update` only fires on state changes; pure events
    // wouldn't otherwise trigger a redraw.)
    if (state.selectedIcao && (
          ev.icao === state.selectedIcao ||
          ev.threat_icao === state.selectedIcao)) {
      renderDetail();
    }
  }

  // ─── RA timeline ────────────────────────────────────────────────────

  function renderRaTimeline() {
    el.raCount.textContent = state.raEvents.length === 0 ? '0 events' :
      `${state.raEvents.length} event${state.raEvents.length === 1 ? '' : 's'}`;
    if (state.raEvents.length === 0) {
      el.raTimeline.innerHTML = '<div class="ra-empty">No resolution advisories observed.</div>';
      return;
    }
    // Newest first
    el.raTimeline.innerHTML = state.raEvents.slice().reverse().map(ev => {
      const when = new Date((ev.started_at || Date.now()/1000) * 1000)
        .toISOString().substr(11, 8);
      const dur = ev.ended_at ? ` (${(ev.ended_at - ev.started_at).toFixed(1)}s)` :
                                 ' (active)';
      return `<div class="ra-event ev-clickable" data-icao="${ev.icao || ''}">
        <span class="ra-when">${when}${dur}</span>
        <span class="ra-icao">${ev.callsign || ev.icao}</span>
        <span class="ra-cmd">${ev.summary || ''}</span>
        <span class="ra-threat">vs ${ev.threat_icao || '?'}</span>
        <span class="ra-source">${ev.source || ''}</span>
      </div>`;
    }).join('');
    el.raTimeline.querySelectorAll('.ra-event[data-icao]').forEach(row => {
      const icao = row.dataset.icao;
      if (!icao) return;
      row.addEventListener('click', () => selectAircraft(icao));
    });
  }

  // ─── Stats display ──────────────────────────────────────────────────

  function fmtUptime(s) {
    if (!s) return '—';
    s = Math.floor(s);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h) return `${h}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;
    return `${m}:${String(sec).padStart(2,'0')}`;
  }

  function updateStats(s) {
    if (!s) return;
    Object.assign(state.stats, s);
    el.statUptime.textContent = fmtUptime(s.uptime_s);
    el.statFrames.textContent = (s.total_frames || 0).toLocaleString();
    el.statDrop.textContent = s.frames_dropped || 0;
    el.statAircraft.textContent = s.active_aircraft || 0;

    // Frame rate over the last 5s
    const now = Date.now();
    state.frameTimes.push([now, s.total_frames || 0]);
    while (state.frameTimes.length > 0 && now - state.frameTimes[0][0] > 5000) {
      state.frameTimes.shift();
    }
    if (state.frameTimes.length >= 2) {
      const [t0, f0] = state.frameTimes[0];
      const [tn, fn] = state.frameTimes[state.frameTimes.length - 1];
      const dt = (tn - t0) / 1000;
      const rate = dt > 0 ? Math.round((fn - f0) / dt) : 0;
      el.statRate.textContent = `${rate}/s`;
    }
  }

  function updateRxInfo(rx) {
    if (!rx) return;
    state.receiver = rx;
    if (rx.lat != null && rx.lon != null) {
      el.rxInfo.innerHTML =
        `RX: ${rx.lat.toFixed(3)}, ${rx.lon.toFixed(3)} · range ${rx.max_range_nm}NM`;
      // Recentre on the primary receiver on first sight.
      if (!state.receiverCentred) {
        map.setView([rx.lat, rx.lon], 8);
        state.receiverCentred = true;
      }
    } else {
      el.rxInfo.textContent = 'RX: position not configured';
    }
  }

  // Render every registered receiver's range ring on the map.  Each
  // ring is colour-coded so multi-receiver setups can tell at a glance
  // which area is covered by which feed.  Disconnected receivers are
  // drawn dimmer.
  const RX_RING_COLOURS = ['#79ddc1', '#5ae0ff', '#f5b83d', '#d077ff', '#b8f5d0'];

  function updateReceivers(receivers) {
    state.receivers = receivers || [];
    rangeRingLayer.clearLayers();
    let connected = 0;
    state.receivers.forEach((rx, i) => {
      if (rx.connected) connected++;
      if (rx.lat == null || rx.lon == null) return;
      const colour = RX_RING_COLOURS[i % RX_RING_COLOURS.length];
      L.circle([rx.lat, rx.lon], {
        radius: rx.max_range_nm * 1852,
        className: 'range-ring',
        color: colour,
        weight: 1,
        opacity: rx.connected ? 0.6 : 0.2,
        dashArray: '1 4',
        fill: false,
        interactive: false,
      }).addTo(rangeRingLayer);
      // Tiny RX marker at the centre, with the receiver name on hover.
      L.circleMarker([rx.lat, rx.lon], {
        radius: 3,
        color: colour,
        fillColor: colour,
        fillOpacity: 0.6,
        weight: 1,
        interactive: true,
      }).bindTooltip(`${rx.name || rx.id} (${rx.connected ? 'on' : 'off'})`,
                      { permanent: false, direction: 'top' })
        .addTo(rangeRingLayer);
    });
    // Topbar RECEIVERS stat: only relevant when more than one
    // receiver is configured (single-RX setups already see LINK).
    if (state.receivers.length > 1) {
      el.statRx.hidden = false;
      el.statRx.querySelector('.val').textContent =
        `${connected}/${state.receivers.length}`;
    } else {
      el.statRx.hidden = true;
    }
  }

  // ─── WebSocket protocol handler ─────────────────────────────────────

  function handleSnapshot(msg) {
    state.aircraft.clear();
    for (const ac of msg.aircraft || []) state.aircraft.set(ac.icao, ac);
    if (msg.receiver) updateRxInfo(msg.receiver);
    if (msg.receivers) updateReceivers(msg.receivers);
    if (msg.stats) updateStats(msg.stats);
    if (msg.config) applyConfig(msg.config);
    if (msg.tcas_events) {
      // Replace; the snapshot is authoritative
      state.raEvents = msg.tcas_events.map(e => ({
        icao: e.icao,
        callsign: state.aircraft.get(e.icao)?.callsign,
        started_at: e.started_at,
        ended_at: e.ended_at,
        summary: e.summary,
        threat_icao: e.threat_icao,
        source: e.source,
      }));
      renderRaTimeline();
    }
    // Replay any persisted ticker events so the per-aircraft EVENTS
    // block in the detail pane has history immediately on connect,
    // not just from this point forward.  Snapshot lists oldest-first.
    if (Array.isArray(msg.events)) {
      state.events = msg.events.slice(-state.eventsMax);
    }
    rerenderAll();
  }

  function handleUpdates(msg) {
    for (const ac of msg.aircraft || []) {
      const prev = state.aircraft.get(ac.icao);
      // Preserve trail across updates (engine sends full record but trail
      // is also there)
      state.aircraft.set(ac.icao, ac);
      updateMarker(ac);
      updateTrail(ac);
    }
    updateTcasLinks();
    renderList();
    if (state.selectedIcao && msg.aircraft.some(a => a.icao === state.selectedIcao)) {
      renderDetail();
    }
  }

  function rerenderAll() {
    aircraftLayer.clearLayers();
    trailLayer.clearLayers();
    markers.clear();
    trails.clear();
    for (const ac of state.aircraft.values()) {
      updateMarker(ac);
      updateTrail(ac);
    }
    updateTcasLinks();
    renderList();
    renderDetail();
  }

  function handleEvent(env) {
    pushEvent(env.event);
  }

  // ─── WebSocket connection lifecycle ─────────────────────────────────

  let ws = null;
  let reconnectTimer = null;

  function connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    // Default WS port is 8765; can be overridden in URL ?ws=...
    const params = new URLSearchParams(location.search);
    const wsUrl = params.get('ws') || `${proto}//${location.hostname}:8765/`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      console.log('WS connected to', wsUrl);
      el.connStat.classList.add('ok');
      el.connStat.querySelector('.val').textContent = 'CONNECTED';
    };

    ws.onclose = () => {
      console.log('WS disconnected; will retry');
      el.connStat.classList.remove('ok');
      el.connStat.querySelector('.val').textContent = 'DISCONN';
      reconnectTimer = setTimeout(connect, 2000);
    };

    ws.onerror = (e) => { console.warn('WS error', e); };

    ws.onmessage = (msg) => {
      let env;
      try { env = JSON.parse(msg.data); }
      catch (e) { console.warn('Bad WS message', e); return; }
      switch (env.type) {
        case 'snapshot':
          handleSnapshot(env);
          break;
        case 'updates':
          handleUpdates(env);
          break;
        case 'update':
          // Single-aircraft update form
          handleUpdates({ aircraft: [env.data] });
          break;
        case 'event':
          handleEvent(env);
          break;
        case 'config':
          applyConfig(env.config);
          break;
        default:
          // Unknown/forward-compat
          break;
      }
    };
  }

  connect();

  // ─── Staleness ticker ───────────────────────────────────────────────
  // No new frames are needed to *advance* the LAST column or to dim the
  // map markers — those are derived purely from wall-clock time vs each
  // aircraft's `last_seen`.  Re-render the list and the marker icons
  // every 2 seconds so the UI doesn't claim a contact is "5s old" when
  // it's actually been silent for two minutes.
  setInterval(() => {
    renderList();
    if (state.selectedIcao && state.aircraft.has(state.selectedIcao)) {
      renderDetail();
    }
    // Marker icons: re-icon only when the staleness tier has actually
    // changed since the last paint — avoid pointless full rebuilds.
    if (!markers || !markers.size) return;
    for (const [icao, m] of markers) {
      const ac = state.aircraft.get(icao);
      if (!ac) continue;
      const want = staleLevel(ageOf(ac));
      const have = (m._sw_stale === undefined) ? null : m._sw_stale;
      if (want !== have) {
        m._sw_stale = want;
        updateMarker(ac);
      }
    }
  }, 2000);
})();
