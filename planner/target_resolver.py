"""Resolve catalog names to RA/Dec via CDS Sesame, with local cache.

Shares the cache with GMNJ's astronomy_targets fetcher so lookups done
by either project benefit both.
"""

import json
import logging
import re
from pathlib import Path

from astropy.coordinates import SkyCoord

log = logging.getLogger(__name__)

CACHE_PATH = Path.home() / ".cache" / "gmnj" / "astro_catalog.json"

_CALDWELL_MAP = {
    "1": "NGC 188", "2": "NGC 40", "3": "NGC 4236", "4": "NGC 7023",
    "5": "IC 342", "6": "NGC 6543", "7": "NGC 2403", "8": "NGC 559",
    "9": "Sh 2-155", "10": "NGC 663", "11": "NGC 7635", "12": "NGC 6946",
    "13": "NGC 457", "14": "NGC 869", "15": "NGC 884", "16": "NGC 7243",
    "17": "NGC 147", "18": "NGC 185", "19": "IC 5146", "20": "NGC 7000",
    "21": "NGC 4449", "22": "NGC 7662", "23": "NGC 891", "24": "NGC 1275",
    "25": "NGC 2419", "26": "NGC 4244", "27": "NGC 6888", "28": "NGC 752",
    "29": "NGC 5005", "30": "NGC 7331", "31": "IC 405", "32": "NGC 4631",
    "33": "NGC 6992", "34": "NGC 6960", "35": "NGC 4889", "36": "NGC 4559",
    "37": "NGC 6885", "38": "NGC 4565", "39": "NGC 2392", "40": "NGC 3626",
    "41": "Melotte 25", "42": "NGC 7006", "43": "NGC 7814", "44": "NGC 7479",
    "45": "NGC 5248", "46": "NGC 2261", "47": "NGC 6934", "48": "NGC 2775",
    "49": "NGC 2237", "50": "NGC 2244", "51": "IC 1613", "52": "NGC 4697",
    "53": "NGC 3115", "54": "NGC 2506", "55": "NGC 7009", "56": "NGC 246",
    "57": "NGC 6822", "58": "NGC 2360", "59": "NGC 3242", "60": "NGC 4038",
    "61": "NGC 4039", "62": "NGC 247", "63": "NGC 7293", "64": "NGC 2362",
    "65": "NGC 253", "66": "NGC 5694", "67": "NGC 1097", "68": "NGC 6729",
    "69": "NGC 6302", "70": "NGC 300", "71": "NGC 2477", "72": "NGC 55",
    "73": "NGC 1851", "74": "NGC 3132", "75": "NGC 6124", "76": "NGC 6231",
    "77": "NGC 5128", "78": "NGC 6541", "79": "NGC 3201", "80": "NGC 5139",
    "81": "NGC 6352", "82": "NGC 6193", "83": "NGC 4945", "84": "NGC 5286",
    "85": "IC 2391", "86": "NGC 6397", "87": "NGC 1261", "88": "NGC 5823",
    "89": "NGC 6087", "90": "NGC 2867", "91": "NGC 3532", "92": "NGC 3372",
    "93": "NGC 6752", "94": "NGC 4755", "95": "NGC 6025", "96": "NGC 2516",
    "97": "NGC 3766", "98": "NGC 4609", "99": "Coalsack", "100": "IC 2944",
    "101": "NGC 6744", "102": "NGC 2070", "103": "NGC 2547", "104": "NGC 362",
    "105": "NGC 4833", "106": "NGC 104", "107": "NGC 6101", "108": "NGC 4372",
    "109": "NGC 3195",
}

_CALDWELL_RE = re.compile(r'^(?:CALDWELL|C)\s*(\d{1,3})$')


def _load_cache() -> dict[str, dict]:
    if CACHE_PATH.exists():
        try:
            return json.loads(CACHE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict[str, dict]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache, indent=2))


def resolve_target(name: str) -> dict:
    """Resolve a catalog name to {"id": ..., "name": ..., "ra": ..., "dec": ...}.

    Uses the shared GMNJ cache, falling back to CDS Sesame online lookup.
    Raises ValueError if the name cannot be resolved.
    """
    cache = _load_cache()
    key = name.strip().upper()

    if key in cache:
        entry = cache[key]
        return {
            "id": name.strip(),
            "name": name.strip(),
            "ra": entry["ra_deg"],
            "dec": entry["dec_deg"],
        }

    m = _CALDWELL_RE.match(key)
    lookup_name = _CALDWELL_MAP.get(m.group(1)) if m else None
    lookup_name = lookup_name or name.strip()

    if lookup_name != name.strip():
        log.info("resolving %s -> %s via CDS Sesame", name, lookup_name)
    else:
        log.info("resolving %s via CDS Sesame", name)

    try:
        coord = SkyCoord.from_name(lookup_name)
    except Exception as e:
        raise ValueError(f"Could not resolve target '{name}': {e}") from e

    ra_deg, dec_deg = coord.ra.deg, coord.dec.deg
    cache[key] = {"ra_deg": ra_deg, "dec_deg": dec_deg}
    _save_cache(cache)

    return {
        "id": name.strip(),
        "name": name.strip(),
        "ra": float(ra_deg),
        "dec": float(dec_deg),
    }
