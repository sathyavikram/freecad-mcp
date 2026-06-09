#!/usr/bin/env python3
"""
Test harness for freecad-mcp v3 — exercises all 4 tool payloads against
a real FreeCAD part script, prints pass/fail for each, and saves all
rendered PNG images into the media/ folder for review.

Usage:
    python3 test_tools.py
"""

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE        = Path(__file__).parent.resolve()
RENDERER    = str(HERE / "freecad_renderer.py")
PYTHON      = str(HERE / "venv" / "bin" / "python")
FREECAD_LIB = "/Applications/FreeCAD.app/Contents/Resources/lib"
SCRIPT      = "/Users/intelligentmachine/Documents/workspace/3d-models/clothes-drying-rack/part_01_leg_segment.py"
MEDIA_DIR   = HERE / "media"

PASS = "✅ PASS"
FAIL = "❌ FAIL"

MEDIA_DIR.mkdir(exist_ok=True)


def save_images(test_slug: str, result: dict):
    """Save every rendered view from a result dict to media/<test_slug>_<view>.png"""
    saved = []
    for v in result.get("views") or []:
        b64 = v.get("image_b64")
        if not b64:
            continue
        # Sanitise the view name for use in a filename
        view_name = v.get("view", "view").replace("(", "").replace(")", "").replace(",", "").replace(" ", "_").replace("=", "")
        filename = MEDIA_DIR / f"{test_slug}__{view_name}.png"
        filename.write_bytes(base64.b64decode(b64))
        saved.append(str(filename))
        print(f"  💾  Saved: media/{filename.name}  ({len(b64)//1024} KB b64)")
    return saved


def run(payload: dict, timeout: int = 180) -> dict:
    env = os.environ.copy()
    env["DYLD_LIBRARY_PATH"] = FREECAD_LIB
    for var in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT"):
        env.pop(var, None)
    t0 = time.perf_counter()
    proc = subprocess.run(
        [PYTHON, RENDERER],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=env,
        timeout=timeout,
    )
    elapsed = time.perf_counter() - t0
    stdout = proc.stdout.decode(errors="replace")
    stderr = proc.stderr.decode(errors="replace")
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            result = json.loads(line)
            result["_elapsed"] = round(elapsed, 2)
            result["_exit"]    = proc.returncode
            return result
    return {
        "success": False,
        "error": f"No JSON in stdout. exit={proc.returncode}\nstderr={stderr[:500]}",
        "_elapsed": round(elapsed, 2),
        "_exit": proc.returncode,
    }


def check(label: str, result: dict, extra_checks=None):
    ok    = result.get("success", False)
    elapsed = result.get("_elapsed", "?")
    meta  = result.get("metadata") or {}
    views = result.get("views") or []
    err   = result.get("error", "")

    status = PASS if ok else FAIL
    print(f"\n{'─'*60}")
    print(f"{status}  {label}  [{elapsed}s]")

    if not ok:
        print(f"  ERROR: {err[:300]}")
        return False

    # Common checks
    obj_count = meta.get("object_count", 0)
    print(f"  Objects : {obj_count}")
    print(f"  Views   : {len(views)}  {[v['view'] for v in views]}")

    bb = meta.get("bounding_box")
    if bb:
        mn, mx = bb["min"], bb["max"]
        print(f"  Bounds  : W={mx[0]-mn[0]:.1f}  D={mx[1]-mn[1]:.1f}  H={mx[2]-mn[2]:.1f} mm")

    for s in meta.get("shape_info", []):
        if "error" in s:
            print(f"  Shape   : {s['label']} ERROR — {s['error'][:80]}")
        else:
            print(f"  Shape   : {s['label']}  type={s.get('type')}  "
                  f"vol={s.get('volume','?')}  faces={s.get('face_count','?')}  "
                  f"solid={s.get('is_solid','?')}  "
                  f"centroid={s.get('centroid','?')}")
            if s.get("orientation_check"):
                oc = s["orientation_check"]
                print(f"            print_face={oc.get('estimated_print_face')}  "
                      f"dims={oc.get('dimensions_mm')}")

    tp = meta.get("touching_pairs", [])
    if tp:
        print(f"  Touching: {[p['objects'] for p in tp]}")

    joints = meta.get("joints", [])
    if joints:
        print(f"  Joints  : {joints}")

    iface = result.get("interference_report")
    if iface is not None:
        for item in iface:
            sym = "✅" if item.get("clear") else "❌"
            print(f"  Interf  : {item['pair']}  {item.get('overlap_volume_mm3','?')} mm³  {sym}")

    if extra_checks:
        for desc, cond in extra_checks:
            sym = "  ✅" if cond else "  ❌"
            print(f"{sym}  {desc}")

    return True


def check_and_save(test_slug: str, label: str, result: dict, extra_checks=None):
    ok = check(label, result, extra_checks)
    if ok:
        save_images(test_slug, result)
    return ok


# ═════════════════════════════════════════════════════════════════════════════
print("=" * 60)
print(" FreeCAD MCP v3 — tool test suite")
print(f" Script: {SCRIPT}")
print("=" * 60)

results = {}

# ── Test 1: render_freecad_script — multi-view burst ─────────────────────────
print("\n[1/5] render_freecad_script — multi-view burst")
r1 = run({
    "mode":         "render",
    "script_path":  SCRIPT,
    "render_views": ["Isometric", "Front", "Top", "Right"],
    "zoom":         1.0,
    "background":   "#0d1117",
    "explode_factor": 0.0,
    "highlight_objects": [],
    "focus_object": None,
    "show_dimensions": False,
    "render_mode":  "shaded",
    "section_plane": None,
    "orientation_check": False,
})
results["render_burst"] = check_and_save("01_render_burst", "render_freecad_script (4-view burst)", r1, [
    ("4 views returned",        len(r1.get("views") or []) == 4),
    ("image_b64 present",       bool(r1.get("image_b64"))),
    ("object_count > 0",        (r1.get("metadata") or {}).get("object_count", 0) > 0),
    ("bounding_box present",    bool((r1.get("metadata") or {}).get("bounding_box"))),
    ("shape_info has centroid", all("centroid" in s for s in (r1.get("metadata") or {}).get("shape_info", [{}]))),
    ("shape_info has face_count", all("face_count" in s for s in (r1.get("metadata") or {}).get("shape_info", [{}]))),
    ("shape_info has color_rgb",  all("color_rgb"  in s for s in (r1.get("metadata") or {}).get("shape_info", [{}]))),
    ("shape_info has placement",  all("placement"  in s for s in (r1.get("metadata") or {}).get("shape_info", [{}]))),
])

# ── Test 2: render_freecad_script — single custom view ───────────────────────
print("\n[2/5] render_freecad_script — custom elevation/azimuth")
r2 = run({
    "mode":         "render",
    "script_path":  SCRIPT,
    "view_angle":   None,
    "render_views": None,
    "elevation":    30,
    "azimuth":      60,
    "zoom":         1.5,
    "background":   "#ffffff",
    "explode_factor": 0.0,
    "highlight_objects": [],
    "focus_object": None,
    "show_dimensions": False,
    "render_mode":  "shaded",
    "section_plane": None,
    "orientation_check": False,
})
results["render_custom"] = check_and_save("02_render_custom_angle", "render_freecad_script (custom angle + white bg)", r2, [
    ("1 view returned",  len(r2.get("views") or []) == 1),
])

# ── Test 3: inspect_freecad_assembly — show_dimensions + orientation_check ────
print("\n[3/5] inspect_freecad_assembly — show_dimensions + orientation_check")
r3 = run({
    "mode":              "render",
    "script_path":       SCRIPT,
    "view_angle":        "Isometric",
    "render_views":      None,
    "elevation":         None,
    "azimuth":           None,
    "zoom":              1.0,
    "width":             1600,
    "height":            1200,
    "background":        "#0d1117",
    "explode_factor":    0.0,
    "highlight_objects": [],
    "focus_object":      None,
    "show_dimensions":   True,
    "render_mode":       "shaded",
    "section_plane":     None,
    "orientation_check": True,
})
results["inspect"] = check_and_save("03_inspect_dimensions", "inspect_freecad_assembly (show_dimensions + orientation_check)", r3, [
    ("orientation_check in shape_info",
     any("orientation_check" in s for s in (r3.get("metadata") or {}).get("shape_info", []))),
])

# ── Test 4: section_freecad_model — XZ cross-section + wireframe ─────────────
print("\n[4/5] section_freecad_model — XZ section + wireframe")
r4 = run({
    "mode":              "render",
    "script_path":       SCRIPT,
    "view_angle":        "Front",
    "render_views":      None,
    "elevation":         None,
    "azimuth":           None,
    "zoom":              1.2,
    "width":             1600,
    "height":            1200,
    "background":        "#0d1117",
    "section_plane":     "XZ",
    "section_offset":    0.5,
    "render_mode":       "wireframe",
    "orientation_check": False,
    "explode_factor":    0.0,
    "highlight_objects": [],
    "focus_object":      None,
    "show_dimensions":   False,
})
results["section"] = check_and_save("04_section_xz_wireframe", "section_freecad_model (XZ section + wireframe)", r4)

# ── Test 5: section_freecad_model — normals render mode ──────────────────────
print("\n[5/5] section_freecad_model — normals render mode")
r5 = run({
    "mode":              "render",
    "script_path":       SCRIPT,
    "view_angle":        "Isometric",
    "render_views":      None,
    "elevation":         None,
    "azimuth":           None,
    "zoom":              1.0,
    "width":             1600,
    "height":            1200,
    "background":        "#0d1117",
    "section_plane":     None,
    "section_offset":    0.5,
    "render_mode":       "normals",
    "orientation_check": False,
    "explode_factor":    0.0,
    "highlight_objects": [],
    "focus_object":      None,
    "show_dimensions":   False,
})
results["normals"] = check_and_save("05_normals_mode", "section_freecad_model (normals render mode)", r5)

# ── Summary ───────────────────────────────────────────────────────────────────
print(f"\n{'='*60}")
passed = sum(1 for v in results.values() if v)
total  = len(results)
print(f"Results: {passed}/{total} passed")
for name, ok in results.items():
    print(f"  {'✅' if ok else '❌'}  {name}")
print("=" * 60)
sys.exit(0 if passed == total else 1)
