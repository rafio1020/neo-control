obj-m := neo_control.o
KVER ?= $(shell uname -r)
KDIR ?= /lib/modules/$(KVER)/build
# CachyOS kernels commonly use Clang/LTO. Match the target kernel toolchain.
ifneq ($(shell grep -s '^CONFIG_CC_IS_CLANG=y$$' $(KDIR)/include/config/auto.conf),)
KBUILD_TOOLCHAIN := LLVM=1
endif
all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) $(KBUILD_TOOLCHAIN) modules
clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) $(KBUILD_TOOLCHAIN) clean
