"""Render every variant in a hackathon response envelope to PNG.

Usage
-----
    python render.py path/to/output.json
    python render.py output.json --out-dir renders/
    python render.py output.json --strict       # grader mode
    python render.py output.json --show         # also pop GUI windows
    python render.py output.json --2d-only      # skip 3D
    python render.py output.json --3d-only      # skip 2D
    python render.py output.json --catalog ./catalog.json

Input contract
--------------
The response JSON must match section 4 of ``Hackthon.md``::

    {
      "request_id":  "uuid",
      "duration_ms": 18420,
      "layouts": [
        {
          "id":          "variant-1",
          "environment": { "floor": {...}, "wall": [...] },
          "layout":      { "south_wall": {...}, "base_cabinet_1": {...}, ... },
          ...
        },
        ...
      ]
    }

For every variant the script writes::

    <out_dir>/<variant.id>_top.png      (2D top-down with grid)
    <out_dir>/<variant.id>_3d.png       (3D isometric)

Exit codes
----------
    0  -> all variants rendered successfully
    1  -> one or more variants failed (details printed to stderr)
    2  -> bad CLI usage / unreadable input file

This script is intentionally dependency-light: it imports only
``LayoutVisualizer`` from ``layout.py`` (which itself needs only
``matplotlib`` + ``numpy``). Drop all three files in the same
folder and you're set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Force a non-interactive backend by default so this works headlessly
# (CI, SSH, the grader). The user can flip back to GUI with --show,
# which we honor below by NOT forcing Agg in that case.
if "--show" not in sys.argv and not os.environ.get("MPLBACKEND"):
    os.environ["MPLBACKEND"] = "Agg"

# Import after backend is set, otherwise matplotlib pins the wrong one.
from layout import LayoutVisualizer  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render every variant in a hackathon response envelope to PNG.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "response",
        type=Path,
        help="Path to the response JSON envelope (see Hackthon.md \u00a74).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("renders"),
        help="Directory to write PNGs into (default: ./renders).",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="Override catalog.json path (default: auto-discover next to "
             "the visualizer or in the cwd).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail loudly on unknown SKUs (this is what the grader does).",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="Pop up the interactive 3D viewer for each variant "
             "(2D top-down is still written to disk but not shown). "
             "Default: fully headless.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--2d-only",
        dest="two_d_only",
        action="store_true",
        help="Skip the 3D render.",
    )
    group.add_argument(
        "--3d-only",
        dest="three_d_only",
        action="store_true",
        help="Skip the 2D top-down render.",
    )
    return parser.parse_args(argv)


def _load_response(path: Path) -> dict[str, Any]:
    if not path.exists():
        sys.exit(f"ERROR: response file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"ERROR: {path} is not valid JSON: {exc}")


def _validate_envelope(payload: dict[str, Any], src: Path) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or "layouts" not in payload:
        sys.exit(
            f"ERROR: {src} does not match the response contract; expected "
            f"a top-level 'layouts' array (see Hackthon.md \u00a74)."
        )
    layouts = payload["layouts"]
    if not isinstance(layouts, list) or not layouts:
        sys.exit(f"ERROR: {src} has no variants in 'layouts'.")
    return layouts


def _render_variant(
    visualizer: LayoutVisualizer,
    variant: dict[str, Any],
    out_dir: Path,
    *,
    do_2d: bool,
    do_3d: bool,
    show: bool,
) -> tuple[list[Path], str | None]:
    """Render one variant. Returns (output_files, error_message_or_None)."""
    missing = [k for k in ("id", "environment", "layout") if k not in variant]
    if missing:
        return [], f"variant missing required keys: {missing}"

    variant_id = str(variant["id"])
    title_base = f"{variant_id}"
    if "family" in variant:
        title_base += f" ({variant['family']})"
    if "score" in variant:
        title_base += f"  score={variant['score']}"

    written: list[Path] = []
    try:
        if do_2d:
            # 2D is always written to disk but never popped live — the
            # --show flag is reserved for the interactive 3D viewer so
            # the user isn't forced to dismiss a static top-down window
            # before they can orbit the 3D scene.
            top_path = out_dir / f"{variant_id}_top.png"
            visualizer.plot_layout_2d_with_grid(
                variant["environment"],
                variant["layout"],
                title=f"{title_base} \u2014 Top",
                view="top",
                save_to=str(top_path),
                show=False,
            )
            written.append(top_path)
        if do_3d:
            three_d_path = out_dir / f"{variant_id}_3d.png"
            visualizer.plot_layout_3d(
                variant["environment"],
                variant["layout"],
                title=f"{title_base} \u2014 3D",
                save_to=str(three_d_path),
                show=show,
            )
            written.append(three_d_path)
    except Exception as exc:  # noqa: BLE001 - we want the failure message verbatim
        return written, f"{type(exc).__name__}: {exc}"

    return written, None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    payload = _load_response(args.response)
    layouts = _validate_envelope(payload, args.response)

    args.out_dir.mkdir(parents=True, exist_ok=True)

    visualizer = LayoutVisualizer(
        catalog=args.catalog,
        strict=args.strict,
    )
    if not visualizer._catalog and not args.strict:
        print(
            "WARNING: no catalog loaded; placed-item dimensions will fall "
            "back to the JSON-supplied dimensions_mm (input validation disabled).",
            file=sys.stderr,
        )

    do_2d = not args.three_d_only
    do_3d = not args.two_d_only

    print(
        f"Rendering {len(layouts)} variant(s) from {args.response} "
        f"into {args.out_dir}/  (catalog SKUs: {len(visualizer._catalog)}, "
        f"strict={args.strict})"
    )

    failures: list[tuple[str, str]] = []
    started = time.perf_counter()
    for variant in layouts:
        variant_id = str(variant.get("id", "<no-id>"))
        files, error = _render_variant(
            visualizer,
            variant,
            args.out_dir,
            do_2d=do_2d,
            do_3d=do_3d,
            show=args.show,
        )
        if error:
            failures.append((variant_id, error))
            print(f"  [FAIL] {variant_id}: {error}", file=sys.stderr)
        else:
            rels = ", ".join(p.name for p in files)
            print(f"  [ OK ] {variant_id} -> {rels}")

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    print(
        f"Done in {elapsed_ms:.0f} ms. "
        f"Success: {len(layouts) - len(failures)}/{len(layouts)}."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
