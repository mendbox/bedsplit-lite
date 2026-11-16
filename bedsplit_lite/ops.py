"""Operators and settings of BedSplit Lite (generated from BedSplit by tools/build_lite.py; do not edit)."""
import bpy
import bmesh
import json
import math
import os
import re
from bpy.props import BoolProperty, EnumProperty, FloatProperty, FloatVectorProperty, PointerProperty, StringProperty
from bpy.types import Operator, PropertyGroup
from mathutils import Vector
from . import core

GAP_MM = 15.0   # distance between the parts laid out next to the source
MAX_PARTS = 64  # more parts than this means a wrong scale, not a real job
FULL_URL = "https://quadfix.gumroad.com/l/bedsplit"


# ---------------------------------------------------------------- settings

def _redraw(self, context):
    if context is not None and context.screen is not None:
        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


class BedSplitLiteSettings(PropertyGroup):
    printer: EnumProperty(
        name="Printer", description="Printer bed size",
        items=[(n, "%s  (%d × %d × %d)" % ((n,) + s), "") for n, s in core.PRINTERS.items()]
        + [("CUSTOM", "Custom size", "Type the bed size in mm")],
        default="Creality Ender 3 / V3 SE")
    bed: FloatVectorProperty(name="Bed", description="Bed width, depth and print height, mm", size=3,
                             default=(220.0, 220.0, 250.0), min=40.0, soft_max=1000.0, precision=0)
    use_target: BoolProperty(name="Resize for print", default=False,
                             description="Scale the parts so that the longest side of the model has the size below. "
                                         "Off: the model is printed at its current size")
    long_mm: FloatProperty(name="Longest side, mm", description="Printed size of the longest side of the whole model",
                           default=400.0, min=10.0, soft_max=3000.0, precision=0)
    report: StringProperty()
    result_collection: StringProperty()


# ---------------------------------------------------------------- object <-> core

def bed_size(p):
    return tuple(p.bed) if p.printer == "CUSTOM" else tuple(float(x) for x in core.PRINTERS[p.printer])


def longest_mm(context, ob):
    return max(ob.dimensions) * context.scene.unit_settings.scale_length * 1000.0


def object_mm(context, ob):
    """Evaluated mesh of the object in world axes, scaled to mm and moved so that the model starts at 0.
    Returns (bm, lo, hi, mm_per_unit) with lo / hi in world units."""
    p = context.scene.bedsplit_lite
    dg = context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(ob.evaluated_get(dg))
    me.transform(ob.matrix_world)
    bm = bmesh.new()
    bm.from_mesh(me)
    bpy.data.meshes.remove(me)
    if not bm.faces:
        bm.free()
        raise ValueError("The object has no faces")
    lo, hi = core.bounds(bm)
    longest = max(hi - lo)
    if longest <= 0.0:
        bm.free()
        raise ValueError("The object has no size")
    if p.use_target:
        k = p.long_mm / longest
    else:
        k = context.scene.unit_settings.scale_length * 1000.0
    for v in bm.verts:
        v.co = (v.co - lo) * k
    return bm, lo, hi, k


def check_size(size, bed):
    """A model that needs hundreds of parts has the wrong scale (STL in mm read as metres is the usual case)."""
    n = 1
    for a in range(3):
        n *= max(1, math.ceil(size[a] / bed[a] - 1e-6))
    if n > MAX_PARTS:
        raise ValueError("The model is %.0f mm long and needs about %d parts. "
                         "Turn on Resize for print and type the real size" % (max(size), n))


def target(context):
    """The mesh to work on. A .glb import makes an empty parent active and puts the mesh under it,
    so fall back to the only selected mesh, then to the only mesh under the active object."""
    ob = context.active_object
    if ob is not None and ob.type == "MESH":
        return ob
    meshes = [o for o in context.selected_objects if o.type == "MESH"]
    if len(meshes) == 1:
        return meshes[0]
    if ob is not None and not meshes:
        meshes = [o for o in ob.children_recursive if o.type == "MESH"]
        if len(meshes) == 1:
            return meshes[0]
    return None


def mesh_ok(context):
    return context.mode == "OBJECT" and target(context) is not None


class Busy:
    def __init__(self, context):
        self.window = context.window

    def __enter__(self):
        if self.window is not None:
            self.window.cursor_set("WAIT")

    def __exit__(self, *exc):
        if self.window is not None:
            self.window.cursor_set("DEFAULT")


def remove_result(source_name):
    for coll in list(bpy.data.collections):
        if coll.get("bedsplit_source") == source_name:
            for ob in list(coll.objects):
                me = ob.data
                bpy.data.objects.remove(ob)
                if me is not None and me.users == 0:
                    bpy.data.meshes.remove(me)
            bpy.data.collections.remove(coll)


PART_COLORS = [(0.95, 0.45, 0.35), (0.35, 0.65, 0.95), (0.45, 0.85, 0.45), (0.95, 0.80, 0.30),
               (0.75, 0.50, 0.95), (0.30, 0.85, 0.80), (0.95, 0.55, 0.80), (0.70, 0.75, 0.40)]


def part_materials():
    """Viewport colours for the parts (Solid mode shows the material colour)."""
    mats = []
    for n, rgb in enumerate(PART_COLORS):
        name = "BedSplit color %d" % (n + 1)
        mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
        mat.diffuse_color = rgb + (1.0,)
        mats.append(mat)
    return mats


# ---------------------------------------------------------------- operators

class BEDSPLITLITE_OT_split(Operator):
    bl_idname = "bedsplit_lite.split"
    bl_label = "Split to Bed"
    bl_description = ("Cut the model into parts that fit the printer bed. Flat cuts, closed parts. "
                      "The parts appear next to the model; the model itself is not changed")
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return mesh_ok(context)

    def execute(self, context):
        p = context.scene.bedsplit_lite
        ob = target(context)
        bed = bed_size(p)
        try:
            with Busy(context):
                bm, lo, hi, k = object_mm(context, ob)
                size = (hi - lo) * k
                try:
                    check_size(size, bed)
                    planes, _, _ = core.auto_planes(bm, bed)
                    if not any(planes):
                        raise ValueError("The model fits the bed, nothing to split")
                    parts, res = core.split_mesh(bm, bed, planes)
                except ValueError:
                    bm.free()
                    raise
        except ValueError as e:
            self.report({"ERROR"}, "BedSplit Lite: %s" % e)
            return {"CANCELLED"}
        except Exception as e:
            self.report({"ERROR"}, "BedSplit Lite: split failed (%s)" % e)
            return {"CANCELLED"}

        remove_result(ob.name)
        coll = bpy.data.collections.new("BedSplit - %s" % ob.name)
        coll["bedsplit_source"] = ob.name
        context.scene.collection.children.link(coll)
        sizes = [core.bounds(part) for part in parts]
        pitch = (max(max(phi.x - plo.x, phi.y - plo.y) for plo, phi in sizes) + GAP_MM) / k
        cols = math.ceil(math.sqrt(len(parts)))
        x0 = hi.x + pitch
        y0 = (lo.y + hi.y) / 2.0 + pitch * (cols - 1) / 2.0
        made = []
        info = []
        palette = part_materials()
        for n, part in enumerate(parts):
            plo, phi = sizes[n]
            psize = phi - plo
            me = bpy.data.meshes.new("%s part %02d" % (ob.name, n + 1))
            part.to_mesh(me)
            part.free()
            me.materials.append(palette[n % len(palette)])
            o = bpy.data.objects.new(me.name, me)
            o["bedsplit"] = "part"
            o.scale = (1.0 / k,) * 3
            o.location = (x0 + pitch * (n % cols), y0 - pitch * (n // cols), lo.z)
            coll.objects.link(o)
            made.append(o)
            src = res["parts"][n]
            fits = psize.x <= bed[0] + 1e-3 and psize.y <= bed[1] + 1e-3 and psize.z <= bed[2] + 1e-3
            info.append({"size": [round(c, 1) for c in psize], "fits": bool(fits or src["fits"]), "closed": bool(src["closed"])})
        for o in context.selected_objects:
            o.select_set(False)
        for o in made:
            o.select_set(True)
        context.view_layer.objects.active = ob   # keep the panel on the source for the next split

        ok = res["checks"]["closed"] and res["checks"]["fits_bed"]
        p.result_collection = coll.name
        p.report = json.dumps({"source": ob.name, "n_parts": len(parts), "time_s": res["time_s"], "ok": bool(ok),
                               "messages": [m for m in res["messages"] if m[0] != "INFO"], "parts": info})
        _redraw(p, context)
        self.report({"INFO" if ok else "WARNING"}, "BedSplit Lite: %d parts" % len(parts))
        return {"FINISHED"}


class BEDSPLITLITE_OT_export(Operator):
    bl_idname = "bedsplit_lite.export"
    bl_label = "Export STL"
    bl_description = "Save every part of the last split as an STL file in mm"
    bl_options = {"REGISTER"}

    directory: StringProperty(subtype="DIR_PATH")
    filter_folder: BoolProperty(default=True, options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return context.scene.bedsplit_lite.result_collection in bpy.data.collections

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        coll = bpy.data.collections.get(context.scene.bedsplit_lite.result_collection)
        folder = bpy.path.abspath(self.directory)
        if coll is None or not folder:
            self.report({"ERROR"}, "BedSplit Lite: choose a folder")
            return {"CANCELLED"}
        try:
            os.makedirs(folder, exist_ok=True)
            count = 0
            for ob in sorted(coll.objects, key=lambda o: o.name):
                if ob.type != "MESH" or "bedsplit" not in ob:
                    continue
                bm = bmesh.new()
                bm.from_mesh(ob.data)
                name = re.sub(r"[^\w\-. ]+", "_", ob.name).strip().replace(" ", "_")
                core.write_stl(bm, os.path.join(folder, name + ".stl"))
                bm.free()
                count += 1
        except OSError as e:
            self.report({"ERROR"}, "BedSplit Lite: cannot write the files (%s)" % e)
            return {"CANCELLED"}
        self.report({"INFO"}, "BedSplit Lite: %d STL file(s) saved to %s" % (count, folder))
        return {"FINISHED"}


classes = (BedSplitLiteSettings, BEDSPLITLITE_OT_split, BEDSPLITLITE_OT_export)


def register_props():
    bpy.types.Scene.bedsplit_lite = PointerProperty(type=BedSplitLiteSettings)


def unregister_props():
    del bpy.types.Scene.bedsplit_lite
