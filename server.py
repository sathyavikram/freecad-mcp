"""
FreeCAD MCP Server (v3 — 4 focused tools)
==========================================
Tools:

  render_freecad_script    — standard rendering: views, camera, image size/background
  inspect_freecad_assembly — assembly inspection: explode, highlight, isolate, dimensions
  section_freecad_model    — diagnostic views: cross-section, wireframe/normals/curvature
  check_interference       — geometry analysis: Boolean overlap volume between part pairs

Transport: stdio (launched by Claude Desktop / VS Code Copilot / any MCP client).
"""

import os
import sys
import json
import time
import logging
import subprocess
from pathlib import Path
from typing import List, Union

from mcp.server import Server
from mcp.types import Tool, TextContent, ImageContent

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE        = Path(__file__).parent.resolve()
_RENDERER    = str(_HERE / "freecad_renderer.py")
_PYTHON      = str(_HERE / "venv" / "bin" / "python")
_FREECAD_LIB = "/Applications/FreeCAD.app/Contents/Resources/lib"

app = Server("freecad-mcp")

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("freecad-mcp")


# ═══════════════════════════════════════════════════════════════════════════════
# Subprocess helper
# ═══════════════════════════════════════════════════════════════════════════════

def _run_renderer(payload: dict, timeout: int = 300) -> dict:
    """Spawn freecad_renderer.py, send JSON on stdin, parse JSON from stdout."""
    log.info("Renderer | tool=%s script=%s", payload.get("_tool"), payload.get("script_path", "?"))
    payload.pop("_tool", None)   # internal tag — not forwarded to renderer

    t0 = time.perf_counter()
    env = os.environ.copy()
    env["DYLD_LIBRARY_PATH"] = _FREECAD_LIB
    for var in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT"):
        env.pop(var, None)

    proc = subprocess.run(
        [_PYTHON, _RENDERER],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    log.info("Renderer exit=%d elapsed=%.3fs", proc.returncode, time.perf_counter() - t0)

    if proc.stderr:
        for line in proc.stderr.decode(errors="replace").splitlines():
            if line.strip():
                log.debug("[renderer] %s", line)

    if proc.returncode != 0 and not proc.stdout:
        stderr = proc.stderr.decode(errors="replace")[:2000]
        return {
            "success": False, "image_b64": None, "views": None,
            "metadata": None, "interference_report": None,
            "error": f"Renderer failed (exit {proc.returncode}):\n{stderr}",
        }

    try:
        stdout_text = proc.stdout.decode(errors="replace")
        for line in reversed(stdout_text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        return json.loads(stdout_text)
    except json.JSONDecodeError as e:
        stderr = proc.stderr.decode(errors="replace")[:1000]
        return {
            "success": False, "image_b64": None, "views": None,
            "metadata": None, "interference_report": None,
            "error": f"Could not parse renderer output: {e}\nstderr: {stderr}",
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _validate_script(arguments: dict):
    """Return (Path, None) on success or (None, error_response) on failure."""
    script_path = arguments.get("script_path", "").strip()
    if not script_path:
        return None, [TextContent(type="text", text="Error: 'script_path' is required.")]
    path = Path(script_path).expanduser().resolve()
    if not path.exists():
        return None, [TextContent(type="text", text=f"Error: script not found: {path}")]
    if not path.is_file():
        return None, [TextContent(type="text", text=f"Error: not a file: {path}")]
    return path, None


def _format_metadata(meta: dict) -> str:
    """Build the text metadata block returned alongside images."""
    lines = []

    # View info
    if meta.get("render_views"):
        lines.append(f"**Views:** {', '.join(meta['render_views'])}")
    else:
        lines += [
            f"**View:** {meta.get('view_angle', '?')}",
            f"- Elevation: {meta.get('elevation', 0):.1f}°  Azimuth: {meta.get('azimuth', 0):.1f}°",
            f"- Zoom: {meta.get('zoom', 1.0)}",
        ]

    lines += [
        "",
        f"**Image:** {meta.get('image_width', '?')} × {meta.get('image_height', '?')} px  "
        f"({len(meta.get('views') or [])} view(s))",
        f"**Objects:** {meta.get('object_count', 0)}",
    ]

    # Bounding box
    bb = meta.get("bounding_box")
    if bb:
        mn, mx = bb["min"], bb["max"]
        dx, dy, dz = mx[0]-mn[0], mx[1]-mn[1], mx[2]-mn[2]
        lines.append(
            f"**Bounds:** W={dx:.2f}  D={dy:.2f}  H={dz:.2f} mm"
        )

    # Per-object summary
    if meta.get("shape_info"):
        lines.append("\n**Shapes**")
        for s in meta["shape_info"]:
            if "error" in s:
                lines.append(f"- {s['label']} — ERROR: {s['error']}")
                continue
            parts = [s["type"]]
            if s.get("volume") is not None:
                parts.append(f"vol={s['volume']:.2f} mm³")
            if s.get("face_count") is not None:
                parts.append(f"faces={s['face_count']}  edges={s['edge_count']}")
            if s.get("centroid"):
                c = s["centroid"]
                parts.append(f"centroid=({c[0]:.1f}, {c[1]:.1f}, {c[2]:.1f})")
            lines.append(f"- **{s['label']}**: {', '.join(parts)}")

            oc = s.get("orientation_check")
            if oc and "dimensions_mm" in oc:
                d = oc["dimensions_mm"]
                lines.append(
                    f"  ↳ print_face={oc.get('estimated_print_face')}  "
                    f"W={d['width']:.1f} D={d['depth']:.1f} H={d['height']:.1f} mm"
                )

    # Touching pairs
    if meta.get("touching_pairs"):
        lines.append("\n**Touching / Mated Pairs**")
        for tp in meta["touching_pairs"]:
            lines.append(f"- {tp['objects'][0]} ↔ {tp['objects'][1]}  ({tp['overlap_type']})")

    # Joints from DSL
    if meta.get("joints"):
        lines.append("\n**Declared Joints**")
        for j in meta["joints"]:
            if j["type"] == "joint":
                extras = {k: v for k, v in j.items() if k not in ("type", "from", "to")}
                lines.append(f"- {j['from']} → {j['to']}  {extras}")
            else:
                extras = {k: v for k, v in j.items() if k not in ("type", "body_a", "body_b", "constraint")}
                lines.append(f"- {j['body_a']} {j['constraint']} {j['body_b']}  {extras}")

    lines.append(f"\n```json\n{json.dumps(meta, indent=2)}\n```")
    return "\n".join(lines)


def _build_image_response(result: dict) -> List[Union[TextContent, ImageContent]]:
    """Turn a renderer result into MCP content blocks."""
    if not result.get("success"):
        return [TextContent(type="text", text=f"Render error:\n\n{result.get('error', 'Unknown')}")]

    meta    = result["metadata"]
    views   = result.get("views") or []
    content: List[Union[TextContent, ImageContent]] = [
        TextContent(type="text", text=_format_metadata(meta))
    ]
    if len(views) == 1:
        content.append(ImageContent(type="image", data=views[0]["image_b64"], mimeType="image/png"))
    else:
        for v in views:
            content.append(TextContent(type="text",
                text=f"**{v['view']}** — elev={v['elevation']:.1f}° azim={v['azimuth']:.1f}°"))
            content.append(ImageContent(type="image", data=v["image_b64"], mimeType="image/png"))
    return content


# ═══════════════════════════════════════════════════════════════════════════════
# Tool definitions
# ═══════════════════════════════════════════════════════════════════════════════

@app.list_tools()
async def list_tools() -> List[Tool]:

    VIEW_ENUM = ["Top", "Bottom", "Front", "Back", "Left", "Right", "Isometric"]

    return [

        # ── Tool 1 ────────────────────────────────────────────────────────────
        Tool(
            name="render_freecad_script",
            description=(
                "Execute a FreeCAD Python script and return rendered PNG image(s) with geometry "
                "metadata. Use this for standard rendering: choose a view angle, request multiple "
                "views in one call, set zoom and image size. "
                "Returns per-object metadata: centroid, volume, face count, colour, placement, "
                "touching pairs, and any declared joints."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "script_path": {
                        "type": "string",
                        "description": (
                            "Absolute or ~-relative path to a FreeCAD Python (.py) file. "
                            "FreeCAD, App, Part, Mesh, MeshPart, Draft, Sketcher, and doc "
                            "are pre-imported. __file__ is set to the script path."
                        ),
                    },
                    "render_views": {
                        "type": "array",
                        "items": {"type": "string", "enum": VIEW_ENUM},
                        "description": (
                            "Render multiple preset views in one call — returns a labelled PNG "
                            "per view. Example: [\"Isometric\",\"Front\",\"Top\",\"Right\"]. "
                            "When set, view_angle and elevation/azimuth are ignored."
                        ),
                    },
                    "view_angle": {
                        "type": "string",
                        "enum": VIEW_ENUM,
                        "description": "Single preset view (default: Isometric). Ignored when render_views is set.",
                    },
                    "elevation": {
                        "type": "number",
                        "description": "Custom camera elevation in degrees (-90 to 90). Use with azimuth.",
                    },
                    "azimuth": {
                        "type": "number",
                        "description": "Custom camera azimuth in degrees (0–360). Use with elevation.",
                    },
                    "zoom": {
                        "type": "number",
                        "description": "1.0 = fit-all (default). 2.0 = 2× closer. 0.5 = 2× farther.",
                    },
                    "width": {
                        "type": "integer",
                        "description": "Output image width in pixels (default: 1600).",
                    },
                    "height": {
                        "type": "integer",
                        "description": "Output image height in pixels (default: 1200).",
                    },
                    "background": {
                        "type": "string",
                        "description": "Background hex colour. '#0d1117' dark (default) or '#ffffff' white.",
                    },
                },
                "required": ["script_path"],
            },
        ),

        # ── Tool 2 ────────────────────────────────────────────────────────────
        Tool(
            name="inspect_freecad_assembly",
            description=(
                "Visually inspect a FreeCAD assembly or multi-body part. "
                "Use this to: explode parts apart to see what is inside, dim everything except "
                "a specific part (highlight), zoom in on a single part (focus), or draw "
                "W×D×H dimension labels on the image. "
                "Best used after render_freecad_script has identified which parts to investigate."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "script_path": {
                        "type": "string",
                        "description": "Absolute path to the FreeCAD Python script.",
                    },
                    "view_angle": {
                        "type": "string",
                        "enum": VIEW_ENUM,
                        "description": "Camera preset (default: Isometric).",
                    },
                    "zoom": {
                        "type": "number",
                        "description": "1.0 = fit-all (default). 2.0 = 2× closer.",
                    },
                    "explode_factor": {
                        "type": "number",
                        "description": (
                            "Displace parts outward from the assembly centroid. "
                            "0.0 = no explode (default). 1.0 = moderate. 2.0 = wide. "
                            "Geometry is not modified — render-only transform."
                        ),
                    },
                    "highlight_objects": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Render these objects at full opacity; all others dimmed to ~12% grey. "
                            "Use exact label values from shape_info. "
                            "Useful to isolate a joint interface or suspect part."
                        ),
                    },
                    "focus_object": {
                        "type": "string",
                        "description": (
                            "Render only this single object, zoomed to fit — all others excluded. "
                            "Use the exact label value from shape_info."
                        ),
                    },
                    "show_dimensions": {
                        "type": "boolean",
                        "description": (
                            "Draw W × D × H in mm at the image corner and per-object name labels "
                            "at their centroids. Requires Pillow (installed by setup.sh)."
                        ),
                    },
                },
                "required": ["script_path"],
            },
        ),

        # ── Tool 3 ────────────────────────────────────────────────────────────
        Tool(
            name="section_freecad_model",
            description=(
                "Render diagnostic views of a FreeCAD model: cross-section cuts to reveal "
                "internal features (bores, threads, snap tabs, wall thickness), wireframe for "
                "topology review, surface normal map for orientation checking, or Gaussian "
                "curvature heat-map to find thin spots. Also returns per-object orientation "
                "data (centre of mass, estimated print face) when requested."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "script_path": {
                        "type": "string",
                        "description": "Absolute path to the FreeCAD Python script.",
                    },
                    "view_angle": {
                        "type": "string",
                        "enum": VIEW_ENUM,
                        "description": "Camera preset (default: Isometric).",
                    },
                    "zoom": {
                        "type": "number",
                        "description": "1.0 = fit-all (default). 2.0 = 2× closer.",
                    },
                    "section_plane": {
                        "description": (
                            "Clip the model at a plane to reveal internal geometry. "
                            "String: 'XY' | 'XZ' | 'YZ'. "
                            "Or object: {\"normal\": [nx,ny,nz], \"origin\": [ox,oy,oz]}."
                        ),
                    },
                    "section_offset": {
                        "type": "number",
                        "description": (
                            "Cut position along the bounding box (0.0–1.0). "
                            "0.5 = midplane (default). 0.25 = first quarter."
                        ),
                    },
                    "render_mode": {
                        "type": "string",
                        "enum": ["shaded", "wireframe", "shaded+wireframe", "normals", "curvature"],
                        "description": (
                            "shaded — Phong + feature edges (default). "
                            "wireframe — all edges, no fill, topology review. "
                            "shaded+wireframe — fill with wireframe overlaid. "
                            "normals — face normals as RGB, orientation diagnostic. "
                            "curvature — Gaussian curvature heat-map (blue=flat, red=convex peak)."
                        ),
                    },
                    "orientation_check": {
                        "type": "boolean",
                        "description": (
                            "Add orientation data to each shape_info entry: "
                            "dimensions_mm, center_of_mass, inertia_matrix, "
                            "estimated_print_face, aspect_ratios."
                        ),
                    },
                },
                "required": ["script_path"],
            },
        ),

        # ── Tool 4 ────────────────────────────────────────────────────────────
        Tool(
            name="check_interference",
            description=(
                "Compute the Boolean intersection volume (mm³) between named object pairs in a "
                "FreeCAD assembly. No image is produced — pure geometry analysis. "
                "Use this to verify clearance joints have zero overlap, or to detect unintended "
                "interference between parts that should not touch. "
                "Find object labels from the shape_info returned by render_freecad_script."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "script_path": {
                        "type": "string",
                        "description": "Absolute path to the FreeCAD Python script defining the assembly.",
                    },
                    "pairs": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 2,
                            "maxItems": 2,
                        },
                        "description": (
                            "Object label pairs to check. Use exact label values from shape_info. "
                            "Example: [[\"Body\", \"Body001\"], [\"Body\", \"Body002\"]]"
                        ),
                    },
                },
                "required": ["script_path", "pairs"],
            },
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# Tool handler — routes to the right payload builder
# ═══════════════════════════════════════════════════════════════════════════════

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> List[Union[TextContent, ImageContent]]:
    log.info("Tool: %s", name)

    path, err = _validate_script(arguments)
    if err:
        return err

    if name == "render_freecad_script":
        return await _handle_render(path, arguments)
    elif name == "inspect_freecad_assembly":
        return await _handle_inspect(path, arguments)
    elif name == "section_freecad_model":
        return await _handle_section(path, arguments)
    elif name == "check_interference":
        return await _handle_interference(path, arguments)
    else:
        raise ValueError(f"Unknown tool: {name!r}")


# ═══════════════════════════════════════════════════════════════════════════════
# Handler 1: render_freecad_script
# ═══════════════════════════════════════════════════════════════════════════════

async def _handle_render(path: Path, a: dict) -> List[Union[TextContent, ImageContent]]:
    payload = {
        "_tool":        "render_freecad_script",
        "mode":         "render",
        "script_path":  str(path),
        "render_views": a.get("render_views"),
        "view_angle":   a.get("view_angle"),
        "elevation":    a.get("elevation"),
        "azimuth":      a.get("azimuth"),
        "zoom":         a.get("zoom", 1.0),
        "width":        a.get("width", 1600),
        "height":       a.get("height", 1200),
        "background":   a.get("background", "#0d1117"),
        # Defaults for params owned by other tools
        "explode_factor":    0.0,
        "highlight_objects": [],
        "focus_object":      None,
        "show_dimensions":   False,
        "render_mode":       "shaded",
        "section_plane":     None,
        "orientation_check": False,
    }
    return _build_image_response(_run_renderer(payload))


# ═══════════════════════════════════════════════════════════════════════════════
# Handler 2: inspect_freecad_assembly
# ═══════════════════════════════════════════════════════════════════════════════

async def _handle_inspect(path: Path, a: dict) -> List[Union[TextContent, ImageContent]]:
    payload = {
        "_tool":             "inspect_freecad_assembly",
        "mode":              "render",
        "script_path":       str(path),
        "view_angle":        a.get("view_angle", "Isometric"),
        "render_views":      None,
        "elevation":         None,
        "azimuth":           None,
        "zoom":              a.get("zoom", 1.0),
        "width":             1600,
        "height":            1200,
        "background":        "#0d1117",
        "explode_factor":    a.get("explode_factor", 0.0),
        "highlight_objects": a.get("highlight_objects") or [],
        "focus_object":      a.get("focus_object"),
        "show_dimensions":   a.get("show_dimensions", False),
        # Defaults for params owned by other tools
        "render_mode":       "shaded",
        "section_plane":     None,
        "orientation_check": False,
    }
    return _build_image_response(_run_renderer(payload))


# ═══════════════════════════════════════════════════════════════════════════════
# Handler 3: section_freecad_model
# ═══════════════════════════════════════════════════════════════════════════════

async def _handle_section(path: Path, a: dict) -> List[Union[TextContent, ImageContent]]:
    payload = {
        "_tool":             "section_freecad_model",
        "mode":              "render",
        "script_path":       str(path),
        "view_angle":        a.get("view_angle", "Isometric"),
        "render_views":      None,
        "elevation":         None,
        "azimuth":           None,
        "zoom":              a.get("zoom", 1.0),
        "width":             1600,
        "height":            1200,
        "background":        "#0d1117",
        "section_plane":     a.get("section_plane"),
        "section_offset":    a.get("section_offset", 0.5),
        "render_mode":       a.get("render_mode", "shaded"),
        "orientation_check": a.get("orientation_check", False),
        # Defaults for params owned by other tools
        "explode_factor":    0.0,
        "highlight_objects": [],
        "focus_object":      None,
        "show_dimensions":   False,
    }
    return _build_image_response(_run_renderer(payload))


# ═══════════════════════════════════════════════════════════════════════════════
# Handler 4: check_interference
# ═══════════════════════════════════════════════════════════════════════════════

async def _handle_interference(path: Path, a: dict) -> List[Union[TextContent, ImageContent]]:
    pairs = a.get("pairs") or []
    if not pairs:
        return [TextContent(type="text", text="Error: 'pairs' is required.")]

    payload = {
        "_tool":       "check_interference",
        "mode":        "check_interference",
        "script_path": str(path),
        "pairs":       pairs,
    }
    result = _run_renderer(payload)

    if not result.get("success"):
        return [TextContent(type="text", text=f"Error:\n\n{result.get('error', 'Unknown')}")]

    report = result.get("interference_report") or []
    meta   = result.get("metadata") or {}

    lines = [
        "## Interference Report\n",
        f"Script: `{path}`  —  Objects: {meta.get('object_count', '?')}\n",
        "| Pair | Overlap (mm³) | Clear? |",
        "|---|---|---|",
    ]
    all_clear = True
    for item in report:
        if "error" in item:
            lines.append(f"| {item['pair'][0]} ↔ {item['pair'][1]} | ERROR: {item['error']} | ❓ |")
            all_clear = False
        else:
            sym = "✅" if item["clear"] else "❌"
            lines.append(f"| {item['pair'][0]} ↔ {item['pair'][1]} | {item['overlap_volume_mm3']:.5f} | {sym} |")
            if not item["clear"]:
                all_clear = False

    lines.append(f"\n**{'✅ No interference detected' if all_clear else '❌ Interference found — see table above'}**")

    if meta.get("touching_pairs"):
        lines.append("\n### All touching / mated pairs")
        for tp in meta["touching_pairs"]:
            lines.append(f"- {tp['objects'][0]} ↔ {tp['objects'][1]}  ({tp['overlap_type']})")

    lines.append(f"\n```json\n{json.dumps(report, indent=2)}\n```")
    return [TextContent(type="text", text="\n".join(lines))]


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio

    async def _run_stdio():
        from mcp.server.stdio import stdio_server
        log.info("FreeCAD MCP server v3 starting (stdio transport) — 4 tools")
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())

    asyncio.run(_run_stdio())
