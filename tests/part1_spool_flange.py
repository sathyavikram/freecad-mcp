"""
Part 1 – Spool Flange  (print 2 identical copies)
===================================================
Large disc – the two outer walls of the cord reel spool.
Matches reference: thick black disc, 6 rectangular lightening slots
arranged radially, centre bore for the hub axle, outer lip.

Key dimensions (all mm):
  Outer diameter  : 240  (fits 256 mm build plate with 8 mm margin)
  Thickness       : 8
  Centre bore dia : 18   (slip fit for axle dia 17; 0.5 mm radial clearance)
  Slot count      : 6
  Slot size       : 60 × 18 mm (radial × tangential)
  Slot inner rad  : 45 mm from centre
  Hub collar bore : 44 mm, depth 5 mm (press-fit collar on inner face)

Print orientation: FLAT (face down, no supports needed).
Print 2 copies.
"""

import os, sys, math
sys.path.append("/usr/lib/freecad/lib")
sys.path.append("/usr/lib/freecad-daily/lib")

try:
    import FreeCAD as App
    import Part
except ImportError:
    raise SystemExit("FreeCAD Python libs not found – run inside FreeCAD or fix sys.path.")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_DIR    = "/Users/intelligentmachine/Documents/workspace/3d-models/cord-storage-reel/lib"

# ── Dimensions ──────────────────────────────────────────────────────────────
OUTER_R     = 120.0   # flange outer radius (dia 240 – fits 256 mm build plate)
THICKNESS   =   8.0   # overall disc thickness
BORE_R      =   9.0   # axle bore radius (dia 18, slip fit for axle dia 17)
COLLAR_D    =   5.0   # depth of inner collar recess (hub boss seats here)
COLLAR_R    =  22.0   # collar recess radius (hub boss OD + 0.3 clearance)
SLOT_COUNT  =   6
SLOT_RADIAL =  60.0   # slot length (radial direction)
SLOT_TANG   =  18.0   # slot width  (tangential)
SLOT_IN_R   =  45.0   # distance from centre to slot inner edge
SLOT_DEPTH  =   8.0   # through-slot (full thickness)

# ── Helper ───────────────────────────────────────────────────────────────────
def make_rounded_slot(length, width, depth):
    """Box with semicircular ends in the XY plane, centred at origin."""
    ext = length / 2 - width / 2
    cyl1 = Part.makeCylinder(width / 2, depth,
                              App.Vector(-ext, 0, 0),
                              App.Vector(0, 0, 1))
    cyl2 = Part.makeCylinder(width / 2, depth,
                              App.Vector( ext, 0, 0),
                              App.Vector(0, 0, 1))
    box  = Part.makeBox(length - width, width, depth,
                        App.Vector(-ext, -width / 2, 0))
    return cyl1.fuse(cyl2).fuse(box)

def make_flange():
    # 1. Full disc
    disc = Part.makeCylinder(OUTER_R, THICKNESS,
                             App.Vector(0, 0, 0), App.Vector(0, 0, 1))

    # 2. Centre bore (through)
    bore = Part.makeCylinder(BORE_R, THICKNESS + 2,
                             App.Vector(0, 0, -1), App.Vector(0, 0, 1))
    disc = disc.cut(bore)

    # 3. Inner collar recess on face Z=0 (inner face)
    collar = Part.makeCylinder(COLLAR_R, COLLAR_D,
                               App.Vector(0, 0, 0), App.Vector(0, 0, 1))
    disc = disc.cut(collar)

    # 4. Radial lightening slots
    slot_cx = SLOT_IN_R + SLOT_RADIAL / 2   # centre of slot from disc centre
    for i in range(SLOT_COUNT):
        ang = math.radians(i * 360 / SLOT_COUNT)
        slot = make_rounded_slot(SLOT_RADIAL, SLOT_TANG, SLOT_DEPTH + 2)
        # move slot centre to (slot_cx, 0, -1)
        slot.translate(App.Vector(slot_cx, 0, -1))
        # rotate around Z
        slot.rotate(App.Vector(0, 0, 0), App.Vector(0, 0, 1),
                    math.degrees(ang))
        disc = disc.cut(slot)

    # 5. Cord-entry notch on outer rim (align with slot 0)
    # Small notch at the rim between flange and hub so cord exits neatly
    notch_w = 8.0
    notch_d = 12.0   # depth into thickness
    notch = Part.makeBox(notch_d + 2, notch_w, THICKNESS + 2,
                         App.Vector(OUTER_R - notch_d, -notch_w / 2, -1))
    disc = disc.cut(notch)

    return disc


def main():
    doc = App.newDocument("Part1_SpoolFlange")
    shape = make_flange()

    feature = doc.addObject("Part::Feature", "SpoolFlange")
    feature.Shape = shape
    doc.recompute()

    out_path = os.path.join(LIB_DIR, "part1_spool_flange.step")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    Part.export([feature], out_path)
    print(f"Part 1 – Spool Flange  exported → {out_path}")
    print(f"Bounding box: {feature.Shape.BoundBox}")
    print("Print 2 copies  |  Orientation: FLAT (face down)")


if __name__ == "__main__":
    main()
