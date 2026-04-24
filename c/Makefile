# ccrun - C Implementation
# Compile with: make

CC = gcc
CFLAGS = -Wall -Wextra -O2
LDFLAGS = -lcurl -ljson-c

TARGET = ccrun

.PHONY: all clean

all: $(TARGET)

$(TARGET): ccrun.c
	$(CC) $(CFLAGS) -o $(TARGET) ccrun.c $(LDFLAGS)

clean:
	rm -f $(TARGET)
