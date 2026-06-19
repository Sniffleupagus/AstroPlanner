"""Generate an interactive all-sky coverage map from capture records."""

import json as _json
import math
from string import Template

from planner.scanner import CaptureRecord
import plotly.graph_objects as go


def _format_exposure(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _ra_to_hms(ra_deg: float) -> str:
    h = ra_deg / 15.0
    hours = int(h)
    minutes = int((h - hours) * 60)
    return f"{hours:02d}h{minutes:02d}m"


_OVERLAY_JS = Template(r"""(function(){
var MASK=${mask_json};
var LAT=${lat},LON=${lon},NUM_BASE=${num_base};
var div=document.getElementById('skymap');
var NR=180,ND=90,raG=[],decG=[];
for(var i=0;i<NR;i++) raG.push(i*360/NR);
for(var i=0;i<ND;i++) decG.push(-90+i*180/(ND-1));
var D2R=Math.PI/180,R2D=180/Math.PI;
function cl(v){return v<-1?-1:v>1?1:v}

function sunPos(dt){
    var J=dt.getTime()/864e5+2440587.5,n=J-2451545;
    var L=((280.46+.9856474*n)%360+360)%360;
    var g=((357.528+.9856003*n)%360+360)%360*D2R;
    var lam=(L+1.915*Math.sin(g)+.02*Math.sin(2*g))*D2R;
    var eps=(23.439-4e-7*n)*D2R;
    return{ra:((Math.atan2(Math.cos(eps)*Math.sin(lam),Math.cos(lam))*R2D)+360)%360,
           dec:Math.asin(cl(Math.sin(eps)*Math.sin(lam)))*R2D};
}

function lst(dt){
    var J=dt.getTime()/864e5+2440587.5,n=J-2451545,T=n/36525;
    var g=280.46061837+360.98564736629*n+3.87933e-4*T*T-T*T*T/3.871e7;
    return((g+LON)%360+360)%360;
}

function rd2aa(r,d,L){
    var h=(L-r)*D2R,dc=d*D2R,la=LAT*D2R;
    var sa=Math.sin(dc)*Math.sin(la)+Math.cos(dc)*Math.cos(la)*Math.cos(h);
    var alt=Math.asin(cl(sa)),ca=Math.cos(alt);
    if(Math.abs(ca)<1e-10) return{alt:alt*R2D,az:0};
    var cz=(Math.sin(dc)-Math.sin(la)*sa)/(Math.cos(la)*ca);
    var az=Math.acos(cl(cz));
    if(Math.sin(h)>0) az=2*Math.PI-az;
    return{alt:alt*R2D,az:az*R2D};
}

function aa2rd(a,z,L){
    var al=a*D2R,az=z*D2R,la=LAT*D2R;
    var sd=Math.sin(al)*Math.sin(la)+Math.cos(al)*Math.cos(la)*Math.cos(az);
    var dc=Math.asin(cl(sd)),cd=Math.cos(dc);
    if(Math.abs(cd)<1e-10) return{ra:0,dec:dc*R2D};
    var cH=(Math.sin(al)-Math.sin(la)*sd)/(Math.cos(la)*cd);
    var H=Math.acos(cl(cH));
    if(Math.sin(az)>0) H=2*Math.PI-H;
    return{ra:((L-H*R2D)%360+360)%360,dec:dc*R2D};
}

function sep(r1,d1,r2,d2){
    var a=Math.sin((d1-d2)*D2R/2),b=Math.sin((r1-r2)*D2R/2);
    return 2*Math.asin(Math.sqrt(Math.max(0,Math.min(1,
        a*a+Math.cos(d1*D2R)*Math.cos(d2*D2R)*b*b))))*R2D;
}

function mAlt(az){
    if(!MASK.length) return 0;
    az=((az%360)+360)%360;
    var n=MASK.length,i;
    for(i=0;i<n;i++) if(MASK[i].azimuth>az) break;
    var hi=i%n,lo=(i-1+n)%n;
    var s=((MASK[hi].azimuth-MASK[lo].azimuth)%360+360)%360;
    if(!s) return MASK[lo].min_altitude;
    return MASK[lo].min_altitude
        +(((az-MASK[lo].azimuth)%360+360)%360)/s
        *(MASK[hi].min_altitude-MASK[lo].min_altitude);
}

function refresh(){
    var n=div.data.length;
    if(n>NUM_BASE){
        var d=[];for(var i=NUM_BASE;i<n;i++) d.push(i);
        Plotly.deleteTraces(div,d);
    }
    var now=new Date(),L=lst(now),sun=sunPos(now);
    var hZ=[],sZ=[];
    for(var j=0;j<ND;j++){
        var hr=[],sr=[];
        for(var i=0;i<NR;i++){
            var aa=rd2aa(raG[i],decG[j],L);
            if(aa.alt<mAlt(aa.az)){hr.push(1);sr.push(null);}
            else{
                hr.push(null);
                var sp=sep(raG[i],decG[j],sun.ra,sun.dec);
                sr.push(sp<60?1-sp/60:null);
            }
        }
        hZ.push(hr);sZ.push(sr);
    }
    var pts=[];
    for(var az=0;az<360;az++) pts.push(aa2rd(mAlt(az),az,L));
    var lR=[],lD=[];
    for(var i=0;i<pts.length;i++){
        if(i>0&&Math.abs(pts[i].ra-pts[i-1].ra)>300){lR.push(null);lD.push(null);}
        lR.push(pts[i].ra);lD.push(pts[i].dec);
    }
    Plotly.addTraces(div,[
        {x:raG,y:decG,z:hZ,type:'heatmap',
         colorscale:[[0,'rgba(60,40,15,0.55)'],[1,'rgba(60,40,15,0.55)']],
         showscale:false,hoverinfo:'skip',zmin:0,zmax:1},
        {x:raG,y:decG,z:sZ,type:'heatmap',
         colorscale:[[0,'rgba(255,220,50,0.02)'],[0.4,'rgba(255,180,30,0.15)'],
                     [0.7,'rgba(255,140,20,0.3)'],[1,'rgba(255,100,0,0.45)']],
         showscale:false,hoverinfo:'skip',zmin:0,zmax:1},
        {x:[sun.ra],y:[sun.dec],type:'scatter',mode:'markers',
         name:'Sun',showlegend:true,
         marker:{size:16,color:'#FFD700',symbol:'circle',
                 line:{width:2,color:'#FF8C00'}},
         hovertext:'Sun  RA '+sun.ra.toFixed(1)+'°  Dec '+sun.dec.toFixed(1)+'°',
         hoverinfo:'text'},
        {x:lR,y:lD,type:'scatter',mode:'lines',
         name:'Horizon',showlegend:true,
         line:{color:'rgba(255,100,50,0.8)',width:2,dash:'dot'},
         hoverinfo:'skip'}
    ]);
    Plotly.relayout(div,{title:
        'Sky Coverage — All Captures<br>'
        +'<sup style="color:rgba(255,255,255,0.4);font-weight:normal">'
        +'Overlays at '+now.toLocaleTimeString()+' (auto-refresh 5 min)</sup>'});
}

if(document.readyState==='complete') setTimeout(refresh,300);
else window.addEventListener('load',function(){setTimeout(refresh,300)});
setInterval(refresh,300000);
})();
""")


def build_skymap(records: list[CaptureRecord], output_path: str = "skymap.html",
                 horizon_mask_path: str = None, lat: float = None,
                 lon: float = None):
    _palette = ["#4ecdc4", "#ff6b6b", "#ffd93d", "#a855f7",
                "#f97316", "#06b6d4", "#84cc16", "#ec4899"]
    scopes = sorted({r.scope for r in records if r.scope})
    scope_colors = {s: _palette[i % len(_palette)] for i, s in enumerate(scopes)}

    fig = go.Figure()

    for scope_name, color in scope_colors.items():
        scope_recs = [r for r in records if r.scope == scope_name]
        if not scope_recs:
            continue

        ra_vals = [r.ra_deg for r in scope_recs]
        dec_vals = [r.dec_deg for r in scope_recs]
        total_exp = [r.total_exposure_sec for r in scope_recs]

        max_exp = max(total_exp) if total_exp else 1
        sizes = [max(6, min(40, 6 + 34 * (t / max_exp))) for t in total_exp]

        hover_text = []
        for r in scope_recs:
            hover_text.append(
                f"<b>{r.target}</b><br>"
                f"RA: {_ra_to_hms(r.ra_deg)} ({r.ra_deg:.2f}°)<br>"
                f"DEC: {r.dec_deg:+.2f}°<br>"
                f"Frames: {r.num_frames}<br>"
                f"Exposure: {_format_exposure(r.total_exposure_sec)}<br>"
                f"Filter: {r.filter_name}<br>"
                f"Gain: {r.gain}<br>"
                f"Date: {r.date_obs[:10] if r.date_obs else '?'}<br>"
                f"{'MOSAIC' if r.is_mosaic else 'Single'}"
            )

        fig.add_trace(go.Scatter(
            x=ra_vals,
            y=dec_vals,
            mode="markers",
            name=scope_name,
            marker=dict(
                size=sizes,
                color=color,
                opacity=0.7,
                line=dict(width=1, color="white"),
            ),
            text=hover_text,
            hoverinfo="text",
        ))

    fig.update_layout(
        title="Sky Coverage Map — All Captures",
        xaxis=dict(
            title="RA (degrees)",
            range=[360, 0],
            dtick=30,
            ticktext=[f"{h}h" for h in range(0, 25, 2)],
            tickvals=[h * 15 for h in range(0, 25, 2)],
            gridcolor="rgba(255,255,255,0.1)",
        ),
        yaxis=dict(
            title="DEC (degrees)",
            range=[-90, 90],
            dtick=15,
            gridcolor="rgba(255,255,255,0.1)",
        ),
        plot_bgcolor="#0a0a2e",
        paper_bgcolor="#0a0a2e",
        font=dict(color="white"),
        legend=dict(
            bgcolor="rgba(0,0,0,0.5)",
            font=dict(size=14),
        ),
        height=700,
        margin=dict(l=60, r=30, t=60, b=60),
    )

    overlay_js = None
    if horizon_mask_path and (lat is None or lon is None):
        try:
            with open(horizon_mask_path) as f:
                mask_meta = _json.load(f)
            loc = mask_meta.get("location", {})
            lat = lat if lat is not None else loc.get("lat")
            lon = lon if lon is not None else loc.get("lon")
        except (FileNotFoundError, _json.JSONDecodeError):
            pass

    if lat is not None and lon is not None:
        boundary = []
        if horizon_mask_path:
            try:
                with open(horizon_mask_path) as f:
                    boundary = _json.load(f).get("boundary", [])
            except (FileNotFoundError, _json.JSONDecodeError):
                pass
        num_traces = len(fig.data)
        overlay_js = _OVERLAY_JS.substitute(
            mask_json=_json.dumps(boundary),
            lat=lat, lon=lon, num_base=num_traces,
        )

    if overlay_js:
        html = fig.to_html(full_html=True, include_plotlyjs="cdn", div_id="skymap")
        html = html.replace("</body>",
                            f"<script>\n{overlay_js}\n</script>\n</body>")
        with open(output_path, "w") as f:
            f.write(html)
    else:
        fig.write_html(output_path, include_plotlyjs="cdn")

    print(f"Sky map written to {output_path}")
    return fig


def print_summary(records: list[CaptureRecord]):
    by_scope: dict[str, list[CaptureRecord]] = {}
    for r in records:
        by_scope.setdefault(r.scope, []).append(r)

    print("\n=== Capture Summary ===\n")
    total_time = 0
    total_frames = 0

    for scope, recs in sorted(by_scope.items()):
        targets = set(r.target for r in recs)
        frames = sum(r.num_frames for r in recs)
        exposure = sum(r.total_exposure_sec for r in recs)
        total_time += exposure
        total_frames += frames

        print(f"{scope}:")
        print(f"  {len(recs)} capture sessions, {len(targets)} unique targets")
        print(f"  {frames:,} total frames, {_format_exposure(exposure)} total exposure")
        print()

    print(f"Grand total: {total_frames:,} frames, {_format_exposure(total_time)} total exposure")

    unknowns = [r for r in records if r.target.lower() in ("unknown", "unknown(1)", "unknown(2)", "unknown(3)", "unknown(4)") or r.target.startswith("HD ")]
    if unknowns:
        print(f"\n{len(unknowns)} captures with unidentified targets (Unknown / HD star catalog)")
