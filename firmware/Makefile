# PCAP04 v1.5 stage-1 diagnostic firmware
TARGET   := pcap04_diag
CROSS    := arm-none-eabi-
CC       := $(CROSS)gcc
OBJCOPY  := $(CROSS)objcopy
SIZE     := $(CROSS)size

CPU      := -mcpu=cortex-m0plus -mthumb
CFLAGS   := $(CPU) -Os -g3 -std=c11 -ffreestanding \
            -ffunction-sections -fdata-sections \
            -Wall -Wextra -Wshadow -Wundef -Werror=implicit-function-declaration
LDSCRIPT := stm32g0b1cb.ld
LDFLAGS  := $(CPU) -nostdlib -T$(LDSCRIPT) -Wl,--gc-sections -Wl,-Map=$(TARGET).map
LDLIBS   := -lgcc

SRCS := startup.c main.c usb_cdc.c
OBJS := $(SRCS:.c=.o)

all: $(TARGET).elf $(TARGET).bin $(TARGET).hex size

%.o: %.c stm32g0b1.h board.h usb_cdc.h
	$(CC) $(CFLAGS) -c $< -o $@

$(TARGET).elf: $(OBJS) $(LDSCRIPT)
	$(CC) $(OBJS) $(LDFLAGS) $(LDLIBS) -o $@

$(TARGET).bin: $(TARGET).elf
	$(OBJCOPY) -O binary $< $@

$(TARGET).hex: $(TARGET).elf
	$(OBJCOPY) -O ihex $< $@

size: $(TARGET).elf
	@$(SIZE) $<

clean:
	rm -f $(OBJS) $(TARGET).elf $(TARGET).bin $(TARGET).hex $(TARGET).map

.PHONY: all clean size
