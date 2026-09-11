/* STM32G0B1CBT6 : 128 KB flash @ 0x08000000, 144 KB SRAM @ 0x20000000.
 * SRAM is declared as 128 KB so the image is valid whether or not the
 * RAM_PARITY_CHECK option bit reserves the parity block.                    */
MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 128K
  RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
}

_estack = ORIGIN(RAM) + LENGTH(RAM);
_Min_Stack_Size = 0x800;

ENTRY(Reset_Handler)

SECTIONS
{
  .isr_vector : { KEEP(*(.isr_vector)) . = ALIGN(4); } >FLASH

  .text : {
    . = ALIGN(4);
    *(.text) *(.text*)
    *(.rodata) *(.rodata*)
    *(.glue_7) *(.glue_7t) *(.eh_frame)
    KEEP(*(.init)) KEEP(*(.fini))
    . = ALIGN(4);
    _etext = .;
  } >FLASH

  .ARM.extab : { *(.ARM.extab* .gnu.linkonce.armextab.*) } >FLASH
  .ARM       : { __exidx_start = .; *(.ARM.exidx*) __exidx_end = .; } >FLASH

  /* SWD console mailbox: fixed at the very start of RAM (0x20000000) so the
   * host tool can find it without symbol lookup.  NOLOAD - firmware fills it. */
  .mailbox (NOLOAD) : { . = ALIGN(4); KEEP(*(.mailbox)) . = ALIGN(4); } >RAM

  /* survives a system reset (only a power cycle randomises it): used for the
   * double-tap-reset escape hatch.  Must NOT be cleared by the C runtime.   */
  .noinit (NOLOAD)  : { . = ALIGN(4); KEEP(*(.noinit))  . = ALIGN(4); } >RAM

  _sidata = LOADADDR(.data);
  .data : {
    . = ALIGN(4);
    _sdata = .;
    *(.data) *(.data*)
    . = ALIGN(4);
    _edata = .;
  } >RAM AT> FLASH

  . = ALIGN(4);
  .bss : {
    _sbss = .;
    __bss_start__ = _sbss;
    *(.bss) *(.bss*) *(COMMON)
    . = ALIGN(4);
    _ebss = .;
    __bss_end__ = _ebss;
  } >RAM

  ._user_heap_stack : {
    . = ALIGN(8);
    . = . + _Min_Stack_Size;
    . = ALIGN(8);
  } >RAM

  .ARM.attributes 0 : { *(.ARM.attributes) }
}
