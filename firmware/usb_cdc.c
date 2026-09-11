/* SPDX-License-Identifier: MIT */
/* usb_cdc.c - bare-metal USB CDC-ACM device for the STM32G0B1.
 *
 * Board facts this depends on, all verified against the v1.5 netlist and
 * RM0444 Rev 6 rather than assumed:
 *
 *  - USB_DM / USB_DP land on physical pins 33 / 34, which are the dedicated
 *    PA11 / PA12 pins of the LQFP-48 part.  PA9 and PA10 are separate pins
 *    here and are unconnected, so SYSCFG_CFGR1.PA11_RMP / PA12_RMP must stay
 *    at their reset value of 0.  No GPIO configuration is needed either: on
 *    the G0 the USB peripheral takes the pins over when it is enabled.
 *  - There is no crystal (PF0/PF1 and PC14/PC15 are all unconnected), so USB
 *    is clocked from HSI48 trimmed by the CRS against the host's SOF packets.
 *    RCC_CCIPR2.USBSEL = 00 selects HSI48.  CRS_CFGR resets to 0x2022BB7F,
 *    which is already the USB-SOF configuration, so only CEN + AUTOTRIMEN
 *    have to be set.  The ROM bootloader's DFU runs this way on this board,
 *    which is what proves the whole path works.
 *  - This is the USB_DRD_FS peripheral: USB_CHEPnR registers, a fixed buffer
 *    descriptor table at the start of USBSRAM (no BTABLE register), and
 *    32-bit packet memory access.
 *
 * The peripheral is polled from the main loop.  Nothing here uses interrupts,
 * so it cannot disturb the SPI timing of a conversion in progress.
 */

#include "usb_cdc.h"
#include "stm32g0b1.h"

/* ------------------------------------------------------------------ */
/* peripheral                                                          */
/* ------------------------------------------------------------------ */
#define USB_BASE        0x40005C00UL
#define USBRAM_BASE     0x40009800UL
#define CRS_BASE        0x40006C00UL

#define USB_CHEP(n)     (*(__IO uint32_t *)(USB_BASE + 4u * (uint32_t)(n)))
#define USB_CNTR        (*(__IO uint32_t *)(USB_BASE + 0x40u))
#define USB_ISTR        (*(__IO uint32_t *)(USB_BASE + 0x44u))
#define USB_DADDR       (*(__IO uint32_t *)(USB_BASE + 0x4Cu))
#define USB_BCDR        (*(__IO uint32_t *)(USB_BASE + 0x58u))

#define CRS_CR          (*(__IO uint32_t *)(CRS_BASE + 0x00u))

/* USB_CNTR */
#define CNTR_HOST       (1UL << 31)
#define CNTR_RESETM     (1UL << 10)
#define CNTR_SUSPM      (1UL << 11)
#define CNTR_CTRM       (1UL << 15)
#define CNTR_PDWN       (1UL << 1)
#define CNTR_USBRST     (1UL << 0)

/* USB_ISTR */
#define ISTR_CTR        (1UL << 15)
#define ISTR_RESET      (1UL << 10)
#define ISTR_SUSP       (1UL << 11)
#define ISTR_WKUP       (1UL << 12)
#define ISTR_DIR        (1UL << 4)
#define ISTR_IDN_MASK   0x0Fu

/* USB_BCDR */
#define BCDR_DPPU       (1UL << 15)

/* USB_CHEPnR */
#define EP_VTRX         (1UL << 15)
#define EP_DTOGRX       (1UL << 14)
#define EP_STATRX       (3UL << 12)
#define EP_SETUP        (1UL << 11)
#define EP_UTYPE        (3UL << 9)
#define EP_KIND         (1UL << 8)
#define EP_VTTX         (1UL << 7)
#define EP_DTOGTX       (1UL << 6)
#define EP_STATTX       (3UL << 4)
#define EP_EA           (0x0FUL << 0)

#define EP_TYPE_BULK    (0UL << 9)
#define EP_TYPE_CONTROL (1UL << 9)
#define EP_TYPE_ISO     (2UL << 9)
#define EP_TYPE_INTR    (3UL << 9)

#define STATRX_DISABLED (0UL << 12)
#define STATRX_STALL    (1UL << 12)
#define STATRX_NAK      (2UL << 12)
#define STATRX_VALID    (3UL << 12)
#define STATTX_DISABLED (0UL << 4)
#define STATTX_STALL    (1UL << 4)
#define STATTX_NAK      (2UL << 4)
#define STATTX_VALID    (3UL << 4)

/* Bits that behave normally on a write.  Everything else is either rc_w0
 * (VTRX and VTTX: writing 1 leaves them alone, writing 0 clears them) or
 * toggle-only (the DTOG and STAT fields: writing 1 flips the bit).  So every
 * update is read-modify-write with the toggle fields XORed to reach the
 * target value, and the rc_w0 flags forced to 1 so they survive. */
#define EP_REG_MASK     (EP_VTRX | EP_SETUP | EP_UTYPE | EP_KIND | EP_VTTX | EP_EA)

static void ep_set_stat_rx(uint32_t n, uint32_t stat)
{
    uint32_t r = USB_CHEP(n);
    r = (r & (EP_REG_MASK | EP_STATRX)) ^ stat;
    USB_CHEP(n) = r | EP_VTRX | EP_VTTX;      /* 1 = leave the flag alone */
}

static void ep_set_stat_tx(uint32_t n, uint32_t stat)
{
    uint32_t r = USB_CHEP(n);
    r = (r & (EP_REG_MASK | EP_STATTX)) ^ stat;
    USB_CHEP(n) = r | EP_VTRX | EP_VTTX;
}

static void ep_clear_vtrx(uint32_t n)
{
    USB_CHEP(n) = (USB_CHEP(n) & EP_REG_MASK & ~EP_VTRX) | EP_VTTX;
}

static void ep_clear_vttx(uint32_t n)
{
    USB_CHEP(n) = (USB_CHEP(n) & EP_REG_MASK & ~EP_VTTX) | EP_VTRX;
}

/* ------------------------------------------------------------------ */
/* packet memory                                                       */
/* ------------------------------------------------------------------ */
/* Buffer descriptor table is fixed at the start of USBSRAM: two words per
 * endpoint, TX first.  Everything after 0x40 is ours. */
#define BD_TX(n)        (*(__IO uint32_t *)(USBRAM_BASE + 8u * (uint32_t)(n)))
#define BD_RX(n)        (*(__IO uint32_t *)(USBRAM_BASE + 8u * (uint32_t)(n) + 4u))

#define EP0_SIZE        64u
#define EPINT_SIZE      8u
#define EPBULK_SIZE     64u

#define PMA_EP0_TX      0x040u
#define PMA_EP0_RX      0x080u
#define PMA_EP1_TX      0x0C0u        /* CDC notification, never actually sent */
#define PMA_EP2_TX      0x0D0u
#define PMA_EP2_RX      0x110u        /* ends at 0x150, USBSRAM is 1 KB       */

#define EP_CTRL         0u
#define EP_NOTIFY       1u
#define EP_DATA         2u

/* size field for a receive buffer: BLSIZE=1 selects 32-byte blocks and the
 * count is NUM_BLOCK+1 of them, so 64 bytes is BLSIZE=1, NUM_BLOCK=1. */
#define RX_BLOCKS_64    ((1UL << 31) | (1UL << 26))

static void pma_write(uint32_t off, const uint8_t *src, uint32_t len)
{
    __IO uint32_t *dst = (__IO uint32_t *)(USBRAM_BASE + off);
    uint32_t i = 0;
    while (i + 4u <= len) {
        *dst++ = (uint32_t)src[i] | ((uint32_t)src[i + 1] << 8)
               | ((uint32_t)src[i + 2] << 16) | ((uint32_t)src[i + 3] << 24);
        i += 4u;
    }
    if (i < len) {
        uint32_t v = 0, s = 0;
        while (i < len) { v |= (uint32_t)src[i++] << s; s += 8u; }
        *dst = v;
    }
}

static void pma_read(uint32_t off, uint8_t *dst, uint32_t len)
{
    __IO uint32_t *src = (__IO uint32_t *)(USBRAM_BASE + off);
    uint32_t i = 0, v = 0;
    while (i < len) {
        if ((i & 3u) == 0u) { v = *src++; }
        dst[i] = (uint8_t)(v & 0xFFu);
        v >>= 8;
        i++;
    }
}

/* ------------------------------------------------------------------ */
/* descriptors                                                         */
/* ------------------------------------------------------------------ */
/* VID/PID: pid.codes 0x1209 is the open-source vendor ID and 0x0001 is its
 * documented "prototype / testing only" product ID.  Correct for a one-off
 * lab board; anything that leaves the bench should get its own PID.  Driver
 * binding does not depend on this - CDC-ACM is matched by class. */
#define USB_VID         0x1209u
#define USB_PID         0x0001u

static const uint8_t desc_device[18] = {
    18, 0x01,                       /* bLength, DEVICE                      */
    0x00, 0x02,                     /* bcdUSB 2.00                          */
    0x02, 0x00, 0x00,               /* class CDC, subclass 0, protocol 0    */
    EP0_SIZE,
    (uint8_t)(USB_VID & 0xFF), (uint8_t)(USB_VID >> 8),
    (uint8_t)(USB_PID & 0xFF), (uint8_t)(USB_PID >> 8),
    0x00, 0x03,                     /* bcdDevice 3.00 = firmware v3         */
    1, 2, 3,                        /* iManufacturer, iProduct, iSerial     */
    1                               /* bNumConfigurations                   */
};

#define CONFIG_LEN 67
static const uint8_t desc_config[CONFIG_LEN] = {
    /* configuration */
    9, 0x02, CONFIG_LEN, 0x00, 2, 1, 0,
    0x80,                           /* bus powered, no remote wakeup        */
    250,                            /* 500 mA: the PCAP04 rail comes from here */

    /* interface 0: CDC communication */
    9, 0x04, 0, 0, 1, 0x02, 0x02, 0x01, 0,
    5, 0x24, 0x00, 0x10, 0x01,      /* header, CDC 1.10                     */
    5, 0x24, 0x01, 0x00, 1,         /* call management, data iface 1        */
    4, 0x24, 0x02, 0x02,            /* ACM: supports Set/Get_Line_Coding    */
    5, 0x24, 0x06, 0, 1,            /* union: master 0, slave 1             */
    7, 0x05, 0x80 | EP_NOTIFY, 0x03, EPINT_SIZE, 0x00, 16,

    /* interface 1: CDC data */
    9, 0x04, 1, 0, 2, 0x0A, 0x00, 0x00, 0,
    7, 0x05, 0x00 | EP_DATA, 0x02, EPBULK_SIZE, 0x00, 0,
    7, 0x05, 0x80 | EP_DATA, 0x02, EPBULK_SIZE, 0x00, 0
};

static const uint8_t desc_lang[4] = { 4, 0x03, 0x09, 0x04 };

/* UTF-16LE, built by hand to avoid pulling in any string machinery */
#define STR(n, ...) static const uint8_t n[] = { __VA_ARGS__ }
STR(desc_manuf, 18, 0x03, 'T',0,'s',0,'i',0,'n',0,'g',0,'h',0,'u',0,'a',0);
STR(desc_prod,  46, 0x03, 'P',0,'C',0,'A',0,'P',0,'0',0,'4',0,' ',0,'A',0,
                          'c',0,'q',0,'u',0,'i',0,'s',0,'i',0,'t',0,'i',0,
                          'o',0,'n',0,' ',0,'3',0,'C',0,'H',0);

/* serial number: the 96-bit device UID as 24 hex digits, so two boards on the
 * same host always get distinct ports */
static uint8_t desc_serial[2 + 24 * 2];

static void build_serial(void)
{
    static const char hex[] = "0123456789ABCDEF";
    uint32_t i, w, k = 0;
    desc_serial[0] = sizeof(desc_serial);
    desc_serial[1] = 0x03;
    for (i = 0; i < 3u; i++) {
        w = *(__IO uint32_t *)(UID_BASE + 4u * i);
        for (int s = 28; s >= 0; s -= 4) {
            desc_serial[2 + 2 * k] = (uint8_t)hex[(w >> s) & 0xFu];
            desc_serial[3 + 2 * k] = 0;
            k++;
        }
    }
}

/* ------------------------------------------------------------------ */
/* state                                                               */
/* ------------------------------------------------------------------ */
#define TXQ_SIZE 2048u                 /* power of two */
#define RXQ_SIZE 256u

static uint8_t  g_txq[TXQ_SIZE];
static volatile uint32_t g_tx_wr, g_tx_rd;
static uint8_t  g_rxq[RXQ_SIZE];
static volatile uint32_t g_rx_wr, g_rx_rd;

static uint8_t  g_state;               /* 0 detached .. 3 configured */
static uint8_t  g_dtr;
static uint8_t  g_tx_busy;             /* a bulk IN packet is in flight */
static uint8_t  g_tx_zlp;              /* owe the host a zero-length packet */
static uint32_t g_dropped, g_resets;
static uint8_t  g_pending_addr;

/* control transfer in progress */
static const uint8_t *g_ctl_data;
static uint32_t g_ctl_len;
/* A host-to-device control transfer that carries data (SET_LINE_CODING) has
 * three stages: SETUP, an OUT data packet, then a zero-length IN packet from
 * us as the status stage.  Skipping that last step does not fail loudly - the
 * host just retries and eventually times out, which showed up as tty open
 * taking ~50 s on macOS while everything else worked. */
static uint8_t  g_ctl_out;
static uint8_t  g_line_coding[7] = { 0x00, 0xC2, 0x01, 0x00, 0, 0, 8 };  /* 115200 8N1 */

uint32_t usb_cdc_state(void)   { return g_state; }
uint32_t usb_cdc_dropped(void) { return g_dropped; }
uint32_t usb_cdc_resets(void)  { return g_resets; }
int      usb_cdc_ready(void)   { return (g_state == 3u) && g_dtr; }

/* ------------------------------------------------------------------ */
/* control endpoint                                                    */
/* ------------------------------------------------------------------ */
static void ep0_send(const uint8_t *data, uint32_t len)
{
    uint32_t n = (len > EP0_SIZE) ? EP0_SIZE : len;
    if (n) { pma_write(PMA_EP0_TX, data, n); }
    BD_TX(EP_CTRL) = (n << 16) | PMA_EP0_TX;
    g_ctl_data = data + n;
    g_ctl_len = len - n;
    ep_set_stat_tx(EP_CTRL, STATTX_VALID);
}

static void ep0_stall(void)
{
    ep_set_stat_tx(EP_CTRL, STATTX_STALL);
    ep_set_stat_rx(EP_CTRL, STATRX_STALL);
}

static void handle_setup(void)
{
    uint8_t s[8];
    uint32_t len, want;
    pma_read(PMA_EP0_RX, s, 8);
    want = (uint32_t)s[6] | ((uint32_t)s[7] << 8);

    /* standard device requests */
    if ((s[0] & 0x60u) == 0x00u) {
        switch (s[1]) {
        case 0x05:                                  /* SET_ADDRESS */
            g_pending_addr = s[2] & 0x7Fu;
            ep0_send(0, 0);
            return;
        case 0x06: {                                /* GET_DESCRIPTOR */
            const uint8_t *d = 0;
            switch (s[3]) {
            case 1: d = desc_device; len = sizeof(desc_device); break;
            case 2: d = desc_config; len = CONFIG_LEN; break;
            case 3:
                switch (s[2]) {
                case 0: d = desc_lang;  len = sizeof(desc_lang);  break;
                case 1: d = desc_manuf; len = sizeof(desc_manuf); break;
                case 2: d = desc_prod;  len = sizeof(desc_prod);  break;
                case 3: d = desc_serial; len = sizeof(desc_serial); break;
                default: break;
                }
                break;
            default: break;
            }
            if (!d) { ep0_stall(); return; }
            if (len > want) { len = want; }
            ep0_send(d, len);
            return;
        }
        case 0x09:                                  /* SET_CONFIGURATION */
            g_state = s[2] ? 3u : 2u;
            ep0_send(0, 0);
            return;
        case 0x08: {                                /* GET_CONFIGURATION */
            static uint8_t cfg;
            cfg = (g_state == 3u);
            ep0_send(&cfg, 1);
            return;
        }
        case 0x00: {                                /* GET_STATUS */
            static const uint8_t st[2] = { 0, 0 };
            ep0_send(st, 2);
            return;
        }
        case 0x01: case 0x03:                       /* CLEAR/SET_FEATURE */
            ep0_send(0, 0);
            return;
        default:
            ep0_stall();
            return;
        }
    }

    /* CDC class requests on the communication interface */
    if ((s[0] & 0x60u) == 0x20u) {
        switch (s[1]) {
        case 0x20:                                  /* SET_LINE_CODING */
            /* data stage arrives on the next OUT; we keep the values only so
             * GET_LINE_CODING can echo them back - there is no real UART
             * behind this endpoint */
            g_ctl_out = 1;
            ep_set_stat_rx(EP_CTRL, STATRX_VALID);
            return;
        case 0x21:                                  /* GET_LINE_CODING */
            ep0_send(g_line_coding, 7);
            return;
        case 0x22:                                  /* SET_CONTROL_LINE_STATE */
            g_dtr = (s[2] & 1u) ? 1u : 0u;
            ep0_send(0, 0);
            return;
        case 0x23:                                  /* SEND_BREAK */
            ep0_send(0, 0);
            return;
        default:
            ep0_stall();
            return;
        }
    }
    ep0_stall();
}

static void handle_ep0(uint32_t istr)
{
    if (istr & ISTR_DIR) {                          /* OUT or SETUP */
        uint32_t r = USB_CHEP(EP_CTRL);
        ep_clear_vtrx(EP_CTRL);
        if (r & EP_SETUP) {
            handle_setup();
            ep_set_stat_rx(EP_CTRL, STATRX_VALID);
        } else if (g_ctl_out) {
            /* data stage of SET_LINE_CODING: take the bytes, then close the
             * transfer with a zero-length IN - the host waits for it */
            uint32_t n = (BD_RX(EP_CTRL) >> 16) & 0x3FFu;
            if (n > sizeof(g_line_coding)) { n = sizeof(g_line_coding); }
            pma_read(PMA_EP0_RX, g_line_coding, n);
            g_ctl_out = 0;
            ep_set_stat_rx(EP_CTRL, STATRX_VALID);
            ep0_send(0, 0);
        } else {
            /* status stage of an IN transfer we already completed */
            ep_set_stat_rx(EP_CTRL, STATRX_VALID);
        }
    } else {                                        /* IN completed */
        ep_clear_vttx(EP_CTRL);
        if (g_pending_addr) {
            USB_DADDR = 0x80u | g_pending_addr;
            g_state = 2u;
            g_pending_addr = 0;
        }
        if (g_ctl_len) {
            ep0_send(g_ctl_data, g_ctl_len);
        } else {
            ep_set_stat_rx(EP_CTRL, STATRX_VALID);
        }
    }
}

/* ------------------------------------------------------------------ */
/* bulk data                                                           */
/* ------------------------------------------------------------------ */
static void rx_packet(void)
{
    uint8_t buf[EPBULK_SIZE];
    uint32_t n = (BD_RX(EP_DATA) >> 16) & 0x3FFu;
    uint32_t i;
    if (n > EPBULK_SIZE) { n = EPBULK_SIZE; }
    pma_read(PMA_EP2_RX, buf, n);
    for (i = 0; i < n; i++) {
        uint32_t nxt = (g_rx_wr + 1u) % RXQ_SIZE;
        if (nxt == g_rx_rd) { break; }              /* console is not reading */
        g_rxq[g_rx_wr] = buf[i];
        g_rx_wr = nxt;
    }
    ep_set_stat_rx(EP_DATA, STATRX_VALID);
}

static void tx_pump(void)
{
    uint8_t buf[EPBULK_SIZE];
    uint32_t n = 0;
    if (g_tx_busy || g_state != 3u) { return; }
    while (n < EPBULK_SIZE && g_tx_rd != g_tx_wr) {
        buf[n++] = g_txq[g_tx_rd];
        g_tx_rd = (g_tx_rd + 1u) % TXQ_SIZE;
    }
    if (n == 0u) {
        if (!g_tx_zlp) { return; }
        g_tx_zlp = 0;                               /* terminate the transfer */
    } else {
        /* a full-size packet needs a following zero-length packet so the host
         * knows the transfer ended */
        g_tx_zlp = (n == EPBULK_SIZE);
        pma_write(PMA_EP2_TX, buf, n);
    }
    BD_TX(EP_DATA) = (n << 16) | PMA_EP2_TX;
    g_tx_busy = 1;
    ep_set_stat_tx(EP_DATA, STATTX_VALID);
}

/* ------------------------------------------------------------------ */
/* reset / init                                                        */
/* ------------------------------------------------------------------ */
static void on_reset(void)
{
    g_resets++;
    g_state = 1u;
    g_dtr = 0;
    g_tx_busy = 0;
    g_tx_zlp = 0;
    g_pending_addr = 0;
    g_ctl_len = 0;
    g_ctl_out = 0;
    g_tx_rd = g_tx_wr = 0;
    g_rx_rd = g_rx_wr = 0;

    BD_TX(EP_CTRL) = PMA_EP0_TX;
    BD_RX(EP_CTRL) = RX_BLOCKS_64 | PMA_EP0_RX;
    USB_CHEP(EP_CTRL) = EP_TYPE_CONTROL | EP_CTRL;
    ep_set_stat_rx(EP_CTRL, STATRX_VALID);
    ep_set_stat_tx(EP_CTRL, STATTX_NAK);

    BD_TX(EP_NOTIFY) = PMA_EP1_TX;
    USB_CHEP(EP_NOTIFY) = EP_TYPE_INTR | EP_NOTIFY;
    ep_set_stat_tx(EP_NOTIFY, STATTX_NAK);
    ep_set_stat_rx(EP_NOTIFY, STATRX_DISABLED);

    BD_TX(EP_DATA) = PMA_EP2_TX;
    BD_RX(EP_DATA) = RX_BLOCKS_64 | PMA_EP2_RX;
    USB_CHEP(EP_DATA) = EP_TYPE_BULK | EP_DATA;
    ep_set_stat_rx(EP_DATA, STATRX_VALID);
    ep_set_stat_tx(EP_DATA, STATTX_NAK);

    USB_DADDR = 0x80u;                              /* enabled, address 0 */
}

void usb_cdc_init(void)
{
    uint32_t guard;

    build_serial();

    /* HSI48 for the USB kernel clock; the core stays on the 64 MHz PLL */
    RCC_CR |= RCC_CR_HSI48ON;
    guard = 200000u;
    while (!(RCC_CR & RCC_CR_HSI48RDY) && guard) { guard--; }
    if (!guard) { return; }                         /* no USB, UART still works */

    RCC_CCIPR2 &= ~RCC_CCIPR2_USBSEL;               /* 00 = HSI48 */
    RCC_APBENR1 |= RCC_APBENR1_USBEN | RCC_APBENR1_CRSEN;

    /* CRS_CFGR resets to the USB-SOF configuration already, so only turn the
     * trimmer on: HSI48 alone is not accurate enough for USB over temperature */
    CRS_CR |= (1UL << 6) | (1UL << 5);              /* AUTOTRIMEN | CEN */

    USB_CNTR &= ~CNTR_HOST;                         /* device mode */
    USB_CNTR &= ~CNTR_PDWN;                         /* power up the transceiver */
    guard = 20000u; while (guard--) { __asm volatile("nop"); }   /* t_STARTUP */
    USB_CNTR &= ~CNTR_USBRST;

    USB_ISTR = 0;
    on_reset();
    USB_BCDR |= BCDR_DPPU;                          /* pull DP up = attach */
    g_state = 1u;
}

/* Detach cleanly before handing the machine to the ROM bootloader.  If the
 * pull-up were left asserted and the peripheral configured, the ROM's own USB
 * init would come up on a bus that already has a device attached and DFU
 * enumeration can fail - which would break both the 'dfu' command and the
 * double-tap RESET escape hatch, i.e. every way back into the board. */
void usb_cdc_detach(void)
{
    if (!(RCC_APBENR1 & RCC_APBENR1_USBEN)) { return; }
    USB_BCDR &= ~BCDR_DPPU;                 /* host sees a disconnect */
    USB_CNTR = CNTR_PDWN | CNTR_USBRST;
    RCC_APBENR1 &= ~(RCC_APBENR1_USBEN | RCC_APBENR1_CRSEN);
    RCC_CR &= ~RCC_CR_HSI48ON;
    g_state = 0;
    g_dtr = 0;
}

void usb_cdc_poll(void)
{
    uint32_t istr, n, budget = 32u;

    if (!(RCC_APBENR1 & RCC_APBENR1_USBEN)) { return; }

    while (budget--) {
        istr = USB_ISTR;
        if (istr & ISTR_RESET) {
            USB_ISTR = (uint32_t)~ISTR_RESET;
            on_reset();
            continue;
        }
        if (istr & ISTR_WKUP) { USB_ISTR = (uint32_t)~ISTR_WKUP; continue; }
        if (istr & ISTR_SUSP) { USB_ISTR = (uint32_t)~ISTR_SUSP; continue; }
        if (!(istr & ISTR_CTR)) { break; }

        n = istr & ISTR_IDN_MASK;
        if (n == EP_CTRL) {
            handle_ep0(istr);
        } else if (n == EP_DATA) {
            if (istr & ISTR_DIR) {
                ep_clear_vtrx(EP_DATA);
                rx_packet();
            } else {
                ep_clear_vttx(EP_DATA);
                g_tx_busy = 0;
            }
        } else {
            ep_clear_vtrx(n);
            ep_clear_vttx(n);
        }
    }
    tx_pump();
}

/* ------------------------------------------------------------------ */
/* console interface                                                   */
/* ------------------------------------------------------------------ */
void usb_cdc_putc(char c)
{
    uint32_t nxt;
    if (g_state != 3u) { return; }        /* not enumerated: silently discard */
    nxt = (g_tx_wr + 1u) % TXQ_SIZE;
    if (nxt == g_tx_rd) {
        /* Host is not draining us.  Dropping is the only safe answer: the
         * console must never block, or a closed terminal would freeze a
         * measurement in progress. */
        g_dropped++;
        return;
    }
    g_txq[g_tx_wr] = (uint8_t)c;
    g_tx_wr = nxt;
    if (!g_tx_busy) { tx_pump(); }
}

int usb_cdc_getc(void)
{
    int c;
    if (g_rx_rd == g_rx_wr) { return -1; }
    c = (int)g_rxq[g_rx_rd];
    g_rx_rd = (g_rx_rd + 1u) % RXQ_SIZE;
    return c;
}
