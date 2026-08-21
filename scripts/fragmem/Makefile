CC = aarch64-linux-gnu-gcc
CFLAGS = -O2 -Wall -Wextra -static
TARGET = fragmem

all: $(TARGET)

$(TARGET): fragmem.c
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f $(TARGET)

push: $(TARGET)
	adb push $(TARGET) /data/local/tmp/fragmem
	adb shell chmod 755 /data/local/tmp/fragmem

.PHONY: all clean push
