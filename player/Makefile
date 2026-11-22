# Needs libmgba and SDL2 development packages:
#   sudo apt install libmgba-dev libsdl2-dev
CC ?= cc
CFLAGS ?= -O2 -Wall

shadow-play: shadow_play.c
	$(CC) $(CFLAGS) $$(sdl2-config --cflags) -o $@ $< -lmgba $$(sdl2-config --libs)

clean:
	rm -f shadow-play

.PHONY: clean
