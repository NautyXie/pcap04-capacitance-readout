/* SPDX-License-Identifier: MIT */
/* main.c - PCAP04 v1.5 PCAP04 control firmware
 *
 * Console is available over TWO transports simultaneously:
 *   1. a RAM mailbox read/written by the host over SWD  (no extra wires)
 *   2. USART2 on PA2/PA3 -> header J10               (115200 8N1)
 *
 * The firmware never powers the PCAP04 by itself; every rail change follows
 * the sequence written on schematic sheet P2.
 */
#include <stdint.h>
#include <stdarg.h>
#include "stm32g0b1.h"
#include "board.h"
#include "usb_cdc.h"
#include "pcap04_stdfw.h"

/* =====================================================================
 * SWD mailbox console
 * ===================================================================== */
#define MBX_MAGIC0 0x50434150u   /* "PCAP" */
#define MBX_MAGIC1 0x4D42582Du   /* "MBX-" */
#define MBX_TX_SIZE 2048u
#define MBX_RX_SIZE 256u

typedef struct {
    volatile uint32_t magic0, magic1;
    volatile uint32_t version;
    volatile uint32_t tx_size, tx_wr, tx_rd;   /* fw -> host */
    volatile uint32_t rx_size, rx_wr, rx_rd;   /* host -> fw */
    volatile uint32_t heartbeat;
    volatile uint32_t dropped;
    volatile char tx_buf[MBX_TX_SIZE];
    volatile char rx_buf[MBX_RX_SIZE];
} mailbox_t;

static mailbox_t g_mbx __attribute__((section(".mailbox"), used));

/* =====================================================================
 * time base
 * ===================================================================== */
static volatile uint32_t g_ms;
void SysTick_Handler(void) { g_ms++; }
static uint32_t now_ms(void) { return g_ms; }
/* Bounded: if SysTick is ever dead (a stale NVIC/clock state inherited from
 * the ROM bootloader, say) this still returns, so the console always comes up
 * and the board stays debuggable instead of hanging silently at boot.       */
static void delay_ms(uint32_t ms)
{
    uint32_t t0 = g_ms;
    usb_cdc_poll();
    volatile uint32_t guard = ms * 8000u + 200000u;
    while (((g_ms - t0) < ms) && guard) { guard--; usb_cdc_poll(); }
}

/* =====================================================================
 * bounded hardware waits
 *
 * Bring-up firmware must never spin forever on a peripheral flag: one
 * misbehaving block would kill the console and take the board with it.
 * Every wait below gives up after a bounded number of spins and records
 * which one failed, so a stuck peripheral becomes a message, not silence.
 * ~2e6 spins is on the order of 100 ms at 64 MHz.
 * ===================================================================== */
#define SPIN_BUDGET 2000000u

static uint32_t g_timeouts;
#define TO_HSI    (1u<<0)
#define TO_SWHSI  (1u<<1)
#define TO_PLLOFF (1u<<2)
#define TO_FLASH  (1u<<3)
#define TO_PLLON  (1u<<4)
#define TO_SWPLL  (1u<<5)
#define TO_ADCCAL (1u<<6)
#define TO_ADCRDY (1u<<7)
#define TO_CCRDY  (1u<<8)
#define TO_EOC    (1u<<9)
#define TO_SPITX  (1u<<10)
#define TO_SPIRX  (1u<<11)

static const char *const TO_NAME[12] = {
    "HSI-ready", "switch-to-HSI", "PLL-off", "flash-latency", "PLL-lock",
    "switch-to-PLL", "ADC-calibration", "ADC-ready", "ADC-channel-ready",
    "ADC-end-of-conversion", "SPI-TXE", "SPI-RXNE"
};

static int wait_for(volatile uint32_t *reg, uint32_t mask, int want_set, uint32_t flag)
{
    uint32_t spins = SPIN_BUDGET;
    while (spins--) {
        uint32_t v = (*reg) & mask;
        if (want_set ? (v != 0u) : (v == 0u)) { return 0; }
    }
    g_timeouts |= flag;
    return -1;
}

/* =====================================================================
 * clock: HSI16 -> PLL(N=8,M=1,R=2) -> 64 MHz  (RM0444 5.4.4)
 * ===================================================================== */
static void clock_init(void)
{
    /* We may have been entered by a jump from the ROM bootloader with the PLL
     * already running AS the system clock.  Disabling a PLL that is feeding
     * SYSCLK stops the CPU dead, so switch to HSI FIRST, then touch the PLL. */
    RCC_CR |= RCC_CR_HSION;
    wait_for(&RCC_CR, RCC_CR_HSIRDY, 1, TO_HSI);
    RCC_CFGR &= ~RCC_CFGR_SW_Msk;                  /* 000 = HSISYS           */
    wait_for(&RCC_CFGR, RCC_CFGR_SWS_Msk, 0, TO_SWHSI);
    RCC_CR &= ~RCC_CR_PLLON;
    wait_for(&RCC_CR, RCC_CR_PLLRDY, 0, TO_PLLOFF);

    FLASH_ACR = (FLASH_ACR & ~FLASH_ACR_LATENCY_Msk) | FLASH_ACR_LATENCY_2WS
              | FLASH_ACR_PRFTEN | FLASH_ACR_ICEN;

    /* VCO = 16 MHz * 8 = 128 MHz (must be 96..344), R = /2 -> 64 MHz */
    RCC_PLLCFGR = RCC_PLLCFGR_PLLSRC_HSI16 | RCC_PLLCFGR_PLLM(0)
                | RCC_PLLCFGR_PLLN(8) | RCC_PLLCFGR_PLLR(1) | RCC_PLLCFGR_PLLREN;

    RCC_CR |= RCC_CR_PLLON;
    if (wait_for(&RCC_CR, RCC_CR_PLLRDY, 1, TO_PLLON) == 0) {
        RCC_CFGR = (RCC_CFGR & ~RCC_CFGR_SW_Msk) | RCC_CFGR_SW_PLL;
        wait_for(&RCC_CFGR, RCC_CFGR_SWS_Msk, 1, TO_SWPLL);
    }
    /* If the PLL never locks we stay on HSI16: the console still comes up
     * instead of the board going dark.                                     */

    SYST_RVR = (SYSCLK_HZ / 1000u) - 1u;
    SYST_CVR = 0;
    SYST_CSR = 5u;                 /* enable, processor clock, no IRQ...     */
    SYST_CSR = 7u;                 /* ...then enable the tick interrupt      */
}

/* =====================================================================
 * GPIO helpers
 * ===================================================================== */
static void pin_mode(GPIO_TypeDef *g, uint32_t pin, uint32_t mode)
{ g->MODER = (g->MODER & ~(3u << (pin * 2))) | (mode << (pin * 2)); }
static void pin_pull(GPIO_TypeDef *g, uint32_t pin, uint32_t pull)
{ g->PUPDR = (g->PUPDR & ~(3u << (pin * 2))) | (pull << (pin * 2)); }
static void pin_af(GPIO_TypeDef *g, uint32_t pin, uint32_t af)
{ uint32_t i = pin >> 3, s = (pin & 7u) * 4u; g->AFR[i] = (g->AFR[i] & ~(0xFu << s)) | (af << s); }
static void pin_speed_high(GPIO_TypeDef *g, uint32_t pin)
{ g->OSPEEDR |= (2u << (pin * 2)); }
static void pin_set(GPIO_TypeDef *g, uint32_t pin) { g->BSRR = 1u << pin; }
static void pin_clr(GPIO_TypeDef *g, uint32_t pin) { g->BRR  = 1u << pin; }
static uint32_t pin_get(GPIO_TypeDef *g, uint32_t pin) { return (g->IDR >> pin) & 1u; }

/* Bring the control pins up in their SAFE state.  Order matters: the output
 * data register is written BEFORE the pin becomes an output, so the pin never
 * glitches to the wrong level.  (At MCU reset both lines are already held
 * safe by RENPD1 100k pull-down and ROEPU1 100k pull-up on the board.)      */
static void control_pins_init(void)
{
    RCC_IOPENR |= RCC_IOPENR_GPIOAEN | RCC_IOPENR_GPIOBEN;

    pin_set(GPIOB, PIN_SPI_OE_N);            /* 1 = SPI buffer tri-stated    */
    pin_mode(GPIOB, PIN_SPI_OE_N, GPIO_MODE_OUT);

    pin_clr(GPIOB, PIN_PCAP_PWR_EN);         /* 0 = +3V3P off                */
    pin_mode(GPIOB, PIN_PCAP_PWR_EN, GPIO_MODE_OUT);

    pin_set(GPIOB, PIN_PCAP_CS);             /* SSN idle high                */
    pin_mode(GPIOB, PIN_PCAP_CS, GPIO_MODE_OUT);
    pin_speed_high(GPIOB, PIN_PCAP_CS);

    pin_pull(GPIOB, PIN_PCAP_IRQ, GPIO_PULL_UP);   /* INTN is active low     */
    pin_mode(GPIOB, PIN_PCAP_IRQ, GPIO_MODE_IN);

    pin_clr(GPIOB, PIN_HOST_READY); pin_mode(GPIOB, PIN_HOST_READY, GPIO_MODE_OUT);
    pin_clr(GPIOB, PIN_MEAS_BUSY);  pin_mode(GPIOB, PIN_MEAS_BUSY,  GPIO_MODE_OUT);

    pin_mode(GPIOA, PIN_PWR_SENSE, GPIO_MODE_ANALOG);
}

/* =====================================================================
 * USART2 (PA2/PA3, AF1) -> J10
 * ===================================================================== */
static void uart_init(void)
{
    RCC_APBENR1 |= RCC_APBENR1_USART2EN;
    pin_af(GPIOA, PIN_UART_TX, AF_USART2); pin_mode(GPIOA, PIN_UART_TX, GPIO_MODE_AF);
    pin_af(GPIOA, PIN_UART_RX, AF_USART2); pin_mode(GPIOA, PIN_UART_RX, GPIO_MODE_AF);
    pin_pull(GPIOA, PIN_UART_RX, GPIO_PULL_UP);
    USART2->CR1 = 0;
    USART2->BRR = (PCLK_HZ + UART_BAUD / 2u) / UART_BAUD;
    USART2->CR1 = USART_CR1_UE | USART_CR1_TE | USART_CR1_RE;
}
static void uart_putc(char c)
{
    uint32_t guard = 200000u;
    while (!(USART2->ISR & USART_ISR_TXE) && guard--) { }
    USART2->TDR = (uint8_t)c;
}
static int uart_getc(void)
{
    if (USART2->ISR & USART_ISR_ORE) { USART2->ICR = USART_ICR_ORECF; }
    if (USART2->ISR & USART_ISR_RXNE) { return (int)(USART2->RDR & 0xFFu); }
    return -1;
}

/* =====================================================================
 * console output: mailbox + UART
 * ===================================================================== */
static void mbx_putc(char c)
{
    uint32_t wr = g_mbx.tx_wr, nxt = (wr + 1u) % MBX_TX_SIZE;
    if (nxt == g_mbx.tx_rd) { g_mbx.dropped++; return; }   /* host too slow  */
    g_mbx.tx_buf[wr] = c;
    g_mbx.tx_wr = nxt;
}
static void cputc(char c)
{
    if (c == '\n') { mbx_putc('\r'); uart_putc('\r'); usb_cdc_putc('\r'); }
    mbx_putc(c); uart_putc(c); usb_cdc_putc(c);
}
static void cputs(const char *s) { while (*s) { cputc(*s++); } }

static void put_u32(uint32_t v, uint32_t base, uint32_t width, char pad)
{
    char buf[12]; uint32_t n = 0;
    if (v == 0) { buf[n++] = '0'; }
    while (v) { uint32_t d = v % base; buf[n++] = (char)(d < 10 ? '0' + d : 'A' + d - 10); v /= base; }
    while (n < width) { buf[n++] = pad; }
    while (n) { cputc(buf[--n]); }
}
static void cprintf(const char *fmt, ...)
{
    va_list ap; va_start(ap, fmt);
    while (*fmt) {
        if (*fmt != '%') { cputc(*fmt++); continue; }
        fmt++;
        char pad = ' '; uint32_t width = 0;
        if (*fmt == '0') { pad = '0'; fmt++; }
        while (*fmt >= '0' && *fmt <= '9') { width = width * 10u + (uint32_t)(*fmt++ - '0'); }
        switch (*fmt++) {
        case 'u': put_u32(va_arg(ap, uint32_t), 10, width, pad); break;
        case 'd': { int32_t v = va_arg(ap, int32_t);
                    if (v < 0) { cputc('-'); v = -v; }
                    put_u32((uint32_t)v, 10, width, pad); } break;
        case 'x': case 'X': put_u32(va_arg(ap, uint32_t), 16, width, pad); break;
        case 'c': cputc((char)va_arg(ap, int)); break;
        case 's': cputs(va_arg(ap, const char *)); break;
        case '%': cputc('%'); break;
        default: break;
        }
    }
    va_end(ap);
}
/* millivolts -> "3.297 V" */
static void print_mv(uint32_t mv) { put_u32(mv / 1000u, 10, 0, ' '); cputc('.'); put_u32(mv % 1000u, 10, 3, '0'); cputs(" V"); }

/* =====================================================================
 * ADC: +3V3P rail monitor via RPS1/RPS2, referenced to VREFINT
 * ===================================================================== */
static void adc_init(void)
{
    RCC_APBENR2 |= RCC_APBENR2_ADCEN;
    ADC1->CR = ADC_CR_ADVREGEN;
    delay_ms(2);                                   /* >> tADCVREG_STUP 20 us */
    ADC1->CR = ADC_CR_ADVREGEN | ADC_CR_ADCAL;
    wait_for(&ADC1->CR, ADC_CR_ADCAL, 0, TO_ADCCAL);
    delay_ms(1);
    ADC1->CFGR1 = 0;                               /* 12-bit, single shot    */
    ADC1->CFGR2 = ADC_CFGR2_CKMODE_PCLK4;          /* 64/4 = 16 MHz          */
    ADC1->SMPR  = ADC_SMPR_SMP1_160C5;             /* 160.5 clk ~ 10 us      */
    ADC_CCR |= ADC_CCR_VREFEN;
    delay_ms(1);
    ADC1->ISR = ADC_ISR_ADRDY;
    ADC1->CR |= ADC_CR_ADEN;
    wait_for(&ADC1->ISR, ADC_ISR_ADRDY, 1, TO_ADCRDY);
}
static uint16_t adc_read(uint32_t ch)
{
    ADC1->ISR = ADC_ISR_CCRDY;
    ADC1->CHSELR = 1u << ch;
    if (wait_for(&ADC1->ISR, ADC_ISR_CCRDY, 1, TO_CCRDY) != 0) { return 0; }
    ADC1->ISR = ADC_ISR_EOC;
    ADC1->CR |= ADC_CR_ADSTART;
    if (wait_for(&ADC1->ISR, ADC_ISR_EOC, 1, TO_EOC) != 0) { return 0; }
    return (uint16_t)ADC1->DR;
}
static uint32_t vdda_mv(void)
{
    uint16_t cal = *(volatile uint16_t *)VREFINT_CAL_ADDR;
    uint16_t raw = adc_read(ADC_CH_VREFINT);
    if (raw == 0 || cal == 0 || cal == 0xFFFF) { return 0; }
    return (uint32_t)VREFINT_CAL_VREF * cal / raw;
}
/* V(+3V3P) = V(PA0) * (RPS1+RPS2)/RPS2.  See the note in board.h: this board
 * has 33k fitted for RPS1, so the ratio is 1.33 and not the 4.30 of the BOM. */
static uint32_t g_div_num = RAIL_DIV_NUM;
static uint32_t g_div_den = RAIL_DIV_DEN;

static uint32_t rail_mv(void)
{
    uint32_t vdda = vdda_mv();
    uint32_t raw, pa0;
    if (vdda == 0) { return 0; }
    raw = adc_read(PIN_PWR_SENSE);
    pa0 = vdda * raw / 4095u;
    return pa0 * g_div_num / g_div_den;
}

/* =====================================================================
 * SPI1 -> U6 buffer -> PCAP04.  Mode 1 (CPOL=0, CPHA=1), MSB first.
 * ===================================================================== */
static uint32_t g_spi_br = 5u;                     /* PCLK/64 = 1 MHz        */
static void spi_init(void)
{
    RCC_APBENR2 |= RCC_APBENR2_SPI1EN;
    pin_af(GPIOA, PIN_SPI_SCK,  AF_SPI1); pin_mode(GPIOA, PIN_SPI_SCK,  GPIO_MODE_AF); pin_speed_high(GPIOA, PIN_SPI_SCK);
    pin_af(GPIOA, PIN_SPI_MOSI, AF_SPI1); pin_mode(GPIOA, PIN_SPI_MOSI, GPIO_MODE_AF); pin_speed_high(GPIOA, PIN_SPI_MOSI);
    pin_af(GPIOA, PIN_SPI_MISO, AF_SPI1); pin_mode(GPIOA, PIN_SPI_MISO, GPIO_MODE_AF);
    pin_pull(GPIOA, PIN_SPI_MISO, GPIO_PULL_UP);   /* Y4 is Hi-Z when OE_N=1 */
    SPI1->CR1 = 0;
    SPI1->CR2 = SPI_CR2_DS_8BIT | SPI_CR2_FRXTH;
    SPI1->CR1 = SPI_CR1_MSTR | SPI_CR1_SSM | SPI_CR1_SSI
              | SPI_CR1_CPHA                        /* CPHA=1, CPOL=0 = mode1*/
              | (g_spi_br << SPI_CR1_BR_Pos) | SPI_CR1_SPE;
}
static void spi_setbr(uint32_t br)
{
    g_spi_br = br & 7u;
    SPI1->CR1 &= ~SPI_CR1_SPE;
    SPI1->CR1 = (SPI1->CR1 & ~(7u << SPI_CR1_BR_Pos)) | (g_spi_br << SPI_CR1_BR_Pos);
    SPI1->CR1 |= SPI_CR1_SPE;
}
static uint8_t spi_xfer(uint8_t v)
{
    if (wait_for(&SPI1->SR, SPI_SR_TXE, 1, TO_SPITX) != 0) { return 0xFFu; }
    *(volatile uint8_t *)&SPI1->DR = v;            /* 8-bit access is required*/
    if (wait_for(&SPI1->SR, SPI_SR_RXNE, 1, TO_SPIRX) != 0) { return 0xFFu; }
    return *(volatile uint8_t *)&SPI1->DR;
}
static void cs_lo(void) { pin_clr(GPIOB, PIN_PCAP_CS); }
static void cs_hi(void) { pin_set(GPIOB, PIN_PCAP_CS); }

/* =====================================================================
 * escape hatch: double-tap RESET (SW1) enters the ST ROM bootloader
 *
 * Once flash is programmed the chip no longer enters DFU by itself, and the
 * factory default nBOOT_SEL=1 makes the BOOT0 pin (J9-3) inert.  Without this
 * hatch a bad image plus a dead SWD link would be unrecoverable.  RAM keeps
 * its contents across a system reset, so a magic word written for the first
 * 400 ms after boot turns "reset twice quickly" into "enter the bootloader".
 * ===================================================================== */
#define BOOT_MAGIC 0xB007DEADu
static volatile uint32_t g_boot_magic __attribute__((section(".noinit"), used));

/* Set when we have uploaded the ScioSense firmware into the PCAP04's SRAM.
 * Declared here because the rail control below has to invalidate it. */
static uint8_t g_loaded = 0u;

static void safe_shutdown(void);

__attribute__((noreturn)) static void jump_to_bootloader(void)
{
    void (*sysmem)(void);
    uint32_t sp, pc;

    usb_cdc_detach();                      /* let the host see us go away    */
    { volatile uint32_t d = 400000u; while (d--) { } }
    safe_shutdown();                       /* PCAP04 rail off, SPI tri-state */

    __asm volatile("cpsid i");
    SYST_CSR = 0;                          /* stop the tick                  */

    /* back to the reset clock configuration - the ROM code expects HSI16     */
    RCC_CFGR &= ~RCC_CFGR_SW_Msk;
    wait_for(&RCC_CFGR, RCC_CFGR_SWS_Msk, 0, TO_SWHSI);
    RCC_CR &= ~RCC_CR_PLLON;
    wait_for(&RCC_CR, RCC_CR_PLLRDY, 0, TO_PLLOFF);
    FLASH_ACR = (FLASH_ACR & ~FLASH_ACR_LATENCY_Msk);

    /* map system memory at 0x00000000 so the ROM vector table is live       */
    RCC_APBENR2 |= RCC_APBENR2_SYSCFGEN;
    SYSCFG_CFGR1 = (SYSCFG_CFGR1 & ~3u) | SYSCFG_MEM_MODE_SYSTEM;
    __asm volatile("dsb 0xF" ::: "memory");
    __asm volatile("isb 0xF" ::: "memory");

    sp = *(volatile uint32_t *)(SYSTEM_MEMORY_BASE);
    pc = *(volatile uint32_t *)(SYSTEM_MEMORY_BASE + 4u);
    sysmem = (void (*)(void))pc;
    __asm volatile("msr msp, %0" :: "r"(sp));
    __asm volatile("cpsie i");
    sysmem();
    for (;;) { }
}

/* =====================================================================
 * board state + rail sequencing
 * ===================================================================== */
static int g_rail_on, g_bus_on;

static void bus_disable(void) { pin_set(GPIOB, PIN_SPI_OE_N); g_bus_on = 0; }
static void bus_enable(void)  { pin_clr(GPIOB, PIN_SPI_OE_N); g_bus_on = 1; }

/* leave the analogue side in the state the schematic calls safe */
static void safe_shutdown(void)
{
    pin_set(GPIOB, PIN_SPI_OE_N);          /* SPI bus tri-stated             */
    pin_clr(GPIOB, PIN_PCAP_PWR_EN);       /* +3V3P off                      */
    pin_set(GPIOB, PIN_PCAP_CS);
    g_rail_on = 0; g_bus_on = 0; g_loaded = 0u;
}

static int rail_on(void)
{
    uint32_t t0, mv = 0; int i;
    bus_disable();                                  /* step 1: SPI Hi-Z      */
    pin_set(GPIOB, PIN_PCAP_PWR_EN);                /* step 2: U5 ON         */
    t0 = now_ms();
    for (i = 0; i < 400; i++) {
        mv = rail_mv();
        if (mv >= RAIL_ON_MV) { break; }
        delay_ms(1);
    }
    if (mv < RAIL_ON_MV) {
        cprintf("  FAIL +3V3P only reached "); print_mv(mv); cputs(" after 400 ms\n");
        pin_clr(GPIOB, PIN_PCAP_PWR_EN);
        g_rail_on = 0;
        return -1;
    }
    i = (int)(now_ms() - t0);
    g_loaded = 0u;                      /* a fresh rail means a blank PCAP04 */
    delay_ms(PCAP_POWERUP_MS);          /* PCAP04 needs 4 ms; also lets the   */
    mv = rail_mv();                     /* C15 / divider node settle          */
    cprintf("  +3V3P up in %u ms, settled at ", (uint32_t)i); print_mv(mv); cputc('\n');
    g_rail_on = 1;
    return 0;
}
static int rail_off(void)
{
    uint32_t t0, mv = 0; int i;
    bus_disable();                                  /* steps 1-2             */
    pin_clr(GPIOB, PIN_PCAP_PWR_EN);                /* step 3: U5 OFF        */
    t0 = now_ms();
    for (i = 0; i < 1000; i++) {
        mv = rail_mv();
        if (mv <= RAIL_OFF_MV) { break; }
        delay_ms(1);
    }
    g_rail_on = 0;
    g_loaded = 0u;                      /* the PCAP04 just lost its SRAM     */
    if (mv > RAIL_OFF_MV) {
        cprintf("  FAIL +3V3P still at "); print_mv(mv); cputs(" after 1000 ms\n");
        return -1;
    }
    cprintf("  +3V3P discharged below 50 mV in %u ms\n", now_ms() - t0);
    return 0;
}

/* =====================================================================
 * PCAP04 access (opcodes from datasheet SC-001050-DS-6 table 76)
 * ===================================================================== */
/* Set when we have uploaded the ScioSense firmware into the PCAP04's SRAM.
 * Declared up here because the rail control has to invalidate it. */
static int pcap_loaded_now(void);

static int pcap_ready(void)
{
    if (!g_rail_on) { cputs("  ERR +3V3P is off - run 'rail on' first\n"); return 0; }
    if (!g_bus_on)  { cputs("  ERR SPI buffer tri-stated - run 'bus on' first\n"); return 0; }
    return 1;
}
static uint8_t pcap_test_read(void)
{ uint8_t v; cs_lo(); spi_xfer(PCAP_TESTREAD); v = spi_xfer(0x00); cs_hi(); return v; }
static uint8_t pcap_rd_cfg(uint8_t a)
{ uint8_t v; cs_lo(); spi_xfer(PCAP_OP_RD_CFG); spi_xfer(0xC0u | (a & 0x3Fu)); v = spi_xfer(0x00); cs_hi(); return v; }
static void pcap_wr_cfg(uint8_t a, uint8_t d)
{ cs_lo(); spi_xfer(PCAP_OP_WR_CFG); spi_xfer(0xC0u | (a & 0x3Fu)); spi_xfer(d); cs_hi(); }
static uint8_t pcap_rd_res(uint8_t a)
{ uint8_t v; cs_lo(); spi_xfer(PCAP_OP_RD_RES | (a & 0x3Fu)); v = spi_xfer(0x00); cs_hi(); return v; }
static void pcap_cmd(uint8_t op) { cs_lo(); spi_xfer(op); cs_hi(); }
/* NVRAM/SRAM byte access: opcode carries addr[9:8] (datasheet table 76) */
static uint8_t pcap_rd_mem(uint16_t a)
{
    uint8_t v;
    cs_lo();
    spi_xfer((uint8_t)(PCAP_OP_RD_MEM | ((a >> 8) & 3u)));
    spi_xfer((uint8_t)(a & 0xFFu));
    v = spi_xfer(0x00);
    cs_hi();
    return v;
}
static void pcap_wr_mem(uint16_t a, uint8_t d)
{
    cs_lo();
    spi_xfer((uint8_t)(PCAP_OP_WR_MEM | ((a >> 8) & 3u)));
    spi_xfer((uint8_t)(a & 0xFFu));
    spi_xfer(d);
    cs_hi();
}

/* ---- auto-incrementing burst access (datasheet 7.4: the address counter
 * advances by itself as long as SSN stays low) ------------------------- */
static void pcap_wr_mem_burst(uint16_t a, const uint8_t *d, uint32_t n)
{
    cs_lo();
    spi_xfer((uint8_t)(PCAP_OP_WR_MEM | ((a >> 8) & 3u)));
    spi_xfer((uint8_t)(a & 0xFFu));
    while (n--) { spi_xfer(*d++); }
    cs_hi();
}
/* reads back and compares in flight, so no 548-byte buffer is needed */
static uint32_t pcap_vfy_mem(uint16_t a, const uint8_t *d, uint32_t n, uint32_t *first)
{
    uint32_t bad = 0, i;
    cs_lo();
    spi_xfer((uint8_t)(PCAP_OP_RD_MEM | ((a >> 8) & 3u)));
    spi_xfer((uint8_t)(a & 0xFFu));
    for (i = 0; i < n; i++) {
        if (spi_xfer(0x00) != d[i]) { if (!bad && first) { *first = i; } bad++; }
    }
    cs_hi();
    return bad;
}
static void pcap_wr_cfg_burst(uint8_t a, const uint8_t *d, uint32_t n)
{
    cs_lo();
    spi_xfer(PCAP_OP_WR_CFG);
    spi_xfer((uint8_t)(0xC0u | (a & 0x3Fu)));
    while (n--) { spi_xfer(*d++); }
    cs_hi();
}
static uint32_t pcap_vfy_cfg(uint8_t a, const uint8_t *d, uint32_t n, uint32_t *first)
{
    uint32_t bad = 0, i;
    cs_lo();
    spi_xfer(PCAP_OP_RD_CFG);
    spi_xfer((uint8_t)(0xC0u | (a & 0x3Fu)));
    for (i = 0; i < n; i++) {
        if (spi_xfer(0x00) != d[i]) { if (!bad && first) { *first = i; } bad++; }
    }
    cs_hi();
    return bad;
}
/* Result registers are 32-bit, LSB first on the wire (PICOCAP order, see
 * ScioSense user_spi_interface.c Read_Dword_Lite).  Format is Q5.27, so the
 * capacitance ratio is raw / 2^27.                                        */
static uint32_t pcap_rd_result32(uint8_t a)
{
    uint32_t v = 0; int i;
    cs_lo();
    spi_xfer((uint8_t)(PCAP_OP_RD_RES | (a & 0x3Fu)));
    for (i = 0; i < 4; i++) { v |= ((uint32_t)spi_xfer(0x00)) << (8 * i); }
    cs_hi();
    return v;
}
/* ratio scaled to parts per million, avoiding any floating point */
static uint32_t ratio_ppm(uint32_t raw)
{ return (uint32_t)(((uint64_t)raw * 1000000ULL) >> 27); }

/* =====================================================================
 * commands
 * ===================================================================== */
static const char *reset_cause(uint32_t csr)
{
    if (csr & RCC_CSR_LPWRRSTF) return "low-power";
    if (csr & RCC_CSR_WWDGRSTF) return "window watchdog";
    if (csr & RCC_CSR_IWDGRSTF) return "independent watchdog";
    if (csr & RCC_CSR_SFTRSTF)  return "software";
    if (csr & RCC_CSR_PWRRSTF)  return "power-on / BOR";
    if (csr & RCC_CSR_PINRSTF)  return "NRST pin";
    if (csr & RCC_CSR_OBLRSTF)  return "option byte load";
    return "unknown";
}
static uint32_t g_csr_at_boot;

static void cmd_info(void)
{
    uint32_t id = DBGMCU_IDCODE;
    cprintf("firmware     : %s %s\n", FW_NAME, FW_VERSION);
    cprintf("board        : %s\n", BOARD_NAME);
    cprintf("DBGMCU_IDCODE: 0x%08X  DEV_ID=0x%03X REV=0x%04X%s\n",
            id, id & 0xFFFu, id >> 16,
            ((id & 0xFFFu) == 0x467u) ? "  (STM32G0B1 - correct)" : "  (UNEXPECTED)");
    cprintf("UID          : %08X-%08X-%08X\n",
            *(volatile uint32_t *)(UID_BASE), *(volatile uint32_t *)(UID_BASE + 4),
            *(volatile uint32_t *)(UID_BASE + 8));
    cprintf("flash size   : %u KB\n", (uint32_t)(*(volatile uint16_t *)FLASHSIZE_BASE));
    cprintf("SYSCLK       : %u Hz (HSI16 -> PLL x8 / 2), flash 2 WS\n", SYSCLK_HZ);
    cprintf("reset cause  : %s (RCC_CSR=0x%08X)\n", reset_cause(g_csr_at_boot), g_csr_at_boot);
    cprintf("uptime       : %u ms\n", now_ms());
    if (g_timeouts) {
        uint32_t i;
        cprintf("TIMEOUTS     : 0x%03X ->", g_timeouts);
        for (i = 0; i < 12u; i++) { if (g_timeouts & (1u << i)) { cputc(' '); cputs(TO_NAME[i]); } }
        cputc('\n');
    } else {
        cputs("timeouts     : none - every peripheral answered\n");
    }
}
static void cmd_pins(void)
{
    cprintf("PB2  PCAP_PWR_EN  = %u  (1 = +3V3P enabled)\n", pin_get(GPIOB, PIN_PCAP_PWR_EN));
    cprintf("PB10 SPI_OE_N     = %u  (1 = buffer tri-stated)\n", pin_get(GPIOB, PIN_SPI_OE_N));
    cprintf("PB0  PCAP_CS      = %u  (1 = deselected)\n", pin_get(GPIOB, PIN_PCAP_CS));
    cprintf("PB1  PCAP_IRQ     = %u  (INTN active LOW)\n", pin_get(GPIOB, PIN_PCAP_IRQ));
    cprintf("PB11 HOST_READY   = %u\n", pin_get(GPIOB, PIN_HOST_READY));
    cprintf("PB12 MEAS_BUSY    = %u\n", pin_get(GPIOB, PIN_MEAS_BUSY));
}
static void cmd_adc(void)
{
    uint32_t vdda = vdda_mv();
    uint16_t raw  = adc_read(PIN_PWR_SENSE);
    cputs("VDDA (VREFINT)  = "); print_mv(vdda); cputc('\n');
    cprintf("PA0 raw         = %u / 4095\n", (uint32_t)raw);
    cputs("PA0 voltage     = "); print_mv(vdda ? vdda * raw / 4095u : 0u); cputc('\n');
    cprintf("divider         = %u/%u\n", g_div_num, g_div_den);
    cputs("+3V3P           = "); print_mv(rail_mv()); cputc('\n');
}
static void cmd_dump_cfg(void)
{
    uint32_t a;
    if (!pcap_ready()) { return; }
    for (a = 0; a < 64u; a++) {
        if ((a & 15u) == 0u) { cprintf("\n  %02X:", a); }
        cprintf(" %02X", (uint32_t)pcap_rd_cfg((uint8_t)a));
    }
    cputc('\n');
}
static void cmd_dump_res(void)
{
    uint32_t a;
    if (!pcap_ready()) { return; }
    for (a = 0; a < 35u; a++) {
        if ((a & 15u) == 0u) { cprintf("\n  %02X:", a); }
        cprintf(" %02X", (uint32_t)pcap_rd_res((uint8_t)a));
    }
    cputc('\n');
}

static void cmd_selftest(void)
{
    uint32_t id, vdda, mv; uint8_t tr, orig, rb; int ok = 1, step = 0;

    cputs("\n==================== SELF TEST ====================\n");

    cprintf("[%u] MCU identity\n", ++step);
    id = DBGMCU_IDCODE;
    if ((id & 0xFFFu) == 0x467u) { cprintf("     PASS DEV_ID 0x467 = STM32G0B1, REV 0x%04X\n", id >> 16); }
    else { cprintf("     FAIL DEV_ID 0x%03X (expected 0x467)\n", id & 0xFFFu); ok = 0; }

    cprintf("[%u] clock / console\n", ++step);
    cprintf("     PASS running at %u Hz (you are reading this, so the UART divisor is right)\n", SYSCLK_HZ);

    cprintf("[%u] reset cause\n", ++step);
    cprintf("     INFO %s\n", reset_cause(g_csr_at_boot));

    cprintf("[%u] control pin safe state\n", ++step);
    if (pin_get(GPIOB, PIN_SPI_OE_N) == 1u && pin_get(GPIOB, PIN_PCAP_PWR_EN) == 0u) {
        cputs("     PASS SPI buffer tri-stated, +3V3P disabled\n");
    } else { cputs("     FAIL control pins not in safe state\n"); ok = 0; }

    cprintf("[%u] VDDA via VREFINT\n", ++step);
    vdda = vdda_mv();
    cputs("     "); 
    if (vdda >= 3000u && vdda <= 3600u) { cputs("PASS VDDA = "); } else { cputs("FAIL VDDA = "); ok = 0; }
    print_mv(vdda); cputs(" (expect 3.20-3.40)\n");

    cprintf("[%u] +3V3P baseline with rail off\n", ++step);
    mv = rail_mv();
    cputs("     ");
    if (mv <= RAIL_OFF_MV) { cputs("PASS +3V3P = "); } else { cputs("FAIL +3V3P = "); ok = 0; }
    print_mv(mv); cprintf(" (expect < %u mV, U5 is off)\n", RAIL_OFF_MV);

    cprintf("[%u] enable +3V3P (U5 TPS22918)\n", ++step);
    if (rail_on() != 0) { ok = 0; cputs("     FAIL rail did not come up\n"); }
    else { cputs("     PASS\n"); }

    cprintf("[%u] enable SPI buffer (U6)\n", ++step);
    bus_enable(); delay_ms(1);
    cputs("     PASS OE_N driven low\n");

    cprintf("[%u] PCAP04 test read (0x7E -> expect 0x11)\n", ++step);
    tr = pcap_test_read();
    cprintf("     read back 0x%02X -> ", (uint32_t)tr);
    if (tr == PCAP_TESTREAD_OK)   { cputs("PASS  SPI link to PCAP04 is good\n"); }
    else if (tr == 0x88u)         { cputs("FAIL  byte order swapped\n"); ok = 0; }
    else if (tr == 0xEEu)         { cputs("FAIL  all bits inverted\n"); ok = 0; }
    else if (tr == 0x77u)         { cputs("FAIL  inverted AND swapped\n"); ok = 0; }
    else if (tr == 0xFFu)         { cputs("FAIL  MISO stuck high - no response\n"); ok = 0; }
    else if (tr == 0x00u)         { cputs("FAIL  MISO stuck low\n"); ok = 0; }
    else                          { cputs("FAIL  unexpected value\n"); ok = 0; }

    cprintf("[%u] PCAP04 config register read/write\n", ++step);
    if (tr == PCAP_TESTREAD_OK) {
        /* 0x10 = FULLCHARGE_TIME: a plain 8-bit value with no side effects
         * while no conversion is running.  Restored immediately.            */
        orig = pcap_rd_cfg(PCAP_SCRATCH_CFG);
        pcap_wr_cfg(PCAP_SCRATCH_CFG, 0x5Au);
        rb = pcap_rd_cfg(PCAP_SCRATCH_CFG);
        pcap_wr_cfg(PCAP_SCRATCH_CFG, orig);
        cprintf("     cfg[0x10]: was 0x%02X, wrote 0x5A, read 0x%02X -> ", (uint32_t)orig, (uint32_t)rb);
        if (rb == 0x5Au && pcap_rd_cfg(PCAP_SCRATCH_CFG) == orig) { cputs("PASS\n"); }
        else { cputs("FAIL\n"); ok = 0; }
    } else { cputs("     SKIP (test read failed)\n"); }

    cprintf("[%u] PCAP04 SRAM read/write\n", ++step);
    if (tr == PCAP_TESTREAD_OK) {
        orig = pcap_rd_mem(PCAP_SCRATCH_MEM);
        pcap_wr_mem(PCAP_SCRATCH_MEM, 0xA5u);
        rb = pcap_rd_mem(PCAP_SCRATCH_MEM);
        pcap_wr_mem(PCAP_SCRATCH_MEM, orig);
        cprintf("     sram[0x3FF]: was 0x%02X, wrote 0xA5, read 0x%02X -> ", (uint32_t)orig, (uint32_t)rb);
        if (rb == 0xA5u) { cputs("PASS\n"); } else { cputs("FAIL\n"); ok = 0; }
    } else { cputs("     SKIP\n"); }

    cprintf("[%u] PCAP_IRQ pin\n", ++step);
    cprintf("     INFO PB1 = %u (INTN is active low; high = idle)\n", pin_get(GPIOB, PIN_PCAP_IRQ));

    cprintf("[%u] power down sequence\n", ++step);
    if (rail_off() != 0) { ok = 0; } else { cputs("     PASS\n"); }

    cputs("===================================================\n");
    cprintf("RESULT: %s\n\n", ok ? "ALL CHECKS PASSED" : "ONE OR MORE CHECKS FAILED");
}

/* =====================================================================
 * stage 2: load the ScioSense standard firmware and run the CDC
 * ===================================================================== */
static uint8_t g_cfg[PCAP04_STD_CFG_LEN];

/* Front-end mode.  The ScioSense default config is a grounded-sensor setup
 * using an EXTERNAL reference at PC0.  This board has neither, so both modes
 * below use the on-chip reference at its largest setting.
 *
 *   floating : three sensor PAIRS, PC0/PC1, PC2/PC3, PC4/PC5, results in
 *              RES0..RES2.  Both compensations on - external compensation
 *              removes the trace-to-ground capacitance and is only legal
 *              when C_FLOATING = 1.
 *   grounded : six single-ended electrodes PC0..PC5 against GND, results in
 *              RES0..RES5.  Internal compensation only; the datasheet says
 *              C_COMP_EXT "must be avoided when C_FLOATING == 0".
 */
#define MODE_GROUNDED 0
#define MODE_FLOATING 1

#define CFG_MODEREG   0x04u
#define CFG_PORT_EN   0x06u
#define CFG_AVRG_L    0x07u
#define CFG_AVRG_H    0x08u
#define CFG_CONV_0    0x09u
#define CFG_CONV_1    0x0Au
#define CFG_CONV_2    0x0Bu
#define CFG_DISCH_L   0x0Cu
#define CFG_DISCH_H   0x0Du
#define CFG_REF_SEL   0x11u
#define CFG_WD_DIS    0x1Cu
#define CFG_RUNBIT    0x2Fu

/* Measured on this board: C_AVRG >= 512 makes the reading ~19.5 % low with a
 * ~100 pF sensor (256 and 257 are both fine, so it is not a low-byte-zero
 * problem).  Refuse to program past the last value proven good.            */
#define C_AVRG_MAX    256u
#define OLF_HZ        51000u        /* measured: CONV_TIME 2000 -> 12.8 Hz  */
#define C_REF_SEL_DEF 31u

/* The ScioSense standard config ships DISCHARGE_TIME = 0.  That is fine for a
 * C0G part on a short trace, but a real sensor on a cable - anything with
 * series resistance or a lossy dielectric - cannot discharge in zero time and
 * the port reports C_PortError, with the result pinned at full scale.  It
 * looks exactly like "the channel is dead".
 *
 * Measured on this board with a level probe on PC2/PC3: DISCHARGE_TIME = 0
 * errors, 2 already clears it, and anything up to 8 keeps the full 12.8 Hz.
 * At 10 and above the conversion no longer fits one trigger period and the
 * rate halves.  8 is the default here: clear margin, no rate cost. */
#define DISCHARGE_TIME_DEF 8u

static uint8_t g_mode   = MODE_FLOATING;

static void cfg_apply_mode(int floating)
{
    /* bit7 C_REF_INT, bit5 C_COMP_EXT, bit4 C_COMP_INT, bit0 C_FLOATING */
    g_cfg[4]  = floating ? 0xB1u : 0x90u;
    g_cfg[6]  = 0x3Fu;                            /* C_PORT_EN = PC0..PC5   */
    g_cfg[17] = (uint8_t)((g_cfg[17] & 0x03u) | (C_REF_SEL_DEF << 2));
    g_cfg[12] = (uint8_t)(DISCHARGE_TIME_DEF & 0xFFu);
    g_cfg[13] = (uint8_t)((g_cfg[13] & ~0x03u) | ((DISCHARGE_TIME_DEF >> 8) & 3u));
}

static int cmd_load(int floating)
{
    uint32_t bad, first = 0, i;
    uint8_t tr;

    if (!pcap_ready()) { return -1; }

    cputs("  test read ... ");
    tr = pcap_test_read();
    cprintf("0x%02X %s\n", (uint32_t)tr, tr == PCAP_TESTREAD_OK ? "OK" : "FAIL");
    if (tr != PCAP_TESTREAD_OK) { return -1; }

    cputs("  POR (0x88), waiting 500 ms ...\n");
    pcap_cmd(PCAP_OP_POR);
    delay_ms(500);
    cputs("  INIT (0x8A), waiting 10 ms ...\n");
    pcap_cmd(PCAP_OP_INIT);
    delay_ms(10);

    cprintf("  writing %u bytes of standard firmware to SRAM ... ", PCAP04_STD_FW_LEN);
    pcap_wr_mem_burst(0x000u, PCAP04_STD_FW, PCAP04_STD_FW_LEN);
    bad = pcap_vfy_mem(0x000u, PCAP04_STD_FW, PCAP04_STD_FW_LEN, &first);
    if (bad) { cprintf("FAIL, %u bytes differ, first at 0x%03X\n", bad, first); return -1; }
    cputs("verified\n");

    for (i = 0; i < PCAP04_STD_CFG_LEN; i++) { g_cfg[i] = PCAP04_STD_CFG[i]; }
    cfg_apply_mode(floating);

    cprintf("  writing %u config registers (%s) ... ", PCAP04_STD_CFG_LEN,
            floating ? "3x floating, internal reference" : "ScioSense default, grounded");
    pcap_wr_cfg_burst(0x00u, g_cfg, PCAP04_STD_CFG_LEN);
    bad = pcap_vfy_cfg(0x00u, g_cfg, PCAP04_STD_CFG_LEN, &first);
    if (bad) { cprintf("FAIL, %u differ, first at reg %u\n", bad, first); return -1; }
    cputs("verified\n");

    cputs("  INIT (0x8A)\n");
    pcap_cmd(PCAP_OP_INIT);
    delay_ms(10);
    cputs("  CDC_START (0x8C) - conversions are running\n");
    pcap_cmd(PCAP_OP_CDC_START);
    delay_ms(50);
    cprintf("  PCAP_IRQ (INTN, active low) now reads %u\n", pin_get(GPIOB, PIN_PCAP_IRQ));
    g_loaded = 1u;
    g_mode = (uint8_t)(floating ? MODE_FLOATING : MODE_GROUNDED);
    return 0;
}

/* RES0..RES7 as 32-bit Q5.27 ratios */
static void cmd_results(void)
{
    uint32_t i, raw, ppm;
    if (!pcap_ready()) { return; }
    /* Deliberately NOT pcap_loaded_now() here.  That does an extra config read,
     * and an extra SSN edge before the result read makes the chip treat the
     * previous value as consumed (EN_ASYNC_RD is on in the standard config),
     * so the registers can update underneath us mid-read.  Measured cost: ch0
     * jittering 0.128 -> 0.276 pF on the resd path while the INTN-gated stream
     * path stayed flat.  STATUS_0 is read below anyway and carries RUNBIT. */
    for (i = 0; i < 8u; i++) {
        raw = pcap_rd_result32((uint8_t)(i * 4u));
        ppm = ratio_ppm(raw);
        cprintf("  RES%u = 0x%08X  ratio = %u.%06u\n", i, raw, ppm / 1000000u, ppm % 1000000u);
    }
    {
        uint8_t s0 = pcap_rd_res(0x20u), s1 = pcap_rd_res(0x21u), s2 = pcap_rd_res(0x22u);
        cprintf("  status 0x20..0x22 = %02X %02X %02X   INTN=%u\n",
                (uint32_t)s0, (uint32_t)s1, (uint32_t)s2, pin_get(GPIOB, PIN_PCAP_IRQ));
        if (!(s0 & 0x01u)) {
            g_loaded = 0u;
            cputs("  WARNING: RUNBIT is clear - the PCAP04 has been reset.  Its config\n"
                  "  registers are zeroed but the result registers above still hold the\n"
                  "  PREVIOUS conversion, so those numbers are stale.  Run 'load'.\n");
        }
    }
}


/* =====================================================================
 * stage 3: runtime front-end control and streaming
 * ===================================================================== */

/* Any configuration change only reaches the running conversion after INIT
 * followed by CDC_START; writing the register alone leaves the current
 * sequence on the old setting.  The first conversion after a restart is
 * routinely garbage (often 0xFFFFFFFF), so callers drop a few. */
static void fe_restart(void)
{
    pcap_cmd(PCAP_OP_INIT);
    delay_ms(10);
    pcap_cmd(PCAP_OP_CDC_START);
    delay_ms(20);
}

/* consume one byte of host input if there is any; returns 1 if a key arrived */
static int host_key(void)
{
    usb_cdc_poll();
    if (g_mbx.rx_rd != g_mbx.rx_wr) {
        g_mbx.rx_rd = (g_mbx.rx_rd + 1u) % MBX_RX_SIZE;
        return 1;
    }
    if (usb_cdc_getc() >= 0) { return 1; }
    return uart_getc() >= 0;
}
static void host_flush(void)
{
    g_mbx.rx_rd = g_mbx.rx_wr;
    while (uart_getc() >= 0) { }
    usb_cdc_poll();
    while (usb_cdc_getc() >= 0) { }
}

static uint32_t fe_rate_mhz(uint32_t conv_time)
{
    if (conv_time == 0u) { return 0u; }
    return (OLF_HZ * 1000u) / (2u * conv_time);      /* milli-hertz */
}
static uint32_t fe_get_conv_time(void)
{
    return (uint32_t)pcap_rd_cfg((uint8_t)CFG_CONV_0)
         | ((uint32_t)pcap_rd_cfg((uint8_t)CFG_CONV_1) << 8)
         | (((uint32_t)pcap_rd_cfg((uint8_t)CFG_CONV_2) & 0x7Fu) << 16);
}

/* Trusting g_loaded alone is not enough.  Any power-on reset - a rail cycle,
 * or the PCAP04's own watchdog - zeroes every config register while leaving
 * the result registers holding their last values.  The flag then still says
 * "loaded", params() reports a configuration of all zeros, and resd hands
 * back stale numbers that look perfectly plausible.  RUNBIT is the chip's own
 * answer to the question, so ask it. */
static int pcap_loaded_now(void)
{
    if (!g_loaded || !g_rail_on || !g_bus_on) { return 0; }
    if (!(pcap_rd_cfg((uint8_t)CFG_RUNBIT) & 0x01u)) { g_loaded = 0u; return 0; }
    return 1;
}

static int cmd_mode(int floating)
{
    if (!pcap_ready()) { return -1; }
    if (!pcap_loaded_now()) {
        cputs("  no firmware in the PCAP04 (RUNBIT clear) - doing a full load\n");
        return cmd_load(floating);
    }
    g_mode = (uint8_t)(floating ? MODE_FLOATING : MODE_GROUNDED);
    g_cfg[17] = pcap_rd_cfg((uint8_t)CFG_REF_SEL);   /* keep the current ref */
    cfg_apply_mode(floating);
    pcap_wr_cfg((uint8_t)CFG_MODEREG, g_cfg[4]);
    pcap_wr_cfg((uint8_t)CFG_PORT_EN, g_cfg[6]);
    pcap_wr_cfg((uint8_t)CFG_REF_SEL, g_cfg[17]);
    fe_restart();
    cprintf("  mode = %s   cfg04=0x%02X cfg06=0x%02X cfg11=0x%02X\n",
            floating ? "FLOATING (3 pairs -> RES0..RES2)"
                     : "GROUNDED (6 electrodes -> RES0..RES5)",
            (uint32_t)pcap_rd_cfg((uint8_t)CFG_MODEREG),
            (uint32_t)pcap_rd_cfg((uint8_t)CFG_PORT_EN),
            (uint32_t)pcap_rd_cfg((uint8_t)CFG_REF_SEL));
    return 0;
}

static void cmd_avrg(uint32_t n)
{
    if (!pcap_ready()) { return; }
    if (n < 1u) { n = 1u; }
    if (n > C_AVRG_MAX) {
        cprintf("  refusing %u: C_AVRG >= 512 reads about 19.5%% low on this\n"
                "  board with a ~100 pF sensor.  clamped to %u.\n", n, C_AVRG_MAX);
        n = C_AVRG_MAX;
    }
    pcap_wr_cfg((uint8_t)CFG_AVRG_L, (uint8_t)(n & 0xFFu));
    pcap_wr_cfg((uint8_t)CFG_AVRG_H, (uint8_t)((n >> 8) & 0x1Fu));
    fe_restart();
    cprintf("  C_AVRG = %u\n", n);
}

static void cmd_conv(uint32_t ct)
{
    uint32_t mhz;
    if (!pcap_ready()) { return; }
    if (ct < 25u) { ct = 25u; }
    if (ct > 0x7FFFFFu) { ct = 0x7FFFFFu; }
    pcap_wr_cfg((uint8_t)CFG_CONV_0, (uint8_t)(ct & 0xFFu));
    pcap_wr_cfg((uint8_t)CFG_CONV_1, (uint8_t)((ct >> 8) & 0xFFu));
    pcap_wr_cfg((uint8_t)CFG_CONV_2, (uint8_t)((ct >> 16) & 0x7Fu));
    fe_restart();
    mhz = fe_rate_mhz(ct);
    cprintf("  CONV_TIME = %u  -> nominal %u.%03u Hz\n", ct, mhz / 1000u, mhz % 1000u);
    cputs("  (nominal only: if the averaging does not fit in one period the\n"
          "   chip simply skips triggers, so measure the real rate with 'stream')\n");
}

/* DISCHARGE_TIME is split across cfg 0x0C (7:0) and cfg 0x0D (1:0); 0x0D also
 * carries C_TRIG_SEL, which must keep its timer-triggered setting. */
static void cmd_disch(uint32_t n)
{
    if (!pcap_ready()) { return; }
    if (n > 0x3FFu) { n = 0x3FFu; }
    pcap_wr_cfg((uint8_t)CFG_DISCH_L, (uint8_t)(n & 0xFFu));
    pcap_wr_cfg((uint8_t)CFG_DISCH_H,
                (uint8_t)((pcap_rd_cfg((uint8_t)CFG_DISCH_H) & ~0x03u) | ((n >> 8) & 3u)));
    fe_restart();
    cprintf("  DISCHARGE_TIME = %u\n", n);
    if (n == 0u) {
        cputs("  warning: 0 makes any sensor with series resistance or a lossy\n"
              "  dielectric report C_PortError and read full scale\n");
    } else if (n > 8u) {
        cputs("  note: above 8 the conversion may not fit one trigger period,\n"
              "  which halves the sample rate - check with 'stream'\n");
    }
}

static void cmd_refsel(uint32_t n)
{
    if (!pcap_ready()) { return; }
    if (n > 31u) { n = 31u; }
    pcap_wr_cfg((uint8_t)CFG_REF_SEL,
                (uint8_t)((pcap_rd_cfg((uint8_t)CFG_REF_SEL) & 0x03u) | (n << 2)));
    fe_restart();
    cprintf("  C_REF_SEL = %u   (datasheet Cref ~ 0.959*N + 3.23 pF, but this\n"
            "  part measures ~0.975*N + 9.91 pF - calibrate, do not use the formula)\n", n);
}

/* mask bit0 = internal compensation, bit1 = external compensation */
static void cmd_comp(uint32_t mask)
{
    uint8_t v;
    if (!pcap_ready()) { return; }
    v = (uint8_t)(pcap_rd_cfg((uint8_t)CFG_MODEREG) & ~0x30u);
    if (mask & 1u) { v |= 0x10u; }
    if (mask & 2u) {
        if (v & 0x01u) { v |= 0x20u; }
        else { cputs("  external compensation ignored: only legal in floating mode\n"); }
    }
    pcap_wr_cfg((uint8_t)CFG_MODEREG, v);
    fe_restart();
    cprintf("  cfg04 = 0x%02X   C_COMP_INT=%u C_COMP_EXT=%u C_FLOATING=%u\n",
            (uint32_t)v, (v >> 4) & 1u, (v >> 5) & 1u, v & 1u);
}

static void cmd_ports(uint32_t mask)
{
    if (!pcap_ready()) { return; }
    pcap_wr_cfg((uint8_t)CFG_PORT_EN, (uint8_t)(mask & 0x3Fu));
    fe_restart();
    cprintf("  C_PORT_EN = 0x%02X\n", mask & 0x3Fu);
}

static void cmd_params(void)
{
    uint8_t m, ref;
    uint32_t avrg, ct, mhz;
    int loaded;
    if (!pcap_ready()) { return; }
    loaded = pcap_loaded_now();
    m    = pcap_rd_cfg((uint8_t)CFG_MODEREG);
    ref  = pcap_rd_cfg((uint8_t)CFG_REF_SEL);
    avrg = (uint32_t)pcap_rd_cfg((uint8_t)CFG_AVRG_L)
         | (((uint32_t)pcap_rd_cfg((uint8_t)CFG_AVRG_H) & 0x1Fu) << 8);
    ct   = fe_get_conv_time();
    mhz  = fe_rate_mhz(ct);
    cprintf("  firmware in PCAP04 SRAM : %s\n",
            loaded ? "loaded" : "NOT loaded - run 'load'");
    /* On a blank chip cfg 0x04 reads 0, which would report GROUNDED and make
     * the host switch to a 6-channel interpretation of nothing.  Only believe
     * the register when there is firmware behind it. */
    if (loaded) {
        cprintf("  mode                    : %s\n", (m & 0x01u) ? "FLOATING" : "GROUNDED");
    } else {
        cprintf("  mode                    : %s (intended; chip is blank)\n",
                (g_mode == MODE_FLOATING) ? "FLOATING" : "GROUNDED");
    }
    cprintf("  C_REF_INT               : %u   C_REF_SEL = %u\n", (m >> 7) & 1u, (ref >> 2) & 0x1Fu);
    cprintf("  C_COMP_INT / C_COMP_EXT : %u / %u\n", (m >> 4) & 1u, (m >> 5) & 1u);
    cprintf("  C_PORT_EN               : 0x%02X\n", (uint32_t)pcap_rd_cfg((uint8_t)CFG_PORT_EN));
    cprintf("  C_AVRG                  : %u\n", avrg);
    cprintf("  CONV_TIME               : %u  -> nominal %u.%03u Hz\n", ct, mhz / 1000u, mhz % 1000u);
    cprintf("  DISCHARGE_TIME          : %u\n",
            (uint32_t)pcap_rd_cfg((uint8_t)CFG_DISCH_L)
            | (((uint32_t)pcap_rd_cfg((uint8_t)CFG_DISCH_H) & 3u) << 8));
    cprintf("  WD_DIS                  : 0x%02X %s\n", (uint32_t)pcap_rd_cfg((uint8_t)CFG_WD_DIS),
            pcap_rd_cfg((uint8_t)CFG_WD_DIS) == 0x5Au ? "(watchdog off)" : "(WATCHDOG ARMED)");
    cprintf("  RUNBIT (cfg 0x2F)       : 0x%02X\n", (uint32_t)pcap_rd_cfg((uint8_t)CFG_RUNBIT));
}

static void cmd_health(void)
{
    uint8_t s0, s1, s2;
    if (!pcap_ready()) { return; }
    s0 = pcap_rd_res(0x20u); s1 = pcap_rd_res(0x21u); s2 = pcap_rd_res(0x22u);
    cprintf("  STATUS_0 = 0x%02X :", (uint32_t)s0);
    if (s0 & 0x80u) { cputs(" POR_FLAG_WDOG"); }
    if (s0 & 0x40u) { cputs(" POR_FLAG_CONFIG"); }
    if (s0 & 0x20u) { cputs(" POR_CDC_DSP_COLL"); }
    if (s0 & 0x10u) { cputs(" AUTOBOOT_BUSY"); }
    if (s0 & 0x04u) { cputs(" RDC_READY"); }
    if (s0 & 0x02u) { cputs(" CDC_ACTIVE"); }
    cputs((s0 & 0x01u) ? " RUNBIT" : " *** RUNBIT CLEAR - chip is idle ***");
    cputc('\n');
    cprintf("  STATUS_1 = 0x%02X :", (uint32_t)s1);
    if (s1 & 0x08u) { cputs(" RDC_ERR"); }
    if (s1 & 0x04u) { cputs(" MUP_ERR"); }
    if (s1 & 0x02u) { cputs(" ERR_OVFL"); }
    if (s1 & 0x01u) { cputs(" COMB_ERR"); }
    if (!(s1 & 0x0Fu)) { cputs(" no errors"); }
    cputc('\n');
    cprintf("  STATUS_2 = 0x%02X :", (uint32_t)s2);
    if (s2 & 0x40u) { cputs(" PortErr(int.ref)"); }
    { uint32_t i; for (i = 0; i < 6u; i++) { if (s2 & (1u << i)) { cprintf(" PortErr(PC%u)", i); } } }
    if (!(s2 & 0x7Fu)) { cputs(" no port errors"); }
    cputc('\n');
    cprintf("  INTN pin (PB1)  = %u  (active low)\n", pin_get(GPIOB, PIN_PCAP_IRQ));
    cprintf("  +3V3P rail      = %s, measured ", g_rail_on ? "ON" : "OFF");
    print_mv(rail_mv()); cputc('\n');
}

/* One frame per conversion, gated on the INTN falling edge so that a frame
 * is never a re-read of the previous conversion.  Text, not binary: at
 * 115200 baud a 90-byte frame still allows ~120 frames/s, far above the
 * chip's rate, and it stays greppable. */
static void cmd_stream(uint32_t nmax)
{
    uint32_t n = 0u, i, reloads = 0u, t;
    uint8_t s0, s1, s2;
    int aborted = 0;

    if (!pcap_ready()) { return; }
    if (!pcap_loaded_now()) { cputs("  no firmware loaded - run 'load' or 'loadf' first\n"); return; }

    cputs("# fmt: D <t_ms> <RES0..RES7 hex> <s0> <s1> <s2>   ('#' lines are comments)\n");
    cprintf("# mode=%s  any key stops\n", (g_mode == MODE_FLOATING) ? "floating" : "grounded");

    host_flush();
    while (nmax == 0u || n < nmax) {
        /* wait for a new conversion: INTN low.  Bounded, so a stopped chip
         * reports instead of hanging the console. */
        uint32_t t0 = now_ms();
        int got = 0;
        while ((now_ms() - t0) < 3000u) {
            if (pin_get(GPIOB, PIN_PCAP_IRQ) == 0u) { got = 1; break; }
            if (host_key()) { aborted = 1; break; }
        }
        if (aborted) { break; }
        if (!got) {
            s0 = pcap_rd_res(0x20u);
            if (!(s0 & 0x01u)) {
                if (reloads >= 3u) { cputs("# RUNBIT lost and 3 reloads did not help - stopping\n"); break; }
                reloads++;
                cprintf("# RUNBIT lost (STATUS_0=0x%02X) - reloading firmware, attempt %u\n",
                        (uint32_t)s0, reloads);
                if (cmd_load(g_mode == MODE_FLOATING) != 0) { cputs("# reload failed - stopping\n"); break; }
                continue;
            }
            cprintf("# no INTN for 3 s but RUNBIT is set (STATUS_0=0x%02X);"
                    " is PG5_INTN_EN still on in cfg 0x1E?\n", (uint32_t)s0);
            break;
        }

        t = now_ms();
        cputs("D ");
        put_u32(t, 10, 0, ' ');
        for (i = 0; i < 8u; i++) { cputc(' '); put_u32(pcap_rd_result32((uint8_t)(i * 4u)), 16, 8, '0'); }
        s0 = pcap_rd_res(0x20u); s1 = pcap_rd_res(0x21u); s2 = pcap_rd_res(0x22u);
        cprintf(" %02X %02X %02X\n", (uint32_t)s0, (uint32_t)s1, (uint32_t)s2);
        n++;
        if (host_key()) { aborted = 1; break; }
    }
    host_flush();
    cprintf("# %u frames%s\n", n, aborted ? ", stopped by host" : "");
}

static void cmd_help(void)
{
    cputs(
    "commands:\n"
    "  help                 this list\n"
    "  info                 MCU id, UID, flash size, clocks, reset cause\n"
    "  pins                 state of every control pin\n"
    "  adc                  VDDA, PA0 raw, computed +3V3P\n"
    "  rail on|off          power sequence for +3V3P (U5)\n"
    "  bus on|off           SPI buffer output enable (U6)\n"
    "  spi <0-7>            SPI prescaler: 0=/2 ... 5=/64 ... 7=/256\n"
    "  test                 PCAP04 test read, 0x7E -> 0x11\n"
    "  rd <a>               read PCAP04 config register (hex)\n"
    "  wr <a> <d>           write PCAP04 config register (hex)\n"
    "  div [n d]            show/set the +3V3P divider ratio\n"
    "  mrd <a> / mwr <a> <d>  PCAP04 SRAM byte access (10-bit address)\n"
    "  cfg                  dump config registers 00..3F\n"
    "  res                  dump result registers 00..22\n"
    "  init | por | start   PCAP04 opcodes 0x8A / 0x88 / 0x8C\n"
    "  load | loadf         load ScioSense standard firmware, grounded / floating\n"
    "  resd                 RES0..RES7 as Q5.27 capacitance ratios\n"
    "\n measurement control (decimal arguments):\n"
    "  mode g|f             switch grounded / floating without a full reload\n"
    "  stream [n]           one line per conversion, gated on INTN; any key stops\n"
    "  avrg <n>             C_AVRG sample size, 1..256 (>=512 reads ~19.5%% low)\n"
    "  conv <n>             CONV_TIME, trigger period = 2*n/f_OLF\n"
    "  refsel <n>           C_REF_SEL 0..31, on-chip reference capacitor\n"
    "  disch <n>            DISCHARGE_TIME; raise it if a port reports an error\n"
    "  comp <0-3>           bit0 internal, bit1 external compensation\n"
    "  ports <hex>          C_PORT_EN bitmask for PC0..PC5\n"
    "  params               decode the current front-end configuration\n"
    "  usb                  USB CDC link state, resets, dropped bytes\n"
    "  health               decode STATUS_0/1/2, INTN and the rail\n"
    "  selftest             run the whole staged bring-up\n"
    "  reset                reset the MCU\n"
    "  dfu                  enter the ST ROM bootloader (USB DFU) for reflashing\n"
    "\nescape hatch: press RESET (SW1) twice within 400 ms and the board enters\n"
    "the USB bootloader even if this firmware is broken.\n");
}

/* tiny parser helpers */
static int str_eq(const char *a, const char *b)
{ while (*a && *b) { if (*a++ != *b++) return 0; } return *a == *b; }
static const char *skip_sp(const char *s) { while (*s == ' ' || *s == '\t') s++; return s; }
static uint32_t parse_dec(const char **pp)
{
    const char *s = skip_sp(*pp); uint32_t v = 0;
    while (*s >= '0' && *s <= '9') { v = v * 10u + (uint32_t)(*s++ - '0'); }
    *pp = s; return v;
}
static uint32_t parse_hex(const char **pp)
{
    const char *s = skip_sp(*pp); uint32_t v = 0;
    while (1) {
        char c = *s;
        if (c >= '0' && c <= '9') v = v * 16u + (uint32_t)(c - '0');
        else if (c >= 'a' && c <= 'f') v = v * 16u + (uint32_t)(c - 'a' + 10);
        else if (c >= 'A' && c <= 'F') v = v * 16u + (uint32_t)(c - 'A' + 10);
        else break;
        s++;
    }
    *pp = s; return v;
}

static void execute(char *line)
{
    const char *p = skip_sp(line);
    char verb[16]; uint32_t n = 0;
    while (*p && *p != ' ' && n < sizeof(verb) - 1u) { verb[n++] = *p++; }
    verb[n] = 0;
    if (n == 0u) { return; }

    if      (str_eq(verb, "help")) { cmd_help(); }
    else if (str_eq(verb, "info")) { cmd_info(); }
    else if (str_eq(verb, "pins")) { cmd_pins(); }
    else if (str_eq(verb, "adc"))  { cmd_adc(); }
    else if (str_eq(verb, "selftest")) { cmd_selftest(); }
    else if (str_eq(verb, "rail")) {
        p = skip_sp(p);
        if (str_eq(p, "on")) { rail_on(); }
        else if (str_eq(p, "off")) { rail_off(); }
        else { cprintf("  +3V3P is %s, measured ", g_rail_on ? "ON" : "OFF"); print_mv(rail_mv()); cputc('\n'); }
    }
    else if (str_eq(verb, "bus")) {
        p = skip_sp(p);
        if (str_eq(p, "on")) {
            if (!g_rail_on) { cputs("  ERR turn the rail on first\n"); }
            else { bus_enable(); cputs("  SPI buffer enabled\n"); }
        } else if (str_eq(p, "off")) { bus_disable(); cputs("  SPI buffer tri-stated\n"); }
        else { cprintf("  SPI buffer is %s\n", g_bus_on ? "ENABLED" : "TRI-STATED"); }
    }
    else if (str_eq(verb, "spi")) {
        uint32_t br = parse_hex(&p) & 7u; spi_setbr(br);
        cprintf("  SPI prescaler /%u -> %u Hz\n", 2u << br, PCLK_HZ >> (br + 1u));
    }
    else if (str_eq(verb, "test")) {
        if (pcap_ready()) {
            uint8_t v = pcap_test_read();
            cprintf("  0x7E -> 0x%02X  %s\n", (uint32_t)v,
                    v == PCAP_TESTREAD_OK ? "PASS" :
                    v == 0x88u ? "FAIL byte order swapped" :
                    v == 0xEEu ? "FAIL bits inverted" :
                    v == 0x77u ? "FAIL inverted+swapped" :
                    v == 0xFFu ? "FAIL MISO stuck high" :
                    v == 0x00u ? "FAIL MISO stuck low" : "FAIL unexpected");
        }
    }
    else if (str_eq(verb, "rd")) {
        if (pcap_ready()) { uint32_t a = parse_hex(&p) & 0x3Fu;
            cprintf("  cfg[0x%02X] = 0x%02X\n", a, (uint32_t)pcap_rd_cfg((uint8_t)a)); }
    }
    else if (str_eq(verb, "wr")) {
        if (pcap_ready()) { uint32_t a = parse_hex(&p) & 0x3Fu, d = parse_hex(&p) & 0xFFu;
            pcap_wr_cfg((uint8_t)a, (uint8_t)d);
            cprintf("  cfg[0x%02X] <- 0x%02X, read back 0x%02X\n", a, d, (uint32_t)pcap_rd_cfg((uint8_t)a)); }
    }
    else if (str_eq(verb, "div")) {
        const char *q = skip_sp(p);
        if (*q) { g_div_num = parse_hex(&p); g_div_den = parse_hex(&p); }
        cprintf("  rail divider = %u/%u  (board as built 133/100; BOM says %u/100)\n",
                g_div_num, g_div_den, RAIL_DIV_NUM_DESIGN);
        cputs("  +3V3P now reads "); print_mv(rail_mv()); cputc('\n');
    }
    else if (str_eq(verb, "mrd")) {
        if (pcap_ready()) { uint32_t a = parse_hex(&p) & 0x3FFu;
            cprintf("  sram[0x%03X] = 0x%02X\n", a, (uint32_t)pcap_rd_mem((uint16_t)a)); }
    }
    else if (str_eq(verb, "mwr")) {
        if (pcap_ready()) { uint32_t a = parse_hex(&p) & 0x3FFu, d = parse_hex(&p) & 0xFFu;
            pcap_wr_mem((uint16_t)a, (uint8_t)d);
            cprintf("  sram[0x%03X] <- 0x%02X, read back 0x%02X\n", a, d,
                    (uint32_t)pcap_rd_mem((uint16_t)a)); }
    }
    else if (str_eq(verb, "load"))  { cmd_load(0); }
    else if (str_eq(verb, "loadf")) { cmd_load(1); }
    else if (str_eq(verb, "mode")) {
        const char *q = skip_sp(p);
        if (*q == 'f' || *q == 'F') { cmd_mode(1); }
        else if (*q == 'g' || *q == 'G') { cmd_mode(0); }
        else { cprintf("  mode is %s - use 'mode g' or 'mode f'\n",
                       pcap_loaded_now() ? ((g_mode == MODE_FLOATING) ? "FLOATING" : "GROUNDED")
                                : "unset (no firmware loaded)"); }
    }
    else if (str_eq(verb, "stream")) { cmd_stream(parse_dec(&p)); }
    else if (str_eq(verb, "avrg"))   { cmd_avrg(parse_dec(&p)); }
    else if (str_eq(verb, "conv"))   { cmd_conv(parse_dec(&p)); }
    else if (str_eq(verb, "refsel")) { cmd_refsel(parse_dec(&p)); }
    else if (str_eq(verb, "disch"))  { cmd_disch(parse_dec(&p)); }
    else if (str_eq(verb, "comp"))   { cmd_comp(parse_dec(&p)); }
    else if (str_eq(verb, "ports"))  { cmd_ports(parse_hex(&p)); }
    else if (str_eq(verb, "params")) { cmd_params(); }
    else if (str_eq(verb, "usb")) {
        static const char *ST[4] = { "detached", "powered", "addressed", "configured" };
        cprintf("  state      : %s\n", ST[usb_cdc_state() & 3u]);
        cprintf("  host port  : %s\n", usb_cdc_ready() ? "open (DTR asserted)" : "not open");
        cprintf("  bus resets : %u\n", usb_cdc_resets());
        cprintf("  tx dropped : %u bytes\n", usb_cdc_dropped());
        cputs("  (UART on J10 stays live in parallel - it is the fallback)\n");
    }
    else if (str_eq(verb, "health")) { cmd_health(); }
    else if (str_eq(verb, "resd"))  { cmd_results(); }
    else if (str_eq(verb, "cfg"))   { cmd_dump_cfg(); }
    else if (str_eq(verb, "res"))   { cmd_dump_res(); }
    else if (str_eq(verb, "init"))  { if (pcap_ready()) { pcap_cmd(PCAP_OP_INIT);  cputs("  sent 0x8A initialize\n"); } }
    else if (str_eq(verb, "por"))   { if (pcap_ready()) { pcap_cmd(PCAP_OP_POR);   cputs("  sent 0x88 POR\n"); } }
    else if (str_eq(verb, "start")) { if (pcap_ready()) { pcap_cmd(PCAP_OP_CDC_START); cputs("  sent 0x8C CDC start\n"); } }
    else if (str_eq(verb, "reset")) { cputs("  resetting...\n"); delay_ms(20); SCB_AIRCR = SCB_AIRCR_SYSRESETREQ; }
    else if (str_eq(verb, "dfu")) {
        cputs("  powering down the PCAP04 and entering the ST ROM bootloader.\n"
              "  the board will re-enumerate as 0483:DF11 - flash it with pcapdfu.py\n");
        delay_ms(50);
        jump_to_bootloader();
    }
    else { cprintf("  unknown command '%s' - try 'help'\n", verb); }
}

/* =====================================================================
 * main
 * ===================================================================== */
int main(void)
{
    static char line[96];
    uint32_t li = 0;

    uint32_t tap = g_boot_magic;

    /* We may have been entered by a JUMP from the ROM bootloader rather than
     * by a reset.  In that case SYSCFG.MEM_MODE still maps system memory at
     * 0x00000000, so every interrupt would vector through the ROM's table and
     * SysTick would never reach our handler - the first delay_ms() would hang
     * forever.  Point VTOR at our own table and restore the main-flash
     * mapping before anything enables an interrupt.                         */
    __asm volatile("cpsid i");
    SCB_VTOR = 0x08000000UL;
    NVIC_ICER0 = 0xFFFFFFFFUL;    /* the ROM bootloader leaves USB IRQ armed;*/
    NVIC_ICPR0 = 0xFFFFFFFFUL;    /* it would vector into Default_Handler    */
    SYST_CSR = 0;
    RCC_APBENR2 |= RCC_APBENR2_SYSCFGEN;
    SYSCFG_CFGR1 &= ~3u;
    __asm volatile("cpsie i");

    /* Arm the escape hatch FIRST, before any code that could hang.  If the
     * clock setup below ever fails to lock the PLL the magic simply stays
     * armed, so a second reset always gets you into the ROM bootloader.     */
    g_boot_magic = BOOT_MAGIC;

    g_csr_at_boot = RCC_CSR;
    RCC_CSR |= RCC_CSR_RMVF;

    control_pins_init();          /* safe pin states before anything else    */

    /* Still running on the reset clock (HSI16) here, which is exactly what
     * the ROM bootloader expects - so the jump needs nothing but GPIO.      */
    if (tap == BOOT_MAGIC) { g_boot_magic = 0u; jump_to_bootloader(); }

    clock_init();
    delay_ms(400);                /* second tap window                       */
    g_boot_magic = 0u;

    /* publish the mailbox so the host tool can find it at 0x20000000 */
    g_mbx.tx_size = MBX_TX_SIZE; g_mbx.tx_wr = 0; g_mbx.tx_rd = 0;
    g_mbx.rx_size = MBX_RX_SIZE; g_mbx.rx_wr = 0; g_mbx.rx_rd = 0;
    g_mbx.heartbeat = 0; g_mbx.dropped = 0; g_mbx.version = 1;
    g_mbx.magic0 = MBX_MAGIC0; g_mbx.magic1 = MBX_MAGIC1;

    uart_init();
    adc_init();
    spi_init();
    usb_cdc_init();      /* HSI48 + CRS; harmless if the cable is not in */

    cputs("\n\n=== " BOARD_NAME " ===\n");
    cputs("PCAP04 control firmware " FW_VERSION "  (console: SWD mailbox + UART 115200 on J10)\n");
    cputs("console: USB-C CDC + UART 115200 on J10 + SWD mailbox\n");
    cputs("type 'help', or 'selftest' to run everything\n\n> ");

    for (;;) {
        int c = -1;
        usb_cdc_poll();
        /* three console transports, whichever speaks first: the SWD mailbox,
         * the board's own USB-C, and the UART on J10.  Keeping the UART alive
         * means a USB regression can never lock us out of the board. */
        if (g_mbx.rx_rd != g_mbx.rx_wr) {
            c = (int)(uint8_t)g_mbx.rx_buf[g_mbx.rx_rd];
            g_mbx.rx_rd = (g_mbx.rx_rd + 1u) % MBX_RX_SIZE;
        } else if ((c = usb_cdc_getc()) < 0) {
            c = uart_getc();
        }
        if (c >= 0) {
            if (c == '\r' || c == '\n') {
                cputc('\n');
                line[li] = 0; execute(line); li = 0;
                cputs("> ");
            } else if ((c == 8 || c == 127) && li > 0u) {
                li--; cputs("\b \b");
            } else if (c >= 32 && c < 127 && li < sizeof(line) - 1u) {
                line[li++] = (char)c; cputc((char)c);
            }
        }
        g_mbx.heartbeat++;
    }
}
