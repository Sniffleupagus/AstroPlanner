"""Scan astrophotography RAID directories for capture metadata."""

import json
import glob
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from astropy.io import fits


@dataclass
class CaptureRecord:
    scope: str
    target: str
    ra_deg: float
    dec_deg: float
    exposure_sec: float  # per-frame exposure
    num_frames: int
    total_exposure_sec: float
    filter_name: str
    gain: int
    date_obs: str
    directory: str
    is_mosaic: bool = False


def _parse_exposure(val) -> float:
    s = str(val)
    if "/" in s:
        num, den = s.split("/", 1)
        return float(num) / float(den)
    return float(s)


def _position_key(ra_deg: float, dec_deg: float, tolerance_deg: float = 0.3) -> tuple[int, int]:
    """Bin RA/DEC into grid cells for grouping nearby positions."""
    return (round(ra_deg / tolerance_deg), round(dec_deg / tolerance_deg))


def _is_dso_restack(filename: str) -> bool:
    return os.path.basename(filename).startswith("DSO_Stacked_")


def _dedup_seestar_stacks(records: list[tuple[str, CaptureRecord]]) -> list[CaptureRecord]:
    """Deduplicate stacks within each target directory.

    Groups by (directory, filter, approximate position). Per group, prefers
    DSO_Stacked (curated re-stack) over Stacked_, then latest date.
    """
    from collections import defaultdict
    groups: dict[tuple, list[tuple[str, CaptureRecord]]] = defaultdict(list)

    for fpath, rec in records:
        key = (rec.directory, rec.filter_name, _position_key(rec.ra_deg, rec.dec_deg))
        groups[key].append((fpath, rec))

    deduped = []
    total_dropped = 0
    for key, entries in groups.items():
        if len(entries) == 1:
            deduped.append(entries[0][1])
            continue

        def sort_key(item):
            fpath, rec = item
            return (_is_dso_restack(fpath), rec.date_obs)

        entries.sort(key=sort_key, reverse=True)
        best = entries[0][1]
        deduped.append(best)
        total_dropped += len(entries) - 1

    if total_dropped:
        print(f"  Deduplicated: dropped {total_dropped} superseded stacks")

    return deduped


def scan_seestar_stacks(base_path: str) -> list[CaptureRecord]:
    raw_records: list[tuple[str, CaptureRecord]] = []
    stack_files = glob.glob(os.path.join(base_path, "*/Stacked_*.fit")) + \
                  glob.glob(os.path.join(base_path, "*/DSO_Stacked_*.fit"))

    for fpath in stack_files:
        try:
            with fits.open(fpath) as hdul:
                h = hdul[0].header
                ra = h.get("RA")
                dec = h.get("DEC")
                if ra is None or dec is None:
                    continue

                stack_count = h.get("STACKCNT", 1)
                exp_per_frame = h.get("EXPTIME", h.get("EXPOSURE", 0))
                total_exp = h.get("TOTALEXP", stack_count * exp_per_frame)

                raw_records.append((fpath, CaptureRecord(
                    scope="Seestar S50",
                    target=h.get("OBJECT", "Unknown"),
                    ra_deg=float(ra),
                    dec_deg=float(dec),
                    exposure_sec=float(exp_per_frame),
                    num_frames=int(stack_count),
                    total_exposure_sec=float(total_exp),
                    filter_name=h.get("FILTER", "Unknown"),
                    gain=int(h.get("GAIN", 0)),
                    date_obs=h.get("DATE-OBS", ""),
                    directory=str(Path(fpath).parent),
                    is_mosaic="_mosaic" in Path(fpath).parent.name,
                )))
        except Exception as e:
            print(f"  WARN: {fpath}: {e}")

    print(f"  Raw stacks: {len(raw_records)}")
    return _dedup_seestar_stacks(raw_records)


def scan_dwarf_sessions(base_path: str, scope_name: str) -> list[CaptureRecord]:
    records = []
    astro_dir = os.path.join(base_path, "Astronomy")
    if not os.path.isdir(astro_dir):
        return records

    for entry in os.listdir(astro_dir):
        entry_path = os.path.join(astro_dir, entry)
        if not os.path.isdir(entry_path):
            continue
        if entry in ("CALI_FRAME", "DWARF_DARK"):
            continue

        is_mosaic = "_MOSAIC_" in entry

        if is_mosaic:
            records.extend(_scan_dwarf_mosaic(entry_path, scope_name))
        else:
            rec = _scan_dwarf_single(entry_path, scope_name)
            if rec:
                records.append(rec)

    return records


def _scan_dwarf_single(session_dir: str, scope_name: str) -> CaptureRecord | None:
    json_path = os.path.join(session_dir, "shotsInfo.json")
    if not os.path.exists(json_path):
        return None

    try:
        with open(json_path) as f:
            info = json.load(f)

        ra_hours = info.get("RA")
        dec_deg = info.get("DEC")
        if ra_hours is None or dec_deg is None:
            return None

        ra_deg = float(ra_hours) * 15.0
        exp_per_frame = _parse_exposure(info.get("exp", 0))
        shots_taken = int(info.get("shotsTaken", 0))
        total_exp = exp_per_frame * shots_taken

        return CaptureRecord(
            scope=scope_name,
            target=info.get("target", "Unknown"),
            ra_deg=ra_deg,
            dec_deg=float(dec_deg),
            exposure_sec=exp_per_frame,
            num_frames=shots_taken,
            total_exposure_sec=total_exp,
            filter_name=info.get("ir", "Unknown"),
            gain=int(info.get("gain", 0)),
            date_obs=Path(session_dir).name.split("_")[-1],
            directory=session_dir,
            is_mosaic=False,
        )
    except Exception as e:
        print(f"  WARN: {session_dir}: {e}")
        return None


def _scan_dwarf_mosaic(mosaic_dir: str, scope_name: str) -> list[CaptureRecord]:
    """For Dwarf mosaics, read the top-level shotsInfo.json.

    The panels share a single RA/DEC in shotsInfo (the mosaic center).
    For v1 we treat the whole mosaic as one point; later we can read
    per-panel FITS headers for exact positions.
    """
    json_path = os.path.join(mosaic_dir, "shotsInfo.json")
    if not os.path.exists(json_path):
        return []

    try:
        with open(json_path) as f:
            info = json.load(f)

        ra_hours = info.get("RA")
        dec_deg = info.get("DEC")
        if ra_hours is None or dec_deg is None:
            return []

        ra_deg = float(ra_hours) * 15.0
        exp_per_frame = _parse_exposure(info.get("exp", 0))
        shots_taken = int(info.get("shotsTaken", 0))
        total_exp = exp_per_frame * shots_taken

        return [CaptureRecord(
            scope=scope_name,
            target=info.get("target", "Unknown"),
            ra_deg=ra_deg,
            dec_deg=float(dec_deg),
            exposure_sec=exp_per_frame,
            num_frames=shots_taken,
            total_exposure_sec=total_exp,
            filter_name=info.get("ir", "Unknown"),
            gain=int(info.get("gain", 0)),
            date_obs=Path(mosaic_dir).name.split("_")[-1],
            directory=mosaic_dir,
            is_mosaic=True,
        )]
    except Exception as e:
        print(f"  WARN: {mosaic_dir}: {e}")
        return []


def scan_all(raid_base: str = "/mnt/zarchive/Pictures/Astrophotography") -> list[CaptureRecord]:
    records = []

    print("Scanning Seestar stacks...")
    seestar = scan_seestar_stacks(os.path.join(raid_base, "Seestar"))
    print(f"  Found {len(seestar)} stacked captures")
    records.extend(seestar)

    print("Scanning Dwarf3 sessions...")
    dwarf3 = scan_dwarf_sessions(os.path.join(raid_base, "Dwarf3"), "Dwarf 3")
    print(f"  Found {len(dwarf3)} sessions")
    records.extend(dwarf3)

    print("Scanning Dwarf-mini sessions...")
    mini = scan_dwarf_sessions(os.path.join(raid_base, "Dwarf-mini"), "Dwarf mini")
    print(f"  Found {len(mini)} sessions")
    records.extend(mini)

    print(f"Total: {len(records)} capture records")
    return records
