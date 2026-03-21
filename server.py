"""
FreeCAD MCP Server
==================
Exposes FreeCAD scripting + headless rendering as an MCP tool.

The tool `execute_freecad_script` accepts a path to a FreeCAD Python script plus
optional view/zoom parameters, executes the script in an isolated subprocess, and
returns a rendered PNG image alongside view/geometry metadata.

Rendering is headless (no display needed) using matplotlib + FreeCAD's
MeshPart tessellation.
"""

import os
import sys
import json
import base64
import time
import logging
import argparse
import subprocess
from pathlib import Path
from typing import List, Union

from mcp.server import Server
from mcp.types import Tool, TextContent, ImageContent

# ── Paths ──────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent.resolve()
_RENDERER = str(_HERE / "freecad_renderer.py")
_PYTHON = str(_HERE / "venv" / "bin" / "python")
_FREECAD_LIB = "/Applications/FreeCAD.app/Contents/Resources/lib"

app = Server("freecad-mcp")

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("freecad-mcp")

# ── Subprocess helper ──────────────────────────────────────────────────────────

def _run_renderer(payload: dict, timeout: int = 120) -> dict:
    """Spawn freecad_renderer.py, send payload as JSON on stdin, parse stdout."""
    script = payload.get("script_path", "?")
    log.info("Spawning renderer subprocess | script=%s view=%s zoom=%s size=%dx%d",
             script,
             payload.get("view_angle"),
             payload.get("zoom"),
             payload.get("width", 800),
             payload.get("height", 600))
    t0 = time.perf_counter()
    env = os.environ.copy()
    env["DYLD_LIBRARY_PATH"] = _FREECAD_LIB
    # Remove Python env vars that VS Code may inject; they conflict with
    # FreeCAD's bundled Python libs loaded inside the renderer subprocess.
    for _var in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV",
                 "VIRTUAL_ENV_PROMPT"):
        env.pop(_var, None)

    proc = subprocess.run(
        [_PYTHON, _RENDERER],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    elapsed = time.perf_counter() - t0
    log.info("Renderer subprocess finished | exit=%d elapsed=%.3fs",
             proc.returncode, elapsed)

    # Forward renderer's stderr to our log (DEBUG level so it's visible but not intrusive)
    if proc.stderr:
        for line in proc.stderr.decode(errors="replace").splitlines():
            if line.strip():
                log.debug("[renderer] %s", line)

    if proc.returncode != 0 and not proc.stdout:
        stderr = proc.stderr.decode(errors="replace")[:2000]
        log.error("Renderer process failed (exit %d)", proc.returncode)
        return {"success": False, "image_b64": None, "metadata": None,
                "error": f"Renderer process failed (exit {proc.returncode}):\n{stderr}"}

    try:
        # The script may print text to stdout before the renderer writes its JSON.
        # Find the last line that looks like a JSON object.
        stdout_text = proc.stdout.decode(errors="replace")
        log.debug("Renderer stdout: %d bytes", len(stdout_text))
        for line in reversed(stdout_text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                parsed = json.loads(line)
                if parsed.get("success"):
                    meta = parsed.get("metadata") or {}
                    log.info("Renderer result: SUCCESS | objects=%d view=%s",
                             meta.get("object_count", "?"), meta.get("view_angle", "?"))
                else:
                    log.warning("Renderer result: FAILURE | error=%s",
                                str(parsed.get("error", ""))[:200])
                return parsed
        log.debug("No JSON line found in stdout, trying full text")
        return json.loads(stdout_text)  # fallback: try the whole thing
    except json.JSONDecodeError as e:
        stderr = proc.stderr.decode(errors="replace")[:1000]
        log.error("Failed to parse renderer output: %s", e)
        return {"success": False, "image_b64": None, "metadata": None,
                "error": f"Could not parse renderer output: {e}\nstderr: {stderr}"}


# ── Tool definitions ───────────────────────────────────────────────────────────

@app.list_tools()
async def list_tools() -> List[Tool]:
    log.debug("list_tools called")
    return [
        Tool(
            name="execute_freecad_script",
            description=(
                "Execute a FreeCAD Python script, render the resulting 3-D geometry "
                "headlessly, and return a PNG image together with view/geometry metadata. "
                "Use this to create, inspect, or modify 3D models through FreeCAD's Python API."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "script_path": {
                        "type": "string",
                        "description": (
                            "Absolute or ~ -relative path to a FreeCAD Python (.py) file to "
                            "execute. The script runs with `FreeCAD`, `App`, `Part`, `Mesh`, "
                            "`MeshPart`, `Draft`, and `doc` pre-imported. "
                            "Add objects to the document via Part.show(), doc.addObject(), etc."
                        ),
                    },
                    "view_angle": {
                        "type": "string",
                        "enum": ["Top", "Bottom", "Front", "Back", "Left", "Right", "Isometric"],
                        "description": (
                            "Preset camera orientation. Defaults to 'Isometric'. "
                            "Ignored when both elevation and azimuth are supplied."
                        ),
                    },
                    "elevation": {
                        "type": "number",
                        "description": (
                            "Custom camera elevation in degrees (-90 to 90). "
                            "Use together with azimuth to set an arbitrary view direction."
                        ),
                    },
                    "azimuth": {
                        "type": "number",
                        "description": (
                            "Custom camera azimuth in degrees (0–360). "
                            "Use together with elevation to set an arbitrary view direction."
                        ),
                    },
                    "zoom": {
                        "type": "number",
                        "description": (
                            "Zoom factor relative to 'fit all'. "
                            "1.0 = fit geometry to frame, 2.0 = 2x closer, 0.5 = 2x farther out."
                        ),
                    },
                    "width": {
                        "type": "integer",
                        "description": "Output image width in pixels. Default: 800.",
                    },
                    "height": {
                        "type": "integer",
                        "description": "Output image height in pixels. Default: 600.",
                    },
                    "background": {
                        "type": "string",
                        "description": (
                            "Background colour as a hex string, e.g. '#ffffff' for white "
                            "or '#0d1117' for dark (default)."
                        ),
                    },
                },
                "required": ["script_path"],
            },
        )
    ]


# ── Tool handler ───────────────────────────────────────────────────────────────

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> List[Union[TextContent, ImageContent]]:
    if name != "execute_freecad_script":
        log.warning("Unknown tool requested: %r", name)
        raise ValueError(f"Unknown tool: {name!r}")

    log_args = {k: v for k, v in arguments.items() if v is not None and v != ""}
    log.info("Tool call: %s  args=%s", name, log_args)

    script_path = arguments.get("script_path", "").strip()
    if not script_path:
        log.warning("Tool call rejected: missing script_path")
        return [TextContent(type="text", text="Error: 'script_path' is required.")]

    path = Path(script_path).expanduser().resolve()
    log.debug("Resolved script path: %s", path)
    if not path.exists():
        log.warning("Script not found: %s", path)
        return [TextContent(type="text", text=f"Error: script_path not found: {path}")]
    if not path.is_file():
        log.warning("Script path is not a file: %s", path)
        return [TextContent(type="text", text=f"Error: script_path is not a file: {path}")]

    payload = {
        "script_path": str(path),
        "view_angle": arguments.get("view_angle"),
        "elevation":  arguments.get("elevation"),
        "azimuth":    arguments.get("azimuth"),
        "zoom":       arguments.get("zoom", 1.0),
        "width":      arguments.get("width", 800),
        "height":     arguments.get("height", 600),
        "background": arguments.get("background", "#0d1117"),
    }

    result = _run_renderer(payload)

    if not result.get("success"):
        error_msg = result.get("error", "Unknown error in renderer.")
        log.error("execute_freecad_script failed: %s", error_msg[:300])
        return [TextContent(type="text", text=f"FreeCAD script error:\n\n{error_msg}")]

    meta = result["metadata"]
    log.info("execute_freecad_script succeeded | objects=%d view=%s elev=%.1f azim=%.1f",
             meta.get("object_count", 0),
             meta.get("view_angle", "?"),
             meta.get("elevation", 0),
             meta.get("azimuth", 0))
    meta_text = (
        f"Script executed successfully.\n\n"
        f"**View**\n"
        f"- Preset / angle: {meta['view_angle']}\n"
        f"- Elevation: {meta['elevation']}°\n"
        f"- Azimuth:   {meta['azimuth']}°\n"
        f"- Zoom:      {meta['zoom']}\n\n"
        f"**Image**\n"
        f"- Size: {meta['image_width']} × {meta['image_height']} px\n\n"
        f"**Geometry**\n"
        f"- Objects: {meta['object_count']}\n"
    )
    if meta.get("bounding_box"):
        bb = meta["bounding_box"]
        meta_text += (
            f"- Bounding box:\n"
            f"  Min: ({bb['min'][0]:.3f}, {bb['min'][1]:.3f}, {bb['min'][2]:.3f})\n"
            f"  Max: ({bb['max'][0]:.3f}, {bb['max'][1]:.3f}, {bb['max'][2]:.3f})\n"
        )
    if meta.get("shape_info"):
        meta_text += "\n**Shapes**\n"
        for s in meta["shape_info"]:
            if "error" in s:
                meta_text += f"- {s['label']} ({s['name']}): ERROR — {s['error']}\n"
            else:
                vol = f", volume={s['volume']:.3f}" if s.get("volume") is not None else ""
                meta_text += f"- {s['label']} ({s['name']}): {s['type']}{vol}\n"
    meta_text += f"\n```json\n{json.dumps(meta, indent=2)}\n```"

    return [
        TextContent(type="text", text=meta_text),
        ImageContent(
            type="image",
            data=result["image_b64"],
            mimeType="image/png",
        ),
    ]


# ── Entry point ────────────────────────────────────────────────────────────────

async def _run_http(host: str, port: int):
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

    session_manager = StreamableHTTPSessionManager(app=app, stateless=True)

    async def asgi_app(scope, receive, send):
        if scope["type"] == "lifespan":
            await receive()  # lifespan.startup
            async with session_manager.run():
                await send({"type": "lifespan.startup.complete"})
                await receive()  # lifespan.shutdown
            await send({"type": "lifespan.shutdown.complete"})
        elif scope["type"] == "http":
            path = scope.get("path", "")
            method = scope.get("method", "?")
            log.debug("HTTP %s %s", method, path)
            if path == "/mcp":
                await session_manager.handle_request(scope, receive, send)
            else:
                await send({"type": "http.response.start", "status": 404,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body",
                            "body": b"Not found. Use POST /mcp"})

    log.info("FreeCAD MCP server (Streamable HTTP) listening on http://%s:%d", host, port)
    log.info("  MCP endpoint: http://%s:%d/mcp", host, port)

    config = uvicorn.Config(
        asgi_app,
        host=host,
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    import asyncio

    parser = argparse.ArgumentParser(description="FreeCAD MCP Server (Streamable HTTP)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    args = parser.parse_args()

    asyncio.run(_run_http(args.host, args.port))
