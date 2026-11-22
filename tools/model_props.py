"""Build the prop kit's Blender models (models/props/<name>.blend) from code.

    blender -b --python tools/model_props.py [-- table chair]

Each prop gets its own scene, written to models/props/<name>.blend; `make models` then
exports shadow/web/models/prop_<name>.glb. After that the .blend files are the source:
edit them by hand in Blender if you like (re-running this script overwrites them).

Units: 1 Blender unit = one 16 px map cell. Sizes below are in GBA pixels, measured from
the original art seen through the game's oblique projection (screen y = z - y): the
models cover about the same pixels as the art from the game's point of view.
Axes: +x east, +y north, +z up; origin = centre of the footprint, on the floor.
Prop definitions (which metatiles, where): PROP_KIT in shadow/games/pokeemerald.py.
"""
import math
import sys
from pathlib import Path

import bmesh
import bpy

PX = 1 / 16
OUT = Path(__file__).resolve().parent.parent / "models" / "props"


def srgb(hexstr):
    c = [int(hexstr[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return [(v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4) for v in c] + [1.0]


def material(name, hexstr, roughness=0.7):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = m.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = srgb(hexstr)
    bsdf.inputs["Roughness"].default_value = roughness
    m.diffuse_color = srgb(hexstr)
    return m


def _object(scene, name, bm, mat, bevel):
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    me.materials.append(mat)
    ob = bpy.data.objects.new(name, me)
    scene.collection.objects.link(ob)
    if bevel:
        mod = ob.modifiers.new("Bevel", "BEVEL")
        mod.width = bevel * PX
        mod.segments = 2
        mod.limit_method = "ANGLE"
        mod.harden_normals = False
    return ob


def box(scene, name, x0, x1, y0, y1, z0, z1, mat, bevel=0.35):
    """Axis-aligned box, coordinates in px."""
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    for v in bm.verts:
        v.co.x = (x0 if v.co.x < 0 else x1) * PX
        v.co.y = (y0 if v.co.y < 0 else y1) * PX
        v.co.z = (z0 if v.co.z < 0 else z1) * PX
    return _object(scene, name, bm, mat, bevel)


def prism(scene, name, outline, z0, z1, mat, bevel=0.25):
    """Vertical prism over a convex outline [(x, y), ...] counter-clockwise, in px."""
    bm = bmesh.new()
    bottom = [bm.verts.new((x * PX, y * PX, z0 * PX)) for x, y in outline]
    top = [bm.verts.new((x * PX, y * PX, z1 * PX)) for x, y in outline]
    bm.faces.new(list(reversed(bottom)))
    bm.faces.new(top)
    n = len(outline)
    for i in range(n):
        j = (i + 1) % n
        bm.faces.new([bottom[i], bottom[j], top[j], top[i]])
    return _object(scene, name, bm, mat, bevel)


def octagon(hx, hy, cut):
    return [(-hx + cut, -hy), (hx - cut, -hy), (hx, -hy + cut), (hx, hy - cut),
            (hx - cut, hy), (-hx + cut, hy), (-hx, hy - cut), (-hx, -hy + cut)]


def new_scene(name):
    old = bpy.data.scenes.get(name)
    if old:
        for ob in list(old.collection.all_objects):
            data = ob.data
            bpy.data.objects.remove(ob)
            if isinstance(data, bpy.types.Mesh) and not data.users:
                bpy.data.meshes.remove(data)
        bpy.data.scenes.remove(old)
    sc = bpy.data.scenes.new(name)
    sc.unit_settings.system = "METRIC"
    return sc


# -- the props ---------------------------------------------------------------------------
def build_table():
    """Brendan's house dining table: 28 x 24 px, top 8 px up, pink octagonal cloth.
    Art: top surface on screen rows 96-119, front edge 120-122, legs 123-127 (x 52-56, 71-75)."""
    sc = new_scene("table")
    wood = material("Wood", "#b5a54a", 0.6)
    leg = material("WoodDark", "#947329", 0.6)
    top = material("TableTop", "#d6d6b5", 0.5)
    cloth = material("Cloth", "#f7ceef", 0.9)
    hem = material("ClothHem", "#d67bad", 0.9)
    piping = material("ClothPiping", "#ffffff", 0.8)
    hx, hy = 14, 12
    for sx in (-1, 1):
        for sy in (-1, 1):
            x, y = sx * 10, sy * 10
            box(sc, f"Leg_{'LR'[sx > 0]}{'FB'[sy > 0]}", x - 2, x + 2, y - 2, y + 2, 0, 5.4, leg, 0.4)
    box(sc, "Board", -hx, hx, -hy, hy, 5, 7.8, wood, 0.5)
    box(sc, "BoardTop", -hx + 0.3, hx - 0.3, -hy + 0.3, hy - 0.3, 7.6, 8.0, top, 0.2)
    # The cloth: an octagon lying on top, touching the middle of each edge, with its
    # straight sides hanging down over the edges (the dark pink band in the art).
    cut = 8
    prism(sc, "Cloth", octagon(hx + 0.3, hy + 0.3, cut), 8.0, 8.35, cloth, 0.12)
    t = 0.3
    box(sc, "Hem_F", -hx + cut, hx - cut, -hy - 0.3 - t, -hy - 0.3, 5.4, 8.35, hem, 0.1)
    box(sc, "Hem_B", -hx + cut, hx - cut, hy + 0.3, hy + 0.3 + t, 5.4, 8.35, hem, 0.1)
    box(sc, "Hem_L", -hx - 0.3 - t, -hx - 0.3, -hy + cut, hy - cut, 5.4, 8.35, hem, 0.1)
    box(sc, "Hem_R", hx + 0.3, hx + 0.3 + t, -hy + cut, hy - cut, 5.4, 8.35, hem, 0.1)
    # White piping along the cloth's diagonal edges, as in the art.
    for i, (cx, cy) in enumerate(((-1, -1), (1, -1), (1, 1), (-1, 1))):
        ax, ay = cx * (hx + 0.3 - cut / 2), cy * (hy + 0.3 - cut / 2)
        length = cut * math.sqrt(2) - 0.6
        ob = box(sc, f"Piping_{i}", -length / 2, length / 2, -0.3, 0.3, 8.0, 8.5, piping, 0.1)
        ob.location = (ax * PX - cx * 0.3 * PX, ay * PX - cy * 0.3 * PX, 0)
        ob.rotation_euler.z = math.atan2(-cy, cx)          # along the edge
    return sc


def build_chair():
    """Green dining chair, seen from the side in the art; the seat faces +x (east).
    14 px long (x), 6 px wide (y), seat top 5 px up, back 10 px up.
    Art (left chair, cell x 32-47): back x 33-36, seat x 37-46, seat top on screen rows
    101-106 with its front 107-108, legs to row 111."""
    sc = new_scene("chair")
    frame = material("Frame", "#52526b", 0.55)
    cushion = material("Cushion", "#b5ff6b", 0.85)
    cushion_dark = material("CushionPiping", "#8cc64a", 0.85)
    hy = 3
    for x in (-5, 4):
        for y in (-2.1, 2.1):
            box(sc, f"Leg_{'BF'[x > 0]}{'RL'[y > 0]}", x - 0.8, x + 0.8, y - 0.8, y + 0.8, 0, 2.5, frame, 0.25)
    # Seat: a frame whose front end shows past the cushion (the dark edge in the art).
    box(sc, "SeatFrame", -6.8, 6.9, -hy, hy, 2.2, 3.4, frame, 0.3)
    box(sc, "SeatEnd", 5.4, 6.9, -hy, hy, 3.0, 4.6, frame, 0.4)
    box(sc, "SeatPiping", -3.0, 5.5, -hy + 0.1, hy - 0.1, 3.2, 4.0, cushion_dark, 0.3)
    box(sc, "Seat", -3.2, 5.4, -hy + 0.2, hy - 0.2, 3.8, 5.0, cushion, 0.6)
    # Back: a frame panel with posts at the corners and a padded green top.
    for y in (-2.3, 2.3):
        box(sc, f"Post_{'RL'[y > 0]}", -7, -4.4, y - 0.7, y + 0.7, 2.5, 8.6, frame, 0.3)
    box(sc, "BackPanel", -6.8, -3.4, -hy + 0.4, hy - 0.4, 3.0, 8.4, frame, 0.25)
    box(sc, "BackTop", -7.1, -3.2, -hy, hy, 8.0, 10.0, cushion, 0.7)
    box(sc, "BackPiping", -7.0, -3.3, -hy + 0.1, hy - 0.1, 7.6, 8.3, cushion_dark, 0.2)
    return sc


PROPS = {"table": build_table, "chair": build_chair}


def main(names):
    OUT.mkdir(parents=True, exist_ok=True)
    written = []
    for name in names:
        sc = PROPS[name]()
        path = OUT / f"{name}.blend"
        bpy.data.libraries.write(str(path), {sc}, fake_user=False, compress=True)
        written.append(str(path))
    return written


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    print("wrote", main(argv or list(PROPS)))
