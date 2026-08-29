# Building the mirror-flasher (clang MIPS cross-compiler)

## Toolchain (all present now)
- clang 21.1.8 (apt) as MIPS cross-compiler
- binutils-mips-linux-gnu: mips-linux-gnu-as/ld/objdump/nm/objcopy

## Working recipe (bare-metal -nostdlib MIPS-I big-endian o32)
clang -target mips-linux-gnu -mips1 -EB -O2 -nostdlib -fno-common \
      -fno-pic -fno-pie -static -fno-builtin -ffreestanding \
      -Wl,-T,source/mirror-flash.ld -o OUT SRC.c

Key fixes discovered:
- `-fno-builtin -ffreestanding`   : stops clang turning slen() loop into libc strlen()
- custom linker script            : avoids .MIPS.abiflags/.reginfo overlapping -Ttext=0x400000
  (mirrors shipped section order: .text@0x400000 then abiflags/reginfo/rodata/data/bss)

Output profile matches shipped bank2-safe-flash-v2-block:
  ELF32 MSB MIPS R3000 (mips1) o32 static, entry 0x400000, no undefined syms

## Verified gate in binary
0x4000c0 li at,50 ; 0x4000c4 lbu v0,0(sp) ; 0x4000cc bne v0,at -> REFUSING
  => flasher refuses unless /proc/bootbank first byte == '2' (running from protected Bank2)

## Mirror safety (same safety model, banks swapped)
- writes ONLY idle Bank1: rootfs /dev/mtdblock1, kernel /dev/mtdblock0
- requires running from protected Bank2 (never overwrites running bank)
- rootfs-first, kernel-LAST; exact-size checks; block-device probe; byte-for-byte verify; sync
