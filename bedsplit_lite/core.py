"""Core of BedSplit Lite: cut to printer bed size, checks, STL export (generated from BedSplit).

Units: 1 Blender unit = 1 mm. No UI code here: the add-on calls split_mesh(), the tests call split_file().
"""
import bpy
import bmesh
import json
import math
import os
import struct
import time
from mathutils import Vector, Matrix
from mathutils.bvhtree import BVHTree
from mathutils.geometry import intersect_point_tri

PEG_R = 3.0        # dowel radius
PEG_HALF = 6.0     # dowel half length (depth in each part)
CLEAR = 0.2        # clearance, radial and at the socket bottom
WALL = 1.6         # minimum wall around a socket
SHAPE_TOL = 0.5      # 99% of the repaired surface must stay this close to the source, mm
# printable volume in mm (x, y, z)
PRINTERS = {
    "Bambu Lab A1 mini": (180, 180, 180),
    "Bambu Lab A1 / P1 / X1": (256, 256, 256),
    "Bambu Lab H2D": (350, 320, 325),
    "Prusa MINI+": (180, 180, 180),
    "Prusa MK4 / MK3": (250, 210, 220),
    "Prusa XL": (360, 360, 360),
    "Prusa CORE One": (250, 220, 270),
    "Creality Ender 3 / V3 SE": (220, 220, 250),
    "Creality K1": (220, 220, 250),
    "Creality K1 Max": (300, 300, 300),
    "Creality Ender 5 Plus": (350, 350, 400),
    "Anycubic Kobra 2": (220, 220, 250),
    "Anycubic Kobra 3": (250, 250, 260),
    "Elegoo Neptune 4": (225, 225, 265),
    "Elegoo Neptune 4 Max": (420, 420, 480),
    "Sovol SV06": (220, 220, 250),
    "Voron 2.4 300": (300, 300, 280),
    "Voron 0.2": (120, 120, 120),
}
AXES = (Vector((1, 0, 0)), Vector((0, 1, 0)), Vector((0, 0, 1)))


# ---------------------------------------------------------------- import / repair

def bounds(bm):
    xs = [v.co for v in bm.verts]
    lo = Vector((min(c.x for c in xs), min(c.y for c in xs), min(c.z for c in xs)))
    hi = Vector((max(c.x for c in xs), max(c.y for c in xs), max(c.z for c in xs)))
    return lo, hi


def defects(bm):
    boundary = sum(1 for e in bm.edges if len(e.link_faces) == 1)
    multi = sum(1 for e in bm.edges if len(e.link_faces) > 2)
    wire = sum(1 for e in bm.edges if not e.link_faces)
    flipped = sum(1 for e in bm.edges if len(e.link_faces) == 2 and not e.is_contiguous)
    return {"boundary": boundary, "multi": multi, "wire": wire, "flipped": flipped}


def is_closed(bm):
    d = defects(bm)
    return bool(bm.faces) and not (d["boundary"] or d["multi"] or d["wire"] or d["flipped"])


def islands(bm):
    seen = set()
    out = []
    for f in bm.faces:
        if f in seen:
            continue
        stack = [f]
        seen.add(f)
        comp = []
        while stack:
            g = stack.pop()
            comp.append(g)
            for e in g.edges:
                for h in e.link_faces:
                    if h not in seen:
                        seen.add(h)
                        stack.append(h)
        out.append(comp)
    return out


VOXEL_FACES = 600000
VOXELS = (0.8, 1.15, 1.6, 2.4, 3.2)   # mm


# ---------------------------------------------------------------- watertight check

def lite_check(bm):
    """BedSplit Lite does not repair. Returns (bm, "none", defects) for a watertight mesh, raises ValueError otherwise."""
    before = defects(bm)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
    bm.normal_update()
    if not is_closed(bm):
        d = defects(bm)
        raise ValueError("The mesh is not watertight (%d open edges, %d flipped, %d non-manifold). "
                         "BedSplit Lite splits closed meshes only. The full version repairs holes, normals and AI meshes"
                         % (d["boundary"], d["flipped"], d["multi"] + d["wire"]))
    return bm, "none", before


# ---------------------------------------------------------------- cutting

def cap_layer(bm):
    return bm.faces.layers.int.get("cap") or bm.faces.layers.int.new("cap")


def cut_keep(bm, co, no, pid):
    """Keep the half space on the negative side of (co, no); cap the cut; tag caps with pid."""
    lay = cap_layer(bm)
    geom = list(bm.verts) + list(bm.edges) + list(bm.faces)
    res = bmesh.ops.bisect_plane(bm, geom=geom, dist=1e-5, plane_co=co, plane_no=no,
                                 clear_outer=True, clear_inner=False)
    if not bm.faces:
        return False
    edges = [e for e in res["geom_cut"] if isinstance(e, bmesh.types.BMEdge)
             and e.is_valid and len(e.link_faces) == 1]
    if edges:
        fill = bmesh.ops.triangle_fill(bm, use_beauty=True, use_dissolve=False,
                                       edges=edges, normal=no)
        faces = [g for g in fill["geom"] if isinstance(g, bmesh.types.BMFace)]
        for f in faces:
            f.normal_update()
            if f.normal.dot(no) < 0:
                f.normal_flip()
            f[lay] = pid
    return True


def section_stats(bm, co, no):
    """Cross-section of bm at the plane: list of island areas."""
    tmp = bm.copy()
    geom = list(tmp.verts) + list(tmp.edges) + list(tmp.faces)
    res = bmesh.ops.bisect_plane(tmp, geom=geom, dist=1e-5, plane_co=co, plane_no=no,
                                 clear_outer=True, clear_inner=False)
    edges = [e for e in res["geom_cut"] if isinstance(e, bmesh.types.BMEdge)
             and e.is_valid and len(e.link_faces) == 1]
    areas = []
    if edges:
        fill = bmesh.ops.triangle_fill(tmp, use_beauty=False, use_dissolve=False,
                                       edges=edges, normal=no)
        faces = set(g for g in fill["geom"] if isinstance(g, bmesh.types.BMFace))
        seen = set()
        for f in faces:
            if f in seen:
                continue
            stack = [f]
            seen.add(f)
            a = 0.0
            while stack:
                g = stack.pop()
                a += g.calc_area()
                for e in g.edges:
                    for h in e.link_faces:
                        if h in faces and h not in seen:
                            seen.add(h)
                            stack.append(h)
            areas.append(a)
    tmp.free()
    return areas


def choose_planes(bm, lo, hi, bed, tail=0.0):
    """Per axis: minimal count of slabs, plane positions shifted away from sections too small for a dowel.
    tail: length a dovetail key adds to every part but the last one of an axis, mm (0 for dowels). The cuts are the
    same as without keys whenever those leave room for the key; otherwise the axis is cut again with less room."""
    need_r = PEG_R + CLEAR + WALL
    need_area = math.pi * need_r * need_r * 1.5
    planes = []
    for ax in range(3):
        dim = hi[ax] - lo[ax]
        for room in (bed[ax], bed[ax] - tail):
            n = max(1, math.ceil((dim - 1e-6) / bed[ax]))
            if n > 1:
                n = math.ceil((dim - 1e-6) / room)
            pos = []
            if n > 1:
                slab = dim / n
                d = min((room - slab) / 2.0, slab * 0.2)
                for i in range(1, n):
                    nominal = lo[ax] + slab * i
                    best = None
                    for k in range(-3, 4):
                        p = nominal + d * k / 3.0 + min(d, 0.137)
                        co = Vector((0, 0, 0))
                        co[ax] = p
                        # a plane through existing vertices gives degenerate cuts
                        on_plane = sum(1 for vert in bm.verts if abs(vert.co[ax] - p) < 0.02)
                        areas = section_stats(bm, co, AXES[ax])
                        small = sum(1 for a in areas if a < need_area)
                        score = (on_plane, small, len(areas), abs(k))
                        if best is None or score < best[0]:
                            best = (score, p)
                    pos.append(best[1])
            stops = [lo[ax]] + pos
            if tail <= 0.0 or all(b - a + tail <= bed[ax] for a, b in zip(stops, stops[1:])):
                break
        planes.append(pos)
    return planes


def make_cells(bm, lo, hi, planes):
    """Returns dict (i,j,k) -> bmesh, plane id = ax*1000 + index + 1 on the + side, negative id on the - side."""
    cells = {}
    edges = [[lo[ax]] + planes[ax] + [hi[ax]] for ax in range(3)]
    for i in range(len(edges[0]) - 1):
        for j in range(len(edges[1]) - 1):
            for k in range(len(edges[2]) - 1):
                idx = (i, j, k)
                c = bm.copy()
                ok = True
                for ax in range(3):
                    n = len(edges[ax]) - 1
                    a = idx[ax]
                    if a < n - 1:      # upper plane
                        co = Vector((0, 0, 0))
                        co[ax] = edges[ax][a + 1]
                        ok = ok and cut_keep(c, co, AXES[ax], ax * 1000 + a + 1)
                    if ok and a > 0:   # lower plane
                        co = Vector((0, 0, 0))
                        co[ax] = edges[ax][a]
                        ok = ok and cut_keep(c, co, -AXES[ax], -(ax * 1000 + a))
                    if not ok:
                        break
                if ok and c.faces and abs(c.calc_volume()) > 1.0:
                    cells[idx] = c
                else:
                    c.free()
    return cells, edges


# ---------------------------------------------------------------- dowels

def bad_edges(bm):
    out = []
    for e in bm.edges:
        n = len(e.link_faces)
        if n != 2 or e.link_loops[0].vert == e.link_loops[1].vert:
            out.append(e)
    return out


def heal_part(bm):
    """Local repair of a cut part: weld the few vertices around a defective edge, drop the faces that collapse,
    close what opens. The weld distance stays far under the printer resolution."""
    for dist in (0.02, 0.06, 0.15, 0.3):
        bad = bad_edges(bm)
        if not bad:
            return True
        verts = {v for e in bad for v in e.verts}
        verts |= {e2.other_vert(v) for v in list(verts) for e2 in v.link_edges}
        bmesh.ops.remove_doubles(bm, verts=list(verts), dist=dist)
        bmesh.ops.dissolve_degenerate(bm, edges=bm.edges, dist=1e-5)
        # two faces on the same vertices are a zero thickness flap: delete both
        seen = {}
        dead = set()
        for f in bm.faces:
            key = frozenset(v.index for v in f.verts)
            if key in seen:
                dead.add(f)
                dead.add(seen[key])
            else:
                seen[key] = f
        if dead:
            bmesh.ops.delete(bm, geom=list(dead), context="FACES")
        for _ in range(4):
            fins = {f for e in bm.edges if len(e.link_faces) > 2 for f in e.link_faces
                    if any(len(g.link_faces) == 1 for g in f.edges)}
            if not fins:
                break
            bmesh.ops.delete(bm, geom=list(fins), context="FACES")
        loose = [e for e in bm.edges if not e.link_faces]
        if loose:
            bmesh.ops.delete(bm, geom=loose, context="EDGES")
        loose = [v for v in bm.verts if not v.link_edges]
        if loose:
            bmesh.ops.delete(bm, geom=loose, context="VERTS")
        border = [e for e in bm.edges if len(e.link_faces) == 1]
        if border:
            bmesh.ops.holes_fill(bm, edges=border, sides=0)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return not bad_edges(bm)


# ---------------------------------------------------------------- checks / export

def lay_side(bm, idx, edges, bed, keys=None, grow=None):
    """The cut side the part can lie on for printing, the largest first: (axis, sign), or None when there is none.
    keys: dovetail keys of the part as tuples that start with (kind, axis, sign, slide axis). A face with a tail
    cannot lie on the bed, and a key in a side wall (tail or socket) prints without support only with its slide axis
    upright: level, the flank of a tail hangs in the air and the lip of a socket is one long bridge.
    grow: what the tails add to the size of bm per axis, when bm is the part before its tails are joined."""
    lo, hi = bounds(bm)
    size = list(hi - lo)
    if grow:
        size = [size[a] + grow[a] for a in range(3)]
    best = None
    for ax in range(3):
        for sign, n in ((-1.0, idx[ax]), (1.0, idx[ax] + 1)):
            if n == 0 or n == len(edges[ax]) - 1:
                continue
            if keys:
                if any(q[0] == "tail" and (q[1], q[2]) == (ax, sign) for q in keys):
                    continue
                if any(q[1] != ax and q[3] != ax for q in keys):
                    continue
            pos = edges[ax][n]
            area = sum(f.calc_area() for f in bm.faces
                       if abs(f.normal[ax]) > 0.999 and all(abs(v.co[ax] - pos) < 1e-3 for v in f.verts))
            other = [size[a] for a in range(3) if a != ax]
            fits = size[ax] <= bed[2] and (
                (other[0] <= bed[0] and other[1] <= bed[1]) or (other[1] <= bed[0] and other[0] <= bed[1]))
            if area > 0 and fits and (best is None or area > best[0]):
                best = (area, ax, sign)
    return (best[1], best[2]) if best else None


def lay_flat(bm, idx, edges, bed, keys=None):
    """Turn the part so its largest cut face lies on the bed, then move it to z = 0 over the origin.
    Returns the chosen side as (axis, sign) or None when no cut side fits the bed or the keys allow none (see
    lay_side): then the part stays as it stands in the model, where a tail points up or runs upright."""
    side = lay_side(bm, idx, edges, bed, keys)
    best = (None,) + side if side else None
    if best:
        normal = AXES[best[1]] * best[2]
        rot = normal.rotation_difference(Vector((0, 0, -1))).to_matrix().to_4x4()
        bmesh.ops.transform(bm, matrix=rot, verts=bm.verts)
        lo, hi = bounds(bm)
        size = hi - lo
        if size[0] > bed[0] + 1e-3 or size[1] > bed[1] + 1e-3:
            bmesh.ops.transform(bm, matrix=Matrix.Rotation(math.pi / 2, 4, "Z"), verts=bm.verts)
    lo, hi = bounds(bm)
    bmesh.ops.translate(bm, verts=bm.verts, vec=Vector((-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2])))
    bm.normal_update()
    return (best[1], best[2]) if best else None


def write_stl(bm, path):
    """Binary STL. N-gons are tessellated: a cut face can be concave, and a plain fan would fold over itself there
    (the slicer then sees phantom overhangs). No scene access, safe to call from an operator."""
    bm.normal_update()
    tris = [(tri[0].face.normal, tri[0].vert.co, tri[1].vert.co, tri[2].vert.co) for tri in bm.calc_loop_triangles()]
    with open(path, "wb") as fh:
        fh.write(b"BedSplit".ljust(80, b" "))
        fh.write(struct.pack("<I", len(tris)))
        for n, a, b, c in tris:
            fh.write(struct.pack("<12fH", n.x, n.y, n.z, a.x, a.y, a.z, b.x, b.y, b.z, c.x, c.y, c.z, 0))


def auto_planes(bm, bed, tail=0.0):
    """Cut plane positions per axis for a mesh in mm, without repair (for the preview)."""
    lo, hi = bounds(bm)
    return choose_planes(bm, lo, hi, [float(x) for x in bed], tail), lo, hi


def messages(res):
    """Plain sentences for the user: what was repaired, what needs attention. List of (level, text)."""
    out = []
    method = res["repair"]
    if method == "light":
        out.append(("INFO", "Repaired: holes closed, normals fixed"))
    elif method == "union":
        out.append(("INFO", "Repaired: overlapping shells joined"))
    elif method == "voxel":
        dev = res["repair_dev_p99"] or 0.0
        if dev > SHAPE_TOL:
            out.append(("WARNING", "Heavy repair: surface rebuilt, fine detail smoothed (%.1f mm)" % dev))
        else:
            out.append(("INFO", "Repaired: surface rebuilt (within %.1f mm)" % max(dev, 0.1)))
    if not res["checks"]["closed"]:
        bad = sum(1 for p in res["parts"] if not p["closed"])
        out.append(("ERROR", "%d part(s) not watertight: move a cut plane and split again" % bad))
    if not res["checks"]["fits_bed"]:
        bad = sum(1 for p in res["parts"] if not p["fits"])
        text = "%d part(s) larger than the bed: add a cut plane" % bad
        out.append(("ERROR", text))
    if res["joints_without_dowel"]:
        out.append(("WARNING", "%d joint(s) without dowel (wall too thin): glue these" % res["joints_without_dowel"]))
    if not res["checks"]["dowel_fit"]:
        out.append(("WARNING", "A dowel socket came out wrong: check the joints"))
    return out


def split_mesh(bm, bed=(220.0, 220.0, 250.0), planes=None):
    """Split a mesh given in mm. planes: None for automatic, or three lists of positions (X, Y, Z).
    Returns (parts, res): parts is a list of bmesh laid flat on z = 0, res is the result dict. Frees bm."""
    bed = [float(x) for x in bed]
    t0 = time.time()
    res = {"bed": bed, "blender": bpy.app.version_string, "faces_in": len(bm.faces)}
    if not bm.faces:
        raise ValueError("The object has no faces")
    bm.faces.ensure_lookup_table()
    orig_tree = BVHTree.FromBMesh(bm)
    bm, method, before = lite_check(bm)
    # shape kept: how far the repaired surface is from the source surface (filled holes count as deviation)
    dev = sorted(orig_tree.find_nearest(v.co)[3] for v in bm.verts)
    res["repair_dev_p99"] = round(dev[int(len(dev) * 0.99)], 3) if dev else None
    res["repair_dev_max"] = round(dev[-1], 3) if dev else None
    res["repair"] = method
    res["defects_in"] = before
    res["closed_after_repair"] = is_closed(bm)
    res["faces"] = len(bm.faces)
    bm.normal_update()
    vol_src = abs(bm.calc_volume())
    res["volume_src"] = round(vol_src, 1)
    res["t_repair"] = round(time.time() - t0, 2)

    lo, hi = bounds(bm)
    res["size"] = [round(x, 1) for x in (hi - lo)]
    cap_layer(bm)
    tail = 0.0
    base_planes = [sorted(ps) for ps in planes] if planes is not None else choose_planes(bm, lo, hi, bed, tail)
    bm.faces.ensure_lookup_table()
    src_tree = BVHTree.FromBMesh(bm)
    # a cut through touching contours can leave a non-manifold edge: move the planes a little and cut again
    attempt = 0
    cut_at = None
    to_dowels = set()   # cuts (axis, number) whose keys gave way to dowels
    while True:
        if cut_at != attempt:
            planes = [[p + 0.731 * attempt for p in ps] for ps in base_planes]
            res["attempts"] = attempt + 1
            res["planes"] = [[round(p, 2) for p in ps] for ps in planes]
            cells, edges = make_cells(bm, lo, hi, planes)
            res["t_cut"] = round(time.time() - t0, 2)
            cut_at = attempt

        keys = []
        key_sides = {}
        sockets = {idx: [] for idx in cells}
        dowels = []
        joints = []
        res["joints"] = joints
        res["dowels"] = 0
        res["joints_without_dowel"] = 0

        parts = {}
        healed = 0
        bodies = {}
        for idx in list(cells):
            bodies[idx] = len(islands(cells[idx]))
            parts[idx] = cells[idx]
        res["t_sockets"] = round(time.time() - t0, 2)

        for p in parts.values():
            # weld first: the slicer welds the STL, so an unwelded seam must count as an open part
            bmesh.ops.remove_doubles(p, verts=p.verts, dist=1e-4)
            bmesh.ops.dissolve_degenerate(p, edges=p.edges, dist=1e-5)
            bmesh.ops.recalc_face_normals(p, faces=p.faces)
            if not is_closed(p):
                p.verts.index_update()
                healed += 1 if heal_part(p) else 0
        res["parts_healed"] = healed
        # a socket that the boolean left as a separate shell inside the part shows up as an extra body
        sockets_open = all(len(islands(p)) <= bodies[idx] for idx, p in parts.items())
        res["sockets_open"] = sockets_open
        if (sockets_open and all(is_closed(p) for p in parts.values())) or attempt >= 4:
            break
        for p in parts.values():
            p.free()
        attempt += 1

    # checks
    part_info = []
    vol_sum = 0.0
    all_closed = True
    all_fit = True
    for n, (idx, p) in enumerate(sorted(parts.items())):
        p.normal_update()
        closed = is_closed(p)
        vol = abs(p.calc_volume())
        plo, phi = bounds(p)
        size = phi - plo
        fit = all(size[a] <= bed[a] + 1e-3 for a in range(3))
        vol_sum += vol
        all_closed = all_closed and closed
        all_fit = all_fit and fit
        part_info.append({"cell": idx, "closed": closed, "defects": defects(p), "volume": round(vol, 1),
                          "size": [round(x, 1) for x in size], "fits": fit,
                          "bodies": len(islands(p)), "sockets": len(sockets[idx])})
    socket_vol = 0.0
    res["parts"] = part_info
    res["n_parts"] = len(parts)
    res["volume_parts"] = round(vol_sum, 1)
    res["volume_sockets"] = round(socket_vol, 1)
    res["volume_err_pct"] = round(100.0 * abs(vol_sum + socket_vol - vol_src) / vol_src, 4) if vol_src else None

    gap_min = None
    hits = 0
    out = []
    for n, (idx, p) in enumerate(sorted(parts.items())):
        part_info[n]["down"] = lay_flat(p, idx, edges, bed, key_sides.get(idx))
        out.append(p)
    bm.free()
    res["dowel_intersections"] = hits
    res["dowel_gap_min"] = round(gap_min, 4) if gap_min is not None else None
    res["time_s"] = round(time.time() - t0, 2)
    gap_ok = True
    res["checks"] = {
        "closed": all_closed and res["closed_after_repair"],
        "fits_bed": all_fit,
        "volume": res["volume_err_pct"] is not None and res["volume_err_pct"] <= 1.0,
        "dowel_fit": hits == 0 and gap_ok,
        "shape": res["repair_dev_p99"] is not None and res["repair_dev_p99"] <= SHAPE_TOL,
        "time": res["time_s"] <= 30.0,
    }
    res["pass_blender"] = all(res["checks"].values())
    res["messages"] = messages(res)
    return out, res

