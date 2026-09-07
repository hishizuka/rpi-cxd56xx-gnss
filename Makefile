# SPDX-License-Identifier: Apache-2.0
CC ?= cc
CFLAGS ?= -O2 -std=gnu11 -Wall -Wextra
CPPFLAGS += -Isrc/cxd56xx_gnss
ifdef CXD56XX_GPIO_CHIP
CPPFLAGS += -DCXD5610_GPIO_CHIP_DEV='"$(CXD56XX_GPIO_CHIP)"'
endif
LDLIBS += -lgpiod -pthread -lm

.PHONY: all FORCE
all: build/cxd5610

# Rebuild when command-line settings change, even without source changes.
build/cxd5610: src/cxd56xx_gnss/cxd5610_rpi.c src/cxd56xx_gnss/cxd5610_rpi.h FORCE
	mkdir -p build
	$(CC) $(CPPFLAGS) $(CFLAGS) $< -o $@ $(LDFLAGS) $(LDLIBS)

FORCE:
