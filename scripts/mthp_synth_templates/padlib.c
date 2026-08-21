#include <stddef.h>
#include <stdint.h>

#ifndef PAD_INDEX
#define PAD_INDEX 0
#endif
#ifndef PAD_RODATA_KB
#define PAD_RODATA_KB 256
#endif
#ifndef PAD_DATA_KB
#define PAD_DATA_KB 64
#endif

__attribute__((used, visibility("default"), aligned(4096)))
const uint8_t mthp_pad_rodata[PAD_RODATA_KB * 1024] = { [0 ... (PAD_RODATA_KB * 1024 - 1)] = (uint8_t)(PAD_INDEX + 17) };

__attribute__((used, visibility("default"), aligned(4096)))
uint8_t mthp_pad_data[PAD_DATA_KB * 1024];

__attribute__((visibility("default")))
size_t mthp_pad_touch(size_t stride, size_t write_data) {
    volatile size_t sum = 0;
    if (stride == 0) stride = 4096;
    for (size_t i = 0; i < sizeof(mthp_pad_rodata); i += stride) {
        sum += mthp_pad_rodata[i];
    }
    if (write_data) {
        for (size_t i = 0; i < sizeof(mthp_pad_data); i += 4096) {
            mthp_pad_data[i] = (uint8_t)(sum + i + PAD_INDEX);
        }
    }
    return sum;
}
