    .set noreorder
    .set nomacro
    .text
    .globl _start
    .ent _start
_start:
    lw      $t0, 0($sp)         # argc
    addiu   $t1, $zero, 2
    bne     $t0, $t1, usage
    nop
    lw      $a0, 8($sp)         # argv[1]
    addiu   $a1, $zero, 493     # 0755 octal = 493 decimal
    addiu   $v0, $zero, 4015    # __NR_chmod (MIPS o32)
    syscall
    nop
    bne     $a3, $zero, fail
    nop
    addu    $a0, $zero, $zero
    addiu   $v0, $zero, 4001    # exit(0)
    syscall
    nop
fail:
    addiu   $a0, $zero, 1
    addiu   $v0, $zero, 4001
    syscall
    nop
usage:
    addiu   $a0, $zero, 2
    addiu   $v0, $zero, 4001
    syscall
    nop
    .end _start
