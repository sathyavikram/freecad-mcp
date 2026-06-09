#!/usr/bin/env python3
"""
FreeCAD MCP Server — freecad_renderer.py

Subprocess renderer: reads JSON from stdin, executes a FreeCAD Python script,
renders the resulting geometry with VTK (headless offscreen, no display required),
and writes JSON result to stdout.

Input JSON schema:
{
    "script_path": str,          # path to FreeCAD Python script to execute
    "view_angle":  str | null,   # "Top"|"Bottom"|"Front"|"Back"|"Left"|"Right"|"Isometric"
    "elevation":   float | null, # custom elevation angle in degrees (overrides view_angle)
    "azimuth":     float | null, # custom azimuth angle in degrees (overrides view_angle)
    "zoom":        float | null, # zoom factor: 1.0=default, >1 zooms in, <1 zooms out
    "width":       int,          # image width in pixels (default: 1600)
    "height":      int,          # image height in pixels (default: 1200)
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
import math

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
for p in (FREECAD_SITE, FREECAD_LIB):
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

# ── VTK import ────────────────────────────────────────────────────────────────
log.debug("Importing VTK ...")
import vtk
log.debug("VTK imported (version: %s)", vtk.vtkVersion.GetVTKVersion())

# ── View angle presets ────────────────────────────────────────────────────────
#   Each preset is (elevation_deg, azimuth_deg) using the same convention as
#   before, but we convert to VTK camera position below.
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


def _hex_to_rgb_float(hex_color: str) -> tuple[float, float, float]:
    """Convert '#rrggbb' hex string to (r, g, b) in 0-1 range."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (0.05, 0.067, 0.09)
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return (r, g, b)


def _is_dark_background(bg: tuple[float, float, float]) -> bool:
    """Return True if background is perceptually dark."""
    lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
    return lum < 0.4


def shape_to_vtk_polydata(shape) -> vtk.vtkPolyData:
    """
    Tessellate a FreeCAD TopoShape and return a vtkPolyData with normals.
    We use a finer tessellation than before for crisp edges.
    """
    log.debug("Tessellating shape (type=%s) ...", shape.ShapeType)
    t0 = time.perf_counter()
    msh = MeshPart.meshFromShape(
        Shape=shape,
        LinearDeflection=0.02,   # finer than the old 0.05 → smoother curves
        AngularDeflection=0.05,  # finer angular tolerance
        Relative=False,
    )
    elapsed = time.perf_counter() - t0
    log.debug("Tessellation done: %d facets in %.3fs", msh.CountFacets, elapsed)

    points = vtk.vtkPoints()
    cells  = vtk.vtkCellArray()

    for facet in msh.Facets:
        pts = facet.Points          # list of 3 (x,y,z) tuples
        ids = []
        for p in pts:
            ids.append(points.InsertNextPoint(p[0], p[1], p[2]))
        tri = vtk.vtkTriangle()
        tri.GetPointIds().SetId(0, ids[0])
        tri.GetPointIds().SetId(1, ids[1])
        tri.GetPointIds().SetId(2, ids[2])
        cells.InsertNextCell(tri)

    pd = vtk.vtkPolyData()
    pd.SetPoints(points)
    pd.SetPolys(cells)

    # Merge duplicate vertices so normals can be computed correctly
    clean = vtk.vtkCleanPolyData()
    clean.SetInputData(pd)
    clean.Update()

    # Compute smooth normals
    normals = vtk.vtkPolyDataNormals()
    normals.SetInputConnection(clean.GetOutputPort())
    normals.SetFeatureAngle(30.0)   # crease angle — edges sharper than 30° kept hard
    normals.SplittingOn()
    normals.ConsistencyOn()
    normals.AutoOrientNormalsOn()
    normals.Update()

    return normals.GetOutput()


def mesh_to_vtk_polydata(mesh_obj) -> vtk.vtkPolyData:
    """Convert a FreeCAD Mesh object directly to vtkPolyData."""
    points = vtk.vtkPoints()
    cells  = vtk.vtkCellArray()
    for facet in mesh_obj.Facets:
        pts = facet.Points
        ids = []
        for p in pts:
            ids.append(points.InsertNextPoint(p[0], p[1], p[2]))
        tri = vtk.vtkTriangle()
        tri.GetPointIds().SetId(0, ids[0])
        tri.GetPointIds().SetId(1, ids[1])
        tri.GetPointIds().SetId(2, ids[2])
        cells.InsertNextCell(tri)
    pd = vtk.vtkPolyData()
    pd.SetPoints(points)
    pd.SetPolys(cells)
    normals = vtk.vtkPolyDataNormals()
    normals.SetInputData(pd)
    normals.SetFeatureAngle(30.0)
    normals.SplittingOn()
    normals.ConsistencyOn()
    normals.AutoOrientNormalsOn()
    normals.Update()
    return normals.GetOutput()


def _elev_azim_to_camera(
    center: tuple[float, float, float],
    radius: float,
    elev_deg: float,
    azim_deg: float,
) -> tuple[tuple, tuple]:
    """
    Convert elevation/azimuth (matplotlib convention) to VTK camera position.
    Returns (position, view_up).
    """
    elev = math.radians(elev_deg)
    azim = math.radians(azim_deg)

    # Spherical → Cartesian (camera on a sphere around center)
    x = center[0] + radius * math.cos(elev) * math.cos(azim)
    y = center[1] + radius * math.cos(elev) * math.sin(azim)
    z = center[2] + radius * math.sin(elev)

    # ViewUp: derivative of position w.r.t. elevation
    # Points "up" on the sphere surface — perpendicular to look direction, in elev plane
    ux = -math.sin(elev) * math.cos(azim)
    uy = -math.sin(elev) * math.sin(azim)
    uz =  math.cos(elev)

    # Guard against degenerate up vector when looking straight up/down
    if abs(uz) > 0.999:
        ux, uy, uz = 0.0, 1.0, 0.0

    return (x, y, z), (ux, uy, uz)


def render(
    polydata_list: list,
    elev: float,
    azim: float,
    zoom: float,
    width: int,
    height: int,
    background: str,
    object_colors: list | None = None,
) -> bytes:
    """
    Render a list of vtkPolyData objects into a high-quality PNG using VTK
    offscreen rendering with Phong shading, edge highlighting, and multi-light setup.
    """
    log.info("VTK rendering %d objects | elev=%.1f azim=%.1f zoom=%.2f | %dx%d bg=%s",
             len(polydata_list), elev, azim, zoom, width, height, background)
    t0 = time.perf_counter()

    bg_rgb = _hex_to_rgb_float(background)
    dark_bg = _is_dark_background(bg_rgb)

    # ── Palette ──────────────────────────────────────────────────────────────
    OBJECT_COLORS = [
        (0.29, 0.56, 0.89),   # steel blue
        (0.34, 0.74, 0.56),   # teal green
        (0.91, 0.62, 0.26),   # amber
        (0.78, 0.36, 0.91),   # violet
        (0.91, 0.37, 0.43),   # rose
        (0.35, 0.82, 0.87),   # cyan
    ]
    EDGE_DIM    = 0.40        # edge colour = face colour * this factor (dark tint)
    AMBIENT    = 0.18
    DIFFUSE    = 0.72
    SPECULAR   = 0.55
    SPEC_POWER = 60.0

    # ── Renderer ─────────────────────────────────────────────────────────────
    renderer = vtk.vtkRenderer()
    renderer.SetBackground(*bg_rgb)

    # ── Actors ───────────────────────────────────────────────────────────────
    for i, pd in enumerate(polydata_list):
        color = (object_colors[i] if object_colors and i < len(object_colors)
                 else OBJECT_COLORS[i % len(OBJECT_COLORS)])

        # Solid surface
        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(pd)
        mapper.ScalarVisibilityOff()

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(*color)
        prop.SetAmbient(AMBIENT)
        prop.SetDiffuse(DIFFUSE)
        prop.SetSpecular(SPECULAR)
        prop.SetSpecularPower(SPEC_POWER)
        prop.SetOpacity(1.0)
        renderer.AddActor(actor)

        # Feature edge overlay for crisp boundary lines
        edges = vtk.vtkFeatureEdges()
        edges.SetInputData(pd)
        edges.BoundaryEdgesOn()
        edges.FeatureEdgesOn()
        edges.SetFeatureAngle(25.0)
        edges.ManifoldEdgesOff()
        edges.NonManifoldEdgesOff()
        edges.Update()

        edge_mapper = vtk.vtkPolyDataMapper()
        edge_mapper.SetInputConnection(edges.GetOutputPort())
        edge_mapper.ScalarVisibilityOff()   # prevent default red scalar colouring

        edge_actor = vtk.vtkActor()
        edge_actor.SetMapper(edge_mapper)
        ep = edge_actor.GetProperty()
        # Edges are a lighter, slightly desaturated tint of the face colour
        # so they're visible but harmonious
        ec = (
            min(1.0, color[0] * 0.6 + 0.35),
            min(1.0, color[1] * 0.6 + 0.35),
            min(1.0, color[2] * 0.6 + 0.35),
        )
        ep.SetColor(*ec)
        ep.SetAmbient(1.0)   # unlit flat colour for edges
        ep.SetDiffuse(0.0)
        ep.SetSpecular(0.0)
        ep.SetLineWidth(1.5)
        renderer.AddActor(edge_actor)

    # ── Lighting ─────────────────────────────────────────────────────────────
    renderer.RemoveAllLights()

    # Key light (warm, slightly off-center)
    key = vtk.vtkLight()
    key.SetPosition(1.0, 1.0, 2.0)
    key.SetFocalPoint(0, 0, 0)
    key.SetIntensity(0.85)
    key.SetColor(1.0, 0.97, 0.90)
    renderer.AddLight(key)

    # Fill light (cool, opposite side)
    fill = vtk.vtkLight()
    fill.SetPosition(-1.5, -0.5, 0.5)
    fill.SetFocalPoint(0, 0, 0)
    fill.SetIntensity(0.35)
    fill.SetColor(0.70, 0.80, 1.00)
    renderer.AddLight(fill)

    # Rim / back light for silhouette separation
    rim = vtk.vtkLight()
    rim.SetPosition(0.0, -1.5, -1.0)
    rim.SetFocalPoint(0, 0, 0)
    rim.SetIntensity(0.25)
    rim.SetColor(0.85, 0.85, 1.00)
    renderer.AddLight(rim)

    # ── Render window (offscreen) ─────────────────────────────────────────────
    ren_win = vtk.vtkRenderWindow()
    ren_win.SetOffScreenRendering(1)
    ren_win.SetSize(width, height)
    ren_win.AddRenderer(renderer)

    # ── Camera ───────────────────────────────────────────────────────────────
    renderer.ResetCamera()
    camera = renderer.GetActiveCamera()

    # Compute bounding sphere of all geometry
    bounds = [float("inf"), float("-inf"),
              float("inf"), float("-inf"),
              float("inf"), float("-inf")]
    for pd in polydata_list:
        b = pd.GetBounds()   # (xmin, xmax, ymin, ymax, zmin, zmax)
        bounds[0] = min(bounds[0], b[0])
        bounds[1] = max(bounds[1], b[1])
        bounds[2] = min(bounds[2], b[2])
        bounds[3] = max(bounds[3], b[3])
        bounds[4] = min(bounds[4], b[4])
        bounds[5] = max(bounds[5], b[5])

    cx = (bounds[0] + bounds[1]) * 0.5
    cy = (bounds[2] + bounds[3]) * 0.5
    cz = (bounds[4] + bounds[5]) * 0.5
    diag = math.sqrt(
        (bounds[1] - bounds[0]) ** 2 +
        (bounds[3] - bounds[2]) ** 2 +
        (bounds[5] - bounds[4]) ** 2
    )
    radius = max(diag, 1e-6) * 1.5 / zoom   # distance from center to camera

    cam_pos, cam_up = _elev_azim_to_camera((cx, cy, cz), radius, elev, azim)

    camera.SetFocalPoint(cx, cy, cz)
    camera.SetPosition(*cam_pos)
    camera.SetViewUp(*cam_up)
    camera.SetClippingRange(radius * 0.01, radius * 10.0)

    # ── Render and capture ────────────────────────────────────────────────────
    ren_win.Render()

    w2i = vtk.vtkWindowToImageFilter()
    w2i.SetInput(ren_win)
    w2i.SetScale(1)
    w2i.ReadFrontBufferOff()
    w2i.Update()

    buf = vtk.vtkPNGWriter()
    buf.SetInputConnection(w2i.GetOutputPort())
    buf.WriteToMemoryOn()
    buf.Write()

    png_data = bytes(buf.GetResult())
    elapsed = time.perf_counter() - t0
    log.info("VTK render complete: %.1f KB PNG in %.3fs", len(png_data) / 1024, elapsed)
    return png_data


def bounding_box(polydata_list: list) -> dict | None:
    if not polydata_list:
        return None
    mn = [float("inf")] * 3
    mx = [float("-inf")] * 3
    for pd in polydata_list:
        b = pd.GetBounds()
        mn[0] = min(mn[0], b[0])
        mn[1] = min(mn[1], b[2])
        mn[2] = min(mn[2], b[4])
        mx[0] = max(mx[0], b[1])
        mx[1] = max(mx[1], b[3])
        mx[2] = max(mx[2], b[5])
    return {"min": mn, "max": mx}


def main():
    log.info("Renderer started — reading payload from stdin")
    t_total = time.perf_counter()
    payload = json.load(sys.stdin)

    script_path: str = payload["script_path"]
    view_angle: str | None = payload.get("view_angle")
    custom_elev: float | None = payload.get("elevation")
    custom_azim: float | None = payload.get("azimuth")
    zoom: float = float(payload.get("zoom") or 1.0)
    width: int  = int(payload.get("width") or 1600)
    height: int = int(payload.get("height") or 1200)
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
        all_polydata = []
        shape_info = []
        for dname in FreeCAD.listDocuments():
            doc_objs = FreeCAD.getDocument(dname).Objects
            log.debug("  document %s: %d object(s)", dname, len(doc_objs))
            for obj in doc_objs:
                shape = None
                if hasattr(obj, "Shape") and obj.Shape is not None:
                    shape = obj.Shape
                elif hasattr(obj, "Mesh") and obj.Mesh is not None:
                    try:
                        pd = mesh_to_vtk_polydata(obj.Mesh)
                        all_polydata.append(pd)
                        shape_info.append({
                            "name": obj.Name,
                            "label": obj.Label,
                            "type": "Mesh",
                            "facets": obj.Mesh.CountFacets,
                        })
                        log.debug("    [Mesh] %s (%s): %d facets", obj.Label, obj.Name, obj.Mesh.CountFacets)
                    except Exception as e:
                        log.warning("    [Mesh] %s (%s): conversion error — %s", obj.Label, obj.Name, e)
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
                    pd = shape_to_vtk_polydata(shape)
                    all_polydata.append(pd)
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
                    log.debug("    [Shape] %s (%s): volume=%.3f", obj.Label, obj.Name, vol or 0)
                except Exception as e:
                    log.warning("    [Shape] %s (%s): tessellation error — %s",
                                obj.Label, obj.Name, e)
                    shape_info.append({
                        "name": obj.Name,
                        "label": obj.Label,
                        "error": str(e),
                    })

        if not all_polydata:
            log.error("No renderable geometry found in any open document")
            raise RuntimeError(
                "Script executed but no renderable geometry was found. "
                "Make sure the script adds geometry to the active document."
            )

        log.info("Collected %d shape(s)", len(shape_info))
        bb = bounding_box(all_polydata)
        if bb:
            log.debug("Bounding box: min=%s  max=%s", bb["min"], bb["max"])

        png_bytes = render(all_polydata, elev, azim, zoom, width, height, background)
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
