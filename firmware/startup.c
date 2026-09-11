/* SPDX-License-Identifier: MIT */
/* startup.c - reset vector, C runtime init and vector table for STM32G0B1CB */
#include <stdint.h>
#include "stm32g0b1.h"

extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss, _estack;
extern int main(void);

void Reset_Handler(void);
void Default_Handler(void);

/* Every fault lands here.  Kept as a tight loop so a debugger attached over
 * SWD stops on a recognisable address instead of running away.             */
void Default_Handler(void) { for (;;) { __asm volatile("nop"); } }

#define ALIAS __attribute__((weak, alias("Default_Handler")))
void NMI_Handler(void)        ALIAS;
void HardFault_Handler(void)  ALIAS;
void SVC_Handler(void)        ALIAS;
void PendSV_Handler(void)     ALIAS;
void SysTick_Handler(void)    ALIAS;

/* Cortex-M0+ : 16 system entries followed by 32 device IRQs (RM0444 tab. 63) */
__attribute__((section(".isr_vector"), used))
void (* const g_vectors[16 + 32])(void) = {
    (void (*)(void))&_estack,
    Reset_Handler, NMI_Handler, HardFault_Handler,
    0, 0, 0, 0, 0, 0, 0,
    SVC_Handler, 0, 0, PendSV_Handler, SysTick_Handler,
    /* IRQ0..IRQ31 - unused in the diagnostic firmware */
    [16 ... 47] = Default_Handler,
};

void Reset_Handler(void)
{
    uint32_t *src = &_sidata, *dst = &_sdata;
    while (dst < &_edata) { *dst++ = *src++; }
    for (dst = &_sbss; dst < &_ebss; ) { *dst++ = 0u; }
    main();
    for (;;) { __asm volatile("wfi"); }
}
