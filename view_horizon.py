#!/usr/bin/env python3
"""Generate an interactive alt/az horizon view from one or more horizon masks.

Usage:
    python view_horizon.py masks/horizon.json
    python view_horizon.py masks/horizon.json masks/horizon20260524.json
    python view_horizon.py masks/*.json -o comparison.html
    python view_horizon.py masks/*.json --no-captures
"""

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path
from string import Template


_DEFAULT_ARCHIVE = "/mnt/zarchive/Pictures/Astrophotography"

HORIZON_COLORS = [
    ('#4ecdc4', 'rgba(78,205,196,0.18)'),
    ('#ff6b6b', 'rgba(255,107,107,0.18)'),
    ('#ffd93d', 'rgba(255,217,61,0.18)'),
    ('#a29bfe', 'rgba(162,155,254,0.18)'),
]

_SCOPE_PALETTE = ['#4ecdc4', '#ff6b6b', '#ffd93d', '#a855f7',
                  '#f97316', '#06b6d4', '#84cc16', '#ec4899']


def _scope_colors_from_captures(captures):
    scopes = sorted({c['scope'] for c in captures if c.get('scope')})
    return {s: _SCOPE_PALETTE[i % len(_SCOPE_PALETTE)] for i, s in enumerate(scopes)}

_HTML = Template(r'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Horizon View — AstroPlanner</title>
  <script src="https://cdn.plot.ly/plotly-3.5.0.min.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0a0a2e; color: #fff; font-family: sans-serif; overflow: hidden; }
    #chart { height: 100vh; width: 100%; }
    #status {
      position: fixed; bottom: 12px; right: 16px;
      color: rgba(255,255,255,0.4); font-size: 12px; pointer-events: none;
    }
  </style>
</head>
<body>
  <div id="chart"></div>
  <div id="status">Loading…</div>

  <script>
  'use strict';

  var HORIZONS = $horizons_json;
  var CAPTURES = $captures_json;
  var LAT = $lat, LON = $lon;
  var SCOPE_COLORS = $scope_colors_json;

  var D2R = Math.PI/180, R2D = 180/Math.PI;
  function cl(v) { return v < -1 ? -1 : v > 1 ? 1 : v; }

  function sunPos(dt) {
    var J = dt.getTime()/864e5 + 2440587.5, n = J - 2451545;
    var L = ((280.46 + .9856474*n) % 360 + 360) % 360;
    var g = ((357.528 + .9856003*n) % 360 + 360) % 360 * D2R;
    var lam = (L + 1.915*Math.sin(g) + .02*Math.sin(2*g)) * D2R;
    var eps = (23.439 - 4e-7*n) * D2R;
    return {
      ra:  ((Math.atan2(Math.cos(eps)*Math.sin(lam), Math.cos(lam))*R2D) + 360) % 360,
      dec: Math.asin(cl(Math.sin(eps)*Math.sin(lam))) * R2D
    };
  }

  function lst(dt) {
    var J = dt.getTime()/864e5 + 2440587.5, n = J - 2451545, T = n/36525;
    var g = 280.46061837 + 360.98564736629*n + 3.87933e-4*T*T - T*T*T/3.871e7;
    return ((g + LON) % 360 + 360) % 360;
  }

  function rd2aa(r, d, L) {
    var h = (L-r)*D2R, dc = d*D2R, la = LAT*D2R;
    var sa = Math.sin(dc)*Math.sin(la) + Math.cos(dc)*Math.cos(la)*Math.cos(h);
    var alt = Math.asin(cl(sa)), ca = Math.cos(alt);
    if (Math.abs(ca) < 1e-10) return {alt: alt*R2D, az: 0};
    var cz = (Math.sin(dc) - Math.sin(la)*sa) / (Math.cos(la)*ca);
    var az = Math.acos(cl(cz));
    if (Math.sin(h) > 0) az = 2*Math.PI - az;
    return {alt: alt*R2D, az: az*R2D};
  }

  function mAlt(boundary, az) {
    if (!boundary.length) return 0;
    az = ((az % 360) + 360) % 360;
    var n = boundary.length, i = 0;
    for (; i < n; i++) if (boundary[i].azimuth > az) break;
    var hi = i % n, lo = (i - 1 + n) % n;
    var s = ((boundary[hi].azimuth - boundary[lo].azimuth) % 360 + 360) % 360;
    if (!s) return boundary[lo].min_altitude;
    return boundary[lo].min_altitude
      + (((az - boundary[lo].azimuth) % 360 + 360) % 360) / s
      * (boundary[hi].min_altitude - boundary[lo].min_altitude);
  }

  function fmtExp(sec) {
    if (sec < 60)   return sec.toFixed(0) + 's';
    if (sec < 3600) return (sec / 60).toFixed(1) + 'm';
    return (sec / 3600).toFixed(1) + 'h';
  }

  function cardinalDir(az) {
    var dirs = ['N','NNE','NE','ENE','E','ESE','SE','SSE',
                'S','SSW','SW','WSW','W','WNW','NW','NNW'];
    return dirs[Math.round(az / 22.5) % 16];
  }

  var selectedTarget = null;

  function fmtTime(dt) {
    return dt.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
  }

  function drawPath() {
    var div = document.getElementById('chart');

    var toRemove = [];
    for (var i = div.data.length - 1; i >= 0; i--) {
      if (div.data[i]._isPath) toRemove.push(i);
    }
    if (toRemove.length) Plotly.deleteTraces(div, toRemove);
    Plotly.relayout(div, {annotations: []});

    if (!selectedTarget) return;

    var ra = selectedTarget.ra, dec = selectedTarget.dec, tgt = selectedTarget.name;
    var boundary = HORIZONS.length ? HORIZONS[0].boundary : [];
    var now = new Date();

    var pts = [];
    for (var m = -720; m <= 720; m += 2) {
      var dt = new Date(now.getTime() + m * 60000);
      var L = lst(dt);
      var aa = rd2aa(ra, dec, L);
      var hAlt = boundary.length ? mAlt(boundary, aa.az) : 0;
      pts.push({az: aa.az, alt: aa.alt, time: dt, hAlt: hAlt});
    }

    var pathAz = [], pathAlt = [], pathHover = [];
    var prevAz = null;
    for (var i = 0; i < pts.length; i++) {
      var p = pts[i];
      if (p.alt <= 0) {
        if (pathAz.length && pathAz[pathAz.length - 1] !== null) {
          pathAz.push(null); pathAlt.push(null); pathHover.push('');
        }
        prevAz = null;
        continue;
      }
      if (prevAz !== null && Math.abs(p.az - prevAz) > 180) {
        pathAz.push(null); pathAlt.push(null); pathHover.push('');
      }
      prevAz = p.az;
      pathAz.push(p.az);
      pathAlt.push(p.alt);
      pathHover.push(
        fmtTime(p.time) + '<br>' +
        cardinalDir(p.az) + ' Az ' + p.az.toFixed(1) + '° Alt ' + p.alt.toFixed(1) + '°'
      );
    }

    if (!pathAz.length) return;

    var transit = null;
    for (var i = 0; i < pts.length; i++) {
      if (pts[i].alt > 0 && (!transit || pts[i].alt > transit.alt)) {
        transit = pts[i];
      }
    }

    var crossings = [];
    for (var i = 1; i < pts.length; i++) {
      var prev = pts[i - 1], curr = pts[i];
      if (prev.alt <= 0 && curr.alt <= 0) continue;
      var prevVis = prev.alt > 0 && prev.alt >= prev.hAlt;
      var currVis = curr.alt > 0 && curr.alt >= curr.hAlt;
      if (prevVis !== currVis) {
        var cp = currVis ? curr : prev;
        crossings.push({
          az: cp.az, alt: Math.max(cp.alt, cp.hAlt),
          time: cp.time,
          type: currVis ? 'rise' : 'set'
        });
      }
    }

    var newTraces = [];
    var annotations = [];

    newTraces.push({
      x: pathAz, y: pathAlt,
      type: 'scatter', mode: 'lines',
      line: {color: 'rgba(255,255,255,0.4)', width: 1.5, dash: 'dot'},
      name: tgt + ' track',
      showlegend: true,
      text: pathHover, hoverinfo: 'text',
      _isPath: true
    });

    if (transit) {
      newTraces.push({
        x: [transit.az], y: [transit.alt],
        type: 'scatter', mode: 'markers',
        marker: {size: 14, color: '#FFD700', symbol: 'star'},
        showlegend: false,
        hovertext: ['Transit: ' + fmtTime(transit.time) + '<br>Alt ' + transit.alt.toFixed(1) + '°'],
        hoverinfo: 'text',
        _isPath: true
      });
      annotations.push({
        x: transit.az, y: transit.alt,
        text: fmtTime(transit.time),
        showarrow: true, arrowhead: 0, arrowcolor: '#FFD700',
        ax: 0, ay: -25,
        font: {color: '#FFD700', size: 11},
        bgcolor: 'rgba(0,0,0,0.6)', borderpad: 3
      });
    }

    if (crossings.length) {
      var cAz = [], cAlt = [], cHover = [], cColors = [];
      for (var i = 0; i < crossings.length; i++) {
        var c = crossings[i];
        var color = c.type === 'rise' ? '#4ecdc4' : '#ff6b6b';
        cAz.push(c.az);
        cAlt.push(c.alt);
        cColors.push(color);
        cHover.push((c.type === 'rise' ? 'Rises' : 'Sets') + ': ' + fmtTime(c.time));
        annotations.push({
          x: c.az, y: c.alt,
          text: (c.type === 'rise' ? '▲ ' : '▼ ') + fmtTime(c.time),
          showarrow: true, arrowhead: 0, arrowcolor: color,
          ax: 0, ay: -25,
          font: {color: color, size: 10},
          bgcolor: 'rgba(0,0,0,0.6)', borderpad: 3
        });
      }
      newTraces.push({
        x: cAz, y: cAlt,
        type: 'scatter', mode: 'markers',
        marker: {size: 10, color: cColors, symbol: 'diamond'},
        showlegend: false,
        hovertext: cHover, hoverinfo: 'text',
        _isPath: true
      });
    }

    if (newTraces.length) Plotly.addTraces(div, newTraces);
    if (annotations.length) Plotly.relayout(div, {annotations: annotations});
  }

  // Build static horizon traces (filled areas + lines)
  var staticTraces = [];
  for (var hi = 0; hi < HORIZONS.length; hi++) {
    var h = HORIZONS[hi];
    var azArr = [], altArr = [];
    for (var a = 0; a <= 360; a++) {
      azArr.push(a);
      altArr.push(mAlt(h.boundary, a));
    }
    staticTraces.push({
      x: azArr, y: altArr,
      fill: 'tozeroy',
      fillcolor: h.fillcolor,
      line: { color: h.color, width: 2 },
      name: h.label,
      hoverinfo: 'skip'
    });
  }

  var NUM_BASE = staticTraces.length;

  var LAYOUT = {
    title: 'Horizon View',
    xaxis: {
      title: 'Azimuth',
      range: [0, 360],
      dtick: 45,
      ticktext: ['N','NE','E','SE','S','SW','W','NW','N'],
      tickvals: [0, 45, 90, 135, 180, 225, 270, 315, 360],
      gridcolor: 'rgba(255,255,255,0.1)'
    },
    yaxis: {
      title: 'Altitude (°)',
      range: [0, 90],
      dtick: 10,
      gridcolor: 'rgba(255,255,255,0.1)'
    },
    plot_bgcolor: '#0a0a2e',
    paper_bgcolor: '#0a0a2e',
    font: { color: 'white' },
    legend: { bgcolor: 'rgba(0,0,0,0.5)', font: { size: 14 } },
    margin: { l: 60, r: 30, t: 60, b: 60 }
  };

  function refresh() {
    var div = document.getElementById('chart');
    var n = div.data.length;
    if (n > NUM_BASE) {
      var d = [];
      for (var i = NUM_BASE; i < n; i++) d.push(i);
      Plotly.deleteTraces(div, d);
    }

    var now = new Date(), L = lst(now), sun = sunPos(now);
    var sunAA = rd2aa(sun.ra, sun.dec, L);

    // Convert captures to alt/az, group by scope
    var byScope = {};
    for (var ci = 0; ci < CAPTURES.length; ci++) {
      var r = CAPTURES[ci];
      var aa = rd2aa(r.ra_deg, r.dec_deg, L);
      if (aa.alt < 0) continue;
      var scope = r.scope || 'Unknown';
      if (!byScope[scope]) byScope[scope] = [];
      byScope[scope].push({
        target: r.target, az: aa.az, alt: aa.alt,
        total_exposure_sec: r.total_exposure_sec,
        num_frames: r.num_frames,
        filter_name: r.filter_name,
        ra_deg: r.ra_deg, dec_deg: r.dec_deg
      });
    }

    var newTraces = [];
    var scopes = Object.keys(SCOPE_COLORS);
    for (var si = 0; si < scopes.length; si++) {
      var scope = scopes[si], color = SCOPE_COLORS[scope];
      var recs = byScope[scope];
      if (!recs || !recs.length) continue;
      var maxExp = 1;
      for (var j = 0; j < recs.length; j++)
        if (recs[j].total_exposure_sec > maxExp) maxExp = recs[j].total_exposure_sec;

      var xv = [], yv = [], sz = [], txt = [], cd = [];
      for (var j = 0; j < recs.length; j++) {
        var rc = recs[j];
        xv.push(rc.az);
        yv.push(rc.alt);
        sz.push(Math.max(8, Math.min(30, 8 + 22 * rc.total_exposure_sec / maxExp)));
        txt.push(
          '<b>' + rc.target + '</b><br>' +
          cardinalDir(rc.az) + ' Az ' + rc.az.toFixed(1) + '°  Alt ' + rc.alt.toFixed(1) + '°<br>' +
          'Frames: ' + rc.num_frames + '<br>' +
          'Exposure: ' + fmtExp(rc.total_exposure_sec) + '<br>' +
          'Filter: ' + rc.filter_name
        );
        cd.push([rc.ra_deg, rc.dec_deg, rc.target]);
      }
      newTraces.push({
        x: xv, y: yv,
        customdata: cd,
        type: 'scatter', mode: 'markers', name: scope,
        marker: { color: color, opacity: 0.8, size: sz,
                  line: { width: 1, color: 'white' } },
        text: txt, hoverinfo: 'text'
      });
    }

    // Sun
    if (sunAA.alt > -2) {
      newTraces.push({
        x: [sunAA.az], y: [Math.max(0, sunAA.alt)],
        type: 'scatter', mode: 'markers',
        name: 'Sun', showlegend: true,
        marker: { size: 18, color: '#FFD700', symbol: 'circle',
                  line: { width: 2, color: '#FF8C00' } },
        hovertext: 'Sun  ' + cardinalDir(sunAA.az) +
                   ' Az ' + sunAA.az.toFixed(1) + '° Alt ' + sunAA.alt.toFixed(1) + '°',
        hoverinfo: 'text'
      });
    }

    if (newTraces.length) Plotly.addTraces(div, newTraces);

    var ts = now.toLocaleTimeString();
    var aboveCount = 0;
    for (var k in byScope) aboveCount += byScope[k].length;
    Plotly.relayout(div, { title:
      'Horizon View — ' + aboveCount + ' targets above horizon<br>' +
      '<sup style="color:rgba(255,255,255,0.4);font-weight:normal">' +
      ts + ' (auto-refresh 5 min)</sup>'
    });
    document.getElementById('status').textContent = 'Updated ' + ts;
    drawPath();
  }

  Plotly.newPlot('chart', staticTraces, LAYOUT, {responsive: true}).then(function() {
    var div = document.getElementById('chart');
    div.on('plotly_click', function(data) {
      var pt = data.points[0];
      if (!pt.customdata) return;
      var ra = pt.customdata[0], dec = pt.customdata[1], name = pt.customdata[2];
      if (selectedTarget && selectedTarget.ra === ra && selectedTarget.dec === dec) {
        selectedTarget = null;
      } else {
        selectedTarget = {ra: ra, dec: dec, name: name};
      }
      drawPath();
    });
    refresh();
    setInterval(refresh, 300000);
  });
  </script>
</body>
</html>
''')


def main():
    parser = argparse.ArgumentParser(
        description="Generate an interactive alt/az horizon view from horizon masks."
    )
    parser.add_argument('masks', nargs='+',
                        help='Horizon mask JSON file(s) to overlay')
    parser.add_argument('-o', '--output', default='horizon_view.html',
                        help='Output HTML file (default: horizon_view.html)')
    parser.add_argument('--archive', default=_DEFAULT_ARCHIVE,
                        help='Astrophotography archive path for capture data')
    parser.add_argument('--no-captures', action='store_true',
                        help='Skip loading capture data (horizon-only view)')
    parser.add_argument('--regenerate-cache', action='store_true',
                        help='Ignore cache and rescan all files from the archive')

    args = parser.parse_args()

    horizons_js = []
    lat, lon = None, None

    for i, path in enumerate(args.masks):
        with open(path) as f:
            data = json.load(f)
        line_color, fill_color = HORIZON_COLORS[i % len(HORIZON_COLORS)]
        label = Path(path).stem
        if label == 'horizon':
            gen = data.get('generated', '')
            if gen:
                label += ' ' + gen[:10]
        horizons_js.append({
            'label': label,
            'boundary': data.get('boundary', []),
            'color': line_color,
            'fillcolor': fill_color,
        })
        if lat is None:
            loc = data.get('location', {})
            lat = loc.get('lat')
            lon = loc.get('lon')

    captures = []
    if not args.no_captures:
        try:
            from planner.capture_cache import CaptureCache, find_db
            db_path = find_db(args.archive)

            if db_path and not args.regenerate_cache:
                cache = CaptureCache(db_path, read_only=True)
                records = cache.load_all()
                cache.close()
                print(f"Loaded {len(records)} captures from cache")
            else:
                if args.regenerate_cache:
                    print("Regenerating: scanning all files...")
                else:
                    print("No cache found — scanning all files (run update_cache.py to build one)")
                from planner.scanner import scan_all
                records = scan_all(args.archive)
            captures = [asdict(r) for r in records]
        except Exception as e:
            print(f"Warning: Could not load captures: {e}")

    html = _HTML.substitute(
        horizons_json=json.dumps(horizons_js),
        captures_json=json.dumps(captures),
        lat=lat or 0,
        lon=lon or 0,
        scope_colors_json=json.dumps(_scope_colors_from_captures(captures)),
    )

    with open(args.output, 'w') as f:
        f.write(html)

    print(f"Horizon view written to {args.output}")
    print(f"  {len(horizons_js)} horizon(s), {len(captures)} captures")


if __name__ == '__main__':
    main()
