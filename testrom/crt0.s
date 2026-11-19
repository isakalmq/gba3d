@ Minimal GBA startup: cartridge header, stacks, .data/.bss init, jump to main.
    .section .text.start, "ax"
    .arm
    .global _start
_start:
    b       reset
    .fill   156, 1, 0           @ Nintendo logo (not checked by mGBA's HLE BIOS)
    .ascii  "SHADOWTEST\0\0"    @ game title (12 bytes)
    .ascii  "STST"              @ game code
    .ascii  "00"                @ maker code
    .byte   0x96                @ fixed value
    .byte   0                   @ unit code
    .byte   0                   @ device type
    .fill   7, 1, 0             @ reserved
    .byte   0                   @ software version
    .byte   0                   @ header checksum (patched by fixheader.py)
    .fill   2, 1, 0             @ reserved

reset:
    mov     r0, #0x12           @ IRQ mode
    msr     cpsr_c, r0
    ldr     sp, =0x03007FA0
    mov     r0, #0x1F           @ System mode
    msr     cpsr_c, r0
    ldr     sp, =0x03007F00

    ldr     r0, =__data_lma
    ldr     r1, =__data_start
    ldr     r2, =__data_end
1:  cmp     r1, r2
    ldrlo   r3, [r0], #4
    strlo   r3, [r1], #4
    blo     1b

    ldr     r1, =__bss_start
    ldr     r2, =__bss_end
    mov     r3, #0
2:  cmp     r1, r2
    strlo   r3, [r1], #4
    blo     2b

    ldr     r0, =main
    bx      r0
    .pool
