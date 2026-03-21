# freecad-mcp

A Model Context Protocol (MCP) server that integrates with **FreeCAD** to execute Python scripts, render 3D geometry headlessly, and return images with view metadata — all without opening a GUI.

The server communicates over **Streamable HTTP**, making it compatible with any MCP client that supports the streamable HTTP transport.

## Requirements

| Requirement | Details |
|---|---|
| macOS | Tested on macOS 13+ |
| [FreeCAD ≥ 1.0](https://www.freecad.org/downloads.php) | Must be installed at `/Applications/FreeCAD.app` |
| Python | Provided by FreeCAD (3.11) — no separate install needed |

---

## Setup

```bash
# 1. Clone or open the project
cd /path/to/free-cad-mcp

# 2. Run the one-time setup script (creates venv + installs dependencies)
bash setup.sh
```

`setup.sh` creates a Python virtual environment rooted in FreeCAD's bundled Python (so `FreeCAD`, `Part`, `Mesh`, `matplotlib`, `numpy`, etc. are all available), installs pip dependencies (`mcp`, `uvicorn`).

---

## Running the MCP Server

```bash
DYLD_LIBRARY_PATH=/Applications/FreeCAD.app/Contents/Resources/lib \
  ./venv/bin/python server.py
```

By default it listens on **`http://127.0.0.1:8000`**.

```
MCP endpoint: http://127.0.0.1:8000/mcp
```

Options:
```bash
bash setup.sh --host 0.0.0.0   # bind to all interfaces
bash setup.sh --port 9000      # custom port
```

To start the server directly after setup:
```bash
DYLD_LIBRARY_PATH=/Applications/FreeCAD.app/Contents/Resources/lib \
  ./venv/bin/python server.py --host 127.0.0.1 --port 8000
```

### Connect to Claude Desktop

Add this block to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "freecad": {
      "type": "streamable-http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

The server must be running before Claude Desktop connects. Start it with the command above.

A template is provided:
```bash
cp mcp_config.example.json mcp_config.json
```
`mcp_config.json` is gitignored.

### Connect to VS Code GitHub Copilot

Add to `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "freecad": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

---

## Tool: `execute_freecad_script`

Execute a FreeCAD Python script from a file path and get back a rendered PNG image + geometry metadata.

### Input parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `script_path` | string | ✅ | Absolute or `~`-relative path to a `.py` file. `FreeCAD`, `App`, `Part`, `Mesh`, `MeshPart`, `Draft`, and `doc` are pre-imported. |
| `view_angle` | string | | Preset view: `Top` `Bottom` `Front` `Back` `Left` `Right` `Isometric` (default: `Isometric`) |
| `elevation` | number | | Custom camera elevation in degrees. Use with `azimuth` to override `view_angle`. |
| `azimuth` | number | | Custom camera azimuth in degrees. Use with `elevation` to override `view_angle`. |
| `zoom` | number | | Zoom factor — `1.0` fits all geometry, `2.0` is 2× closer, `0.5` is 2× farther. |
| `width` | integer | | Output image width in pixels (default: `800`) |
| `height` | integer | | Output image height in pixels (default: `600`) |
| `background` | string | | Background hex colour, e.g. `"#ffffff"` for white or `"#0d1117"` for dark (default) |

### Output

- **Text**: formatted summary with view angle, elevation, azimuth, zoom, bounding box, per-object type/volume, and full JSON metadata
- **Image**: rendered PNG of the geometry

### Example

Given a file `my_part.py`:
```python
import Part

box = Part.makeBox(30, 20, 10)
hole = Part.makeCylinder(5, 10, App.Vector(15, 10, 0))
Part.show(box.cut(hole))
```

Tool call:
```json
{
  "script_path": "/path/to/my_part.py",
  "view_angle": "Isometric",
  "zoom": 1.2
}
```

---

## Project Structure

```
free-cad-mcp/
├── server.py                 # MCP server (Streamable HTTP transport via uvicorn)
├── freecad_renderer.py       # Subprocess renderer: FreeCAD -> matplotlib -> PNG
├── requirements.txt          # pip dependencies (mcp, uvicorn)
├── setup.sh                  # One-time setup + server start script
├── mcp_config.example.json   # SSE config template for Claude Desktop / VS Code
├── .gitignore
└── tests/
    ├── test_client.py            # General test (box+cylinder, sphere+torus, custom view)
    ├── test_spool_flange.py      # Spool flange part test - 4 rendered views
    ├── part1_spool_flange.py     # Spool flange FreeCAD geometry script
    ├── box_cylinder.py           # Box fused with cylinder geometry script
    ├── sphere_torus.py           # Sphere and torus geometry script
    └── output/                   # Rendered PNG images (gitignored)
```

## How It Works

```
MCP Client (Claude / Copilot)
        |  Streamable HTTP (JSON-RPC)
        v
    server.py  (uvicorn + mcp StreamableHTTPSessionManager)
        |  subprocess (JSON stdin -> JSON stdout)
        v
freecad_renderer.py
   +-- sys.path <- FreeCAD libs (DYLD_LIBRARY_PATH + FreeCAD site-packages)
   +-- exec(script) in FreeCAD document
   +-- MeshPart.meshFromShape -> triangle arrays (numpy)
   +-- matplotlib Agg backend -> PNG bytes -> base64
```

The renderer runs in an **isolated subprocess** per request — a crash or timeout in FreeCAD cannot bring down the MCP server.

---

## Running the Tests

Each test script starts its own server instance on a dedicated port, runs the renders, then shuts the server down. No manual server start needed.

```bash
# General test (box+cylinder, sphere+torus, custom view)
./venv/bin/python tests/test_client.py

# Spool flange test - renders 4 views and saves PNGs to tests/output/
./venv/bin/python tests/test_spool_flange.py
```

Rendered images are saved to `tests/output/` (gitignored).

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'FreeCAD'` | Make sure `DYLD_LIBRARY_PATH` is set when running the server |
| `Error: Failed to open library "3DconnexionNavlib"` | Harmless warning — 3Dconnexion mouse driver not installed; rendering still works |
| `Renderer process failed (exit 1)` | Check that FreeCAD is installed at `/Applications/FreeCAD.app` |
| Script runs but no geometry appears | Ensure script calls `Part.show(shape)` or `doc.addObject(...)` to add objects to the document |
