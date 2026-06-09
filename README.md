# freecad-mcp

A Model Context Protocol (MCP) server that integrates with **FreeCAD** to execute Python scripts, render 3D geometry headlessly, and return images with view metadata — all without opening a GUI.

The server uses **stdio** transport and is launched directly by MCP clients (Claude Desktop, VS Code GitHub Copilot, etc.).

## Requirements

| Requirement | Details |
|---|---|
| macOS | Tested on macOS 13+ |
| [FreeCAD ≥ 1.0](https://www.freecad.org/downloads.php) | Must be installed at `/Applications/FreeCAD.app` |
| Python | Provided by FreeCAD (3.11) — no separate install needed |
| VTK | Bundled inside FreeCAD.app — no separate install needed |

---

## Setup

```bash
# 1. Clone or open the project
cd /path/to/free-cad-mcp

# 2. Run the one-time setup script (creates venv + installs dependencies)
bash setup.sh
```

`setup.sh` creates a Python virtual environment rooted in FreeCAD's bundled Python (so `FreeCAD`, `Part`, `Mesh`, `vtk`, `numpy`, etc. are all available) and installs the `mcp` pip dependency.

---

## Running the MCP Server

The server uses **stdio** transport and is launched on-demand by your MCP client — you do not need to start it manually.

**Important:** FreeCAD requires the `DYLD_LIBRARY_PATH` environment variable to be set so the server can find its dynamic libraries.

### Connect to Claude Desktop

Add this block to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "freecad": {
      "command": "/bin/bash",
      "args": [
        "-c",
        "DYLD_LIBRARY_PATH=/Applications/FreeCAD.app/Contents/Resources/lib /Users/intelligentmachine/Documents/workspace/free-cad-mcp/venv/bin/python /Users/intelligentmachine/Documents/workspace/free-cad-mcp/server.py"
      ]
    }
  }
}
```

### Connect to VS Code GitHub Copilot

Add to `.vscode/mcp.json` in your workspace:

```json
{
  "mcpServers": {
    "freecad": {
      "command": "/bin/bash",
      "args": [
        "-c",
        "DYLD_LIBRARY_PATH=/Applications/FreeCAD.app/Contents/Resources/lib /Users/intelligentmachine/Documents/workspace/free-cad-mcp/venv/bin/python /Users/intelligentmachine/Documents/workspace/free-cad-mcp/server.py"
      ]
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
| `script_path` | string | ✅ | Absolute or `~`-relative path to a `.py` file. `FreeCAD`, `App`, `Part`, `Mesh`, `MeshPart`, `Draft`, `Sketcher`, and `doc` are pre-imported. `__file__` is set to the script path for relative imports. |
| `view_angle` | string | | Preset view: `Top` `Bottom` `Front` `Back` `Left` `Right` `Isometric` (default: `Isometric`) |
| `elevation` | number | | Custom camera elevation in degrees (-90 to 90). Use with `azimuth` to override `view_angle`. |
| `azimuth` | number | | Custom camera azimuth in degrees (0–360). Use with `elevation` to override `view_angle`. |
| `zoom` | number | | Zoom factor — default `1.0` fits all geometry, `2.0` is 2× closer, `0.5` is 2× farther. |
| `width` | integer | | Output image width in pixels (default: `1600`) |
| `height` | integer | | Output image height in pixels (default: `1200`) |
| `background` | string | | Background hex colour, e.g. `"#ffffff"` for white or `"#0d1117"` for dark (default) |

### Output

- **Text**: formatted summary with view angle, elevation, azimuth, zoom, bounding box, per-object type/volume, and full JSON metadata
- **Image**: rendered PNG of the geometry (1600×1200 by default, high-resolution)

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
├── server.py                 # MCP server (stdio transport)
├── freecad_renderer.py       # Subprocess renderer: FreeCAD → VTK → PNG
├── requirements.txt          # pip dependencies (mcp)
├── setup.sh                  # One-time setup script
├── mcp_config.example.json   # Example MCP client config
└── .gitignore
```

## How It Works

```
MCP Client (Claude / Copilot)
        |  stdio (JSON-RPC)
        v
    server.py  (asyncio + mcp stdio_server)
        |  subprocess (JSON stdin → JSON stdout)
        v
freecad_renderer.py
   +-- sys.path <- FreeCAD libs (DYLD_LIBRARY_PATH + FreeCAD site-packages)
   +-- exec(script) in isolated FreeCAD document
   +-- MeshPart.meshFromShape → vtkPolyData (tessellation)
   +-- VTK offscreen renderer (Phong shading, edge highlighting, 3-point lighting)
   +-- vtkPNGWriter → PNG bytes → base64
```

The renderer runs in an **isolated subprocess** per request — a crash or timeout in FreeCAD cannot bring down the MCP server.

### Rendering pipeline details

- **VTK offscreen rendering** — no display server required; uses `vtkRenderWindow.SetOffScreenRendering(1)`
- **High-quality tessellation** — `MeshPart.meshFromShape` with `LinearDeflection=0.02` / `AngularDeflection=0.05` for smooth curves
- **Phong shading** — ambient (0.18) + diffuse (0.72) + specular (0.55) per object
- **Feature edge overlay** — crisp boundary lines at edges sharper than 25°
- **3-point lighting** — warm key light, cool fill light, rim/back light for silhouette separation
- **Per-object colour palette** — up to 6 distinct colours cycling for multi-body models

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'FreeCAD'` | Make sure `DYLD_LIBRARY_PATH` is set in the client config command |
| `Error: Failed to open library "3DconnexionNavlib"` | Harmless warning — 3Dconnexion mouse driver not installed; rendering still works |
| `Renderer process failed (exit 1)` | Check that FreeCAD is installed at `/Applications/FreeCAD.app` |
| Script runs but no geometry appears | Ensure script calls `Part.show(shape)` or `doc.addObject(...)` to add objects to the document |
| `ModuleNotFoundError: No module named 'vtk'` | VTK is bundled with FreeCAD 1.0+ — verify your FreeCAD version |
