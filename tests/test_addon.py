"""Headless test of BedSplit Lite (operators, settings, export, what Lite must NOT have).

  blender -b --factory-startup --python tests/test_addon.py -- <out_dir>

Prints one CHECK line per check and a final ADDON line.
"""
import json
import os
import struct
import sys

import bpy
import bmesh

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
import bedsplit_lite as bl
from bedsplit_lite import core

OUT = sys.argv[sys.argv.index("--") + 1]
results = []


def check(name, ok, note=""):
    results.append(bool(ok))
    print("CHECK %02d %-50s %s %s" % (len(results), name, "OK" if ok else "FAIL", note))


def run(op, **kw):
    try:
        return op(**kw)
    except RuntimeError as e:
        return str(e)


def reset():
    if hasattr(bpy.types.Scene, "bedsplit_lite"):
        bl.unregister()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bl.register()
    return bpy.context.scene.bedsplit_lite


def activate(ob):
    for o in bpy.context.selected_objects:
        o.select_set(False)
    ob.select_set(True)
    bpy.context.view_layer.objects.active = ob
    return ob


def add_cube(size, name="Cube"):
    bpy.ops.mesh.primitive_cube_add(size=size)
    ob = bpy.context.active_object
    ob.name = name
    return ob


def parts_of(p):
    coll = bpy.data.collections.get(p.result_collection)
    return sorted((o for o in coll.objects if o.get("bedsplit") == "part"), key=lambda o: o.name) if coll else []


def mesh_closed(me):
    bm = bmesh.new()
    bm.from_mesh(me)
    ok = core.is_closed(bm)
    bm.free()
    return ok


def stl_size(path):
    with open(path, "rb") as fh:
        fh.seek(80)
        n = struct.unpack("<I", fh.read(4))[0]
        lo, hi = [1e30] * 3, [-1e30] * 3
        for _ in range(n):
            rec = struct.unpack("<12fH", fh.read(50))
            for i in range(3, 12):
                a = i % 3
                lo[a] = min(lo[a], rec[i])
                hi[a] = max(hi[a], rec[i])
    return n, [hi[a] - lo[a] for a in range(3)]


def fits(size, bed):
    return all(size[a] <= bed[a] + 0.01 for a in range(3))


bed = (220.0, 220.0, 250.0)
# ---------------------------------------------------------------- A: what Lite has and has not
bl.register()
bl.unregister()
p = reset()
check("register twice, settings on the scene", p is not None and p.printer == "Creality Ender 3 / V3 SE")
check("no dowel / cut-plane settings", not any(hasattr(p, a) for a in ("peg_diameter", "clearance", "planes", "max_per_joint")))
check("no preview / cut operators", not any(hasattr(bpy.types, "BEDSPLITLITE_OT_" + n) for n in ("preview", "plane_add", "plane_remove")))
check("no dowel / repair code in core", not any(hasattr(core, n) for n in ("dowel_spots", "cut_sockets", "dowel_mesh", "repair", "voxel_remesh", "light_repair")))
check("full version link is a Gumroad URL", bl.ops.FULL_URL.startswith("https://mendbox.gumroad.com/"))
man = open(os.path.join(ROOT, "bedsplit_lite", "blender_manifest.toml")).read()
check("manifest: id, version, licence", all(s in man for s in ('id = "bedsplit_lite"', 'version = "1.0.2"', "GPL-3.0-or-later")))

# ---------------------------------------------------------------- B: cube in metres, automatic
cube = add_cube(0.3)
verts_before = [tuple(v.co) for v in cube.data.vertices]
check("panel helper: longest side 300 mm", abs(bl.ops.longest_mm(bpy.context, cube) - 300.0) < 0.01)
r = run(bpy.ops.bedsplit_lite.split)
parts = parts_of(p)
check("split: 8 parts", r == {"FINISHED"} and len(parts) == 8, str(r))
check("split: parts are closed", all(mesh_closed(o.data) for o in parts))
check("split: parts fit the bed (flat cuts)", all(fits(list(o.dimensions * 1000.0), bed) for o in parts))
check("split: parts beside the source", all(o.matrix_world.translation.x > 0.15 for o in parts))
check("split: no dowel object", not any(o.get("bedsplit") == "dowel" for o in bpy.data.objects))
rep = json.loads(p.report)
check("split: report filled", rep["n_parts"] == 8 and rep["ok"] and len(rep["parts"]) == 8)
check("split: source untouched", [tuple(v.co) for v in cube.data.vertices] == verts_before and cube.name in bpy.context.scene.objects)
check("split: result selected, source not", all(o.select_get() for o in parts) and not cube.select_get())
check("split: parts coloured", all(o.data.materials and o.data.materials[0].name.startswith("BedSplit color") for o in parts))
gap = []
for o in parts:
    bm = bmesh.new()
    bm.from_mesh(o.data)
    d = o.dimensions / o.scale.x
    gap.append(abs(bm.calc_volume() - d.x * d.y * d.z))
    bm.free()
check("split: solid parts, no dowel sockets", max(gap) < 1.0, "%.2f mm3" % max(gap))
activate(cube)
r = run(bpy.ops.bedsplit_lite.split)
colls = [c for c in bpy.data.collections if c.get("bedsplit_source") == cube.name]
check("split again: old result replaced", r == {"FINISHED"} and len(colls) == 1 and len(colls[0].objects) == 8)
out_a = os.path.join(OUT, "a_cube")
r = run(bpy.ops.bedsplit_lite.export, directory=out_a)
files = sorted(os.listdir(out_a)) if os.path.isdir(out_a) else []
check("export: 8 part files, no dowel file", r == {"FINISHED"} and len(files) == 8 and not any("dowel" in f for f in files), str(files[:2]))
sizes = [stl_size(os.path.join(out_a, f))[1] for f in files]
check("export: STL in mm, fits the bed", all(fits(s, bed) and max(s) > 100.0 for s in sizes))

# ---------------------------------------------------------------- C: model fits, custom bed, printer
p = reset()
cube = add_cube(0.2)
r = run(bpy.ops.bedsplit_lite.split)
check("model fits the bed: nothing to split", isinstance(r, str) and "fits the bed" in r and not p.result_collection, str(r)[:60])
p.printer = "Voron 0.2"
r = run(bpy.ops.bedsplit_lite.split)
check("Voron 0.2 (120 mm): 8 parts fit", r == {"FINISHED"} and len(parts_of(p)) == 8
      and all(fits(list(o.dimensions * 1000.0), (120.0, 120.0, 120.0)) for o in parts_of(p)))
p = reset()
cube = add_cube(0.3)
p.printer = "CUSTOM"
p.bed = (160.0, 400.0, 400.0)
r = run(bpy.ops.bedsplit_lite.split)
check("custom bed 160 mm: 2 parts", r == {"FINISHED"} and len(parts_of(p)) == 2, str(r))

# ---------------------------------------------------------------- D: transforms, units, resize
p = reset()
bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=1.0)
ball = bpy.context.active_object
ball.scale = (1.0, 2.0, 0.7)
ball.rotation_euler = (0.3, 0.5, 0.9)
ball.location = (3.0, -2.0, 1.0)
bpy.context.view_layer.update()
r = run(bpy.ops.bedsplit_lite.split)
check("wrong scale (4 m model): clear message", isinstance(r, str) and "Resize for print" in r and not p.result_collection, str(r)[:60])
p.use_target = True
p.long_mm = 400.0
r = run(bpy.ops.bedsplit_lite.split)
parts = parts_of(p)
check("resize to 400 mm, rotated + scaled object", r == {"FINISHED"} and len(parts) >= 2
      and all(mesh_closed(o.data) and fits(list(o.dimensions), bed) for o in parts), str(r))
check("resize: parts keep the viewport scale", all(o.scale.x < 1.0 for o in parts))
out_c = os.path.join(OUT, "c_ball")
run(bpy.ops.bedsplit_lite.export, directory=out_c)
sizes = [stl_size(os.path.join(out_c, f))[1] for f in os.listdir(out_c)]
check("resize: exported STL in mm", len(sizes) == len(parts) and all(fits(s, bed) for s in sizes) and max(max(s) for s in sizes) > 150.0)

p = reset()
cube = add_cube(0.3)
cube.scale = (-1.0, 1.0, 1.0)
bpy.ops.object.modifier_add(type="BEVEL")
cube.modifiers[0].width = 0.02
bpy.context.view_layer.update()
r = run(bpy.ops.bedsplit_lite.split)
parts = parts_of(p)
check("mirrored object with a modifier", r == {"FINISHED"} and len(parts) == 8 and all(mesh_closed(o.data) for o in parts), str(r))

p = reset()
bpy.context.scene.unit_settings.scale_length = 0.001
cube = add_cube(300.0)
r = run(bpy.ops.bedsplit_lite.split)
parts = parts_of(p)
check("scene in millimetres (unit scale 0.001)", r == {"FINISHED"} and len(parts) == 8
      and all(fits(list(o.dimensions), bed) for o in parts) and abs(parts[0].scale.x - 1.0) < 1e-6, str(r))

# ---------------------------------------------------------------- E: not watertight is refused, not silently broken
p = reset()
cube = add_cube(0.3)
bpy.ops.object.mode_set(mode="EDIT")
bm = bmesh.from_edit_mesh(cube.data)
bm.faces.ensure_lookup_table()
bmesh.ops.delete(bm, geom=[bm.faces[0]], context="FACES_ONLY")
bmesh.update_edit_mesh(cube.data)
bpy.ops.object.mode_set(mode="OBJECT")
r = run(bpy.ops.bedsplit_lite.split)
check("open mesh: refused, points to full version", isinstance(r, str) and "watertight" in r and "full version" in r
      and not p.result_collection, str(r)[:80])
check("open mesh: no result objects", len(bpy.data.collections) == 0 and len(bpy.data.objects) == 1)

# ---------------------------------------------------------------- F: no mesh, empty mesh, edit mode, glb parent
p = reset()
bpy.ops.object.empty_add()
check("poll: no mesh object", not bpy.ops.bedsplit_lite.split.poll() and not bpy.ops.bedsplit_lite.export.poll())
me = bpy.data.meshes.new("empty")
ob = bpy.data.objects.new("empty mesh", me)
bpy.context.scene.collection.objects.link(ob)
activate(ob)
r = run(bpy.ops.bedsplit_lite.split)
check("empty mesh: clear message", isinstance(r, str) and "no faces" in r, str(r)[:60])
cube = add_cube(0.3)
bpy.ops.object.mode_set(mode="EDIT")
check("poll: not in Edit Mode", not bpy.ops.bedsplit_lite.split.poll())
bpy.ops.object.mode_set(mode="OBJECT")

p = reset()
par = bpy.data.objects.new("world", None)
bpy.context.scene.collection.objects.link(par)
par.scale = (2.0, 2.0, 2.0)
kid = add_cube(0.3, "geometry_0")
kid.parent = par
par.select_set(True)
bpy.context.view_layer.objects.active = par
check("glb parent: mesh found", bl.ops.target(bpy.context) == kid and bpy.ops.bedsplit_lite.split.poll())
r = run(bpy.ops.bedsplit_lite.split)
parts = parts_of(p)
check("glb parent: split", r == {"FINISHED"} and len(parts) >= 2 and all(fits(list(o.dimensions * 1000.0), bed) for o in parts), str(r))
check("after split: source still active", bl.ops.target(bpy.context) == kid)

# write_stl tessellates a concave n-gon (L shape, area 3) without folding: the signed triangle areas add up to the area
bm = bmesh.new()
lv = [bm.verts.new(c) for c in ((0, 0, 0), (2, 0, 0), (2, 1, 0), (1, 1, 0), (1, 2, 0), (0, 2, 0))]
bm.faces.new(lv)
core.write_stl(bm, os.path.join(OUT, "lshape.stl"))
bm.free()
raw = open(os.path.join(OUT, "lshape.stl"), "rb").read()
nt = struct.unpack("<I", raw[80:84])[0]
area = 0.0
for i in range(nt):
    f = struct.unpack("<12fH", raw[84 + 50 * i:134 + 50 * i])
    a, b, c = f[3:6], f[6:9], f[9:12]
    area += ((b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])) / 2.0
check("write_stl: concave n-gon is tessellated, not fanned", nt == 4 and abs(area - 3.0) < 1e-4, "tris %d area %.3f" % (nt, area))

bl.unregister()
check("unregister", not hasattr(bpy.types.Scene, "bedsplit_lite"))
print("ADDON %s pass %d of %d" % (bpy.app.version_string, sum(results), len(results)))
sys.exit(0 if all(results) else 1)
