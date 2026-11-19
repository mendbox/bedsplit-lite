# SPDX-License-Identifier: GPL-3.0-or-later
import bpy
import json
import os
import textwrap
import tomllib
from bpy.types import Panel
from . import ops


def _version():
    try:
        with open(os.path.join(os.path.dirname(__file__), "blender_manifest.toml"), "rb") as f:
            return tomllib.load(f).get("version", "?")
    except Exception:
        return "?"


VERSION = _version()
ICONS = {"INFO": "INFO", "WARNING": "ERROR", "ERROR": "CANCEL"}


def mm(values):
    return " × ".join("%.0f" % v for v in values)


def wrapped(layout, context, text, icon):
    """A label cuts long text with "..."; this breaks it into lines that fit the sidebar."""
    chars = max(16, int((context.region.width - 70) / (6.5 * context.preferences.system.ui_scale)))
    for n, line in enumerate(textwrap.wrap(text, chars)):
        layout.label(text=line, icon=icon if n == 0 else "BLANK1")


class BEDSPLITLITE_PT_panel(Panel):
    bl_label = "BedSplit Lite"
    bl_idname = "BEDSPLITLITE_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BedSplit Lite"

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def draw(self, context):
        p = context.scene.bedsplit_lite
        ob = ops.target(context)
        layout = self.layout
        if ob is None:
            layout.label(text="Select a mesh object", icon="INFO")
        col = layout.column(align=True)

        box = col.box()
        box.label(text="Printer", icon="MESH_GRID")
        box.prop(p, "printer", text="")
        if p.printer == "CUSTOM":
            box.prop(p, "bed", text="")
        box.label(text="Bed: %s mm" % mm(ops.bed_size(p)))

        box = col.box()
        box.label(text="Model size", icon="FULLSCREEN_ENTER")
        if ob is not None:
            box.label(text="Now: %.0f mm longest side" % ops.longest_mm(context, ob))
        box.prop(p, "use_target")
        row = box.row()
        row.enabled = p.use_target
        row.prop(p, "long_mm")

        col.separator()
        row = col.row()
        row.scale_y = 1.6
        row.operator("bedsplit_lite.split", icon="MOD_EXPLODE")

        box = layout.box()
        wrapped(box, context, "Full version adds dowels and sockets, mesh repair, and cuts you can move.", "INFO")
        box.operator("wm.url_open", text="Get the full version", icon="URL").url = ops.FULL_URL


class BEDSPLITLITE_PT_result(Panel):
    bl_label = f"Result  ·  BedSplit Lite v{VERSION}"
    bl_idname = "BEDSPLITLITE_PT_zresult"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "BedSplit Lite"

    @classmethod
    def poll(cls, context):
        return context.mode == "OBJECT"

    def draw(self, context):
        p = context.scene.bedsplit_lite
        layout = self.layout
        try:
            rep = json.loads(p.report) if p.report else None
        except ValueError:
            rep = None
        if not rep:
            layout.label(text="No result yet", icon="INFO")
            return
        col = layout.column(align=True)
        col.label(text="%d parts" % rep["n_parts"], icon="CHECKMARK" if rep["ok"] else "ERROR")
        wrapped(col, context, "%s, %.1f s" % (rep["source"], rep["time_s"]), "BLANK1")
        for level, text in rep["messages"]:
            wrapped(col, context, text, ICONS.get(level, "INFO"))
        box = layout.box()
        for n, part in enumerate(rep["parts"]):
            ok = part.get("ok", part["fits"] and part["closed"])
            box.label(text="Part %d: %s mm" % (n + 1, mm(part["size"])), icon="CHECKMARK" if ok else "CANCEL")
        row = layout.row()
        row.scale_y = 1.4
        row.operator("bedsplit_lite.export", icon="EXPORT")


classes = (BEDSPLITLITE_PT_panel, BEDSPLITLITE_PT_result)
