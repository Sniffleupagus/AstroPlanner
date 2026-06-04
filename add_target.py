#!/usr/bin/env python3
"""Add a target to targets.json by resolving its catalog name."""

import argparse
import json
import sys
from pathlib import Path

from planner.target_resolver import resolve_target

TARGETS_FILE = Path("targets.json")


def main():
    parser = argparse.ArgumentParser(description="Add an astronomy target to targets.json")
    parser.add_argument("name", help="Catalog name (e.g. M42, NGC 2024, C33, 'Horsehead Nebula')")
    parser.add_argument("--display-name", "-d", help="Friendly display name (default: same as catalog name)")
    parser.add_argument("--id", dest="target_id", help="Short ID for the target (default: catalog name with spaces removed)")
    parser.add_argument("--dry-run", "-n", action="store_true", help="Show what would be added without writing")
    args = parser.parse_args()

    try:
        resolved = resolve_target(args.name)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    target_id = args.target_id or args.name.strip().replace(" ", "")
    display_name = args.display_name or args.name.strip()

    entry = {
        "id": target_id,
        "name": display_name,
        "ra": round(resolved["ra"], 4),
        "dec": round(resolved["dec"], 4),
    }

    if TARGETS_FILE.exists():
        with open(TARGETS_FILE) as f:
            data = json.load(f)
    else:
        data = {"targets": []}

    existing_ids = {t["id"].upper() for t in data["targets"]}
    if target_id.upper() in existing_ids:
        print(f"Target '{target_id}' already exists in {TARGETS_FILE}")
        sys.exit(1)

    print(f"Resolved: RA={entry['ra']:.4f}°  Dec={entry['dec']:.4f}°")
    print(f"  id:   {entry['id']}")
    print(f"  name: {entry['name']}")

    if args.dry_run:
        print("\n(dry run — not written)")
        return

    data["targets"].append(entry)
    with open(TARGETS_FILE, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")

    print(f"\nAdded to {TARGETS_FILE}")


if __name__ == "__main__":
    main()
