# Builds shadowtest.gba with the stock Ubuntu/Debian toolchain:
#   sudo apt install gcc-arm-none-eabi
PREFIX ?= arm-none-eabi-
CC      = $(PREFIX)gcc
OBJCOPY = $(PREFIX)objcopy
CFLAGS  = -mcpu=arm7tdmi -mthumb -mthumb-interwork -O2 -ffreestanding -fno-builtin -Wall -Wextra
LDFLAGS = -mcpu=arm7tdmi -nostdlib -T gba.ld -Wl,--no-warn-rwx-segments

shadowtest.gba: shadowtest.elf
	$(OBJCOPY) -O binary $< $@
	python3 fixheader.py $@

shadowtest.elf: crt0.o main.o gba.ld
	$(CC) $(LDFLAGS) -o $@ crt0.o main.o -lgcc

crt0.o: crt0.s
	$(CC) -mcpu=arm7tdmi -c -o $@ $<

main.o: main.c
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -f *.o *.elf shadowtest.gba

.PHONY: clean
