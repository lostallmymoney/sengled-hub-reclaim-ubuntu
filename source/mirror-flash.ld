OUTPUT_FORMAT(elf32-tradbigmips)
ENTRY(_start)
SECTIONS
{
  . = 0x400000;
  .text : { *(.text) }
  .MIPS.abiflags : { *(.MIPS.abiflags) }
  .reginfo : { *(.reginfo) }
  .rodata : { *(.rodata .rodata.*) }
  .data : { *(.data) }
  .got : { *(.got) }
  .bss : { *(.bss) *(COMMON) }
}
