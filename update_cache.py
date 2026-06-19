#!/usr/bin/env python3
"""Build or refresh the capture metadata cache.

Run this whenever new captures are added to the archive. The resulting DB can
be copied to the RAID so scan.py can use it without writing anything there.

Typical workflow:
  # Build/refresh local cache from whatever archive path is mounted:
  python update_cache.py --archive /Volumes/zarchive/Pictures/Astrophotography

  # Copy to RAID (optional, so scan.py on any machine can use it):
  scp cache/captures.db nas:/path/to/archive/astroplanner_cache.db

  # Force local even if a RAID copy exists:
  python update_cache.py --local
"""

import argparse
import os
import sys

from planner.capture_cache import CaptureCache, raid_db_path, local_db_path
from planner.scanner import (
    scan_seestar_stacks,
    scan_seestar_subs,
    scan_dwarf_sessions,
    scan_dwarf_subs,
)

_DEFAULT_ARCHIVE = "/mnt/zarchive/Pictures/Astrophotography"


def _collect_all_files(archive: str) -> set[str]:
    """Return the set of file paths the scanner will visit."""
    import glob
    files: set[str] = set()

    seestar_base = os.path.join(archive, "Seestar")
    files.update(glob.glob(os.path.join(seestar_base, "*/Stacked_*.fit")))
    files.update(glob.glob(os.path.join(seestar_base, "*/DSO_Stacked_*.fit")))

    for scope_dir in ("Dwarf3", "Dwarf-mini"):
        astro = os.path.join(archive, scope_dir, "Astronomy")
        if not os.path.isdir(astro):
            continue
        for entry in os.listdir(astro):
            if entry in ("CALI_FRAME", "DWARF_DARK"):
                continue
            json_path = os.path.join(astro, entry, "shotsInfo.json")
            if os.path.exists(json_path):
                files.add(json_path)

    return files


def main():
    parser = argparse.ArgumentParser(
        description="Build/refresh the capture metadata cache DB."
    )
    parser.add_argument(
        "--archive", default=_DEFAULT_ARCHIVE,
        help=f"Astrophotography archive base path (default: {_DEFAULT_ARCHIVE})",
    )
    parser.add_argument(
        "--local", action="store_true",
        help="Write to local cache/captures.db even if the RAID copy is writable",
    )
    parser.add_argument(
        "--db", metavar="PATH",
        help="Explicit DB path (overrides --local and RAID default)",
    )
    parser.add_argument(
        "--subs", action="store_true",
        help="Also index raw sub exposures (slow first run, ~82k FITS headers)",
    )
    parser.add_argument(
        "--subs-only", action="store_true",
        help="Only index raw sub exposures, skip stacks",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.archive):
        print(f"ERROR: archive path not found: {args.archive}", file=sys.stderr)
        sys.exit(1)

    if args.db:
        db_path = args.db
    elif args.local:
        db_path = str(local_db_path())
    else:
        raid = raid_db_path(args.archive)
        if os.access(raid.parent, os.W_OK):
            db_path = str(raid)
            print(f"RAID is writable — writing to {db_path}")
            print("  (use --local to write to cache/captures.db instead)")
        else:
            db_path = str(local_db_path())
            print(f"RAID not writable — falling back to {db_path}")

    print(f"Archive: {args.archive}")
    print(f"DB:      {db_path}")
    print()

    with CaptureCache(db_path, read_only=False) as cache:
        total = 0

        if not args.subs_only:
            existing_paths = cache.all_paths()

            print("Scanning Seestar stacks...")
            seestar = scan_seestar_stacks(os.path.join(args.archive, "Seestar"), cache)
            print(f"  {len(seestar)} records")

            print("Scanning Dwarf3 sessions...")
            dwarf3 = scan_dwarf_sessions(os.path.join(args.archive, "Dwarf3"), "DWARF 3", cache)
            print(f"  {len(dwarf3)} records")

            print("Scanning Dwarf-mini sessions...")
            mini = scan_dwarf_sessions(os.path.join(args.archive, "Dwarf-mini"), "DWARF mini", cache)
            print(f"  {len(mini)} records")

            current_files = _collect_all_files(args.archive)
            stale = existing_paths - current_files
            if stale:
                print(f"\nRemoving {len(stale)} stale capture entries")
                cache.delete_paths(stale)

            total = len(seestar) + len(dwarf3) + len(mini)
            print(f"\n{total} capture records cached")

        if args.subs or args.subs_only:
            print("\nScanning Seestar raw subs...")
            seestar_subs = scan_seestar_subs(os.path.join(args.archive, "Seestar"), cache)

            print("\nScanning Dwarf3 raw subs...")
            dwarf3_subs = scan_dwarf_subs(os.path.join(args.archive, "Dwarf3"), "DWARF 3", cache)

            print("\nScanning Dwarf-mini raw subs...")
            mini_subs = scan_dwarf_subs(os.path.join(args.archive, "Dwarf-mini"), "DWARF mini", cache)

            sub_total = seestar_subs + dwarf3_subs + mini_subs
            print(f"\n{sub_total} sub records cached")

    print(f"\nDone. DB: {db_path}")
    if not args.local and not args.db and str(local_db_path()) == db_path:
        print(f"To use on another machine, copy to the RAID:")
        print(f"  scp {db_path} <nas>:{args.archive}/astroplanner_cache.db")


if __name__ == "__main__":
    main()
