# freecad-mcp

A Model Context Protocol (MCP) server that integrates with **FreeCAD** to execute Python
scripts, render 3D geometry headlessly, and return images with geometry metadata —
all without opening a GUI. Designed for LLMs and agents to visually inspect CAD parts,
joints, and assemblies.

---

## Requirements

| Requirement | Details |
|---|---|
| macOS | Tested on macOS 13+ |
| [FreeCAD ≥ 1.0](https://www.freecad.org/downloads.php) | Must be installed at `/Applications/FreeCAD.app` |
| Python | Provided by FreeCAD (3.11) — no separate install needed |
| VTK | Bundled inside FreeCAD.app — no separate install needed |
| Pillow | Installed by `setup.sh` — required for `show_dimensions` |

---

## Setup

```bash
cd /path/to/free-cad-mcp
bash setup.sh
```

---

## Connecting to an MCP client

### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "freecad": {
      "command": "/bin/bash",
      "args": [
        "-c",
        "DYLD_LIBRARY_PATH=/Applications/FreeCAD.app/Contents/Resources/lib /path/to/free-cad-mcp/venv/bin/python /path/to/free-cad-mcp/server.py"
      ]
    }
  }
}
```

### VS Code GitHub Copilot

`.vscode/mcp.json`:

```json
{
  "mcpServers": {
    "freecad": {
      "command": "/bin/bash",
      "args": [
        "-c",
        "DYLD_LIBRARY_PATH=/Applications/FreeCAD.app/Contents/Resources/lib /path/to/free-cad-mcp/venv/bin/python /path/to/free-cad-mcp/server.py"
      ]
    }
  }
}
```

---

## Tools

### `render_freecad_script`

Execute a FreeCAD Python script and return rendered PNG image(s) with geometry metadata.
Use this as the **starting point** for any inspection — it gives you views of the model
plus the object labels and metadata needed to call the other tools.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `script_path` | string | **required** | Absolute or `~`-relative path to a FreeCAD `.py` file. `FreeCAD`, `App`, `Part`, `Mesh`, `MeshPart`, `Draft`, `Sketcher`, and `doc` are pre-imported. |
| `render_views` | string[] | — | Render multiple views in one call. Returns a labelled PNG per view. Example: `["Isometric","Front","Top","Right"]`. When set, `view_angle` and `elevation`/`azimuth` are ignored. |
| `view_angle` | string | `Isometric` | Single preset: `Top` `Bottom` `Front` `Back` `Left` `Right` `Isometric`. Ignored when `render_views` is set. |
| `elevation` | number | — | Custom camera elevation in degrees (-90 to 90). Use with `azimuth`. |
| `azimuth` | number | — | Custom camera azimuth in degrees (0–360). Use with `elevation`. |
| `zoom` | number | `1.0` | `1.0` = fit-all. `2.0` = 2× closer. `0.5` = 2× farther. |
| `width` | integer | `1600` | Output image width in pixels. |
| `height` | integer | `1200` | Output image height in pixels. |
| `background` | string | `#0d1117` | Background hex colour. `"#ffffff"` for white. |

**Response** — text metadata + one `image/png` per view:

| Metadata field | Description |
|---|---|
| `shape_info[].label` | Object label — use this in the other tools |
| `shape_info[].centroid` | `[x, y, z]` centre in mm |
| `shape_info[].volume` | Volume in mm³ |
| `shape_info[].face_count` / `edge_count` | Topology |
| `shape_info[].is_solid` | `true` = Solid body |
| `shape_info[].color_rgb` | `[r, g, b]` 0–1 |
| `shape_info[].placement` | `{position, rotation_axis, rotation_angle_deg}` |
| `touching_pairs` | Auto-detected mated/adjacent object pairs |
| `joints` | Joints parsed from `# @joint` script comments |
| `bounding_box` | Overall scene bounds in mm |

---

### `inspect_freecad_assembly`

Visually inspect a multi-body assembly. Use after `render_freecad_script` has identified
which parts to investigate.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `script_path` | string | **required** | Absolute path to the FreeCAD script. |
| `view_angle` | string | `Isometric` | Camera preset. |
| `zoom` | number | `1.0` | Zoom factor. |
| `explode_factor` | number | `0.0` | Displace parts outward from the assembly centroid. `0` = normal. `1.0` = moderate. `2.0` = wide. Geometry is not modified — render-only. |
| `highlight_objects` | string[] | — | Render these objects at full opacity; all others dimmed to ~12% grey. Use exact `label` values from `shape_info`. |
| `focus_object` | string | — | Render only this object, zoomed to fit. All others excluded. Use exact `label` from `shape_info`. |
| `show_dimensions` | boolean | `false` | Draw W × D × H in mm at the corner and per-object name labels at centroids. Requires Pillow. |

---

### `section_freecad_model`

Diagnostic views: cross-section cuts, wireframe, surface normal map, curvature heat-map,
and per-object orientation analysis.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `script_path` | string | **required** | Absolute path to the FreeCAD script. |
| `view_angle` | string | `Isometric` | Camera preset. |
| `zoom` | number | `1.0` | Zoom factor. |
| `section_plane` | string or object | — | Clip the model to reveal internal geometry. `"XY"` \| `"XZ"` \| `"YZ"` or `{"normal":[nx,ny,nz],"origin":[ox,oy,oz]}`. |
| `section_offset` | number | `0.5` | Cut position along the bounding box (0.0–1.0). `0.5` = midplane. |
| `render_mode` | string | `shaded` | `shaded` — Phong + feature edges. `wireframe` — all edges, no fill. `shaded+wireframe` — fill + wireframe overlay. `normals` — face normals as RGB. `curvature` — Gaussian curvature heat-map (blue=flat → red=convex peak). |
| `orientation_check` | boolean | `false` | Add to each `shape_info` entry: `dimensions_mm`, `center_of_mass`, `inertia_matrix`, `estimated_print_face`, `aspect_ratios`. |

---

### `check_interference`

Compute the Boolean intersection volume (mm³) between named object pairs.
No image produced — pure geometry analysis via `Part.common()`.
Get object labels from `shape_info` returned by `render_freecad_script`.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `script_path` | string | ✅ | Absolute path to the FreeCAD script. |
| `pairs` | `[[str, str]]` | ✅ | Object label pairs to check. Example: `[["Body","Body001"],["Body","Body002"]]` |

**Response** — markdown table:

| Pair | Overlap (mm³) | Clear? |
|---|---|---|
| Body ↔ Body001 | 0.00000 | ✅ |
| Body ↔ Body002 | 14.73200 | ❌ |

`overlap_volume_mm3 > 0.001` on a clearance joint = design error.

---

## Typical agent workflow

```
1. render_freecad_script   render_views=["Isometric","Front","Top","Right"]
        → inspect overall shape, read shape_info labels, touching_pairs, joints

2. inspect_freecad_assembly  explode_factor=1.2
        → confirm all parts are present and correctly shaped when separated

3. inspect_freecad_assembly  highlight_objects=["BodyA","BodyB"]
        → focus on each joint interface identified in touching_pairs

4. check_interference  pairs=[["BodyA","BodyB"],...]
        → verify overlap_volume_mm3 = 0 for clearance joints

5. section_freecad_model  section_plane="XZ"  section_offset=0.5
        → inspect internal features: bores, threads, wall thickness

6. inspect_freecad_assembly  focus_object="BodyA"  show_dimensions=true
        → close-up of suspect part with dimension labels
```

---

## Joint annotation DSL

Declare joints in FreeCAD script comments — automatically parsed into `metadata.joints`:

```python
# @joint BodyA.Face3 -> BodyB.Face1  type=sliding  clearance=0.2mm
# @joint BodyA.Face5 -> BodyC.Face1  type=press
# @constraint BodyA  coaxial  BodyB  axis=Z
```

Cross-check: every `metadata.joints` entry should appear in `metadata.touching_pairs`.
A mismatch means a declared joint is not geometrically mated.

---

## Project structure

```
free-cad-mcp/
├── server.py               # MCP server — 4 tools
├── freecad_renderer.py     # Subprocess renderer: FreeCAD → VTK → PNG
├── requirements.txt        # mcp + Pillow
├── setup.sh                # One-time venv setup
├── mcp_config.example.json # Example MCP client config
└── .gitignore
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'FreeCAD'` | Set `DYLD_LIBRARY_PATH` in the client config command |
| `Error: Failed to open library "3DconnexionNavlib"` | Harmless warning — rendering still works |
| `Renderer process failed (exit 1)` | Verify FreeCAD is at `/Applications/FreeCAD.app` |
| No geometry rendered | Ensure the script calls `Part.show()` or `doc.addObject()` |
| `show_dimensions` has no effect | Re-run `setup.sh` to install Pillow in the venv |
| `check_interference` returns object not found | Use `label` values from `shape_info`, not `name` |
