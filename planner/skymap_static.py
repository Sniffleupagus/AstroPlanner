"""Generate a static PNG sky coverage map."""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from planner.scanner import CaptureRecord


def _format_exposure(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def build_skymap_png(records: list[CaptureRecord], output_path: str = "skymap.png",
                     ra_range: tuple[float, float] | None = None,
                     dec_range: tuple[float, float] | None = None):
    _palette = ["#4ecdc4", "#ff6b6b", "#ffd93d", "#a855f7",
                "#f97316", "#06b6d4", "#84cc16", "#ec4899"]
    _markers = ["o", "s", "D", "^", "v", "P", "*", "X"]
    scopes = sorted({r.scope for r in records if r.scope})
    scope_styles = {s: (_palette[i % len(_palette)], _markers[i % len(_markers)])
                    for i, s in enumerate(scopes)}

    fig, ax = plt.subplots(figsize=(18, 8), facecolor="#0a0a2e")
    ax.set_facecolor("#0a0a2e")

    for scope_name, (color, marker) in scope_styles.items():
        scope_recs = [r for r in records if r.scope == scope_name]
        if not scope_recs:
            continue

        ra = np.array([r.ra_deg for r in scope_recs])
        dec = np.array([r.dec_deg for r in scope_recs])
        total_exp = np.array([r.total_exposure_sec for r in scope_recs])

        sizes = 10 + 200 * (total_exp / max(total_exp.max(), 1))

        ax.scatter(ra, dec, s=sizes, c=color, marker=marker, alpha=0.7,
                   edgecolors="white", linewidths=0.5, label=scope_name, zorder=3)

        for r in scope_recs:
            if r.total_exposure_sec > 3600 or (ra_range and dec_range):
                ax.annotate(r.target, (r.ra_deg, r.dec_deg),
                           fontsize=5, color="white", alpha=0.6,
                           textcoords="offset points", xytext=(4, 4))

    if ra_range:
        ax.set_xlim(ra_range[1], ra_range[0])
    else:
        ax.set_xlim(360, 0)

    if dec_range:
        ax.set_ylim(dec_range)
    else:
        ax.set_ylim(-90, 90)

    ax.set_xlabel("RA (degrees)", color="white", fontsize=12)
    ax.set_ylabel("DEC (degrees)", color="white", fontsize=12)
    ax.set_title("Sky Coverage Map — All Captures", color="white", fontsize=14, pad=15)

    ra_ticks = np.arange(0, 361, 30)
    ax.set_xticks(ra_ticks)
    ax.set_xticklabels([f"{int(t/15)}h" for t in ra_ticks], color="white")
    ax.tick_params(colors="white")

    ax.grid(True, alpha=0.15, color="white")
    ax.legend(loc="upper right", facecolor="black", edgecolor="gray",
              labelcolor="white", fontsize=10)

    plt.tight_layout()
    fig.savefig(output_path, dpi=150, facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"Sky map image written to {output_path}")
