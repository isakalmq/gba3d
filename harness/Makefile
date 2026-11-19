# Needs the libmgba development package:  sudo apt install libmgba-dev
CC ?= cc
CFLAGS ?= -O2 -Wall

shadow-run: shadow_run.c
	$(CC) $(CFLAGS) -o $@ $< -lmgba

clean:
	rm -f shadow-run

.PHONY: clean
