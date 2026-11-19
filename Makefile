PYTHON ?= python3
CAPTURES ?= build/captures

all: rom harness

rom:
	$(MAKE) -C testrom

harness:
	$(MAKE) -C harness

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
	rm -rf build

.PHONY: all rom harness selftest test clean
