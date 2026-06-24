#!/usr/bin/env python3
"""Collect raw sub-exposure .fit files for a target into a working directory.

Queries the captures DB to find the target's coordinates, then finds all
indexed sub-exposures within a radius. Symlinks matching .fit files into
WORK_DIR for Siril stacking.

Usage:
  python session_collector.py "M 13" /tmp/m13_stack
  python session_collector.py "M 13" /tmp/m13_stack --filter IRCUT --radius 1.0
  python session_collector.py "M 13" /tmp/m13_stack --copy
  python session_collector.py "M 13" /tmp/m13_stack --dry-run
"""

import argparse
import math
import os
import shutil
import sys
from pathlib import Path

from planner.capture_cache import CaptureCache, find_db, local_db_path
from planner.target_resolver import resolve_target

_DEFAULT_ARCHIVE = "/mnt/zarchive/Pictures/Astrophotography"


def resolve_target_coords(cache: CaptureCache, target: str) -> tuple[float, float] | None:
    """Look up target RA/Dec by averaging all matching stacked captures."""
    rows = cache._conn.execute(
        "SELECT json_extract(record_json, '$.ra_deg') as ra,"
        "       json_extract(record_json, '$.dec_deg') as dec "
        "FROM captures "
        "WHERE json_extract(record_json, '$.target') = ?",
        (target,),
    ).fetchall()

    if not rows:
        return None

    ra_avg = sum(r["ra"] for r in rows) / len(rows)
    dec_avg = sum(r["dec"] for r in rows) / len(rows)
    return ra_avg, dec_avg


def list_targets(cache: CaptureCache, pattern: str | None = None) -> list[tuple[str, int]]:
    """List known targets with capture counts."""
    if pattern:
        rows = cache._conn.execute(
            "SELECT json_extract(record_json, '$.target') as target, COUNT(*) as cnt "
            "FROM captures "
            "WHERE json_extract(record_json, '$.target') LIKE ? "
            "GROUP BY target ORDER BY target",
            (f"%{pattern}%",),
        ).fetchall()
    else:
        rows = cache._conn.execute(
            "SELECT json_extract(record_json, '$.target') as target, COUNT(*) as cnt "
            "FROM captures "
            "GROUP BY target ORDER BY target",
        ).fetchall()
    return [(r["target"], r["cnt"]) for r in rows]


def angular_distance(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Great-circle distance in degrees between two RA/Dec positions."""
    ra1_r, dec1_r = math.radians(ra1), math.radians(dec1)
    ra2_r, dec2_r = math.radians(ra2), math.radians(dec2)
    dra = ra2_r - ra1_r
    ddec = dec2_r - dec1_r
    a = math.sin(ddec / 2) ** 2 + math.cos(dec1_r) * math.cos(dec2_r) * math.sin(dra / 2) ** 2
    return math.degrees(2 * math.asin(math.sqrt(a)))


def collect_subs(cache: CaptureCache, ra: float, dec: float, radius_deg: float,
                 scope: str | None = None, filter_name: str | None = None,
                 exposure_sec: float | None = None) -> list[dict]:
    """Query indexed subs within radius, with optional filters."""
    subs = cache.query_subs(ra, dec, radius_deg, scope=scope,
                            filter_name=filter_name, exposure_sec=exposure_sec)
    # refine with true angular distance (the SQL box query is approximate)
    return [s for s in subs if angular_distance(ra, dec, s["ra_deg"], s["dec_deg"]) <= radius_deg]


def symlink_subs(subs: list[dict], work_dir: str, dry_run: bool = False,
                 copy: bool = False) -> int:
    """Create symlinks (or copies) for all matched subs into work_dir/lights/. Returns count."""
    work = Path(work_dir) / "lights"
    if not dry_run:
        work.mkdir(parents=True, exist_ok=True)

    existing_targets = set()
    if not dry_run and work.exists():
        for p in work.iterdir():
            if p.is_symlink():
                existing_targets.add(p.resolve())
            elif p.is_file() and copy:
                existing_targets.add((p.name, p.stat().st_size))

    linked = 0
    skipped = 0
    collisions = 0
    for sub in subs:
        src = Path(sub["file_path"])

        if copy:
            try:
                src_size = src.stat().st_size
            except OSError:
                src_size = None
            if (src.name, src_size) in existing_targets:
                skipped += 1
                continue
        else:
            if src.resolve() in existing_targets:
                skipped += 1
                continue

        dst = work / src.name

        if dst.exists() or dst.is_symlink():
            stem = src.stem
            suffix = src.suffix
            i = 1
            while True:
                dst = work / f"{stem}_{i}{suffix}"
                if not dst.exists() and not dst.is_symlink():
                    break
                i += 1
            collisions += 1

        if dry_run:
            linked += 1
            continue

        if copy:
            shutil.copy2(src, dst)
        else:
            dst.symlink_to(src)
        linked += 1

    verb = "copied" if copy else "linked"
    if skipped:
        print(f"  ({skipped} already {verb}, skipped)")
    if collisions:
        print(f"  ({collisions} filename collisions resolved with suffix)")
    return linked


def main():
    parser = argparse.ArgumentParser(
        description="Collect raw sub-exposure .fit files for a target into a working directory.",
    )
    parser.add_argument(
        "target", nargs="?",
        help="Target name (e.g., 'M 13'). Use --list to see available targets.",
    )
    parser.add_argument(
        "work_dir", nargs="?",
        help="Directory to symlink .fit files into",
    )
    parser.add_argument(
        "--ra", type=float,
        help="Override target RA in degrees (skip DB lookup)",
    )
    parser.add_argument(
        "--dec", type=float,
        help="Override target Dec in degrees (skip DB lookup)",
    )
    parser.add_argument(
        "--radius", type=float, default=1.0,
        help="Search radius in degrees (default: 1.0)",
    )
    parser.add_argument(
        "--scope", default=None,
        help="Filter by scope (e.g., 'Seestar S50')",
    )
    parser.add_argument(
        "--filter", dest="filter_name", default=None,
        help="Filter by filter name (e.g., 'IRCUT', 'LP')",
    )
    parser.add_argument(
        "--exposure", type=float, default=None,
        help="Filter by exposure time in seconds",
    )
    parser.add_argument(
        "--archive", default=_DEFAULT_ARCHIVE,
        help=f"Archive base path (default: {_DEFAULT_ARCHIVE})",
    )
    parser.add_argument(
        "--db", default=None,
        help="Explicit DB path",
    )
    parser.add_argument(
        "--count", type=int, default=None,
        help="Limit to N images",
    )
    parser.add_argument(
        "--random", action="store_true",
        help="Randomly sample --count images instead of taking the first N",
    )
    parser.add_argument(
        "--copy", action="store_true",
        help="Copy files instead of symlinking (slower up front, faster stacking)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be linked without doing it",
    )
    parser.add_argument(
        "--list", dest="list_targets", nargs="?", const="", default=None,
        help="List known targets (optionally filter by pattern)",
    )
    args = parser.parse_args()

    if args.db:
        db_path = args.db
    else:
        local = local_db_path()
        db_path = str(local) if local.exists() else find_db(args.archive)
    if db_path is None:
        print("ERROR: No capture DB found. Run update_cache.py --subs first.", file=sys.stderr)
        sys.exit(1)

    cache = CaptureCache(db_path, read_only=True)

    if args.list_targets is not None:
        targets = list_targets(cache, args.list_targets or None)
        if not targets:
            print("No targets found.")
        else:
            for name, count in targets:
                print(f"  {name:30s}  ({count} stacks)")
        cache.close()
        return

    if not args.target:
        parser.error("TARGET is required (or use --list)")

    sub_total = cache.sub_count()
    if sub_total == 0:
        print("ERROR: No subs indexed yet. Run: python update_cache.py --subs", file=sys.stderr)
        cache.close()
        sys.exit(1)

    if args.ra is not None and args.dec is not None:
        ra, dec = args.ra, args.dec
        print(f"Using provided coordinates: RA={ra:.4f}  Dec={dec:.4f}")
    else:
        coords = resolve_target_coords(cache, args.target)
        if coords is not None:
            ra, dec = coords
            print(f"Target: {args.target} (from captures DB)")
        else:
            try:
                resolved = resolve_target(args.target)
                ra, dec = resolved["ra"], resolved["dec"]
                print(f"Target: {args.target} (resolved via catalog)")
            except ValueError:
                print(f"ERROR: Target '{args.target}' not found in captures DB or catalogs.", file=sys.stderr)
                print("Available targets matching your query:", file=sys.stderr)
                for name, count in list_targets(cache, args.target):
                    print(f"  {name}", file=sys.stderr)
                cache.close()
                sys.exit(1)
        print(f"Coordinates: RA={ra:.4f}  Dec={dec:.4f}")

    print(f"Search radius: {args.radius}°")
    filters = []
    if args.scope:
        filters.append(f"scope={args.scope}")
    if args.filter_name:
        filters.append(f"filter={args.filter_name}")
    if args.exposure:
        filters.append(f"exposure={args.exposure}s")
    if filters:
        print(f"Filters: {', '.join(filters)}")

    subs = collect_subs(cache, ra, dec, args.radius,
                        scope=args.scope, filter_name=args.filter_name,
                        exposure_sec=args.exposure)
    cache.close()

    if not subs:
        print("\nNo matching subs found.")
        sys.exit(0)

    total_available = len(subs)

    if args.count and args.count < len(subs):
        import random
        if args.random:
            subs = random.sample(subs, args.count)
        else:
            subs = subs[:args.count]

    # summarize what we found
    from collections import Counter
    by_scope = Counter(s["scope"] for s in subs)
    by_target = Counter(s["target"] for s in subs)
    by_filter = Counter(s["filter_name"] for s in subs)
    by_exp = Counter(s["exposure_sec"] for s in subs)
    by_dir = Counter(s["sub_dir"] for s in subs)

    selected_msg = ""
    if args.count and args.count < total_available:
        selected_msg = f" (selected {len(subs)} {'random' if args.random else 'first'} of {total_available})"
    print(f"\nFound {total_available} matching subs{selected_msg}:")
    print(f"   Scopes:   {dict(by_scope)}")
    print(f"  Targets:   {dict(by_target)}")
    print(f"  Filters:   {dict(by_filter)}")
    print(f"  Exposures: { {f'{k}s': v for k, v in by_exp.items()} }")
    print(f"  From {len(by_dir)} directories")

    if not args.work_dir:
        print("\nNo WORK_DIR specified — showing summary only.")
        return

    verb = "copy" if args.copy else "link"
    action = f"Would {verb}" if args.dry_run else (f"Copying" if args.copy else "Linking")
    print(f"\n{action} {len(subs)} files into {args.work_dir}")
    linked = symlink_subs(subs, args.work_dir, dry_run=args.dry_run, copy=args.copy)
    noun = "copies" if args.copy else "symlinks"
    print(f"{'Would create' if args.dry_run else 'Created'} {linked} {noun}")


if __name__ == "__main__":
    main()
