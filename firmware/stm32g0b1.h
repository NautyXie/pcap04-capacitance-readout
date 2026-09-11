/* SPDX-License-Identifier: MIT */
/* stm32g0b1.h - minimal register definitions for STM32G0B1CBT6
 * Hand-written from RM0444 Rev 6 and DS13560 Rev 6. No vendor HAL required.
 * Every offset/bit in this file was cross-checked against the reference manual.
 */
#ifndef STM32G0B1_H
#define STM32G0B1_H
#include <stdint.h>

#define __IO volatile

/* ---------- RCC (RM0444 5.4) ---------- */
#define RCC_BASE        0x40021000UL
#define RCC_CR          (*(__IO uint32_t *)(RCC_BASE + 0x00))
#define RCC_ICSCR       (*(__IO uint32_t *)(RCC_BASE + 0x04))
#define RCC_CFGR        (*(__IO uint32_t *)(RCC_BASE + 0x08))
#define RCC_PLLCFGR     (*(__IO uint32_t *)(RCC_BASE + 0x0C))
#define RCC_IOPENR      (*(__IO uint32_t *)(RCC_BASE + 0x34))
#define RCC_AHBENR      (*(__IO uint32_t *)(RCC_BASE + 0x38))
#define RCC_APBENR1     (*(__IO uint32_t *)(RCC_BASE + 0x3C))
#define RCC_APBENR2     (*(__IO uint32_t *)(RCC_BASE + 0x40))
#define RCC_CCIPR       (*(__IO uint32_t *)(RCC_BASE + 0x54))
/* G0B1 inserts CCIPR2 at 0x58, which pushes BDCR to 0x5C and CSR to 0x60 -
 * the CSR offset already in use here confirms this layout on this part. */
#define RCC_CCIPR2      (*(__IO uint32_t *)(RCC_BASE + 0x58))
#define RCC_CSR         (*(__IO uint32_t *)(RCC_BASE + 0x60))

#define RCC_CR_HSION    (1UL << 8)
#define RCC_CR_HSIRDY   (1UL << 10)
#define RCC_CR_PLLON    (1UL << 24)
#define RCC_CR_PLLRDY   (1UL << 25)
#define RCC_CR_HSI48ON  (1UL << 22)
#define RCC_CR_HSI48RDY (1UL << 23)
#define RCC_CCIPR2_USBSEL (3UL << 12)   /* 00 = HSI48 */

#define RCC_CFGR_SW_Msk   (7UL << 0)
#define RCC_CFGR_SW_PLL   (2UL << 0)     /* 010 = PLLRCLK */
#define RCC_CFGR_SWS_Msk  (7UL << 3)
#define RCC_CFGR_SWS_PLL  (2UL << 3)

/* PLLCFGR: PLLR[31:29] (001=/2), PLLREN[28], PLLN[14:8], PLLM[6:4] (000=/1),
 * PLLSRC[1:0] (10=HSI16).  VCO must land in 96..344 MHz.                     */
#define RCC_PLLCFGR_PLLSRC_HSI16 (2UL << 0)
#define RCC_PLLCFGR_PLLM(v)      (((uint32_t)(v)) << 4)   /* 0 => /1          */
#define RCC_PLLCFGR_PLLN(v)      (((uint32_t)(v)) << 8)   /* 8..86            */
#define RCC_PLLCFGR_PLLREN       (1UL << 28)
#define RCC_PLLCFGR_PLLR(v)      (((uint32_t)(v)) << 29)  /* 1 => /2          */

#define RCC_IOPENR_GPIOAEN (1UL << 0)
#define RCC_IOPENR_GPIOBEN (1UL << 1)
#define RCC_APBENR1_USART2EN (1UL << 17)
#define RCC_APBENR1_USBEN    (1UL << 13)
#define RCC_APBENR1_CRSEN    (1UL << 16)
#define RCC_APBENR2_SPI1EN   (1UL << 12)
#define RCC_APBENR2_ADCEN    (1UL << 20)

#define RCC_CSR_RMVF      (1UL << 23)
#define RCC_CSR_OBLRSTF   (1UL << 25)
#define RCC_CSR_PINRSTF   (1UL << 26)
#define RCC_CSR_PWRRSTF   (1UL << 27)
#define RCC_CSR_SFTRSTF   (1UL << 28)
#define RCC_CSR_IWDGRSTF  (1UL << 29)
#define RCC_CSR_WWDGRSTF  (1UL << 30)
#define RCC_CSR_LPWRRSTF  (1UL << 31)

/* ---------- FLASH (RM0444 3.7) ---------- */
#define FLASH_BASE      0x40022000UL
#define FLASH_ACR       (*(__IO uint32_t *)(FLASH_BASE + 0x00))
#define FLASH_ACR_LATENCY_Msk (7UL << 0)
#define FLASH_ACR_LATENCY_2WS (2UL << 0)   /* required 48 < HCLK <= 64 MHz    */
#define FLASH_ACR_PRFTEN      (1UL << 8)
#define FLASH_ACR_ICEN        (1UL << 9)

/* ---------- GPIO ---------- */
typedef struct {
    __IO uint32_t MODER, OTYPER, OSPEEDR, PUPDR, IDR, ODR, BSRR, LCKR, AFR[2], BRR;
} GPIO_TypeDef;
#define GPIOA ((GPIO_TypeDef *)0x50000000UL)
#define GPIOB ((GPIO_TypeDef *)0x50000400UL)

#define GPIO_MODE_IN     0u
#define GPIO_MODE_OUT    1u
#define GPIO_MODE_AF     2u
#define GPIO_MODE_ANALOG 3u
#define GPIO_PULL_NONE   0u
#define GPIO_PULL_UP     1u
#define GPIO_PULL_DOWN   2u

/* ---------- USART (RM0444 34.8) ---------- */
typedef struct {
    __IO uint32_t CR1, CR2, CR3, BRR, GTPR, RTOR, RQR, ISR, ICR, RDR, TDR;
} USART_TypeDef;
#define USART2 ((USART_TypeDef *)0x40004400UL)
#define USART_CR1_UE  (1UL << 0)
#define USART_CR1_RE  (1UL << 2)
#define USART_CR1_TE  (1UL << 3)
#define USART_ISR_RXNE (1UL << 5)
#define USART_ISR_TC   (1UL << 6)
#define USART_ISR_TXE  (1UL << 7)
#define USART_ISR_ORE  (1UL << 3)
#define USART_ICR_ORECF (1UL << 3)

/* ---------- SPI (RM0444 35.9) ---------- */
typedef struct {
    __IO uint32_t CR1, CR2, SR, DR, CRCPR, RXCRCR, TXCRCR, I2SCFGR, I2SPR;
} SPI_TypeDef;
#define SPI1 ((SPI_TypeDef *)0x40013000UL)
#define SPI_CR1_CPHA     (1UL << 0)
#define SPI_CR1_CPOL     (1UL << 1)
#define SPI_CR1_MSTR     (1UL << 2)
#define SPI_CR1_BR_Pos   3
#define SPI_CR1_SPE      (1UL << 6)
#define SPI_CR1_SSI      (1UL << 8)
#define SPI_CR1_SSM      (1UL << 9)
#define SPI_CR2_DS_8BIT  (7UL << 8)
#define SPI_CR2_FRXTH    (1UL << 12)
#define SPI_SR_RXNE      (1UL << 0)
#define SPI_SR_TXE       (1UL << 1)
#define SPI_SR_BSY       (1UL << 7)

/* ---------- ADC (RM0444 15.13) ---------- */
typedef struct {
    __IO uint32_t ISR, IER, CR, CFGR1, CFGR2, SMPR;
    __IO uint32_t RESERVED0[2];
    __IO uint32_t AWD1TR, AWD2TR, CHSELR, AWD3TR;
    __IO uint32_t RESERVED1[4];
    __IO uint32_t DR;
} ADC_TypeDef;
#define ADC1     ((ADC_TypeDef *)0x40012400UL)
#define ADC_CCR  (*(__IO uint32_t *)(0x40012400UL + 0x308))
#define ADC_ISR_ADRDY  (1UL << 0)
#define ADC_ISR_EOC    (1UL << 2)
#define ADC_ISR_CCRDY  (1UL << 13)
#define ADC_CR_ADEN     (1UL << 0)
#define ADC_CR_ADDIS    (1UL << 1)
#define ADC_CR_ADSTART  (1UL << 2)
#define ADC_CR_ADVREGEN (1UL << 28)
#define ADC_CR_ADCAL    (1UL << 31)
#define ADC_CFGR2_CKMODE_PCLK4 (2UL << 30)
#define ADC_SMPR_SMP1_160C5    (7UL << 0)   /* 160.5 ADC clocks               */
#define ADC_CCR_VREFEN  (1UL << 22)
#define ADC_CH_VREFINT  13u

/* ---------- SYSCFG (RM0444 8.1) ---------- */
#define SYSCFG_BASE  0x40010000UL
#define SYSCFG_CFGR1 (*(__IO uint32_t *)(SYSCFG_BASE + 0x00))
#define SYSCFG_MEM_MODE_SYSTEM 1UL          /* 01 = system flash @ 0x00000000 */
#define RCC_APBENR2_SYSCFGEN   (1UL << 0)
#define SYSTEM_MEMORY_BASE     0x1FFF0000UL /* ST ROM bootloader, 28 KB       */

/* ---------- Cortex-M0+ core ---------- */
#define SYST_CSR   (*(__IO uint32_t *)0xE000E010UL)
#define SYST_RVR   (*(__IO uint32_t *)0xE000E014UL)
#define SYST_CVR   (*(__IO uint32_t *)0xE000E018UL)
#define NVIC_ICER0 (*(__IO uint32_t *)0xE000E180UL)
#define NVIC_ICPR0 (*(__IO uint32_t *)0xE000E280UL)
#define SCB_VTOR   (*(__IO uint32_t *)0xE000ED08UL)
#define SCB_AIRCR  (*(__IO uint32_t *)0xE000ED0CUL)
#define SCB_AIRCR_SYSRESETREQ 0x05FA0004UL
#define DBGMCU_IDCODE (*(__IO uint32_t *)0x40015800UL)

/* ---------- factory data (DS13560 / RM0444 41) ---------- */
#define UID_BASE          0x1FFF7590UL
#define FLASHSIZE_BASE    0x1FFF75E0UL   /* uint16, kbytes                    */
#define VREFINT_CAL_ADDR  0x1FFF75AAUL   /* uint16, taken at VDDA = 3.0 V     */
#define VREFINT_CAL_VREF  3000u          /* mV                                */

#endif /* STM32G0B1_H */
