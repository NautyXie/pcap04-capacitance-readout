/* SPDX-License-Identifier: MIT */
/* usb_cdc.h - bare-metal USB CDC-ACM device for the STM32G0B1.
 *
 * Polled, no interrupts, no vendor HAL - the same style as the rest of this
 * firmware.  Call usb_cdc_init() once, then usb_cdc_poll() often enough that
 * the host never times out (every few milliseconds is plenty); every long
 * wait loop in main.c does this.
 */
#ifndef USB_CDC_H
#define USB_CDC_H

#include <stdint.h>

void usb_cdc_init(void);
void usb_cdc_poll(void);

/* Detach and power down - call before jumping to the ROM bootloader. */
void usb_cdc_detach(void);

/* Queue one byte for the host.  Never blocks: if the host is not listening
 * the byte is dropped, so a disconnected USB can never wedge the console. */
void usb_cdc_putc(char c);

/* -1 when nothing has arrived. */
int  usb_cdc_getc(void);

/* Host has opened the port (SET_CONTROL_LINE_STATE with DTR asserted). */
int  usb_cdc_ready(void);

/* Diagnostics for the 'usb' console command. */
uint32_t usb_cdc_state(void);        /* 0 detached, 1 powered, 2 addressed, 3 configured */
uint32_t usb_cdc_dropped(void);      /* bytes lost because the host was not reading */
uint32_t usb_cdc_resets(void);       /* USB bus resets seen */

#endif /* USB_CDC_H */
