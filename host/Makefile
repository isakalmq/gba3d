# Needs the libmgba development package:  sudo apt install libmgba-dev
CC ?= cc
CFLAGS ?= -O2 -Wall

libgbahost.so: gbahost.c
	$(CC) $(CFLAGS) -shared -fPIC -o $@ $< -lmgba

clean:
	rm -f libgbahost.so

.PHONY: clean
