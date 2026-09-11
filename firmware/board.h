/* SPDX-License-Identifier: MIT */
/* board.h - PCAP04_Acquisition_3CH v1.5 (100 mm, C496550) pin map.
 * Derived directly from the v1.5 KiCad netlist; see the design report,
 * Table "Microcontroller pin allocation".
 */
#ifndef BOARD_H
#define BOARD_H
#include "stm32g0b1.h"

#define BOARD_NAME  "PCAP04_Acquisition_3CH v1.5 / 100mm / C496550"
#define FW_NAME     "diag+stdfw+stream+usb"
#define FW_VERSION  "3.3.0"

#define SYSCLK_HZ   64000000u
#define PCLK_HZ     64000000u
#define UART_BAUD   115200u

/* ---- port A ------------------------------------------------------------ */
#define PIN_PWR_SENSE   0    /* PA0  ADC_IN0  <- RPS1/RPS2 divider on +3V3P  */
#define PIN_UART_TX     2    /* PA2  AF1 USART2_TX  -> J10 pin 2             */
#define PIN_UART_RX     3    /* PA3  AF1 USART2_RX  <- J10 pin 3             */
#define PIN_SPI_SCK     5    /* PA5  AF0 SPI1_SCK   -> U6 A1                 */
#define PIN_SPI_MISO    6    /* PA6  AF0 SPI1_MISO  <- U6 Y4                 */
#define PIN_SPI_MOSI    7    /* PA7  AF0 SPI1_MOSI  -> U6 A2                 */
#define AF_USART2       1u
#define AF_SPI1         0u

/* ---- port B ------------------------------------------------------------ */
#define PIN_PCAP_CS     0    /* PB0  -> U6 A3 -> RCS1 -> U1 SSN, active low  */
#define PIN_PCAP_IRQ    1    /* PB1  <- RIRQ1 <- U1 PG5/INTN, active LOW     */
#define PIN_PCAP_PWR_EN 2    /* PB2  -> U5 ON.  HIGH = +3V3P on              */
#define PIN_SPI_OE_N    10   /* PB10 -> U6 1..4OE.  HIGH = bus tri-stated    */
#define PIN_HOST_READY  11   /* PB11 -> J10 pin 4                            */
#define PIN_MEAS_BUSY   12   /* PB12 -> J10 pin 5                            */

/* ---- +3V3P rail monitor ------------------------------------------------ *
 * RPS1 = 330k from +3V3P, RPS2 = 100k to GND, C15 = 10 nF.
 * V(+3V3P) = V(PA0) * (330 + 100) / 100 = V(PA0) * 4.30
 * Source impedance 330k||100k = 76.7 kohm -> longest ADC sample time.       */
/* MEASURED ON THE v1.5 BOARD: RPS1 is fitted as 33k, not the 330k in the BOM.
 * PA0 reads 2.485 V with the rail at 3.30 V  ->  ratio (33+100)/100 = 1.33.
 * The design value would be 430/100.  Override at runtime with 'div n d'.   */
#define RAIL_DIV_NUM    133u
#define RAIL_DIV_DEN    100u
#define RAIL_DIV_NUM_DESIGN 430u
#define RAIL_ON_MV      3000u   /* rail considered up above this             */
#define RAIL_OFF_MV     150u    /* ADC offset x divider ratio sets the floor */

/* ---- PCAP04 serial interface (datasheet SC-001050-DS-6, table 76) ------ *
 * SPI mode 1 (CPOL = 0, CPHA = 1), MSB first.                              */
#define PCAP_OP_WR_CFG   0xA3u   /* 0xA3, 0xC0|addr, data                    */
#define PCAP_OP_RD_CFG   0x23u   /* 0x23, 0xC0|addr, -> data                 */
#define PCAP_OP_RD_RES   0x40u   /* 0x40|addr, -> data                       */
#define PCAP_OP_WR_MEM   0xA0u   /* 0xA0|addr[9:8], addr[7:0], data          */
#define PCAP_OP_RD_MEM   0x20u   /* 0x20|addr[9:8], addr[7:0], -> data       */
#define PCAP_OP_POR      0x88u
#define PCAP_OP_INIT     0x8Au
#define PCAP_OP_CDC_START 0x8Cu
#define PCAP_OP_RDC_START 0x8Eu
#define PCAP_OP_DSP_TRIG  0x8Du
#define PCAP_TESTREAD    0x7Eu   /* == RD_RES addr 62; must return 0x11      */
#define PCAP_SCRATCH_CFG 0x10u   /* FULLCHARGE_TIME: plain data, no side effects */
#define PCAP_SCRATCH_MEM 0x3FFu  /* top of the 1 kB SRAM                     */
#define PCAP_TESTREAD_OK 0x11u
#define PCAP_POWERUP_MS  10u     /* datasheet: operational 4 ms after power  */

#endif /* BOARD_H */
