"""Export a .blend character to .glb for the web client.

    blender -b models/player.blend --python tools/export_glb.py -- shadow/web/models/player.glb

Exports meshes and armatures only (no cameras or lights), Y-up, with modifiers applied
(except the armature, which is exported as a skin) and every action as an animation.
"""
import sys

import bpy

out = sys.argv[sys.argv.index("--") + 1]
# The whole scene, not a selection: hidden objects (like the rig) can't be selected,
# and a mesh exported without its armature loses its skin and animation.
bpy.ops.export_scene.gltf(
    filepath=out,
    export_format="GLB",
    export_yup=True,
    export_apply=True,
    export_cameras=False,
    export_lights=False,
    export_skins=True,
    export_animations=True,
    export_animation_mode="ACTIONS",
    export_frame_range=False,
)
print("exported", out)
