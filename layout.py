"""Visualization helpers for furniture layouts.

Input-validation contract
-------------------------
For any placed item that carries a ``product_id`` (SKU), the visualizer
resolves ``dimensions_mm`` from the **catalog** instead of trusting the
JSON. Only ``position_mm`` and ``rotation_z_deg`` are taken from the
client payload. This guards the rendered image against wrong-input
dimensions (inflated or shrunken boxes) while keeping the catalog as
the single source of truth.

Room-structure entries (``is_wall``/``is_floor``/``is_roof`` without a
``zone_type``) are excluded from this rule because walls/floors are not
catalog products; their ``dimensions_mm`` continue to be trusted.

Catalog resolution order (first hit wins):
    1. ``catalog`` kwarg passed to ``LayoutVisualizer(...)`` (dict | path)
    2. ``KITCHEN_CATALOG_PATH`` env var
    3. ``catalog.json`` next to this file (drop-in deployment)
    4. ``./catalog.json`` in the current working directory
    5. ``./starter/catalog.json`` in the current working directory
    6. ``<repo>/kitchen-layout-worker/starter/catalog.json`` (in-repo default)

Standalone use
--------------
This module is intentionally self-contained: it only requires
``matplotlib`` and ``numpy``. Participants who receive only this file
+ ``catalog.json`` can drop both into any folder and import directly:

    pip install matplotlib numpy
    python -c "from layout import LayoutVisualizer; ..."
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path
from typing import Any

# import matplotlib
# matplotlib.use("Agg")  # Use non-GUI backend on Windows to avoid Tkinter threading issues
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Arc, Patch, Rectangle
from matplotlib.transforms import Affine2D
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

# Optional dependency on the worker's config module.
# Kept optional so this file can be shipped to hackathon participants
# standalone without dragging the whole `app/` package along.
try:  # pragma: no cover - import guard
    from app.config.constants import MM_TO_METERS
except ModuleNotFoundError:  # pragma: no cover - standalone path
    MM_TO_METERS = 0.001  # 1 mm = 0.001 m — Cyncly convention

def _build_catalog_candidates() -> tuple[Path, ...]:
    """Catalog auto-discovery candidates (first existing file wins).

    Ordered from most-specific (next to this file, drop-in style) to
    most-generic (in-repo worker default). The repo-default candidate
    is built defensively because participants may drop this file into
    a shallow directory like ``/tmp/demo/`` where ``parents[4]`` would
    raise ``IndexError``.
    """
    here = Path(__file__).resolve()
    candidates: list[Path] = [
        here.parent / "catalog.json",
        Path.cwd() / "catalog.json",
        Path.cwd() / "starter" / "catalog.json",
    ]
    # In-repo default: <worker_root>/starter/catalog.json
    # (this file lives 4 directories deep inside the worker)
    try:
        candidates.append(here.parents[4] / "starter" / "catalog.json")
    except IndexError:
        pass
    return tuple(candidates)


_CATALOG_CANDIDATES: tuple[Path, ...] = _build_catalog_candidates()

# Color coding for different item types
ITEM_TYPE_COLORS = {
    "corner_cabinet": "#FF6B6B",      # Coral red
    "standard_cabinet": "#4ECDC4",     # Teal
    "wall_cabinet": "#45B7D1",         # Sky blue
    "tall_cabinet": "#96CEB4",         # Sage green
    "integrated_appliance": "#9B59B6", # Purple
    "freestanding_appliance": "#F39C12", # Orange
    "countertop": "#95A5A6",           # Gray
    "sink": "#3498DB",                 # Blue
    "worktop": "#BDC3C7",              # Light gray
    "default": "#2ECC71",              # Green
}

# Zone colors for visualization
ZONE_COLORS = {
    "cooking": "#FF6B6B",  # Coral red
    "cleaning": "#4ECDC4",  # Teal
    "cooling": "#45B7D1",  # Sky blue
    "preparation": "#FFD700",  # Gold
    "default": "#95A5A6",  # Gray
}

# Door / window styling (matches PS.MD §4 SVG conventions:
# door swing arc = dashed orange; door leaf = warm brown; window = light blue).
DOOR_COLOR = "#8B5A2B"
DOOR_EDGE_COLOR = "#3E2515"
DOOR_SWING_COLOR = "#FF8C00"
WINDOW_COLOR = "#A8D0F0"
WINDOW_FRAME_COLOR = "#2E6FB5"

# Wrong-input overlay: items whose JSON-declared dimensions_mm disagree
# with the catalog get a red dashed outline so the silent catalog-wins
# behaviour in `_resolve_dims_and_label` becomes obvious in the rendered
# PNG (treated as a per-item input error, not a fatal failure).
ERROR_OUTLINE_COLOR = "#E00033"
DIM_ERROR_TOLERANCE_MM = 5.0

# Keywords to identify item types from name or classification
ITEM_TYPE_KEYWORDS = {
    "corner_cabinet": ["corner", "l-shape", "blind_corner"],
    "wall_cabinet": ["wall_cabinet", "wall cabinet", "upper"],
    "tall_cabinet": ["tall", "pantry", "oven_housing", "utility"],
    "integrated_appliance": ["integrated", "built-in", "builtin", "dishwasher", "oven", "microwave", "hood"],
    "freestanding_appliance": ["fridge", "refrigerator", "range", "cooktop", "freestanding"],
    "countertop": ["countertop", "counter top"],
    "sink": ["sink"],
    "worktop": ["worktop", "work top"],
    "standard_cabinet": ["base", "cabinet", "drawer"],
}


class LayoutVisualizer:
    """Structured visualization adapted from the layout engine.

    Args:
        catalog: Either a SKU-keyed dict (``{"SKU-C01": {...}, ...}``),
            a path to a ``catalog.json`` file, or ``None`` to auto-load.
        strict: If ``True``, raise ``ValueError`` when a placed item is
            missing ``product_id`` or its SKU is not in the catalog.
            If ``False`` (default), fall back to the JSON-supplied
            ``dimensions_mm`` and emit a single warning per unknown SKU.
    """

    def __init__(
        self,
        catalog: dict[str, dict[str, Any]] | str | Path | None = None,
        strict: bool = False,
    ) -> None:
        self._strict = strict
        self._catalog: dict[str, dict[str, Any]] = self._load_catalog(catalog)
        self._warned_skus: set[str] = set()

    @staticmethod
    def _load_catalog(
        source: dict[str, dict[str, Any]] | str | Path | None,
    ) -> dict[str, dict[str, Any]]:
        """Resolve and load the catalog into a SKU-keyed dict.

        See module docstring for the full resolution order.
        """
        if isinstance(source, dict):
            return source

        path: Path | None = None
        if isinstance(source, (str, Path)):
            path = Path(source)
        elif (env := os.environ.get("KITCHEN_CATALOG_PATH")):
            path = Path(env)
        else:
            for candidate in _CATALOG_CANDIDATES:
                if candidate.exists():
                    path = candidate
                    break

        if path is None or not path.exists():
            warnings.warn(
                "LayoutVisualizer: no catalog.json found in any known "
                "location; falling back to JSON-supplied dimensions_mm "
                "for every item. Place catalog.json next to this file, "
                "in ./starter/, set KITCHEN_CATALOG_PATH, or pass "
                "catalog=... to enable the catalog dimension validation.",
                stacklevel=2,
            )
            return {}

        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)

        if isinstance(data, list):
            return {item["id"]: item for item in data if "id" in item}
        if isinstance(data, dict):
            return data
        raise ValueError(
            f"Unsupported catalog format at {path}: expected list or dict."
        )

    def _is_room_structure(self, data: dict[str, Any]) -> bool:
        """Return True for walls/floors/roofs that are NOT zone overlays."""
        is_struct = bool(
            data.get("is_wall") or data.get("is_floor") or data.get("is_roof")
        )
        return is_struct and not data.get("zone_type")

    def _dim_error_info(
        self, data: dict[str, Any]
    ) -> dict[str, tuple[float, float]] | None:
        """Detect a wrong-input dimension error (JSON dims disagree with catalog dims).

        Returns a dict ``{axis: (declared, catalog)}`` for every axis that
        exceeds the tolerance, or ``None`` when:
          * the item is not a placed catalog product (no SKU or unknown SKU);
          * no ``dimensions_mm`` were supplied to compare against; or
          * every axis matches the catalog within ``DIM_ERROR_TOLERANCE_MM``.

        This is purely diagnostic — the visualizer continues to render at
        catalog dimensions in ``_resolve_dims_and_label``; this helper only
        surfaces *that* the JSON declared a wrong size so the drawer can
        flag it visually.
        """
        sku = data.get("product_id")
        if not sku or sku not in self._catalog or "dimensions_mm" not in data:
            return None
        catalog = self._catalog[sku]
        declared = data["dimensions_mm"]
        diffs: dict[str, tuple[float, float]] = {}
        for axis, key in (
            ("width", "width_mm"),
            ("depth", "depth_mm"),
            ("height", "height_mm"),
        ):
            try:
                d_val = float(declared.get(axis, catalog[key]))
            except (TypeError, ValueError):
                continue
            c_val = float(catalog[key])
            if abs(d_val - c_val) > DIM_ERROR_TOLERANCE_MM:
                diffs[axis] = (d_val, c_val)
        return diffs or None

    def _resolve_dims_and_label(
        self, name: str, data: dict[str, Any]
    ) -> tuple[dict[str, float], str, str]:
        """Resolve authoritative dimensions + on-canvas label for an item.

        Returns a 3-tuple ``(dims_mm, primary_label, type_label)`` where
        ``type_label`` is the catalog ``type`` field (e.g. ``"base_cabinet_900"``)
        when resolvable, else ``""``. Callers decide whether to render
        the type on a second line.

        Resolution order:
            1. Room structure (is_wall/is_floor/is_roof, no zone_type)
               -> trust JSON ``dimensions_mm``, label = item name, type = "".
            2. Placed item with ``product_id`` in catalog
               -> catalog dimensions WIN, label = SKU id, type = catalog type.
            3. Strict mode + unresolved SKU -> ``ValueError``.
            4. Lenient fallback (no SKU OR unknown SKU)
               -> warn (if SKU given) and use JSON ``dimensions_mm``.

        The ``zone_type`` field is purely a color hint; it is NOT a
        signal that the box is a synthetic zone overlay. Synthetic
        zone overlays simply omit ``product_id`` and fall through to
        rule 4, where the JSON dimensions are honored.
        """
        if self._is_room_structure(data):
            return data["dimensions_mm"], name, ""

        sku = data.get("product_id")
        if sku and sku in self._catalog:
            product = self._catalog[sku]
            return (
                {
                    "width": float(product["width_mm"]),
                    "depth": float(product["depth_mm"]),
                    "height": float(product["height_mm"]),
                },
                sku,
                str(product.get("type", "")),
            )

        if self._strict:
            raise ValueError(
                f"Item '{name}' has no resolvable product_id "
                f"(got {sku!r}); strict mode rejects JSON-supplied dims."
            )
        if sku and sku not in self._warned_skus:
            self._warned_skus.add(sku)
            warnings.warn(
                f"LayoutVisualizer: SKU {sku!r} (item '{name}') not found "
                f"in catalog; falling back to JSON-supplied dimensions_mm.",
                stacklevel=3,
            )

        if "dimensions_mm" not in data:
            raise ValueError(
                f"Item '{name}' has neither a resolvable product_id "
                f"(got {sku!r}) nor dimensions_mm; the visualizer cannot "
                f"determine its size."
            )
        # Unknown/missing SKU: surface the zone_type as a coarse hint
        # when present so the box isn't completely unlabeled.
        return data["dimensions_mm"], sku or name, str(data.get("zone_type") or "")

    def _to_meters(self, value: float | np.ndarray) -> float | np.ndarray:
        return value * MM_TO_METERS

    def _get_item_type(self, name: str, data: dict[str, Any]) -> str:
        """Determine the item type from name and data for color coding.

        Args:
            name: Item name/code
            data: Item data dict containing classification info

        Returns:
            Item type key for color lookup
        """
        name_lower = name.lower()

        # Check classification if available (handle both flat and nested structures)
        classification = data.get("classification", {})

        # Handle nested structure from LangGraph: { baseItemType, characteristics: { subType, cabinetType, ... } }
        characteristics = classification.get("characteristics", {})
        base_item_type = classification.get("baseItemType", "").lower()
        full_item_type = classification.get("fullItemType", "").lower()

        # Extract from characteristics (can be lists or strings)
        sub_types = characteristics.get("subType", [])
        if isinstance(sub_types, str):
            sub_types = [sub_types]
        sub_type = " ".join(s.lower() for s in sub_types)

        cabinet_types = characteristics.get("cabinetType", [])
        if isinstance(cabinet_types, str):
            cabinet_types = [cabinet_types]
        cabinet_type = " ".join(c.lower() for c in cabinet_types)

        installation_types = characteristics.get("installationType", [])
        if isinstance(installation_types, str):
            installation_types = [installation_types]
        installation_type = " ".join(i.lower() for i in installation_types)

        # Handle flat structure (legacy): { category, subType, type }
        category = classification.get("category", "").lower()
        flat_sub_type = classification.get("subType", "")
        if isinstance(flat_sub_type, str):
            flat_sub_type = flat_sub_type.lower()
        item_type = classification.get("type", "").lower()

        # Combine all text for matching
        search_text = f"{name_lower} {category} {sub_type} {flat_sub_type} {item_type} {cabinet_type} {base_item_type} {full_item_type} {installation_type}"

        # Check each type's keywords (order matters - more specific first)
        for type_key, keywords in ITEM_TYPE_KEYWORDS.items():
            for keyword in keywords:
                if keyword in search_text:
                    return type_key

        return "default"

    def _catalog_color(self, data: dict[str, Any]) -> str | None:
        """Return the catalog ``color`` hex for the item, if available.

        Honoured only when the item carries a ``product_id`` resolvable
        in the catalog AND the catalog entry has a non-empty ``color``
        field. This lets per-SKU material colors (oak, walnut, matte
        black, stainless, …) render true-to-life and override the
        generic type-based palette.
        """
        sku = data.get("product_id")
        if not sku or sku not in self._catalog:
            return None
        color = self._catalog[sku].get("color")
        if isinstance(color, str) and color:
            return color
        return None

    def _get_item_color(self, name: str, data: dict[str, Any]) -> str:
        """Get color for an item.

        Priority:
            1. Catalog ``color`` field (per-SKU material color) — wins
               so the actual material color renders exactly.
            2. ``ITEM_TYPE_COLORS`` fallback by detected item type.

        Args:
            name: Item name/code
            data: Item data dict

        Returns:
            Hex color string
        """
        catalog_color = self._catalog_color(data)
        if catalog_color is not None:
            return catalog_color
        item_type = self._get_item_type(name, data)
        return ITEM_TYPE_COLORS.get(item_type, ITEM_TYPE_COLORS["default"])

    def create_furniture_box_3d(self, position: dict[str, float], dimensions: dict[str, float], rotation_z: float) -> np.ndarray:
        """Generate 3D vertices for a furniture box centered on the provided position."""

        pos_m = self._to_meters(np.array([position["x"], position["y"], position["z"]]))
        dims_m = self._to_meters(
            np.array([dimensions["width"], dimensions["depth"], dimensions["height"]])
        )

        half_w, half_d, half_h = dims_m / 2.0
        base = np.array(
            [
                [-half_w, -half_d, -half_h],
                [half_w, -half_d, -half_h],
                [half_w, half_d, -half_h],
                [-half_w, half_d, -half_h],
            ]
        )
        top = np.array(
            [
                [-half_w, -half_d, half_h],
                [half_w, -half_d, half_h],
                [half_w, half_d, half_h],
                [-half_w, half_d, half_h],
            ]
        )

        angle = np.deg2rad(rotation_z)
        cos_a, sin_a = np.cos(angle), np.sin(angle)
        rot_matrix = np.array(
            [[cos_a, -sin_a, 0.0], [sin_a, cos_a, 0.0], [0.0, 0.0, 1.0]]
        )

        base_rot = base @ rot_matrix.T
        top_rot = top @ rot_matrix.T
        vertices = np.vstack([base_rot, top_rot]) + pos_m
        return vertices

    def _draw_3d_scene(
        self,
        ax: plt.Axes,
        environment_data: dict[str, Any],
        layout_data: dict[str, Any],
    ) -> None:
        floor_pts = np.array(
            [[pt["x"], pt["y"], pt["z"]] for pt in environment_data["floor"]["points"]]
        )
        floor_m = self._to_meters(floor_pts)
        x_min, x_max = floor_m[:, 0].min(), floor_m[:, 0].max()
        y_min, y_max = floor_m[:, 1].min(), floor_m[:, 1].max()
        floor = Poly3DCollection(
            [
                [
                    (x_min, y_min, 0.0),
                    (x_max, y_min, 0.0),
                    (x_max, y_max, 0.0),
                    (x_min, y_max, 0.0),
                ]
            ],
            alpha=0.3,
            facecolor="lightgray",
            edgecolor="black",
            linewidth=2,
            label="Floor",
        )
        ax.add_collection3d(floor)

        drawn_walls = 0
        for name, data in layout_data.items():
            if not data.get("is_wall"):
                continue
            # Skip placement items (e.g. wall fillers, wall cabinets): they have zone_type.
            # Only draw room-structure walls from physics (no zone_type).
            if data.get("zone_type"):
                continue
            wall_data = next(w for w in environment_data["wall"] if w["name"] == name)
            pts = np.array([[p["x"], p["y"], p["z"]] for p in wall_data["points"]])
            pts_m = self._to_meters(pts)
            wall_faces = [[pts_m[i] for i in [0, 1, 2, 3]]]
            wall = Poly3DCollection(
                wall_faces,
                alpha=0.2,
                facecolor="indianred",
                edgecolor="darkred",
                linewidth=1,
                label="Walls" if drawn_walls == 0 else "",
            )
            ax.add_collection3d(wall)
            drawn_walls += 1

        roof_height = environment_data["wall"][0]["dimensions"]["height"] * MM_TO_METERS
        roof = Poly3DCollection(
            [
                [
                    (x_min, y_min, roof_height),
                    (x_max, y_min, roof_height),
                    (x_max, y_max, roof_height),
                    (x_min, y_max, roof_height),
                ]
            ],
            alpha=0.3,
            facecolor="lightgray",
            edgecolor="black",
            linewidth=1,
            label="Roof",
        )
        ax.add_collection3d(roof)

        # Track which item types we've drawn for legend
        drawn_types: dict[str, bool] = {}

        for _idx, (name, data) in enumerate(layout_data.items()):
            # Doors and windows are openings, not solid placements — they get
            # their own dedicated pass below so the swing arc and translucent
            # glass render cleanly without being mistaken for cabinets.
            if data.get("is_door") or data.get("is_window"):
                continue
            # Skip room structure (floor/roof/wall planes from physics), but draw
            # placement items that have is_roof/is_floor (e.g. corner_wall_cabinet).
            if data.get("is_wall") or data.get("is_floor") or data.get("is_roof"):
                if not data.get("zone_type"):
                    continue

            # Catalog `color` (per-SKU material color) wins over the
            # zone overlay so material colors render true-to-life.
            # Falls back to zone color, then to the generic type palette.
            catalog_color = self._catalog_color(data)
            zone_type = data.get("zone_type")
            if catalog_color is not None:
                color = catalog_color
                item_type = self._get_item_type(name, data)
            elif zone_type:
                color = ZONE_COLORS.get(zone_type, ZONE_COLORS["default"])
                item_type = f"zone_{zone_type}"
            else:
                item_type = self._get_item_type(name, data)
                color = ITEM_TYPE_COLORS.get(item_type, ITEM_TYPE_COLORS["default"])

            # Catalog wins for dims; JSON only supplies position + rotation.
            dims_mm, box_label, type_label = self._resolve_dims_and_label(name, data)
            vertices = self.create_furniture_box_3d(
                data["position_mm"], dims_mm, data["rotation_z_deg"]
            )
            faces = [
                [vertices[i] for i in [0, 1, 2, 3]],
                [vertices[i] for i in [4, 5, 6, 7]],
                [vertices[i] for i in [0, 1, 5, 4]],
                [vertices[i] for i in [2, 3, 7, 6]],
                [vertices[i] for i in [0, 3, 7, 4]],
                [vertices[i] for i in [1, 2, 6, 5]],
            ]

            # Only add label for first item of each type (for legend)
            label = ""
            if item_type not in drawn_types:
                label = self._format_type_label(item_type)
                drawn_types[item_type] = True

            # Items with wrong-input dimensions still render at catalog
            # dimensions (catalog-wins is in `_resolve_dims_and_label`); we
            # only swap to a red contrasting edge so the dimension error is
            # visually called out in 3D too.
            has_dim_error = self._dim_error_info(data) is not None
            poly = Poly3DCollection(
                faces,
                alpha=0.7,
                facecolor=color,
                edgecolor=ERROR_OUTLINE_COLOR if has_dim_error else "black",
                linewidth=2.4 if has_dim_error else 0.5,
                label=label,
            )
            ax.add_collection3d(poly)
            center = vertices.mean(axis=0)

            # Two-line label: primary line is SKU (or fallback name),
            # secondary line is the catalog `type` when known.
            short_id = (
                box_label[:20] + "..." if len(box_label) > 20 else box_label
            )
            short_type = (
                type_label[:22] + "..." if len(type_label) > 22 else type_label
            )
            display_text = f"{short_id}\n{short_type}" if short_type else short_id
            ax.text(
                center[0],
                center[1],
                center[2],
                display_text,
                fontsize=7,
                ha="center",
                va="center",
                weight="bold",
                linespacing=1.1,
                bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.8},
            )

        # Doors and windows are drawn last so their geometry sits on top of
        # the wall planes and isn't z-fighting with the room shell.
        self._draw_openings_3d(ax, layout_data)

    def _format_type_label(self, item_type: str) -> str:
        """Format item type key into readable legend label.

        Args:
            item_type: Item type key (e.g., "corner_cabinet")

        Returns:
            Formatted label (e.g., "Corner Cabinet")
        """
        return item_type.replace("_", " ").title()

    # ------------------------------------------------------------------
    # Doors & windows
    # ------------------------------------------------------------------

    @staticmethod
    def _opening_endpoints_m(
        data: dict[str, Any],
    ) -> tuple[float, float, float, float, str]:
        """Return the two wall-aligned endpoints of an opening in metres.

        Returns ``(x0, y0, x1, y1, anchor)`` where ``(x0,y0)`` is the
        "lower" endpoint along the wall (smaller x or y) and
        ``(x1,y1)`` is the "upper" endpoint.
        """
        pos = data["position_mm"]
        dims = data["dimensions_mm"]
        anchor = str(data.get("anchor_wall", "south")).lower()
        cx = pos["x"] * MM_TO_METERS
        cy = pos["y"] * MM_TO_METERS
        w = dims["width"] * MM_TO_METERS
        if anchor in ("south", "north"):
            return cx - w / 2, cy, cx + w / 2, cy, anchor
        return cx, cy - w / 2, cx, cy + w / 2, anchor

    def _draw_openings_2d(
        self, ax: plt.Axes, layout_data: dict[str, Any]
    ) -> list[Patch]:
        """Draw all doors and windows on the 2D plan.

        Returns extra legend patches for any opening kinds actually drawn,
        so the caller can extend the legend without double-counting.
        """
        seen: set[str] = set()
        extra: list[Patch] = []
        for _name, data in layout_data.items():
            if data.get("is_door"):
                self._draw_door_2d(ax, data)
                if "door" not in seen:
                    seen.add("door")
                    extra.append(
                        Patch(
                            facecolor=DOOR_COLOR,
                            edgecolor=DOOR_EDGE_COLOR,
                            label="Door",
                        )
                    )
            elif data.get("is_window"):
                self._draw_window_2d(ax, data)
                if "window" not in seen:
                    seen.add("window")
                    extra.append(
                        Patch(
                            facecolor=WINDOW_COLOR,
                            edgecolor=WINDOW_FRAME_COLOR,
                            label="Window",
                        )
                    )
        return extra

    def _draw_door_2d(self, ax: plt.Axes, data: dict[str, Any]) -> None:
        x0, y0, x1, y1, anchor = self._opening_endpoints_m(data)
        w = (data["dimensions_mm"]["width"]) * MM_TO_METERS

        # Brown line for the door leaf footprint, overlaid on the red wall.
        ax.plot(
            [x0, x1],
            [y0, y1],
            color=DOOR_COLOR,
            linewidth=9,
            solid_capstyle="butt",
            zorder=3,
        )

        # Swing arc: hinge at the "first" wall endpoint, swing inward.
        if anchor == "south":
            hinge, t1, t2 = (x0, y0), 0, 90
        elif anchor == "north":
            hinge, t1, t2 = (x1, y1), 180, 270
        elif anchor == "west":
            hinge, t1, t2 = (x0, y0), 0, 90
        else:  # east
            hinge, t1, t2 = (x0, y1), 90, 180

        ax.add_patch(
            Arc(
                hinge,
                w * 2.0,
                w * 2.0,
                theta1=t1,
                theta2=t2,
                color=DOOR_SWING_COLOR,
                linestyle="--",
                linewidth=1.6,
                zorder=4,
            )
        )

        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        ax.text(
            cx,
            cy,
            "DOOR",
            fontsize=7,
            ha="center",
            va="center",
            color=DOOR_EDGE_COLOR,
            weight="bold",
            zorder=5,
            bbox={"boxstyle": "round,pad=0.25", "facecolor": "white", "alpha": 0.9},
        )

    def _draw_window_2d(self, ax: plt.Axes, data: dict[str, Any]) -> None:
        x0, y0, x1, y1, _anchor = self._opening_endpoints_m(data)
        # Two-pass line: thick light-blue band + thinner frame stroke.
        ax.plot(
            [x0, x1],
            [y0, y1],
            color=WINDOW_COLOR,
            linewidth=10,
            solid_capstyle="butt",
            zorder=3,
        )
        ax.plot(
            [x0, x1],
            [y0, y1],
            color=WINDOW_FRAME_COLOR,
            linewidth=2,
            solid_capstyle="butt",
            zorder=4,
        )
        cx = (x0 + x1) / 2.0
        cy = (y0 + y1) / 2.0
        ax.text(
            cx,
            cy,
            "WINDOW",
            fontsize=7,
            ha="center",
            va="center",
            color=WINDOW_FRAME_COLOR,
            weight="bold",
            zorder=5,
            bbox={"boxstyle": "round,pad=0.25", "facecolor": "white", "alpha": 0.9},
        )

    def _draw_openings_3d(
        self, ax: plt.Axes, layout_data: dict[str, Any]
    ) -> None:
        """Draw doors (with floor-projected swing arc) and windows in 3D."""
        for _name, data in layout_data.items():
            if data.get("is_door"):
                self._draw_opening_panel_3d(ax, data, is_door=True)
                self._draw_door_swing_arc_3d(ax, data)
            elif data.get("is_window"):
                self._draw_opening_panel_3d(ax, data, is_door=False)

    def _draw_opening_panel_3d(
        self, ax: plt.Axes, data: dict[str, Any], *, is_door: bool
    ) -> None:
        pos = data["position_mm"]
        dims = data["dimensions_mm"]
        anchor = str(data.get("anchor_wall", "south")).lower()
        cx = pos["x"] * MM_TO_METERS
        cy = pos["y"] * MM_TO_METERS
        cz = pos["z"] * MM_TO_METERS
        w = dims["width"] * MM_TO_METERS
        h = dims["height"] * MM_TO_METERS

        if anchor in ("south", "north"):
            verts = [
                [
                    (cx - w / 2, cy, cz - h / 2),
                    (cx + w / 2, cy, cz - h / 2),
                    (cx + w / 2, cy, cz + h / 2),
                    (cx - w / 2, cy, cz + h / 2),
                ]
            ]
        else:
            verts = [
                [
                    (cx, cy - w / 2, cz - h / 2),
                    (cx, cy + w / 2, cz - h / 2),
                    (cx, cy + w / 2, cz + h / 2),
                    (cx, cy - w / 2, cz + h / 2),
                ]
            ]

        if is_door:
            panel = Poly3DCollection(
                verts,
                alpha=0.85,
                facecolor=DOOR_COLOR,
                edgecolor=DOOR_EDGE_COLOR,
                linewidth=1.5,
            )
        else:
            panel = Poly3DCollection(
                verts,
                alpha=0.45,
                facecolor=WINDOW_COLOR,
                edgecolor=WINDOW_FRAME_COLOR,
                linewidth=2.0,
            )
        ax.add_collection3d(panel)

    def _draw_door_swing_arc_3d(
        self, ax: plt.Axes, data: dict[str, Any]
    ) -> None:
        x0, y0, x1, y1, anchor = self._opening_endpoints_m(data)
        w = (data["dimensions_mm"]["width"]) * MM_TO_METERS

        if anchor == "south":
            hinge, theta_start, theta_end = (x0, y0), 0.0, np.pi / 2
        elif anchor == "north":
            hinge, theta_start, theta_end = (x1, y1), np.pi, 1.5 * np.pi
        elif anchor == "west":
            hinge, theta_start, theta_end = (x0, y0), 0.0, np.pi / 2
        else:  # east
            hinge, theta_start, theta_end = (x0, y1), np.pi / 2, np.pi

        thetas = np.linspace(theta_start, theta_end, 36)
        xs = hinge[0] + w * np.cos(thetas)
        ys = hinge[1] + w * np.sin(thetas)
        zs = np.full_like(xs, 0.01)  # tiny lift so the arc isn't z-fighting the floor
        ax.plot(
            xs,
            ys,
            zs,
            color=DOOR_SWING_COLOR,
            linestyle="--",
            linewidth=1.6,
        )

    def plot_layout_3d(
        self,
        environment_data: dict[str, Any],
        layout_data: dict[str, Any],
        title: str,
        save_to: str | None = None,
        show: bool = True,
    ) -> None:
        """Render a 3D layout plot."""
        fig = plt.figure(figsize=(14, 10))
        ax = fig.add_subplot(111, projection="3d")
        self._draw_3d_scene(ax, environment_data, layout_data)
        ax.set_xlabel("X (m)")
        ax.set_ylabel("Y (m)")
        ax.set_zlabel("Z (m)")
        ax.set_title(title)
        ax.set_box_aspect([1, 1, 0.4])
        ax.view_init(elev=25, azim=45)
        # Matplotlib 3D collections can be problematic in legends on some versions,
        # causing exceptions when handlers inspect facecolors. The legend is helpful
        # but should never prevent rendering.
        try:
            ax.legend()
        except Exception:
            pass
        plt.tight_layout()
        plt.draw()
        if save_to:
            plt.savefig(save_to, dpi=150, bbox_inches="tight")
        if show:
            plt.show()
        plt.close(fig)

    def plot_layout_2d(
        self,
        environment_data: dict[str, Any],
        layout_data: dict[str, Any],
        title: str,
        view: str = "top",
        save_to: str | None = None,
        show: bool = True,
        ax: plt.Axes | None = None,
    ) -> None:
        """Render a 2D layout plot with optional side/front/back views.

        Pass ``ax`` to draw onto an existing Matplotlib axes (used by
        ``plot_layout_2d_with_grid`` so the grid and the boxes land in
        the same figure). When ``ax`` is None we own the figure lifecycle
        (create + save + close).
        """
        owns_fig = ax is None
        if owns_fig:
            fig, ax = plt.subplots(figsize=(12, 10))
            ax.set_title(title)
            ax.set_aspect("equal")
        else:
            fig = ax.figure

        if view in ["side", "front", "back"]:
            plt.close(fig)
            fig = plt.figure(figsize=(14, 10))
            ax = fig.add_subplot(111, projection="3d")
            self._draw_3d_scene(ax, environment_data, layout_data)
            if view == "side":
                ax.view_init(elev=0, azim=90)
                ax.set_box_aspect([1, 0.1, 0.4])
                ax.set_yticks([])
                ax.set_yticklabels([])
                ax.set_xlabel("X (m)")
                ax.set_zlabel("Z (m)")
            elif view == "front":
                ax.view_init(elev=0, azim=0)
                ax.set_box_aspect([0.1, 1, 0.4])
                ax.set_xticks([])
                ax.set_xticklabels([])
                ax.set_ylabel("Y (m)")
                ax.set_zlabel("Z (m)")
            else:
                ax.view_init(elev=0, azim=180)
                ax.set_box_aspect([0.1, 1, 0.4])
                ax.set_xticks([])
                ax.set_xticklabels([])
                ax.set_ylabel("Y (m)")
                ax.set_zlabel("Z (m)")
            ax.set_title(title)
            plt.tight_layout()
            plt.draw()
            if save_to:
                plt.savefig(save_to, dpi=150, bbox_inches="tight")
            if show:
                plt.show()
            plt.close(fig)
            return

        floor_pts = np.array(
            [[pt["x"], pt["y"]] for pt in environment_data["floor"]["points"]]
        )
        floor_m = self._to_meters(floor_pts)
        floor_polygon = np.vstack([floor_m, floor_m[0]])
        ax.plot(
            floor_polygon[:, 0], floor_polygon[:, 1], "k-", linewidth=2, label="Room"
        )

        first_wall = True
        for name, data in layout_data.items():
            if not data.get("is_wall"):
                continue
            wall_data = next(w for w in environment_data["wall"] if w["name"] == name)
            pts = np.array([[p["x"], p["y"]] for p in wall_data["points"]])
            pts_m = self._to_meters(pts)
            ax.plot(
                pts_m[:, 0],
                pts_m[:, 1],
                "r-",
                linewidth=6,
                label="Walls" if first_wall else "",
                alpha=0.7 if not wall_data.get("has_cabinets", True) else 1.0,
            )
            first_wall = False

        # Track which item types we've drawn for legend
        drawn_types: dict[str, bool] = {}
        legend_patches = []

        for _idx, (name, data) in enumerate(layout_data.items()):
            # Doors / windows are openings, not boxes — dedicated pass below.
            if data.get("is_door") or data.get("is_window"):
                continue
            # Skip room structure only; draw placement items even if is_roof
            if data.get("is_wall") or data.get("is_floor") or data.get("is_roof"):
                if not data.get("zone_type"):
                    continue

            # Catalog `color` (per-SKU material color) wins over the
            # generic type palette so material colors (oak, walnut,
            # matte black, stainless, …) render true-to-life in the top view.
            item_type = self._get_item_type(name, data)
            color = self._get_item_color(name, data)

            # Catalog wins for dims; JSON only supplies position + rotation.
            dims_mm, box_label, type_label = self._resolve_dims_and_label(name, data)
            pos_m = self._to_meters(np.array([data["position_mm"]["x"], data["position_mm"]["y"]]))
            width_m = self._to_meters(dims_mm["width"])
            depth_m = self._to_meters(dims_mm["depth"])
            rect = Rectangle((-width_m / 2, -depth_m / 2), width_m, depth_m, facecolor=color, alpha=0.7, edgecolor="black", linewidth=2)
            t = Affine2D().rotate_deg(data["rotation_z_deg"]).translate(pos_m[0], pos_m[1]) + ax.transData
            rect.set_transform(t)
            ax.add_patch(rect)

            # Wrong-input overlay: red dashed outline at the catalog
            # footprint so viewers see exactly which boxes were corrected
            # to catalog dimensions (regardless of what the JSON claimed).
            dim_error = self._dim_error_info(data)
            if dim_error is not None:
                error_rect = Rectangle(
                    (-width_m / 2, -depth_m / 2),
                    width_m,
                    depth_m,
                    facecolor="none",
                    edgecolor=ERROR_OUTLINE_COLOR,
                    linewidth=2.6,
                    linestyle="--",
                    zorder=6,
                )
                error_rect.set_transform(t)
                ax.add_patch(error_rect)
                if "_dim_error" not in drawn_types:
                    legend_patches.append(
                        Patch(
                            facecolor="none",
                            edgecolor=ERROR_OUTLINE_COLOR,
                            linewidth=2.6,
                            linestyle="--",
                            label="Wrong input dim (catalog wins)",
                        )
                    )
                    drawn_types["_dim_error"] = True

            # Add to legend if first of type
            if item_type not in drawn_types:
                legend_patches.append(Patch(facecolor=color, edgecolor="black", label=self._format_type_label(item_type)))
                drawn_types[item_type] = True

            # Two-line label: primary = SKU (or dict key), secondary = catalog type.
            short_id = (
                box_label[:15] + "..." if len(box_label) > 15 else box_label
            )
            short_type = (
                type_label[:18] + "..." if len(type_label) > 18 else type_label
            )
            display_text = f"{short_id}\n{short_type}" if short_type else short_id
            ax.text(
                pos_m[0],
                pos_m[1],
                display_text,
                ha="center",
                va="center",
                fontsize=7,
                weight="bold",
                linespacing=1.1,
                bbox={"boxstyle": "round", "facecolor": "white", "alpha": 0.8},
            )

        ax.set_xlabel("X (m)")
        ax.set_ylabel("Y (m)")

        # Render doors and windows after all boxes so the dashed swing arc
        # and translucent glass sit on top of the wall and floor strokes.
        legend_patches.extend(self._draw_openings_2d(ax, layout_data))

        # Add custom legend for item types
        if legend_patches:
            ax.legend(handles=legend_patches, loc="upper right", fontsize=8, title="Item Types")

        if owns_fig:
            ax.grid(True, alpha=0.3)
            plt.tight_layout()
            if save_to:
                plt.savefig(save_to, dpi=150, bbox_inches="tight")
            if show:
                plt.show()
            plt.close(fig)

    def add_coordinate_grid(
        self, ax: plt.Axes, environment_data: dict[str, Any], view: str = "top"
    ) -> None:
        """Draw a 2m grid to help visualize scale."""
        floor = environment_data["floor"]["points"]
        x_min = min(p["x"] for p in floor) * MM_TO_METERS
        x_max = max(p["x"] for p in floor) * MM_TO_METERS
        y_min = min(p["y"] for p in floor) * MM_TO_METERS
        y_max = max(p["y"] for p in floor) * MM_TO_METERS

        if view == "top":
            for x in np.arange(0, x_max + 1, 2.0):
                ax.axvline(x, color="blue", alpha=0.3, linestyle="--", linewidth=1)
                ax.text(
                    x,
                    y_min - 0.2,
                    f"{x:.0f}m",
                    fontsize=9,
                    color="blue",
                    weight="bold",
                    ha="center",
                    va="top",
                    bbox={"boxstyle": "round,pad=0.3", "facecolor": "white", "alpha": 0.8},
                )
            for y in np.arange(0, y_max + 1, 2.0):
                ax.axhline(y, color="blue", alpha=0.3, linestyle="--", linewidth=1)
                ax.text(
                    x_min - 0.2,
                    y,
                    f"{y:.0f}m",
                    fontsize=9,
                    color="blue",
                    weight="bold",
                    ha="right",
                    va="center",
                    bbox={"boxstyle": "round,pad=0.3", "facecolor": "white", "alpha": 0.8},
                )
            ax.annotate(
                "",
                xy=(x_min + 0.5, y_min),
                xytext=(x_min, y_min),
                arrowprops={"arrowstyle": "->", "color": "red", "lw": 2},
            )
            ax.text(
                x_min + 0.3, y_min - 0.3, "X", fontsize=12, color="red", weight="bold"
            )
            ax.annotate(
                "",
                xy=(x_min, y_min + 0.5),
                xytext=(x_min, y_min),
                arrowprops={"arrowstyle": "->", "color": "green", "lw": 2},
            )
            ax.text(
                x_min - 0.3, y_min + 0.3, "Y", fontsize=12, color="green", weight="bold"
            )
            ax.text(
                (x_min + x_max) / 2.0,
                y_max + 0.5,
                "Coordinate Grid: Each square = 2m × 2m",
                fontsize=11,
                ha="center",
                weight="bold",
                bbox={"boxstyle": "round,pad=0.5", "facecolor": "yellow", "alpha": 0.7},
            )

    def plot_layout_2d_with_grid(
        self,
        environment_data: dict[str, Any],
        layout_data: dict[str, Any],
        title: str,
        view: str = "top",
        save_to: str | None = None,
        show: bool = True,
    ) -> None:
        """Plot 2D layout with coordinate grid overlay."""
        fig, ax = plt.subplots(figsize=(14, 12))
        ax.set_title(title, fontsize=14, weight="bold", pad=20)
        ax.set_aspect("equal")

        if view == "top":
            # Coordinate grid and floor-plan items share one axes; this avoids
            # the previous double-save bug where the grid figure overwrote
            # the boxes figure on disk.
            self.add_coordinate_grid(ax, environment_data, view="top")
            self.plot_layout_2d(
                environment_data,
                layout_data,
                title,
                view="top",
                save_to=None,
                show=False,
                ax=ax,
            )
        else:
            plt.close(fig)
            self.plot_layout_2d(
                environment_data,
                layout_data,
                title,
                view=view,
                save_to=save_to,
                show=show,
            )
            return

        plt.tight_layout()
        if save_to:
            plt.savefig(save_to, dpi=200, bbox_inches="tight")
        if show:
            plt.show()
        plt.close(fig)

    def take_screenshot_2d(
        self,
        environment_data: dict[str, Any],
        layout_data: dict[str, Any],
        filename: str,
        contact_pairs: list[dict[str, float]] | None = None,
        view: str = "top",
    ) -> str:
        """Take a 2D screenshot, optionally showing contact pairs."""
        self.plot_layout_2d(
            environment_data,
            layout_data,
            title=f"2D {view.title()} View - Furniture Layout",
            view=view,
            save_to=filename,
            show=False,
        )
        return f"Screenshot saved as {filename}"
