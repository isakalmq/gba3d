PYTHON ?= python3
CAPTURES ?= build/captures
BLENDER ?= blender

all: rom harness

rom:
	$(MAKE) -C testrom

harness:
	$(MAKE) -C harness

# Optional: the libmgba + SDL2 player that ./start.py launches (needs libsdl2-dev).
player:
	$(MAKE) -C player

# Re-export the Blender models the web client loads (make models BLENDER=/path/to/blender).
# Props (models/props/*.blend, made by tools/model_props.py) become prop_<name>.glb.
PROPS = $(patsubst models/props/%.blend,shadow/web/models/prop_%.glb,$(wildcard models/props/*.blend))
models: shadow/web/models/player.glb shadow/web/models/mom.glb $(PROPS)

shadow/web/models/prop_%.glb: models/props/%.blend tools/export_glb.py
	$(BLENDER) -b $< --python tools/export_glb.py -- $@

shadow/web/models/%.glb: models/%.blend tools/export_glb.py
	$(BLENDER) -b $< --python tools/export_glb.py -- $@

# Rebuild the prop .blend files from code (overwrites hand edits to them).
props-blend:
	$(BLENDER) -b --python tools/model_props.py

# Capture ~120 frames of the test ROM headlessly, then compare the renderer against mGBA.
selftest: all
	rm -rf $(CAPTURES) && mkdir -p $(CAPTURES)
	SHADOW_STREAM=0 SHADOW_CAPTURE_DIR=$(CAPTURES) SHADOW_CAPTURE_EVERY=23 SHADOW_CAPTURE_COUNT=0 \
		harness/shadow-run testrom/shadowtest.gba lua/shadow_bridge.lua 2800
	$(PYTHON) -m shadow verify $(CAPTURES)

test:
	$(PYTHON) -m pytest -q

clean:
	$(MAKE) -C testrom clean
	$(MAKE) -C harness clean
	$(MAKE) -C player clean
	rm -rf build

.PHONY: all rom harness player models props-blend selftest test clean
