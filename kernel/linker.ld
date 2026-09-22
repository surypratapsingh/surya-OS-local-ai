/* NOVA/Nucleus linker script (K1)
 * Limine loads the ELF and maps it at the canonical higher-half base.
 * .limine_requests must land on an 8-byte aligned region containing, in order:
 * start marker -> requests + base revision tag -> end marker.
 */
OUTPUT_FORMAT(elf64-x86-64)
ENTRY(_start)

KERNEL_VIRT_BASE = 0xFFFFFFFF80000000;

SECTIONS
{
    . = KERNEL_VIRT_BASE;

    .limine_requests : ALIGN(8)
    {
        KEEP(*(.limine_requests_start))
        KEEP(*(.limine_requests))
        KEEP(*(.limine_requests_end))
    }

    .text : ALIGN(4K)
    {
        *(.text .text.*)
    }

    .rodata : ALIGN(4K)
    {
        *(.rodata .rodata.*)
    }

    .data : ALIGN(4K)
    {
        *(.data .data.*)
    }

    .bss : ALIGN(4K)
    {
        *(.bss .bss.*)
        *(COMMON)
    }

    /DISCARD/ :
    {
        *(.eh_frame .eh_frame_hdr .note .note.* .comment .debug_* .interp)
    }
}
