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
    // Live ticker filter: 'all' | 'adsb' | 'vdl2'.  Persisted across
    // reloads.  state.events still records every event regardless of
    // the filter — this only changes which ones land in the DOM.
    eventFilter: (() => {
      const v = localStorage.getItem('skywatch.eventFilter');
      return (v === 'all' || v === 'adsb' || v === 'vdl2') ? v : 'all';
    })(),
    stats: {},
    lastFrameSample: null,   // [ts_ms, total_frames] of the last stats payload
    filterText: '',
    // When true, the map shows ONLY the currently-selected aircraft —
    // all other markers, trails, and TCAS links are hidden so the
    // operator can study one target without visual noise.  Session
    // only; not persisted.
    isolateSelected: false,
    // Validated against a known-good set: an earlier bug let the
    // string "undefined" land here, which would otherwise stick on
    // reload (truthy, but not equal to any handled mode) and pin
    // the pane in verbose forever.
    detailMode: (() => {
      const v = localStorage.getItem('skywatch.detailMode');
      return (v === 'compact' || v === 'verbose') ? v : 'compact';
    })(),
    config: { route_enrichment: false, route_enrichment_available: false },
    // Map-marker label fields.  Persisted per-browser.  Default mirrors
    // the original (callsign + FL + V/S + GS) so existing users see no
    // change after upgrading.
    labelFields: new Set(JSON.parse(
      localStorage.getItem('skywatch.labelFields') ||
      '["callsign","fl","vrate","gs"]')),
    // Airport / runway dataset (OurAirports).  Loaded once on connect;
    // the bundled seed has ~60 hubs.  Run `python -m skywatch.airports.fetch`
    // for the canonical full dataset.
    airports: [],
    runways: [],
    airportsOn: localStorage.getItem('skywatch.airports') !== '0',
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
    statVdl2: document.getElementById('stat-vdl2'),
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
  // Selector is scoped to buttons with a `data-mode` attribute so it
  // doesn't catch siblings that share the .detail-mode-btn styling
  // (e.g. ISOLATE) — those have their own click handlers.
  document.querySelectorAll('.detail-mode-btn[data-mode]').forEach(btn => {
    if (btn.dataset.mode === state.detailMode) btn.classList.add('active');
    btn.addEventListener('click', () => {
      if (btn.dataset.mode === state.detailMode) return;
      state.detailMode = btn.dataset.mode;
      localStorage.setItem('skywatch.detailMode', state.detailMode);
      document.querySelectorAll('.detail-mode-btn[data-mode]').forEach(b =>
        b.classList.toggle('active', b.dataset.mode === state.detailMode));
      renderDetail();
    });
  });

  // ─── Map ────────────────────────────────────────────────────────────

  const map = L.map('map', {
    center: [51.4775, -0.4614],
    zoom: 8,
    // Default zoom-control position is topleft, which collides with
    // the legend / map-label panel.  Move to topright (the right
    // edge of the map pane abuts the list pane, so this stays inside
    // the map area without overlapping any other UI).
    zoomControl: false,
    attributionControl: true,
    preferCanvas: false,
    worldCopyJump: true,
  });
  L.control.zoom({ position: 'topright' }).addTo(map);

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
  // Airport markers and runway polygons sit BELOW the aircraft layer
  // so a marker never occludes traffic; we add them to the map last,
  // but Leaflet z-orders by addition order.  The CSS z-index below
  // makes that explicit.
  const airportLayer = L.layerGroup();
  const runwayLayer = L.layerGroup();

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

  // Map visibility predicate.  When `isolateSelected` is on, only the
  // currently-selected aircraft renders on the map (markers, trail,
  // and any TCAS-link line).  All other state — list, detail pane,
  // events ticker — keeps showing every aircraft as normal.
  function isVisibleOnMap(ac) {
    if (!state.isolateSelected) return true;
    return state.selectedIcao && ac.icao === state.selectedIcao;
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
    // Honour the isolate-selected toggle: aircraft that should be
    // hidden are removed from the layer rather than just dimmed.
    if (!isVisibleOnMap(ac)) {
      if (existing) {
        aircraftLayer.removeLayer(existing);
        markers.delete(ac.icao);
      }
      return;
    }
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
    followIfIsolated(ac);
  }

  // Follow-mode: when ISOLATE is active and this update is for the
  // selected aircraft, pan the map to keep it on-screen.  Zoom level
  // is preserved.  Animation is short so a stream of updates feels
  // smooth rather than jerky.
  function followIfIsolated(ac) {
    if (!state.isolateSelected) return;
    if (!state.selectedIcao || ac.icao !== state.selectedIcao) return;
    if (ac.lat == null || ac.lon == null) return;
    map.panTo([ac.lat, ac.lon], { animate: true, duration: 0.4 });
  }

  function updateTrail(ac) {
    const existing = trails.get(ac.icao);
    if (!isVisibleOnMap(ac)) {
      if (existing) {
        trailLayer.removeLayer(existing);
        trails.delete(ac.icao);
      }
      return;
    }
    if (!ac.trail || ac.trail.length < 2) return;
    const latlngs = ac.trail.map(pt => [pt[1], pt[2]]);
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
      // In isolate mode, only render a TCAS link if the selected
      // aircraft is one of the two ends.
      if (state.isolateSelected &&
          a.icao !== state.selectedIcao &&
          b.icao !== state.selectedIcao) continue;
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
    } else if (ev.type === 'cpdlc_msg') {
      cls += ' ev-cpdlc';
      const arrow = ev.direction === 'uplink' ? '▲' :
                    ev.direction === 'downlink' ? '▼' : '◆';
      msg = `CPDLC ${arrow} ${ev.summary || ''}`;
    } else if (ev.type === 'acars_msg') {
      cls += ' ev-acars';
      const arrow = ev.direction === 'uplink' ? '▲' :
                    ev.direction === 'downlink' ? '▼' : '◆';
      msg = `ACARS ${arrow} ${ev.label || ''} · ${ev.summary || ''}`;
    } else if (ev.type === 'vdl2_link') {
      cls += ' ev-vdl2';
      msg = `VDL2 · ${ev.summary || ''}`;
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

  // ACARS H1 wind/temperature grid heuristic.
  //
  // Many airlines downlink periodic wind/temperature observations
  // along the route as ACARS H1 (free-text AOC) messages.  The body
  // is airline-proprietary but follows a recognisable pattern:
  // sequences of "+<offset> <a> <b> <c> <d>-<altitude> <±N>DC" rows
  // separated by whitespace.  When we see ≥ 2 of these rows, render
  // the body as a small table instead of a wrapped run-on string.
  //
  // The columns are airline-dependent — typical pairs are wind
  // direction/speed and aircraft heading/track — but we don't try
  // to label them beyond "p1..p4" because the meaning varies and
  // mis-labelling would be worse than not labelling.
  const _H1_ROW_RE = /\+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)-(\d+)\s+([+-]?\d+)DC/g;
  function tryParseH1Wind(text) {
    if (!text) return null;
    const rows = [];
    let m;
    _H1_ROW_RE.lastIndex = 0;
    while ((m = _H1_ROW_RE.exec(text)) !== null) {
      rows.push({
        offset: m[1], p1: m[2], p2: m[3], p3: m[4], p4: m[5],
        alt: m[6], temp: m[7],
      });
    }
    return rows.length >= 2 ? rows : null;
  }

  function renderH1WindTable(rows) {
    const fmtAlt = a => 'FL' + String(Math.round(parseInt(a, 10) / 100)).padStart(3, '0');
    const body = rows.map(r =>
      `<tr>
        <td>+${r.offset}</td>
        <td>${r.p1}</td><td>${r.p2}</td>
        <td>${r.p3}</td><td>${r.p4}</td>
        <td class="alt">${fmtAlt(r.alt)}</td>
        <td class="sat">${parseInt(r.temp, 10)}°C</td>
      </tr>`).join('');
    return `
      <table class="h1-wind-table">
        <thead><tr>
          <th>Δ</th><th>p1</th><th>p2</th><th>p3</th><th>p4</th>
          <th>FL</th><th>SAT</th>
        </tr></thead>
        <tbody>${body}</tbody>
      </table>`;
  }

  // COMMS block — VDL2 / CPDLC / ACARS messages exchanged with this
  // aircraft.  Renders only when the aircraft has comms data; nothing
  // shown for receive-only-1090 deployments.  Newest-first.
  function commsBlock(ac) {
    const list = ac.comms || [];
    if (!list.length) return '';
    const rows = list.slice().reverse().map(c => {
      const t = new Date((c.ts || Date.now() / 1000) * 1000)
        .toISOString().substr(11, 8);
      const arrow = c.direction === 'uplink' ? '▲' :
                    c.direction === 'downlink' ? '▼' : '◆';
      const cls = 'comms-row comms-' + (c.kind || 'other');
      const label = c.label ? `<span class="c-label">${c.label}</span>` : '';
      // Block-count indicator for reassembled multi-block ACARS
      // messages.  "n parts" if >1; nothing if single-block.
      const blockBadge = (c.blocks && c.blocks > 1)
        ? ` <span class="c-blocks" title="Reassembled from ${c.blocks} ACARS blocks${c.complete === false ? ' (more pending)' : ''}">×${c.blocks}${c.complete === false ? '…' : ''}</span>`
        : '';
      // H1 wind/temp heuristic — when the body matches the regular
      // wind-aloft pattern, render a small table.  Otherwise show
      // the raw text as before.
      let bodyHtml = '';
      if (c.kind === 'acars' && c.label === 'H1') {
        const wind = tryParseH1Wind(c.text);
        if (wind) {
          bodyHtml = renderH1WindTable(wind);
        }
      }
      if (!bodyHtml) {
        bodyHtml = c.text
          ? `<span class="c-text">${c.text}</span>`
          : '';
      }
      return `<div class="${cls}">
        <span class="c-t">${t}</span>
        <span class="c-arrow">${arrow}</span>
        ${label}${blockBadge}
        ${bodyHtml}
      </div>`;
    }).join('');
    return `
      <div class="detail-section">
        <h4>COMMS <span class="c-count">${list.length}</span></h4>
        <div class="comms-list">${rows}</div>
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
      ${commsBlock(ac)}
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
      ${commsBlock(ac)}
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
    refreshIsolateButton();
    // Re-evaluate every aircraft's marker.  Iterates state.aircraft
    // (not just the existing markers map) so that turning the
    // selection ON while in isolate mode adds the newly-selected
    // aircraft back even if it had been removed.
    for (const ac of state.aircraft.values()) {
      updateMarker(ac);
      updateTrail(ac);
    }
    updateTcasLinks();
    const ac = state.aircraft.get(icao);
    if (ac && ac.lat != null && ac.lon != null) {
      map.panTo([ac.lat, ac.lon], { animate: true, duration: 0.5 });
    }
  }

  // ─── Isolate-selected toggle ────────────────────────────────────────
  // Hides every aircraft on the map except the currently-selected one.
  // List + detail pane + ticker stay normal; this is purely a map-clutter
  // reducer.
  const isolateBtn = document.getElementById('isolate-btn');

  function refreshIsolateButton() {
    isolateBtn.disabled = !state.selectedIcao;
    isolateBtn.classList.toggle('active', state.isolateSelected);
  }

  isolateBtn.addEventListener('click', () => {
    if (!state.selectedIcao) return;
    state.isolateSelected = !state.isolateSelected;
    refreshIsolateButton();
    // Walk every aircraft so markers/trails come or go to match the
    // new mode.  Cheap: bounded by len(aircraft) ≤ ~1000.
    for (const ac of state.aircraft.values()) {
      updateMarker(ac);
      updateTrail(ac);
    }
    updateTcasLinks();
    // Immediately re-centre on the isolated aircraft so the user
    // doesn't have to wait for the next position update for follow
    // mode to kick in.
    if (state.isolateSelected) {
      const ac = state.aircraft.get(state.selectedIcao);
      if (ac) followIfIsolated(ac);
    }
  });
  refreshIsolateButton();

  // ─── Event log ──────────────────────────────────────────────────────

  // Event-type families.  ADSB family is everything sourced from the
  // 1090 MHz ingest path (TCAS RA, intent change, emergency, new
  // contacts).  VDL2 family is everything from the 136 MHz ingest
  // path (CPDLC, ACARS, link mgmt).  Anything not classified as VDL2
  // is treated as ADSB so that future engine event types appear in
  // the default-on ADSB filter rather than vanishing.
  const _VDL2_EVENT_TYPES = new Set([
    'cpdlc_msg', 'acars_msg', 'vdl2_link', 'vdl2_msg',
  ]);
  function eventFamily(ev) {
    return _VDL2_EVENT_TYPES.has(ev && ev.type) ? 'vdl2' : 'adsb';
  }
  function eventPassesFilter(ev) {
    if (state.eventFilter === 'all') return true;
    return eventFamily(ev) === state.eventFilter;
  }

  // Build the <li> for one event.  Extracted so applyEventFilter()
  // can rebuild the whole ticker DOM from `state.events` without
  // duplicating the per-type rendering logic.
  function buildEventLi(ev) {
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
    } else if (ev.type === 'tcas_ra_ended') {
      cls = 'ev-tcas';
      msg = `RA END ${ev.icao}: ${ev.summary || ''}`;
    } else if (ev.type === 'emergency') {
      cls = 'ev-emerg';
      msg = `EMERGENCY ${ev.icao}: ${ev.state}`;
    } else if (ev.type === 'intent_change') {
      cls = 'ev-intent ev-intent-' + (ev.subtype || 'misc');
      const who = ev.callsign || ev.icao;
      const src = ev.source ? ` <span class="ev-src">[${ev.source}]</span>` : '';
      msg = `${who} · ${ev.summary || ''}${src}`;
    } else if (ev.type === 'cpdlc_msg') {
      cls = 'ev-cpdlc';
      const who = ev.callsign || ev.icao || '?';
      const arrow = ev.direction === 'uplink' ? '▲' :
                    ev.direction === 'downlink' ? '▼' : '◆';
      msg = `${who} CPDLC ${arrow} ${ev.summary || ''}`;
    } else if (ev.type === 'acars_msg') {
      cls = 'ev-acars';
      const who = ev.callsign || ev.icao || '?';
      const arrow = ev.direction === 'uplink' ? '▲' :
                    ev.direction === 'downlink' ? '▼' : '◆';
      msg = `${who} ACARS ${arrow} ${ev.label || ''} · ${ev.summary || ''}`;
    } else if (ev.type === 'vdl2_link') {
      cls = 'ev-vdl2';
      msg = `${ev.icao || '?'} VDL2 · ${ev.summary || ''}`;
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
    return li;
  }

  function pushEvent(ev) {
    // ── Side effects (always happen, regardless of filter) ─────────
    // RA-timeline mutations.
    if (ev.type === 'tcas_ra_started') {
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
      for (let i = state.raEvents.length - 1; i >= 0; i--) {
        if (state.raEvents[i].icao === ev.icao && state.raEvents[i].ended_at == null) {
          state.raEvents[i].ended_at = ev.t;
          break;
        }
      }
      renderRaTimeline();
    }
    // Mirror into state.events so the per-aircraft EVENTS block in
    // the detail pane and the live ticker filter both see every
    // event.  Trim to the configured cap.
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

    // ── Live ticker DOM insert (gated on the current filter) ───────
    if (!eventPassesFilter(ev)) return;
    const li = buildEventLi(ev);
    el.eventLog.insertBefore(li, el.eventLog.firstChild);
    while (el.eventLog.children.length > 200) {
      el.eventLog.removeChild(el.eventLog.lastChild);
    }
  }

  // Re-render the ticker DOM from scratch using the current filter.
  // Called by the filter buttons; cheap because state.events is
  // capped at eventsMax (500).
  function applyEventFilter() {
    el.eventLog.innerHTML = '';
    // state.events is oldest-first; we want newest-first DOM order
    // (insertBefore-firstChild semantics), so iterate in reverse and
    // append to the end.
    for (let i = state.events.length - 1; i >= 0; i--) {
      const ev = state.events[i];
      if (!eventPassesFilter(ev)) continue;
      const li = buildEventLi(ev);
      el.eventLog.appendChild(li);
      if (el.eventLog.children.length >= 200) break;
    }
    // Update button active states.
    document.querySelectorAll('#event-filter .ev-filter-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.filter === state.eventFilter);
    });
  }

  // Wire up the filter buttons.
  document.querySelectorAll('#event-filter .ev-filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const next = btn.dataset.filter;
      if (next !== state.eventFilter) {
        state.eventFilter = next;
        localStorage.setItem('skywatch.eventFilter', next);
        applyEventFilter();
      }
    });
  });
  applyEventFilter();   // sync initial button state

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
    // VDL2 stat: only visible after the first VDL2 frame so single-1090
    // installs don't see a perma-zero counter.
    const vdl2 = s.total_vdl2_frames || 0;
    if (vdl2 > 0) {
      el.statVdl2.hidden = false;
      el.statVdl2.querySelector('.val').textContent = vdl2.toLocaleString();
    }

    // Frame rate.  Stats payloads only arrive on the snapshot
    // cadence (default 10 s), so a sliding-window average over a
    // shorter span never accumulates two samples.  Compute from the
    // delta against the previous sample directly — works at any
    // cadence, just gives a coarser number than per-frame rates.
    const now = Date.now();
    const total = s.total_frames || 0;
    if (state.lastFrameSample) {
      const [t0, f0] = state.lastFrameSample;
      const dt = (now - t0) / 1000;
      if (dt > 0) {
        const rate = Math.round((total - f0) / dt);
        el.statRate.textContent = `${rate}/s`;
      }
    }
    state.lastFrameSample = [now, total];
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

  // ─── Airport + runway dataset (OurAirports) ─────────────────────────
  // Loaded once on page load.  The frontend prefers the canonical full
  // dataset (`airports.csv.gz`) and falls back to the bundled seed
  // (`airports.seed.csv.gz`) if it isn't present.  Same for runways.

  // Airports + runways toggle.  Wired here (rather than alongside the
  // other top-of-IIFE toggles) because it touches `airportLayer` and
  // `runwayLayer`, which are only declared after the map is built.
  const airportsToggle = document.getElementById('airports-toggle');
  function applyAirportsToggle() {
    airportsToggle.classList.toggle('on', state.airportsOn);
    airportsToggle.querySelector('.val').textContent =
      state.airportsOn ? 'ON' : 'OFF';
    if (state.airportsOn) {
      airportLayer.addTo(map);
      runwayLayer.addTo(map);
    } else {
      airportLayer.remove();
      runwayLayer.remove();
    }
  }
  airportsToggle.addEventListener('click', () => {
    state.airportsOn = !state.airportsOn;
    localStorage.setItem('skywatch.airports', state.airportsOn ? '1' : '0');
    applyAirportsToggle();
    redrawAirports();
    redrawRunways();
  });
  applyAirportsToggle();

  function parseCsvLine(line) {
    // Minimal RFC-4180 line parser: respects "..." quoting and "" as
    // an escaped quote inside a field.  No multi-line fields supported
    // (the OurAirports source doesn't use them).
    const out = [];
    let cur = '';
    let inQ = false;
    for (let i = 0; i < line.length; i++) {
      const c = line[i];
      if (inQ) {
        if (c === '"' && line[i + 1] === '"') { cur += '"'; i++; }
        else if (c === '"') inQ = false;
        else cur += c;
      } else {
        if (c === ',') { out.push(cur); cur = ''; }
        else if (c === '"') inQ = true;
        else cur += c;
      }
    }
    out.push(cur);
    return out;
  }

  async function fetchGzCsv(url) {
    const r = await fetch(url, { cache: 'force-cache' });
    if (!r.ok) throw new Error(`HTTP ${r.status} for ${url}`);
    // Browser-native gzip decoder — supported in every modern engine.
    const ds = new DecompressionStream('gzip');
    const decoded = await new Response(r.body.pipeThrough(ds)).text();
    return decoded;
  }

  function parseCsv(text) {
    // Strip trailing newlines, split, drop empty.
    const lines = text.split(/\r?\n/).filter(Boolean);
    if (lines.length < 1) return { header: [], rows: [] };
    const header = parseCsvLine(lines[0]);
    const idxOf = {};
    header.forEach((h, i) => { idxOf[h] = i; });
    const rows = [];
    for (let i = 1; i < lines.length; i++) {
      rows.push(parseCsvLine(lines[i]));
    }
    return { header, rows, idxOf };
  }

  function num(s) {
    if (s === '' || s == null) return null;
    const n = parseFloat(s);
    return Number.isFinite(n) ? n : null;
  }

  function loadAirportsCsv(text) {
    const { idxOf, rows } = parseCsv(text);
    const I_IDENT = idxOf.ident, I_TYPE = idxOf.type, I_NAME = idxOf.name;
    const I_LAT = idxOf.latitude_deg, I_LON = idxOf.longitude_deg;
    const I_IATA = idxOf.iata_code, I_MUN = idxOf.municipality;
    const out = [];
    for (const r of rows) {
      const lat = num(r[I_LAT]), lon = num(r[I_LON]);
      if (lat == null || lon == null) continue;
      const type = r[I_TYPE] || '';
      // Drop closed airports — they clutter the map and aren't useful.
      if (type === 'closed') continue;
      out.push({
        ident: r[I_IDENT] || '',
        type,
        name: r[I_NAME] || '',
        lat, lon,
        iata: r[I_IATA] || '',
        municipality: r[I_MUN] || '',
      });
    }
    return out;
  }

  function loadRunwaysCsv(text) {
    const { idxOf, rows } = parseCsv(text);
    const I_AID = idxOf.airport_ident;
    const I_LEN = idxOf.length_ft, I_WID = idxOf.width_ft;
    const I_CLOSED = idxOf.closed;
    const I_LE_LAT = idxOf.le_latitude_deg, I_LE_LON = idxOf.le_longitude_deg;
    const I_HE_LAT = idxOf.he_latitude_deg, I_HE_LON = idxOf.he_longitude_deg;
    const I_LE_ID = idxOf.le_ident, I_HE_ID = idxOf.he_ident;
    const out = [];
    for (const r of rows) {
      // Skip rows missing both threshold positions.  Without them we
      // can't draw a polygon.
      const leLat = num(r[I_LE_LAT]), leLon = num(r[I_LE_LON]);
      const heLat = num(r[I_HE_LAT]), heLon = num(r[I_HE_LON]);
      if (leLat == null || heLat == null) continue;
      out.push({
        airport_ident: r[I_AID] || '',
        length_ft: num(r[I_LEN]),
        width_ft: num(r[I_WID]) || 150,
        closed: r[I_CLOSED] === '1',
        le_ident: r[I_LE_ID] || '',
        he_ident: r[I_HE_ID] || '',
        le_lat: leLat, le_lon: leLon,
        he_lat: heLat, he_lon: heLon,
      });
    }
    return out;
  }

  async function loadDataset(primary, fallback, parser) {
    // Try the full dataset first; fall back to the seed.
    for (const url of [primary, fallback]) {
      try {
        const text = await fetchGzCsv(url);
        return parser(text);
      } catch (e) {
        console.debug('skywatch: dataset load failed for', url, e);
      }
    }
    return [];
  }

  // Zoom thresholds — chosen so the map doesn't drown in airport icons
  // until the user is actually looking at a country/region scale.
  function airportZoomMin(type) {
    if (type === 'large_airport')   return 6;
    if (type === 'medium_airport')  return 8;
    if (type === 'small_airport')   return 10;
    return 12;   // heliport, balloonport, seaplane_base
  }

  function airportLabelHtml(ap, zoom) {
    if (zoom >= 11 && ap.name) {
      return `<div class="ap-label">${ap.iata || ap.ident}<br><span class="ap-name">${ap.name}</span></div>`;
    }
    if (zoom >= 8) {
      return `<div class="ap-label">${ap.iata || ap.ident}</div>`;
    }
    return '';
  }

  function redrawAirports() {
    airportLayer.clearLayers();
    if (!state.airportsOn || !state.airports.length) return;
    const zoom = map.getZoom();
    const bounds = map.getBounds().pad(0.1);
    for (const ap of state.airports) {
      if (zoom < airportZoomMin(ap.type)) continue;
      if (!bounds.contains([ap.lat, ap.lon])) continue;
      const icon = L.divIcon({
        html: `<div class="ap-icon"></div>${airportLabelHtml(ap, zoom)}`,
        className: 'ap-marker ap-' + ap.type,
        iconSize: [0, 0],
        iconAnchor: [0, 0],
      });
      L.marker([ap.lat, ap.lon], {
        icon, interactive: false, keyboard: false,
      }).addTo(airportLayer);
    }
  }

  function makeRunwayPolygon(rw) {
    // Distance-correct projection: convert lat/lon offsets to metres,
    // build the centerline, then offset by half-width perpendicular.
    const meanLat = (rw.le_lat + rw.he_lat) / 2;
    const cosLat = Math.cos(meanLat * Math.PI / 180);
    const dLatM = (rw.he_lat - rw.le_lat) * 111320;
    const dLonM = (rw.he_lon - rw.le_lon) * 111320 * cosLat;
    const lenM = Math.sqrt(dLatM * dLatM + dLonM * dLonM);
    if (lenM < 1) return null;
    // Right-hand perpendicular unit vector (in metres).
    const halfW = (rw.width_ft || 150) * 0.3048 / 2;
    const perpLatM = -dLonM / lenM * halfW;
    const perpLonM =  dLatM / lenM * halfW;
    const perpLat = perpLatM / 111320;
    const perpLon = perpLonM / (111320 * cosLat);
    const pts = [
      [rw.le_lat + perpLat, rw.le_lon + perpLon],
      [rw.he_lat + perpLat, rw.he_lon + perpLon],
      [rw.he_lat - perpLat, rw.he_lon - perpLon],
      [rw.le_lat - perpLat, rw.le_lon - perpLon],
    ];
    return L.polygon(pts, {
      className: 'rw-poly',
      color: '#79ddc1',
      weight: 1,
      opacity: 0.45,
      fillColor: '#79ddc1',
      fillOpacity: 0.18,
      interactive: false,
    });
  }

  function redrawRunways() {
    runwayLayer.clearLayers();
    if (!state.airportsOn || !state.runways.length) return;
    const zoom = map.getZoom();
    if (zoom < 10) return;          // not worth drawing at this scale
    const bounds = map.getBounds().pad(0.05);
    for (const rw of state.runways) {
      if (rw.closed) continue;
      if (!bounds.contains([rw.le_lat, rw.le_lon]) &&
          !bounds.contains([rw.he_lat, rw.he_lon])) continue;
      const poly = makeRunwayPolygon(rw);
      if (poly) runwayLayer.addLayer(poly);
    }
  }

  // Reuse one redraw on every pan/zoom — both layers are cheap to clear.
  map.on('zoomend moveend', () => { redrawAirports(); redrawRunways(); });

  (async () => {
    state.airports = await loadDataset(
      '/data/airports.csv.gz',
      '/data/airports.seed.csv.gz',
      loadAirportsCsv);
    state.runways = await loadDataset(
      '/data/runways.csv.gz',
      '/data/runways.seed.csv.gz',
      loadRunwaysCsv);
    console.info(`skywatch: loaded ${state.airports.length} airports, `
                 + `${state.runways.length} runways`);
    redrawAirports();
    redrawRunways();
  })();

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
