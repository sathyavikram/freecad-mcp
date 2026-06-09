#!/usr/bin/env python3
"""
FreeCAD MCP Server — freecad_renderer.py (v2)

Enhanced subprocess renderer with all LLM visual inspection features:

  Enhancement 1  — Multi-view burst        render_views=[...]
  Enhancement 2  — Exploded assembly       explode_factor=1.5
  Enhancement 3  — Cross-section clip      section_plane="XY", section_offset=0.5
  Enhancement 4  — Rich semantic metadata  centroid, face/edge/vertex counts,
                                           touching_pairs, color_rgb, placement
  Enhancement 5  — Dimension overlay       show_dimensions=true  (requires Pillow)
  Enhancement 6  — Highlight / isolate     highlight_objects=[...], focus_object="..."
  Enhancement 7  — Render modes            render_mode: shaded|wireframe|
                                           shaded+wireframe|normals|curvature
  Enhancement 8  — Interference detection  mode="check_interference", pairs=[...]
  Enhancement 9  — Orientation check       orientation_check=true
  Enhancement 10 — Joint annotation DSL    # @joint / # @constraint in script comments

Input JSON schema (stdin):
{
    "script_path":      str,                # required
    "mode":             str,                # "render" (default) | "check_interference"
    "view_angle":       str | null,         # preset name
    "render_views":     list[str] | null,   # burst — overrides view_angle
    "elevation":        float | null,
    "azimuth":          float | null,
    "zoom":             float,
    "width":            int,
    "height":           int,
    "background":       str,
    "explode_factor":   float,              # 0=normal, >0=exploded
    "highlight_objects":list[str],          # dim everything else
    "focus_object":     str | null,         # render only this object, zoom to fit
    "render_mode":      str,                # shaded|wireframe|shaded+wireframe|normals|curvature
    "show_dimensions":  bool,
    "section_plane":    str | dict | null,  # "XY"|"XZ"|"YZ"|{"normal":[...],"origin":[...]}
    "section_offset":   float,              # 0-1 fraction along bounding box
    "orientation_check":bool,
    "pairs":            list[[str,str]]     # for check_interference mode
}

Output JSON schema (stdout):
{
    "success":              bool,
    "image_b64":            str | null,           # first / only image (backward compat)
    "views": [                                    # multi-view burst results
        {"view": str, "elevation": float, "azimuth": float, "image_b64": str}
    ],
    "metadata": {
        "view_angle":       str,
        "elevation":        float,
        "azimuth":          float,
        "zoom":             float,
        "image_width":      int,
        "image_height":     int,
        "object_count":     int,
        "shape_info": [{                          # enriched per-object info
            "name":         str,
            "label":        str,
            "type":         str,
            "volume":       float | null,
            "area":         float | null,
            "centroid":     [x, y, z],
            "face_count":   int,
            "edge_count":   int,
            "vertex_count": int,
            "is_solid":     bool,
            "bounding_box": {"min": [...], "max": [...]},
            "color_rgb":    [r, g, b],
            "placement":    {"position": [...], "rotation_axis": [...], "rotation_angle_deg": float},
            "orientation_check": {...} | null
        }],
        "bounding_box":     {"min": [...], "max": [...]},
        "touching_pairs":   [{"objects": [A, B], "overlap_type": "touching"|"intersecting"}],
        "joints":           [{"type": "joint"|"constraint", ...}]
    },
    "interference_report":  list | null,          # check_interference mode only
    "error":                str | null
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
import re

# ── Logging (stderr — stdout reserved for JSON) ───────────────────────────────
logging.basicConfig(
    stream=sys.stderr,
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] renderer: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("freecad-renderer")

# ── FreeCAD bootstrap ─────────────────────────────────────────────────────────
FREECAD_LIB  = "/Applications/FreeCAD.app/Contents/Resources/lib"
FREECAD_SITE = os.path.join(FREECAD_LIB, "python3.11", "site-packages")

log.debug("Bootstrap: injecting FreeCAD paths")
for _p in (FREECAD_SITE, FREECAD_LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

log.debug("Importing FreeCAD …")
import FreeCAD
import FreeCADGui
FreeCADGui.setupWithoutGUI()
log.debug("FreeCAD %s ready", getattr(FreeCAD, "Version", lambda: ["?"])()[:3])

import Part
import MeshPart
import numpy as np

log.debug("Importing VTK …")
import vtk
log.debug("VTK %s ready", vtk.vtkVersion.GetVTKVersion())

# ── PIL (optional — for dimension overlay) ────────────────────────────────────
try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
    log.debug("Pillow available — dimension overlay enabled")
except ImportError:
    HAS_PIL = False
    log.warning("Pillow not installed — show_dimensions will be skipped. "
                "Run:  pip install Pillow  in the venv to enable it.")

# ── Constants ─────────────────────────────────────────────────────────────────
VIEW_PRESETS: dict[str, tuple[float, float]] = {
    "Top":       (90,    0),
    "Bottom":    (-90,   0),
    "Front":     (0,   -90),
    "Back":      (0,    90),
    "Left":      (0,   180),
    "Right":     (0,     0),
    "Isometric": (35.26, 45),
}
DEFAULT_VIEW = "Isometric"

# 8-colour palette (cycling); skips dark colours that vanish on dark bg
PALETTE = [
    (0.29, 0.56, 0.89),  # steel blue
    (0.34, 0.74, 0.56),  # teal green
    (0.91, 0.62, 0.26),  # amber
    (0.78, 0.36, 0.91),  # violet
    (0.91, 0.37, 0.43),  # rose
    (0.35, 0.82, 0.87),  # cyan
    (0.95, 0.90, 0.35),  # yellow
    (0.55, 0.85, 0.45),  # lime
]


# ═══════════════════════════════════════════════════════════════════════════════
# Colour helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _hex_to_rgb(hex_color: str) -> tuple[float, float, float]:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (0.05, 0.067, 0.09)
    return int(h[:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:], 16) / 255


def _is_dark(rgb: tuple) -> bool:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2] < 0.4


def _lighten(c: tuple, t: float = 0.35) -> tuple:
    return tuple(min(1.0, v * 0.6 + t) for v in c)


def _dim(c: tuple, alpha: float = 0.15) -> tuple:
    """Return a greyed/dimmed version of colour for non-highlighted objects."""
    return (0.5, 0.5, 0.5)


# ═══════════════════════════════════════════════════════════════════════════════
# Tessellation
# ═══════════════════════════════════════════════════════════════════════════════

def _normals_filter(pd: vtk.vtkPolyData) -> vtk.vtkPolyData:
    clean = vtk.vtkCleanPolyData()
    clean.SetInputData(pd)
    clean.Update()
    nf = vtk.vtkPolyDataNormals()
    nf.SetInputConnection(clean.GetOutputPort())
    nf.SetFeatureAngle(30.0)
    nf.SplittingOn()
    nf.ConsistencyOn()
    nf.AutoOrientNormalsOn()
    nf.Update()
    return nf.GetOutput()


def shape_to_vtk(shape) -> vtk.vtkPolyData:
    """Tessellate a FreeCAD TopoShape → vtkPolyData with smooth normals."""
    msh = MeshPart.meshFromShape(
        Shape=shape,
        LinearDeflection=0.02,
        AngularDeflection=0.05,
        Relative=False,
    )
    pts = vtk.vtkPoints()
    cells = vtk.vtkCellArray()
    for facet in msh.Facets:
        ids = [pts.InsertNextPoint(*p) for p in facet.Points]
        tri = vtk.vtkTriangle()
        for k, pid in enumerate(ids):
            tri.GetPointIds().SetId(k, pid)
        cells.InsertNextCell(tri)
    pd = vtk.vtkPolyData()
    pd.SetPoints(pts)
    pd.SetPolys(cells)
    return _normals_filter(pd)


def mesh_to_vtk(mesh_obj) -> vtk.vtkPolyData:
    """Convert a FreeCAD Mesh object → vtkPolyData."""
    pts = vtk.vtkPoints()
    cells = vtk.vtkCellArray()
    for facet in mesh_obj.Facets:
        ids = [pts.InsertNextPoint(*p) for p in facet.Points]
        tri = vtk.vtkTriangle()
        for k, pid in enumerate(ids):
            tri.GetPointIds().SetId(k, pid)
        cells.InsertNextCell(tri)
    pd = vtk.vtkPolyData()
    pd.SetPoints(pts)
    pd.SetPolys(cells)
    nf = vtk.vtkPolyDataNormals()
    nf.SetInputData(pd)
    nf.SetFeatureAngle(30.0)
    nf.SplittingOn()
    nf.ConsistencyOn()
    nf.AutoOrientNormalsOn()
    nf.Update()
    return nf.GetOutput()


# ═══════════════════════════════════════════════════════════════════════════════
# Enhancement 3 — Cross-section clipping
# ═══════════════════════════════════════════════════════════════════════════════

def _resolve_section_plane(spec, bounds: list, offset: float) -> dict | None:
    """
    Convert a section_plane spec into {"normal": [nx,ny,nz], "origin": [ox,oy,oz]}.
    bounds = [xmin, xmax, ymin, ymax, zmin, zmax]
    offset = 0-1 fraction along the cut axis.
    """
    if spec is None:
        return None
    xmin, xmax, ymin, ymax, zmin, zmax = bounds
    if isinstance(spec, dict):
        return spec  # already resolved by caller
    mapping = {
        "XY": ([0, 0, 1],  [0, 0, zmin + (zmax - zmin) * offset]),
        "XZ": ([0, 1, 0],  [0, ymin + (ymax - ymin) * offset, 0]),
        "YZ": ([1, 0, 0],  [xmin + (xmax - xmin) * offset, 0, 0]),
    }
    if spec.upper() not in mapping:
        log.warning("Unknown section_plane spec %r — ignoring", spec)
        return None
    normal, origin = mapping[spec.upper()]
    return {"normal": normal, "origin": origin}


def _clip_polydata(pd: vtk.vtkPolyData, plane: dict) -> vtk.vtkPolyData:
    """Clip geometry with a plane and cap the cut face so it looks solid."""
    vtk_plane = vtk.vtkPlane()
    vtk_plane.SetNormal(*plane["normal"])
    vtk_plane.SetOrigin(*plane["origin"])
    planes = vtk.vtkPlaneCollection()
    planes.AddItem(vtk_plane)
    try:
        ccs = vtk.vtkClipClosedSurface()
        ccs.SetInputData(pd)
        ccs.SetClippingPlanes(planes)
        ccs.SetActivePlaneId(0)
        ccs.SetActivePlaneColor(0.85, 0.85, 0.85)
        ccs.GenerateOutlineOff()
        ccs.Update()
        return ccs.GetOutput()
    except Exception as e:
        log.warning("vtkClipClosedSurface failed (%s) — falling back to vtkClipPolyData", e)
        clip = vtk.vtkClipPolyData()
        clip.SetInputData(pd)
        clip.SetClipFunction(vtk_plane)
        clip.InsideOutOff()
        clip.Update()
        return clip.GetOutput()


# ═══════════════════════════════════════════════════════════════════════════════
# Enhancement 7 — Render mode helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_normal_color_pd(pd: vtk.vtkPolyData) -> vtk.vtkPolyData:
    """
    Create a copy of pd where each point is coloured by its surface normal
    (normal-map style: maps [-1,1] → [0,255] per channel).
    """
    point_normals = pd.GetPointData().GetNormals()
    if point_normals is None:
        return pd
    colors = vtk.vtkUnsignedCharArray()
    colors.SetNumberOfComponents(3)
    colors.SetName("NormalRGB")
    for i in range(point_normals.GetNumberOfTuples()):
        nx, ny, nz = point_normals.GetTuple3(i)
        colors.InsertNextTuple3(
            max(0, min(255, int((nx + 1.0) * 127.5))),
            max(0, min(255, int((ny + 1.0) * 127.5))),
            max(0, min(255, int((nz + 1.0) * 127.5))),
        )
    result = vtk.vtkPolyData()
    result.DeepCopy(pd)
    result.GetPointData().SetScalars(colors)
    return result


def _make_curvature_pd(pd: vtk.vtkPolyData) -> tuple[vtk.vtkPolyData, float, float]:
    """Add Gaussian curvature as scalar field; return (polydata, scalar_min, scalar_max)."""
    try:
        curv = vtk.vtkCurvatures()
        curv.SetInputData(pd)
        curv.SetCurvatureTypeToGaussian()
        curv.Update()
        out = curv.GetOutput()
        scalars = out.GetPointData().GetScalars()
        if scalars:
            r = scalars.GetRange()
            # Clamp extreme curvature outliers to ±0.1 for better colour contrast
            lo = max(r[0], -0.1)
            hi = min(r[1],  0.1)
        else:
            lo, hi = -0.01, 0.01
        return out, lo, hi
    except Exception as e:
        log.warning("Curvature computation failed: %s", e)
        return pd, -0.01, 0.01


# ═══════════════════════════════════════════════════════════════════════════════
# Camera helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _elev_azim_to_camera(
    center: tuple, radius: float, elev_deg: float, azim_deg: float
) -> tuple[tuple, tuple]:
    elev = math.radians(elev_deg)
    azim = math.radians(azim_deg)
    x = center[0] + radius * math.cos(elev) * math.cos(azim)
    y = center[1] + radius * math.cos(elev) * math.sin(azim)
    z = center[2] + radius * math.sin(elev)
    ux = -math.sin(elev) * math.cos(azim)
    uy = -math.sin(elev) * math.sin(azim)
    uz =  math.cos(elev)
    if abs(uz) > 0.999:
        ux, uy, uz = 0.0, 1.0, 0.0
    return (x, y, z), (ux, uy, uz)


def _overall_bounds(polydata_list: list) -> list:
    """[xmin, xmax, ymin, ymax, zmin, zmax] over all polydata."""
    if not polydata_list:
        return [0, 1, 0, 1, 0, 1]
    inf = float("inf")
    b = [inf, -inf, inf, -inf, inf, -inf]
    for pd in polydata_list:
        bb = pd.GetBounds()  # (xmin,xmax,ymin,ymax,zmin,zmax)
        b[0] = min(b[0], bb[0]); b[1] = max(b[1], bb[1])
        b[2] = min(b[2], bb[2]); b[3] = max(b[3], bb[3])
        b[4] = min(b[4], bb[4]); b[5] = max(b[5], bb[5])
    return b


# ═══════════════════════════════════════════════════════════════════════════════
# Enhancement 5 — Dimension overlay (PIL)
# ═══════════════════════════════════════════════════════════════════════════════

def _project_world(renderer, x, y, z, img_h: int) -> tuple[int, int]:
    """World 3-D point → (px, py) image pixel (origin top-left)."""
    coord = vtk.vtkCoordinate()
    coord.SetCoordinateSystemToWorld()
    coord.SetValue(x, y, z)
    dc = coord.GetComputedDisplayValue(renderer)
    return int(dc[0]), int(img_h - dc[1])


def _add_dimension_overlay(
    png_bytes: bytes,
    renderer,
    bounds: list,
    shape_info: list,
    width: int,
    height: int,
    dark_bg: bool,
) -> bytes:
    """Draw bounding-box WxDxH labels and per-object centroid dots + names."""
    if not HAS_PIL:
        return png_bytes
    try:
        img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
        draw = ImageDraw.Draw(img)

        text_col = (240, 240, 240, 230) if dark_bg else (15, 15, 15, 230)
        dim_col  = (90, 200, 255, 220) if dark_bg else (0, 70, 160, 220)
        dot_col  = (255, 220, 60, 240)

        xmin, xmax, ymin, ymax, zmin, zmax = bounds
        dx, dy, dz = xmax - xmin, ymax - ymin, zmax - zmin

        # Corner legend: overall W × D × H
        m, lh = 14, 20
        for i, (label, val) in enumerate([("W", dx), ("D", dy), ("H", dz)]):
            draw.text((m, m + i * lh), f"{label}: {val:.1f} mm", fill=dim_col)

        # Per-object labels at their centroids
        for s in shape_info:
            if "centroid" not in s:
                continue
            cx, cy, cz = s["centroid"]
            try:
                px, py = _project_world(renderer, cx, cy, cz, height)
                if 4 < px < width - 4 and 4 < py < height - 4:
                    name = s.get("label", s.get("name", "?"))
                    draw.ellipse((px - 4, py - 4, px + 4, py + 4), fill=dot_col)
                    draw.text((px + 6, py - 9), name, fill=text_col)
            except Exception:
                pass

        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception as e:
        log.warning("Dimension overlay failed: %s", e)
        return png_bytes


# ═══════════════════════════════════════════════════════════════════════════════
# Core render function (all modes + enhancements)
# ═══════════════════════════════════════════════════════════════════════════════

def render(
    polydata_list: list,
    elev: float,
    azim: float,
    *,
    zoom: float = 1.0,
    width: int = 1600,
    height: int = 1200,
    background: str = "#0d1117",
    object_colors: list | None = None,
    object_names: list | None = None,
    render_mode: str = "shaded",
    highlight_names: list | None = None,
    focus_name: str | None = None,
    explode_offsets: list | None = None,
    section_plane_dict: dict | None = None,
    show_dimensions: bool = False,
    shape_info: list | None = None,
) -> bytes:
    """
    Render polydata_list to a PNG and return raw bytes.

    Parameters
    ----------
    polydata_list    : tessellated geometry, one entry per object
    elev / azim      : camera angles (degrees)
    zoom             : 1.0 = fit-all; >1 zooms in
    object_colors    : list of (r,g,b) 0-1 floats per object
    object_names     : label strings matching polydata_list (for highlight/focus)
    render_mode      : shaded | wireframe | shaded+wireframe | normals | curvature
    highlight_names  : if non-empty, dim all objects NOT in this list
    focus_name       : if set, render only that object (by name) and zoom to fit
    explode_offsets  : per-object (dx,dy,dz) translations (Enhancement 2)
    section_plane_dict : {"normal":[...], "origin":[...]} clipping plane (Enhancement 3)
    show_dimensions  : draw WxDxH annotation + centroid labels (Enhancement 5)
    shape_info       : forwarded to dimension overlay for labels
    """
    t0 = time.perf_counter()
    log.info("render() mode=%s elev=%.1f azim=%.1f zoom=%.2f %dx%d",
             render_mode, elev, azim, zoom, width, height)

    bg_rgb  = _hex_to_rgb(background)
    dark_bg = _is_dark(bg_rgb)

    # ── Enhancement 3: Section plane clipping ────────────────────────────────
    working_pd = []
    if section_plane_dict:
        for pd in polydata_list:
            try:
                working_pd.append(_clip_polydata(pd, section_plane_dict))
            except Exception as e:
                log.warning("Clip failed, using original: %s", e)
                working_pd.append(pd)
    else:
        working_pd = list(polydata_list)

    # ── Enhancement 6: Focus object — keep only that object ──────────────────
    working_names  = list(object_names)  if object_names  else [None] * len(working_pd)
    working_colors = list(object_colors) if object_colors else [PALETTE[i % len(PALETTE)] for i in range(len(working_pd))]
    working_offsets = list(explode_offsets) if explode_offsets else [(0, 0, 0)] * len(working_pd)

    if focus_name:
        try:
            fi = working_names.index(focus_name)
            working_pd      = [working_pd[fi]]
            working_names   = [working_names[fi]]
            working_colors  = [working_colors[fi]]
            working_offsets = [(0, 0, 0)]   # no explode for focused single-part view
            log.debug("focus_object=%r → keeping index %d only", focus_name, fi)
        except ValueError:
            log.warning("focus_object %r not found in object list — rendering all", focus_name)

    # ── VTK Renderer ─────────────────────────────────────────────────────────
    renderer = vtk.vtkRenderer()
    renderer.SetBackground(*bg_rgb)

    AMBIENT    = 0.18
    DIFFUSE    = 0.72
    SPECULAR   = 0.55
    SPEC_POWER = 60.0

    curvature_lut = None

    for i, pd in enumerate(working_pd):
        raw_color = working_colors[i] if i < len(working_colors) else PALETTE[i % len(PALETTE)]
        name      = working_names[i]
        offset    = working_offsets[i] if i < len(working_offsets) else (0, 0, 0)

        # Enhancement 6 — Highlight: dim non-highlighted objects
        is_hi = (not highlight_names) or (name and name in highlight_names)
        color   = raw_color if is_hi else _dim(raw_color)
        opacity = 1.0       if is_hi else 0.12

        def _transform_actor(actor):
            if any(o != 0 for o in offset):
                t = vtk.vtkTransform()
                t.Translate(*offset)
                actor.SetUserTransform(t)

        # ── Mode: normals ─────────────────────────────────────────────────────
        if render_mode == "normals":
            pd_n = _make_normal_color_pd(pd)
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(pd_n)
            mapper.ScalarVisibilityOn()
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetOpacity(opacity)
            _transform_actor(actor)
            renderer.AddActor(actor)
            continue

        # ── Mode: curvature ───────────────────────────────────────────────────
        if render_mode == "curvature":
            pd_c, clo, chi = _make_curvature_pd(pd)
            if curvature_lut is None:
                curvature_lut = vtk.vtkColorTransferFunction()
                curvature_lut.AddRGBPoint(clo,  0.0, 0.0, 1.0)   # blue  = flat / concave
                curvature_lut.AddRGBPoint(0.0,  0.0, 1.0, 0.0)   # green = saddle / zero
                curvature_lut.AddRGBPoint(chi,  1.0, 0.0, 0.0)   # red   = convex peak
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(pd_c)
            mapper.ScalarVisibilityOn()
            mapper.SetLookupTable(curvature_lut)
            mapper.SetScalarRange(clo, chi)
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetOpacity(opacity)
            _transform_actor(actor)
            renderer.AddActor(actor)
            continue

        # ── Mode: shaded (default) ────────────────────────────────────────────
        if render_mode in ("shaded", "shaded+wireframe"):
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
            prop.SetOpacity(opacity)
            _transform_actor(actor)
            renderer.AddActor(actor)

            # Feature edges only in pure shaded mode
            if render_mode == "shaded":
                fe = vtk.vtkFeatureEdges()
                fe.SetInputData(pd)
                fe.BoundaryEdgesOn()
                fe.FeatureEdgesOn()
                fe.SetFeatureAngle(25.0)
                fe.ManifoldEdgesOff()
                fe.NonManifoldEdgesOff()
                fe.Update()
                em = vtk.vtkPolyDataMapper()
                em.SetInputConnection(fe.GetOutputPort())
                em.ScalarVisibilityOff()
                ea = vtk.vtkActor()
                ea.SetMapper(em)
                ep = ea.GetProperty()
                ep.SetColor(*_lighten(color))
                ep.SetAmbient(1.0)
                ep.SetDiffuse(0.0)
                ep.SetSpecular(0.0)
                ep.SetLineWidth(1.5)
                ep.SetOpacity(opacity)
                _transform_actor(ea)
                renderer.AddActor(ea)

        # ── Mode: wireframe / shaded+wireframe ────────────────────────────────
        if render_mode in ("wireframe", "shaded+wireframe"):
            wm = vtk.vtkPolyDataMapper()
            wm.SetInputData(pd)
            wm.ScalarVisibilityOff()
            wa = vtk.vtkActor()
            wa.SetMapper(wm)
            wp = wa.GetProperty()
            wp.SetRepresentationToWireframe()
            wc = color if render_mode == "wireframe" else (
                min(1.0, color[0] * 0.5),
                min(1.0, color[1] * 0.5),
                min(1.0, color[2] * 0.5),
            )
            wp.SetColor(*wc)
            wp.SetAmbient(1.0)
            wp.SetDiffuse(0.0)
            wp.SetLineWidth(1.0 if render_mode == "shaded+wireframe" else 1.5)
            wp.SetOpacity(opacity)
            _transform_actor(wa)
            renderer.AddActor(wa)

    # ── 3-point lighting ──────────────────────────────────────────────────────
    renderer.RemoveAllLights()
    for pos, intensity, color_l in [
        ((1.0,  1.0,  2.0), 0.85, (1.00, 0.97, 0.90)),   # key — warm
        ((-1.5, -0.5, 0.5), 0.35, (0.70, 0.80, 1.00)),   # fill — cool
        ((0.0, -1.5, -1.0), 0.25, (0.85, 0.85, 1.00)),   # rim
    ]:
        lt = vtk.vtkLight()
        lt.SetPosition(*pos)
        lt.SetFocalPoint(0, 0, 0)
        lt.SetIntensity(intensity)
        lt.SetColor(*color_l)
        renderer.AddLight(lt)

    # ── Offscreen render window ───────────────────────────────────────────────
    ren_win = vtk.vtkRenderWindow()
    ren_win.SetOffScreenRendering(1)
    ren_win.SetSize(width, height)
    ren_win.AddRenderer(renderer)

    # ── Camera ────────────────────────────────────────────────────────────────
    # Use explode-adjusted bounds for camera fit
    if any(any(o != 0 for o in off) for off in working_offsets):
        exp_bounds = [float("inf"), float("-inf")] * 3
        exp_b = [float("inf"), float("-inf"), float("inf"), float("-inf"), float("inf"), float("-inf")]
        for j, pd in enumerate(working_pd):
            b = pd.GetBounds()
            off = working_offsets[j]
            exp_b[0] = min(exp_b[0], b[0] + off[0]); exp_b[1] = max(exp_b[1], b[1] + off[0])
            exp_b[2] = min(exp_b[2], b[2] + off[1]); exp_b[3] = max(exp_b[3], b[3] + off[1])
            exp_b[4] = min(exp_b[4], b[4] + off[2]); exp_b[5] = max(exp_b[5], b[5] + off[2])
        cam_bounds = exp_b
    else:
        cam_bounds = _overall_bounds(working_pd)

    cx = (cam_bounds[0] + cam_bounds[1]) * 0.5
    cy = (cam_bounds[2] + cam_bounds[3]) * 0.5
    cz = (cam_bounds[4] + cam_bounds[5]) * 0.5
    diag = math.sqrt(
        (cam_bounds[1] - cam_bounds[0]) ** 2 +
        (cam_bounds[3] - cam_bounds[2]) ** 2 +
        (cam_bounds[5] - cam_bounds[4]) ** 2
    )
    radius   = max(diag, 1e-6) * 1.5 / zoom
    cam_pos, cam_up = _elev_azim_to_camera((cx, cy, cz), radius, elev, azim)
    cam = renderer.GetActiveCamera()
    cam.SetFocalPoint(cx, cy, cz)
    cam.SetPosition(*cam_pos)
    cam.SetViewUp(*cam_up)
    cam.SetClippingRange(radius * 0.01, radius * 10.0)

    ren_win.Render()

    # ── Capture PNG ───────────────────────────────────────────────────────────
    w2i = vtk.vtkWindowToImageFilter()
    w2i.SetInput(ren_win)
    w2i.SetScale(1)
    w2i.ReadFrontBufferOff()
    w2i.Update()
    png_writer = vtk.vtkPNGWriter()
    png_writer.SetInputConnection(w2i.GetOutputPort())
    png_writer.WriteToMemoryOn()
    png_writer.Write()
    png_bytes = bytes(png_writer.GetResult())

    # ── Enhancement 5: Dimension overlay ─────────────────────────────────────
    if show_dimensions:
        overlay_bounds = _overall_bounds(working_pd)
        png_bytes = _add_dimension_overlay(
            png_bytes, renderer, overlay_bounds, shape_info or [], width, height, dark_bg
        )

    log.info("render() done: %.1f KB in %.3fs", len(png_bytes) / 1024, time.perf_counter() - t0)
    return png_bytes


# ═══════════════════════════════════════════════════════════════════════════════
# Enhancement 4 — Rich metadata helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _get_shape_color(obj) -> list | None:
    """Extract ShapeColor from FreeCAD ViewObject if available."""
    try:
        vc = obj.ViewObject.ShapeColor
        return [round(float(vc[0]), 3), round(float(vc[1]), 3), round(float(vc[2]), 3)]
    except Exception:
        return None


def _centroid_from_bb(shape) -> list[float]:
    bb = shape.BoundBox
    return [
        round((bb.XMin + bb.XMax) / 2, 4),
        round((bb.YMin + bb.YMax) / 2, 4),
        round((bb.ZMin + bb.ZMax) / 2, 4),
    ]


def _compute_touching_pairs(shape_list: list, threshold_mm: float = 0.5) -> list:
    """
    Return pairs of objects whose axis-aligned bounding boxes overlap or are
    within threshold_mm of each other.
    """
    pairs = []
    n = len(shape_list)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = shape_list[i], shape_list[j]
            if "bounding_box" not in a or "bounding_box" not in b:
                continue
            amin, amax = a["bounding_box"]["min"], a["bounding_box"]["max"]
            bmin, bmax = b["bounding_box"]["min"], b["bounding_box"]["max"]
            # Check if boxes are within threshold on every axis
            close = all(
                max(bmin[k] - amax[k], amin[k] - bmax[k], 0) <= threshold_mm
                for k in range(3)
            )
            if close:
                intersecting = all(
                    bmin[k] < amax[k] and amin[k] < bmax[k]
                    for k in range(3)
                )
                pairs.append({
                    "objects":      [a["label"], b["label"]],
                    "object_names": [a["name"],  b["name"]],
                    "overlap_type": "intersecting" if intersecting else "touching",
                })
    return pairs


# ═══════════════════════════════════════════════════════════════════════════════
# Enhancement 9 — Orientation & balance check
# ═══════════════════════════════════════════════════════════════════════════════

def _orientation_check(shape) -> dict:
    """Compute orientation / balance information for a FreeCAD TopoShape."""
    out: dict = {}
    try:
        bb = shape.BoundBox
        w = bb.XMax - bb.XMin
        d = bb.YMax - bb.YMin
        h = bb.ZMax - bb.ZMin
        out["dimensions_mm"] = {"width": round(w, 3), "depth": round(d, 3), "height": round(h, 3)}

        # Centre of mass (falls back to bounding-box centre if unavailable)
        try:
            com = shape.CenterOfMass
            out["center_of_mass"] = [round(com.x, 4), round(com.y, 4), round(com.z, 4)]
        except Exception:
            out["center_of_mass"] = [
                round((bb.XMin + bb.XMax) / 2, 4),
                round((bb.YMin + bb.YMax) / 2, 4),
                round((bb.ZMin + bb.ZMax) / 2, 4),
            ]

        # Principal inertia matrix
        try:
            mat = shape.MatrixOfInertia
            out["inertia_matrix"] = [
                [round(mat.get(r, c), 4) for c in range(3)]
                for r in range(3)
            ]
        except Exception:
            pass

        # Largest flat face → estimated print orientation
        face_areas = {"XY": w * d, "XZ": w * h, "YZ": d * h}
        best_face  = max(face_areas, key=face_areas.get)
        out["largest_face_plane"]    = best_face
        out["estimated_print_face"]  = {"XY": "Bottom", "XZ": "Front", "YZ": "Right"}[best_face]
        out["aspect_ratios"] = {
            "W_D": round(w / max(d, 1e-9), 3),
            "W_H": round(w / max(h, 1e-9), 3),
            "D_H": round(d / max(h, 1e-9), 3),
        }
    except Exception as e:
        out["error"] = str(e)
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# Enhancement 10 — Joint / constraint annotation DSL
# ═══════════════════════════════════════════════════════════════════════════════

_JOINT_RE      = re.compile(r"#\s*@joint\s+(\S+)\s*-+>\s*(\S+)(.*)", re.IGNORECASE)
_CONSTRAINT_RE = re.compile(r"#\s*@constraint\s+(\S+)\s+(\S+)\s+(\S+)(.*)", re.IGNORECASE)
_ATTR_RE       = re.compile(r"(\w+)=([\S]+)")


def _parse_joints(script_text: str) -> list:
    """
    Parse # @joint and # @constraint annotations from script comments.

    Syntax:
        # @joint BodyA.Face3 -> BodyB.Face1  type=sliding  clearance=0.2mm
        # @constraint BodyA  coaxial  BodyB  axis=Z
    """
    joints = []
    for line in script_text.splitlines():
        m = _JOINT_RE.search(line)
        if m:
            attrs = dict(_ATTR_RE.findall(m.group(3)))
            joints.append({"type": "joint", "from": m.group(1), "to": m.group(2), **attrs})
            continue
        m = _CONSTRAINT_RE.search(line)
        if m:
            attrs = dict(_ATTR_RE.findall(m.group(4)))
            joints.append({
                "type":       "constraint",
                "body_a":     m.group(1),
                "constraint": m.group(2),
                "body_b":     m.group(3),
                **attrs,
            })
    return joints


# ═══════════════════════════════════════════════════════════════════════════════
# Enhancement 8 — Interference / overlap detection
# ═══════════════════════════════════════════════════════════════════════════════

def _check_interference(shape_map: dict, pairs: list) -> list:
    """
    Compute Boolean intersection volume for each requested pair.
    shape_map: {label_or_name: FreeCAD_Shape}
    """
    report = []
    for pair in pairs:
        name_a, name_b = pair[0], pair[1]
        sa = shape_map.get(name_a)
        sb = shape_map.get(name_b)
        if sa is None or sb is None:
            missing = name_a if sa is None else name_b
            report.append({"pair": [name_a, name_b], "error": f"Object not found: {missing}"})
            continue
        try:
            common = sa.common(sb)
            vol    = common.Volume
            report.append({
                "pair":               [name_a, name_b],
                "overlap_volume_mm3": round(vol, 5),
                "clear":              vol < 0.001,
            })
        except Exception as e:
            report.append({"pair": [name_a, name_b], "error": str(e)})
    return report


# ═══════════════════════════════════════════════════════════════════════════════
# Global bounding_box helper (legacy compat)
# ═══════════════════════════════════════════════════════════════════════════════

def bounding_box(polydata_list: list) -> dict | None:
    if not polydata_list:
        return None
    b = _overall_bounds(polydata_list)
    return {
        "min": [b[0], b[2], b[4]],
        "max": [b[1], b[3], b[5]],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# main()
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    log.info("freecad_renderer v2 started — reading payload from stdin")
    t_total = time.perf_counter()
    payload = json.load(sys.stdin)

    # ── Parse payload ─────────────────────────────────────────────────────────
    mode:           str        = payload.get("mode", "render")
    script_path:    str        = payload["script_path"]
    view_angle:     str | None = payload.get("view_angle")
    render_views:   list | None= payload.get("render_views")    # Enhancement 1
    custom_elev:    float|None = payload.get("elevation")
    custom_azim:    float|None = payload.get("azimuth")
    zoom:           float      = float(payload.get("zoom")   or 1.0)
    width:          int        = int(  payload.get("width")  or 1600)
    height:         int        = int(  payload.get("height") or 1200)
    background:     str        = payload.get("background")   or "#0d1117"
    explode_factor: float      = float(payload.get("explode_factor") or 0.0)   # Enhancement 2
    section_plane_spec         = payload.get("section_plane")                  # Enhancement 3
    section_offset: float      = float(payload.get("section_offset") or 0.5)  # Enhancement 3
    highlight_objs: list       = payload.get("highlight_objects") or []        # Enhancement 6
    focus_object:   str | None = payload.get("focus_object")                   # Enhancement 6
    render_mode:    str        = payload.get("render_mode") or "shaded"        # Enhancement 7
    show_dimensions:bool       = bool(payload.get("show_dimensions", False))   # Enhancement 5
    do_orientation: bool       = bool(payload.get("orientation_check", False)) # Enhancement 9
    iface_pairs:    list       = payload.get("pairs") or []                    # Enhancement 8

    log.info("mode=%s script=%s render_views=%s explode=%.2f section=%s",
             mode, script_path, render_views, explode_factor, section_plane_spec)

    # ── Read + parse script ───────────────────────────────────────────────────
    script_text = open(script_path, encoding="utf-8").read()
    joints = _parse_joints(script_text)   # Enhancement 10
    if joints:
        log.info("Parsed %d joint/constraint annotation(s) from script", len(joints))

    # ── Create isolated FreeCAD document ─────────────────────────────────────
    doc_name = "MCP_Render"
    doc      = FreeCAD.newDocument(doc_name)
    FreeCAD.setActiveDocument(doc_name)

    try:
        exec_globals = {
            "FreeCAD": FreeCAD, "App": FreeCAD, "doc": doc, "Part": Part,
            "__name__": "__main__", "__file__": script_path,
        }
        for mod_name in ("Part", "Mesh", "MeshPart", "Draft", "Sketcher"):
            try:
                exec_globals[mod_name] = __import__(mod_name)
            except ImportError:
                pass

        log.info("Executing script …")
        t_exec = time.perf_counter()
        exec(compile(script_text, "<mcp_script>", "exec"), exec_globals)
        log.info("Script done in %.3fs", time.perf_counter() - t_exec)

        for dname in FreeCAD.listDocuments():
            try:
                FreeCAD.getDocument(dname).recompute()
            except Exception as e:
                log.warning("Recompute failed for %s: %s", dname, e)

        # ── Collect all tessellatable shapes ──────────────────────────────────
        all_polydata:   list = []
        shape_info:     list = []
        object_colors:  list = []
        object_names_l: list = []
        shape_map:      dict = {}    # label→FreeCAD shape (for interference)
        centroids:      list = []    # for explode offsets

        for dname in FreeCAD.listDocuments():
            for obj in FreeCAD.getDocument(dname).Objects:
                shape = None

                # ── Mesh objects ──────────────────────────────────────────────
                if hasattr(obj, "Mesh") and obj.Mesh is not None and (
                    not hasattr(obj, "Shape") or obj.Shape is None
                ):
                    try:
                        pd = mesh_to_vtk(obj.Mesh)
                        all_polydata.append(pd)
                        color_rgb = _get_shape_color(obj) or [round(c, 3) for c in PALETTE[len(all_polydata) % len(PALETTE)]]
                        object_colors.append(tuple(color_rgb))
                        object_names_l.append(obj.Label)
                        centroids.append([0, 0, 0])  # no BoundBox for raw Mesh
                        shape_info.append({
                            "name": obj.Name, "label": obj.Label,
                            "type": "Mesh",
                            "facets": obj.Mesh.CountFacets,
                            "color_rgb": color_rgb,
                        })
                        log.debug("[Mesh] %s: %d facets", obj.Label, obj.Mesh.CountFacets)
                    except Exception as e:
                        log.warning("[Mesh] %s: %s", obj.Label, e)
                    continue

                # ── TopoShape objects ─────────────────────────────────────────
                if hasattr(obj, "Shape") and obj.Shape is not None:
                    shape = obj.Shape

                try:
                    if shape is None or shape.isNull():
                        continue
                except AttributeError:
                    if shape is None:
                        continue

                try:
                    log.debug("[Shape] tessellating %s (%s)", obj.Label, shape.ShapeType)
                    pd = shape_to_vtk(shape)
                    all_polydata.append(pd)

                    bb        = shape.BoundBox
                    vol       = getattr(shape, "Volume", None)
                    area      = getattr(shape, "Area",   None)
                    centroid  = _centroid_from_bb(shape)
                    color_rgb = _get_shape_color(obj)
                    if color_rgb:
                        rc = tuple(color_rgb)
                    else:
                        rc = PALETTE[len(all_polydata) % len(PALETTE)]
                        color_rgb = list(rc)

                    object_colors.append(rc)
                    object_names_l.append(obj.Label)
                    centroids.append(centroid)

                    try:
                        placement = {
                            "position": [
                                round(obj.Placement.Base.x, 4),
                                round(obj.Placement.Base.y, 4),
                                round(obj.Placement.Base.z, 4),
                            ],
                            "rotation_axis": [
                                round(obj.Placement.Rotation.Axis.x, 4),
                                round(obj.Placement.Rotation.Axis.y, 4),
                                round(obj.Placement.Rotation.Axis.z, 4),
                            ],
                            "rotation_angle_deg": round(
                                math.degrees(obj.Placement.Rotation.Angle), 4
                            ),
                        }
                    except Exception:
                        placement = None

                    sinfo: dict = {
                        "name":         obj.Name,
                        "label":        obj.Label,
                        "type":         shape.ShapeType,
                        "volume":       round(vol, 5)  if vol  is not None else None,
                        "area":         round(area, 5) if area is not None else None,
                        "centroid":     centroid,
                        "face_count":   len(shape.Faces),
                        "edge_count":   len(shape.Edges),
                        "vertex_count": len(shape.Vertexes),
                        "is_solid":     shape.ShapeType == "Solid",
                        "bounding_box": {
                            "min": [bb.XMin, bb.YMin, bb.ZMin],
                            "max": [bb.XMax, bb.YMax, bb.ZMax],
                        },
                        "color_rgb":    [round(c, 3) for c in color_rgb],
                        "placement":    placement,
                    }
                    if do_orientation:   # Enhancement 9
                        sinfo["orientation_check"] = _orientation_check(shape)

                    shape_info.append(sinfo)
                    shape_map[obj.Label] = shape
                    shape_map[obj.Name]  = shape
                    log.debug("[Shape] %s vol=%.3f", obj.Label, vol or 0)

                except Exception as e:
                    log.warning("[Shape] %s error: %s", obj.Label, e)
                    shape_info.append({"name": obj.Name, "label": obj.Label, "error": str(e)})

        if not all_polydata:
            raise RuntimeError(
                "Script executed but produced no renderable geometry. "
                "Ensure it calls Part.show(), doc.addObject(), etc."
            )

        log.info("Collected %d object(s)", len(shape_info))

        # Enhancement 4: touching pairs
        touching_pairs = _compute_touching_pairs(shape_info)
        if touching_pairs:
            log.info("Touching pairs: %d", len(touching_pairs))

        # ── Enhancement 8: Interference check mode ────────────────────────────
        if mode == "check_interference":
            iface_report = _check_interference(shape_map, iface_pairs)
            result = {
                "success": True,
                "image_b64": None,
                "views": None,
                "metadata": {
                    "object_count":  len(shape_info),
                    "shape_info":    shape_info,
                    "bounding_box":  bounding_box(all_polydata),
                    "touching_pairs": touching_pairs,
                    "joints":        joints,
                },
                "interference_report": iface_report,
                "error": None,
            }
            sys.stdout.write(json.dumps(result))
            sys.stdout.flush()
            return

        # ── Enhancement 2: Explode offsets ────────────────────────────────────
        explode_offsets = None
        if explode_factor > 0.0 and len(all_polydata) > 1:
            valid_c = [c for c in centroids if c != [0, 0, 0]]
            if valid_c:
                ac = [
                    sum(c[k] for c in valid_c) / len(valid_c)
                    for k in range(3)
                ]
                explode_offsets = []
                for c in centroids:
                    v = [c[k] - ac[k] for k in range(3)]
                    mag = math.sqrt(sum(vv**2 for vv in v))
                    if mag < 1e-9:
                        explode_offsets.append((0, 0, 0))
                    else:
                        # scale by bounding-box diagonal for proportional spread
                        bb_all = _overall_bounds(all_polydata)
                        diag   = math.sqrt(
                            (bb_all[1]-bb_all[0])**2 +
                            (bb_all[3]-bb_all[2])**2 +
                            (bb_all[5]-bb_all[4])**2
                        )
                        scale = diag * explode_factor / max(mag, 1e-9)
                        explode_offsets.append(tuple(vv * scale * 0.5 for vv in v))
                log.info("Explode offsets computed (factor=%.2f)", explode_factor)

        # ── Enhancement 3: Section plane ─────────────────────────────────────
        section_plane_dict = None
        if section_plane_spec:
            ob = _overall_bounds(all_polydata)
            section_plane_dict = _resolve_section_plane(section_plane_spec, ob, section_offset)
            log.info("Section plane: %s", section_plane_dict)

        # ── Common kwargs for render() ────────────────────────────────────────
        common_kwargs = dict(
            zoom=zoom,
            width=width,
            height=height,
            background=background,
            object_colors=object_colors,
            object_names=object_names_l,
            render_mode=render_mode,
            highlight_names=highlight_objs or None,
            focus_name=focus_object,
            explode_offsets=explode_offsets,
            section_plane_dict=section_plane_dict,
            show_dimensions=show_dimensions,
            shape_info=shape_info,
        )

        bb_dict = bounding_box(all_polydata)

        # ── Enhancement 1: Multi-view burst vs single view ────────────────────
        if render_views:
            views_out = []
            for vname in render_views:
                vname = vname if vname in VIEW_PRESETS else DEFAULT_VIEW
                ve, va = VIEW_PRESETS[vname]
                png_bytes = render(all_polydata, ve, va, **common_kwargs)
                views_out.append({
                    "view":      vname,
                    "elevation": ve,
                    "azimuth":   va,
                    "image_b64": base64.b64encode(png_bytes).decode(),
                })
                log.info("  burst: %s rendered (%.1f KB)", vname, len(png_bytes)/1024)

            result = {
                "success":   True,
                "image_b64": views_out[0]["image_b64"],   # backward compat
                "views":     views_out,
                "metadata": {
                    "render_views":   render_views,
                    "zoom":           zoom,
                    "image_width":    width,
                    "image_height":   height,
                    "object_count":   len(shape_info),
                    "shape_info":     shape_info,
                    "bounding_box":   bb_dict,
                    "touching_pairs": touching_pairs,
                    "joints":         joints,
                },
                "interference_report": None,
                "error": None,
            }
        else:
            # Single view (original behaviour)
            if custom_elev is not None and custom_azim is not None:
                elev_r = float(custom_elev)
                azim_r = float(custom_azim)
                resolved_view = f"custom(elev={elev_r}, azim={azim_r})"
            else:
                resolved_view = view_angle if view_angle in VIEW_PRESETS else DEFAULT_VIEW
                elev_r, azim_r = VIEW_PRESETS[resolved_view]

            png_bytes  = render(all_polydata, elev_r, azim_r, **common_kwargs)
            image_b64  = base64.b64encode(png_bytes).decode()

            result = {
                "success":   True,
                "image_b64": image_b64,
                "views": [{
                    "view":      resolved_view,
                    "elevation": elev_r,
                    "azimuth":   azim_r,
                    "image_b64": image_b64,
                }],
                "metadata": {
                    "view_angle":     resolved_view,
                    "elevation":      elev_r,
                    "azimuth":        azim_r,
                    "zoom":           zoom,
                    "image_width":    width,
                    "image_height":   height,
                    "object_count":   len(shape_info),
                    "shape_info":     shape_info,
                    "bounding_box":   bb_dict,
                    "touching_pairs": touching_pairs,
                    "joints":         joints,
                },
                "interference_report": None,
                "error": None,
            }

    except Exception as e:
        log.error("Renderer error: %s: %s", type(e).__name__, e)
        log.debug(traceback.format_exc())
        result = {
            "success":             False,
            "image_b64":           None,
            "views":               None,
            "metadata":            None,
            "interference_report": None,
            "error":               f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
        }
    finally:
        for dname in list(FreeCAD.listDocuments()):
            try:
                FreeCAD.closeDocument(dname)
            except Exception:
                pass

    elapsed = time.perf_counter() - t_total
    log.info("Renderer finished in %.3fs (success=%s)", elapsed, result.get("success"))
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
