#!/usr/bin/env python3
"""
FreeCAD MCP Server — freecad_renderer.py

Subprocess renderer: reads JSON from stdin, executes a FreeCAD Python script,
renders the resulting geometry with matplotlib (headless, no display required),
and writes JSON result to stdout.

Input JSON schema:
{
    "script_path": str,          # path to FreeCAD Python script to execute
    "view_angle":  str | null,   # "Top"|"Bottom"|"Front"|"Back"|"Left"|"Right"|"Isometric"
    "elevation":   float | null, # custom elevation angle in degrees (overrides view_angle)
    "azimuth":     float | null, # custom azimuth angle in degrees (overrides view_angle)
    "zoom":        float | null, # zoom factor: 1.0=default, >1 zooms in, <1 zooms out
    "width":       int,          # image width in pixels (default: 800)
    "height":      int,          # image height in pixels (default: 600)
    "background":  str | null    # hex colour string, e.g. "#1a1a2e" (default: "#0d1117")
}

Output JSON schema:
{
    "success":     bool,
    "image_b64":   str,           # base64-encoded PNG
    "metadata": {
        "view_angle":   str,
        "elevation":    float,
        "azimuth":      float,
        "zoom":         float,
        "image_width":  int,
        "image_height": int,
        "object_count": int,
        "shape_info":   list[dict],
        "bounding_box": {"min": [x,y,z], "max": [x,y,z]} | null
    },
    "error":       str | null
}
"""

import sys
import os
import json
import base64
import io
import time
import logging
import traceback

# ── Logging (stderr only — stdout is reserved for JSON output) ────────────────
logging.basicConfig(
    stream=sys.stderr,
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] renderer: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("freecad-renderer")

# ── FreeCAD bootstrap ────────────────────────────────────────────────────────
FREECAD_LIB = "/Applications/FreeCAD.app/Contents/Resources/lib"
FREECAD_SITE = os.path.join(FREECAD_LIB, "python3.11", "site-packages")

log.debug("Bootstrap: adding FreeCAD paths to sys.path")
for p in (FREECAD_LIB, FREECAD_SITE):
    if p not in sys.path:
        sys.path.insert(0, p)
        log.debug("  sys.path += %s", p)

log.debug("Importing FreeCAD ...")
import FreeCAD
log.debug("FreeCAD imported (version: %s)", getattr(FreeCAD, "Version", lambda: ["?"])()[:3])
import FreeCADGui
FreeCADGui.setupWithoutGUI()
log.debug("FreeCADGui.setupWithoutGUI() done")

log.debug("Importing Part, MeshPart, numpy ...")
import Part
import MeshPart
import numpy as np
log.debug("All FreeCAD modules imported")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

# ── View angle presets ────────────────────────────────────────────────────────
VIEW_PRESETS: dict[str, tuple[float, float]] = {
    "Top":        (90,    0),
    "Bottom":     (-90,   0),
    "Front":      (0,   -90),
    "Back":       (0,    90),
    "Left":       (0,   180),
    "Right":      (0,     0),
    "Isometric":  (35.26, 45),
}
DEFAULT_VIEW = "Isometric"


def shape_to_triangles(shape) -> np.ndarray:
    """Tessellate a FreeCAD TopoShape and return an (N, 3, 3) float32 array."""
    log.debug("Tessellating shape (type=%s) ...", shape.ShapeType)
    t0 = time.perf_counter()
    msh = MeshPart.meshFromShape(
        Shape=shape,
        LinearDeflection=0.05,
        AngularDeflection=0.1,
        Relative=False,
    )
    tris = []
    for facet in msh.Facets:
        tris.append(list(facet.Points))
    elapsed = time.perf_counter() - t0
    log.debug("Tessellation done: %d facets in %.3fs", len(tris), elapsed)
    return np.array(tris, dtype=np.float32)


def fit_axes(ax, triangles: np.ndarray, zoom: float = 1.0):
    """Set equal-aspect axis limits centred on the geometry, scaled by zoom."""
    pts = triangles.reshape(-1, 3)
    mid = pts.mean(axis=0)
    half = (pts.max(axis=0) - pts.min(axis=0)).max() * 0.5 / zoom
    if half < 1e-6:
        half = 1.0
    ax.set_xlim(mid[0] - half, mid[0] + half)
    ax.set_ylim(mid[1] - half, mid[1] + half)
    ax.set_zlim(mid[2] - half, mid[2] + half)
    return mid, half


def render(
    triangles: np.ndarray,
    elev: float,
    azim: float,
    zoom: float,
    width: int,
    height: int,
    background: str,
) -> bytes:
    """Render triangles into a PNG byte-string using matplotlib."""
    log.info("Rendering %d triangles | elev=%.1f azim=%.1f zoom=%.2f | %dx%d bg=%s",
             len(triangles), elev, azim, zoom, width, height, background)
    t0 = time.perf_counter()
    dpi = 100
    fig = plt.figure(figsize=(width / dpi, height / dpi), dpi=dpi)
    fig.patch.set_facecolor(background)

    ax = fig.add_subplot(111, projection="3d")
    ax.set_facecolor(background)

    face_color = "#4a90d9"
    edge_color = "#2a5a8a"

    poly = Poly3DCollection(triangles, alpha=0.92, linewidths=0.3, zsort="average")
    poly.set_facecolor(face_color)
    poly.set_edgecolor(edge_color)
    ax.add_collection3d(poly)

    fit_axes(ax, triangles, zoom)
    ax.view_init(elev=elev, azim=azim)

    # Styling
    label_color = "#8899aa"
    for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
        axis.label.set_color(label_color)
        axis.set_tick_params(colors=label_color)
        axis.pane.fill = False
        axis.pane.set_edgecolor("#303040")

    ax.set_xlabel("X")
    ax.set_ylabel("Y")
    ax.set_zlabel("Z")
    ax.grid(True, color="#303040", linewidth=0.5, linestyle="--", alpha=0.5)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=dpi, bbox_inches="tight", facecolor=background)
    plt.close(fig)
    buf.seek(0)
    png_data = buf.read()
    elapsed = time.perf_counter() - t0
    log.info("Render complete: %.1f KB PNG in %.3fs", len(png_data) / 1024, elapsed)
    return png_data


def bounding_box(triangles: np.ndarray) -> dict | None:
    if triangles.size == 0:
        return None
    pts = triangles.reshape(-1, 3)
    return {
        "min": pts.min(axis=0).tolist(),
        "max": pts.max(axis=0).tolist(),
    }


def main():
    log.info("Renderer started — reading payload from stdin")
    t_total = time.perf_counter()
    payload = json.load(sys.stdin)

    script_path: str = payload["script_path"]
    view_angle: str | None = payload.get("view_angle")
    custom_elev: float | None = payload.get("elevation")
    custom_azim: float | None = payload.get("azimuth")
    zoom: float = float(payload.get("zoom") or 1.0)
    width: int = int(payload.get("width") or 800)
    height: int = int(payload.get("height") or 600)
    background: str = payload.get("background") or "#0d1117"

    log.info("Payload: script=%s view_angle=%s elev=%s azim=%s zoom=%s size=%dx%d",
             script_path, view_angle, custom_elev, custom_azim, zoom, width, height)

    log.debug("Reading script file: %s", script_path)
    script: str = open(script_path, encoding="utf-8").read()
    log.debug("Script loaded: %d bytes, %d lines", len(script), script.count("\n") + 1)

    # Resolve elevation/azimuth
    if custom_elev is not None and custom_azim is not None:
        elev, azim = float(custom_elev), float(custom_azim)
        resolved_view = f"custom(elev={elev}, azim={azim})"
    else:
        resolved_view = view_angle if view_angle in VIEW_PRESETS else DEFAULT_VIEW
        elev, azim = VIEW_PRESETS[resolved_view]
    log.info("View resolved: %s  elev=%.2f  azim=%.2f", resolved_view, elev, azim)

    # Create an isolated FreeCAD document
    doc_name = "MCP_Render"
    log.debug("Creating FreeCAD document: %s", doc_name)
    doc = FreeCAD.newDocument(doc_name)
    FreeCAD.setActiveDocument(doc_name)
    log.debug("FreeCAD document created and set as active")

    try:
        exec_globals = {
            "FreeCAD": FreeCAD,
            "App": FreeCAD,
            "doc": doc,
            "Part": Part,
            # Allow scripts that use `if __name__ == "__main__":` to run correctly
            "__name__": "__main__",
        }
        # Expose __file__ so scripts can use relative path resolution
        exec_globals["__file__"] = script_path
        # Expose commonly-used FreeCAD workbench modules to the script
        for mod_name in ("Part", "Mesh", "MeshPart", "Draft", "Sketcher"):
            try:
                exec_globals[mod_name] = __import__(mod_name)
            except ImportError:
                pass

        log.info("Executing script ...")
        t_exec = time.perf_counter()
        exec(compile(script, "<mcp_script>", "exec"), exec_globals)
        log.info("Script execution done in %.3fs", time.perf_counter() - t_exec)

        # Recompute all documents the script may have created (including its own)
        log.debug("Recomputing all open documents: %s", list(FreeCAD.listDocuments()))
        for dname in FreeCAD.listDocuments():
            try:
                FreeCAD.getDocument(dname).recompute()
                log.debug("  recompute OK: %s", dname)
            except Exception as recompute_err:
                log.warning("  recompute FAILED for %s: %s", dname, recompute_err)

        # Collect all tessellatable shapes from every open document
        log.debug("Collecting shapes from all open documents ...")
        all_triangles = []
        shape_info = []
        for dname in FreeCAD.listDocuments():
            doc_objs = FreeCAD.getDocument(dname).Objects
            log.debug("  document %s: %d object(s)", dname, len(doc_objs))
            for obj in doc_objs:
                shape = None
                if hasattr(obj, "Shape") and obj.Shape is not None:
                    shape = obj.Shape
                elif hasattr(obj, "Mesh") and obj.Mesh is not None:
                    # Already a mesh object — convert to triangles directly
                    tris = []
                    for facet in obj.Mesh.Facets:
                        tris.append(list(facet.Points))
                    if tris:
                        t = np.array(tris, dtype=np.float32)
                        all_triangles.append(t)
                        shape_info.append({
                            "name": obj.Name,
                            "label": obj.Label,
                            "type": "Mesh",
                            "facets": len(tris),
                        })
                        log.debug("    [Mesh] %s (%s): %d facets", obj.Label, obj.Name, len(tris))
                    continue

                try:
                    if shape is None or shape.isNull():
                        continue
                except AttributeError:
                    if shape is None:
                        continue

                try:
                    log.debug("    [Shape] tessellating %s (%s) type=%s",
                              obj.Label, obj.Name, shape.ShapeType)
                    tris = shape_to_triangles(shape)
                    if tris.size == 0:
                        log.warning("    [Shape] %s (%s): tessellation produced 0 triangles — skipped",
                                    obj.Label, obj.Name)
                        continue
                    all_triangles.append(tris)
                    bb = shape.BoundBox
                    vol = shape.Volume if hasattr(shape, "Volume") else None
                    shape_info.append({
                        "name": obj.Name,
                        "label": obj.Label,
                        "type": shape.ShapeType,
                        "volume": vol,
                        "area": shape.Area if hasattr(shape, "Area") else None,
                        "bounding_box": {
                            "min": [bb.XMin, bb.YMin, bb.ZMin],
                            "max": [bb.XMax, bb.YMax, bb.ZMax],
                        },
                    })
                    log.debug("    [Shape] %s (%s): %d triangles volume=%.3f",
                              obj.Label, obj.Name, len(tris), vol or 0)
                except Exception as e:
                    log.warning("    [Shape] %s (%s): tessellation error — %s",
                                obj.Label, obj.Name, e)
                    shape_info.append({
                        "name": obj.Name,
                        "label": obj.Label,
                        "error": str(e),
                    })

        if not all_triangles:
            log.error("No renderable geometry found in any open document")
            raise RuntimeError(
                "Script executed but no renderable geometry was found. "
                "Make sure the script adds geometry to the active document."
            )

        log.info("Collected %d shape(s) totalling %d triangle arrays",
                 len(shape_info), len(all_triangles))
        combined = np.concatenate(all_triangles, axis=0)
        log.info("Combined triangle array: %d triangles", len(combined))
        bb = bounding_box(combined)
        if bb:
            log.debug("Bounding box: min=%s  max=%s", bb["min"], bb["max"])

        png_bytes = render(combined, elev, azim, zoom, width, height, background)
        image_b64 = base64.b64encode(png_bytes).decode("utf-8")
        log.debug("Base64-encoded image: %d chars", len(image_b64))

        result = {
            "success": True,
            "image_b64": image_b64,
            "metadata": {
                "view_angle": resolved_view,
                "elevation": elev,
                "azimuth": azim,
                "zoom": zoom,
                "image_width": width,
                "image_height": height,
                "object_count": len(shape_info),
                "shape_info": shape_info,
                "bounding_box": bb,
            },
            "error": None,
        }

    except Exception as e:
        log.error("Renderer error: %s: %s", type(e).__name__, e)
        log.debug(traceback.format_exc())
        result = {
            "success": False,
            "image_b64": None,
            "metadata": None,
            "error": f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
        }
    finally:
        # Close all documents opened during this render
        docs = list(FreeCAD.listDocuments())
        log.debug("Closing %d FreeCAD document(s): %s", len(docs), docs)
        for dname in docs:
            try:
                FreeCAD.closeDocument(dname)
            except Exception as close_err:
                log.warning("Could not close document %s: %s", dname, close_err)

    elapsed_total = time.perf_counter() - t_total
    if result.get("success"):
        log.info("Renderer finished successfully in %.3fs", elapsed_total)
    else:
        log.warning("Renderer finished with error in %.3fs", elapsed_total)
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()
    log.debug("JSON result written to stdout")


if __name__ == "__main__":
    main()
