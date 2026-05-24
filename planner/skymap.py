"""Generate an interactive all-sky coverage map from capture records."""

import math
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


def build_skymap(records: list[CaptureRecord], output_path: str = "skymap.html"):
    scope_colors = {
        "Seestar S50": "#4ecdc4",
        "Dwarf 3": "#ff6b6b",
        "Dwarf mini": "#ffd93d",
    }

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
