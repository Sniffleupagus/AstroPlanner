# Astro Target Recommender — Project Handoff

Standalone astrophotography planning tool. NOT a GMNJ segment — a separate
application that eventually feeds target lists to GMNJ's astronomy segment.

## Problem

Brian has smart scopes and a library of captured images on his RAID. He needs a
tool that knows what he's captured, what he wants, and what to shoot tonight.

## Core Features

1. **Capture database** — scan RAID for images (FITS headers or directory naming),
   track total integration time per target per filter (L/R/G/B/Ha/OIII/SII)

   /mnt/zarchive/Pictures/Astrophotography/{Seestar,Dwarf3,Dwarf-mini} have the photos
   from my current telescopes.

   - There are a lot of "Unknown", especially on Dwarfs, but are actually just offset from the target enough to not get automatically named by the app, and there is no way to add a name later (yet).
   - I would like to be able to group images by area in the sky, so I can collect things and build big mosaics
   - I would like to know how much exposure each area of the sky around a target has, so I know where to shoot more to even out the mosaic. For example, orionhorsemosaic.jpeg has plenty of exposure around the main nebula, but not much elsewhere

2. **Favorites / wishlist** — target list with desired integration times per filter
3. **Nightly recommender** — given tonight's conditions and location, score and rank
   targets by: visibility window, capture gaps, moon avoidance, seasonal optimality
4. **Horizon mask** — panorama photo(s) → sky/ground boundary → altitude-vs-azimuth
   profile (360 values). Replaces flat altitude threshold with per-azimuth lookup.

## Architecture

```
astro-planner/
├── planner/
│   ├── catalog.py          # Object catalog (reuse GMNJ's Sesame cache)
│   ├── capture_db.py       # Scan RAID, build integration-time database
│   ├── visibility.py       # Alt/az calc (reuse GMNJ's astropy code)
│   ├── horizon_mask.py     # Panorama → sky boundary → alt-vs-azimuth
│   ├── recommender.py      # Multi-factor scoring and ranking
│   ├── favorites.py        # Wishlist management
│   └── cli.py              # CLI interface (Click or Typer)
├── masks/                  # Horizon mask profiles per location
├── config.yaml             # RAID paths, default location, preferences
```

## Horizon Mask Pipeline

1. Take 1-2 panorama JPEGs of the observing site
2. Provide compass heading for a reference point in the image
3. OpenCV: detect sky/ground boundary via brightness gradient or segmentation
4. Convert boundary to altitude-vs-azimuth curve (needs camera FOV + orientation)
5. Output: JSON array of 360 altitude values, one per degree of azimuth
6. Alternative calibration: align against a known star field from a capture

## Recommender Scoring (per target per night)

- `visibility_score` — hours above horizon mask, peak altitude, meridian proximity
- `capture_gap_score` — how much more integration is needed vs. desired
- `condition_score` — seeing/transparency match for target type (faint nebula
  needs transparency; planetary detail needs seeing)
- `moon_score` — angular separation from moon, target brightness vs. moon phase
- `seasonal_score` — is this near its best season?
- Weighted combination → ranked list with explanations

## GMNJ Integration

Recommender outputs a nightly target list → writes `profiles/astronomy_targets.yaml`.
GMNJ's astronomy segment reads that file → spoken report of what to shoot tonight.
Could run as a pre-step before the evening briefing build.

## Code Reuse from GMNJ
- probably duplicate the code, not import GMNJ from the astro planner. planner will be on my laptop, not on the computer where GMNJ runs. I would not make extra efforts to tie the two together.  If anything, the GMNJ astro segment will later use AstroPlanner as a library, for better target predictions, but not the other way around.

- `gmnj/fetchers/astronomy_targets.py` — `resolve_target()`, Sesame cache at
  `~/.cache/gmnj/astro_catalog.json`
- `gmnj/fetchers/astronomy_forecast.py` — `build_night_summaries()` for sky quality
- astropy patterns for alt/az, twilight, moon calculations

## Tech Stack

- Python, astropy (shared with GMNJ)
- OpenCV for horizon mask processing
- Click or Typer for CLI
- JSON or SQLite for capture database

## Incremental Build Order

1. CLI that lists tonight's visible targets from a favorites YAML, scored by visibility
2. Add capture database scanning
3. Add capture gap scoring
4. Add horizon mask processing
5. Add full recommender with all scoring factors
6. Wire up GMNJ integration (auto-write astronomy_targets.yaml)



## other info

### How I capture data

- The scope automatically capture and generate stacks.  Seestar and Dwarf use different formats.
- after a capture, I will go through the individual images on the scope and remove the obvious bad ones (satellite streaks, shake, wind, etc). Then once a week or so, I rsync to the RAID and free up space on the scopes.

### Stacking
- I have stacked using Siril
- I have to copy all of the files I want to stack into a directory together
  - some sort of tool to collect all the images (symlink, probably) within some RA,DEC bounding box, for a particular scope (I dont think I can mix them together because they are different resolutions)

### Questions
- How hard would it be to "rename" the various Untitled data sets? They are not all the same.
  - cluster by location and overlap
  - present a list of potential "main" targets for each cluster
  - show the image(s) in the group
  - Does the whole folder and every file need to be renamed?
    - I think only the seestar app cares about the names
    - probably could move the "C50" Unknowns into a folder named C50_date_whatever_else_it_has, and not rename everything... i don't know.
    