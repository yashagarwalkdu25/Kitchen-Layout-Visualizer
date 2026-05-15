# Kitchen Layout Visualizer

Renders AI-generated kitchen furniture layouts from a JSON output envelope to PNG images — both a 2D top-down floor plan with grid and a 3D isometric view.

## Files

| File | Role |
|------|------|
| `render.py` | CLI entry point — reads an output JSON, renders every variant |
| `layout.py` | `LayoutVisualizer` class — all drawing logic (2D + 3D) |
| `catalog.json` | Product catalog — SKUs with canonical dimensions, colors, constraints |
| `sample_input.json` | Example input to send to the layout AI (room + preferences) |
| `output.json` | Example output envelope with 5 layout variants |

## Setup

```bash
# 1. Clone the repository
git clone <repo-url>

# 2. Enter the project directory
cd PS

# 3. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate      # macOS / Linux
# .venv\Scripts\activate       # Windows
```

## Prerequisites

```bash
pip install matplotlib numpy
```

No other dependencies. Drop `render.py`, `layout.py`, and `catalog.json` in the same folder and you're set.

## Quick start

```bash
# Render all variants from the sample output — PNGs go to ./renders/
python render.py output.json

# Custom output directory
python render.py output.json --out-dir my_renders/

# Only 2D top-down
python render.py output.json --2d-only

# Only 3D isometric
python render.py output.json --3d-only

# Pop the interactive 3D viewer for each variant (instead of headless)
python render.py output.json --show

# Strict mode — fail if any SKU is missing from the catalog
python render.py output.json --strict

# Point to a different catalog
python render.py output.json --catalog /path/to/catalog.json
```

For each variant `<id>` the script writes:

```
renders/<id>_top.png   — 2D top-down floor plan with 2 m grid
renders/<id>_3d.png    — 3D isometric view
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All variants rendered successfully |
| 1 | One or more variants failed (details on stderr) |
| 2 | Bad CLI usage or unreadable input file |

## Input format (`sample_input.json`)

Describes the room and user preferences. Send this to your layout AI to generate an `output.json`.

```json
{
  "environment": {
    "floor": { "points": [{"x": 0, "y": 0, "z": 0}, ...] },
    "wall": [
      {
        "name": "north_wall",
        "anchor": "north",
        "thickness_mm": 100,
        "has_cabinets": true,
        "dimensions": {"length_mm": 4200, "height": 2700},
        "points": [...]
      }
    ],
    "openings": [
      {
        "id": "south_door", "kind": "door", "wall": "south",
        "offset_mm": 600, "width_mm": 900, "height_mm": 2100,
        "hinge_side": "left", "swing_direction": "in",
        "center_mm": {"x": 1050, "y": 0, "z": 1050},
        "dimensions_mm": {"width": 900, "depth": 100, "height": 2100}
      }
    ]
  },
  "preferences": {
    "budget_tier": "mid",
    "must_have": ["dishwasher", "hood"],
    "avoid": ["double_sink"],
    "prompt": ""
  }
}
```

All coordinates are in **millimetres**. The origin is the south-west floor corner.

## Output format (`output.json`)

The visualizer expects the output envelope described below. Each entry in `layouts` is one variant.

```json
{
  "request_id": "uuid",
  "duration_ms": 18420,
  "layouts": [
    {
      "id": "variant-1",
      "family": "L-shaped",
      "score": 0.91,
      "violations": [],
      "environment": { "floor": {...}, "wall": [...] },
      "layout": {
        "north_wall":      { "is_wall": true, "position_mm": {...}, "dimensions_mm": {...}, "rotation_z_deg": 0 },
        "south_door":      { "is_door": true,   "anchor_wall": "south", "position_mm": {...}, "dimensions_mm": {...}, "rotation_z_deg": 0 },
        "north_window":    { "is_window": true,  "anchor_wall": "north", "position_mm": {...}, "dimensions_mm": {...}, "rotation_z_deg": 0 },
        "sink_1": {
          "is_wall": false,
          "product_id": "SKU-S01",
          "position_mm": {"x": 2100, "y": 2725, "z": 450},
          "rotation_z_deg": 180,
          "anchor_wall": "north",
          "zone_type": "cleaning"
        }
      },
      "rationale": [
        {"rule_id": "LAYOUT-01", "text": "Sink centred under north window"}
      ]
    }
  ]
}
```

### Layout item fields

| Field | Required | Description |
|-------|----------|-------------|
| `product_id` | for furniture | SKU from `catalog.json` — dimensions are taken from the catalog, not the JSON |
| `position_mm` | yes | `{x, y, z}` centre of the item in mm |
| `rotation_z_deg` | yes | Rotation around the Z-axis in degrees |
| `anchor_wall` | recommended | Which wall the item faces (`north`, `south`, `east`, `west`) |
| `zone_type` | optional | Work zone color hint: `cooking`, `cleaning`, `cooling`, `preparation` |
| `is_wall` / `is_door` / `is_window` | for room structure | Marks structural elements |
| `dimensions_mm` | fallback only | Used only when `product_id` is absent or not in catalog |

## Catalog (`catalog.json`)

The catalog is the single source of truth for item dimensions and colors. The visualizer resolves dimensions from the catalog and ignores any `dimensions_mm` supplied by the layout AI for known SKUs.

```json
[
  {
    "id": "SKU-C01",
    "type": "base_cabinet_600",
    "category": "cabinet",
    "color": "#C8A878",
    "width_mm": 600,
    "depth_mm": 600,
    "height_mm": 900,
    "must_attach_to": "wall",
    "style_tags": ["modern", "traditional"],
    "price_tier": "low",
    "constraints": {
      "front_clearance_mm": 914,
      "needs_water": false,
      "needs_power": false
    }
  }
]
```

### Available SKUs

| SKU | Type | W × D × H (mm) |
|-----|------|----------------|
| SKU-S01 | Single sink | 600 × 550 × 900 |
| SKU-S02 | Double sink | 900 × 550 × 900 |
| SKU-A01 | Stove 60 cm | 600 × 600 × 900 |
| SKU-A02 | Fridge 70 cm | 700 × 700 × 1800 |
| SKU-A03 | Dishwasher 60 cm | 600 × 600 × 850 |
| SKU-A04 | Built-in oven 60 cm | 600 × 600 × 600 |
| SKU-A05 | Hood 60 cm | 600 × 500 × 400 |
| SKU-A06 | Built-in oven 75 cm | 750 × 600 × 750 |
| SKU-A07 | Built-in microwave 60 cm | 600 × 400 × 400 |
| SKU-A08 | Slim dishwasher 45 cm | 450 × 600 × 850 |
| SKU-C01 | Base cabinet 600 | 600 × 600 × 900 |
| SKU-C02 | Base cabinet 900 | 900 × 600 × 900 |
| SKU-C03 | Corner cabinet 900 | 900 × 900 × 900 |
| SKU-C04 | Tall cabinet 600 | 600 × 600 × 2100 |
| SKU-C05 | Wall cabinet 600 | 600 × 350 × 700 |
| SKU-C06 | Wall cabinet 900 | 900 × 350 × 700 |
| SKU-C07 | Base cabinet 450 | 450 × 600 × 900 |
| SKU-C08 | Base cabinet 750 | 750 × 600 × 900 |
| SKU-C09 | Base cabinet 1200 | 1200 × 600 × 900 |
| SKU-C10 | Base drawer 600 | 600 × 600 × 900 |
| SKU-C11 | Blind corner cabinet 900 | 900 × 900 × 900 |
| SKU-C12–C16 | Wall cabinets (various) | 450–1200 × 350 × 700 |
| SKU-I01 | Kitchen island 1200 | 1200 × 900 × 900 |
| SKU-F01 | Tap mixer | 200 × 200 × 300 |

## Catalog auto-discovery

The visualizer searches for `catalog.json` in this order (first found wins):

1. Same folder as `layout.py`
2. `KITCHEN_CATALOG_PATH` environment variable
3. `./catalog.json` in the current working directory
4. `./starter/catalog.json`
5. `<repo>/kitchen-layout-worker/starter/catalog.json`

Or pass it explicitly: `python render.py output.json --catalog /path/to/catalog.json`

## Wrong-input detection

If a layout item declares `dimensions_mm` that differ from the catalog by more than 5 mm, the visualizer:

- Renders the item at the **catalog dimensions** (catalog always wins)
- Draws a **red dashed outline** around the corrected item in both 2D and 3D

`output.json` includes `variant-3-error-shrunk` and `variant-4-error-inflated` as demos of this behaviour.

## Using `LayoutVisualizer` programmatically

```python
from layout import LayoutVisualizer

viz = LayoutVisualizer(catalog="catalog.json", strict=False)

# 2D top-down with grid
viz.plot_layout_2d_with_grid(
    environment_data=variant["environment"],
    layout_data=variant["layout"],
    title="My Kitchen",
    view="top",
    save_to="output_top.png",
    show=False,
)

# 3D isometric
viz.plot_layout_3d(
    environment_data=variant["environment"],
    layout_data=variant["layout"],
    title="My Kitchen 3D",
    save_to="output_3d.png",
    show=False,
)
```
