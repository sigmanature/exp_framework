/*
 * fragmem - Deterministic memory fragmentor for Android devices.
 *
 * Creates a predictable, fragmented memory state by:
 *   1. mmap a large anonymous region
 *   2. Touch every page to force physical allocation
 *   3. munmap every other chunk (checkerboard pattern) to create order-0/1 fragments
 *   4. Set oom_score_adj = -1000 (immune to OOM killer)
 *   5. Read /proc/buddyinfo to verify fragmentation
 *   6. Print "FRAGMEM_READY" to stdout and pause() indefinitely (zero CPU)
 *
 * The retained pages prevent buddy coalescing, keeping high-order blocks fragmented.
 *
 * Usage:
 *   fragmem --alloc-mb <total_MB> [--chunk-kb <chunk_KB>] [--stride <N>]
 *           [--threshold <sum_order2_plus>] [--zone <zone_name>]
 *
 * Parameters:
 *   --alloc-mb     Total memory to initially mmap (MB). Half will be retained.
 *   --chunk-kb     Size of each chunk for the checkerboard (default: 16 = one 16KB folio).
 *   --stride       Keep 1, release (stride-1). Default 2 = keep half, release half.
 *   --threshold    Target sum(order >= 2) in buddyinfo. 0 = don't check, just fragment.
 *   --zone         Zone name to check in buddyinfo (default: "Normal").
 *
 * Build (static, aarch64):
 *   aarch64-linux-gnu-gcc -O2 -static -o fragmem fragmem.c
 *
 * Or on-device with Android NDK toolchain.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#define PAGE_SIZE       4096
#define DEFAULT_CHUNK   (4 * 1024)    /* 4KB = 1 page: ensures even order-1 is broken */
#define DEFAULT_STRIDE  2             /* keep 1 page, release 1 page */
#define MAX_ORDER       11
#define BUDDY_LINE_MAX  256

/* ---- helpers ---- */

static void die(const char *msg) {
    fprintf(stderr, "fragmem: %s: %s\n", msg, strerror(errno));
    exit(1);
}

static void set_oom_adj(int score) {
    int fd = open("/proc/self/oom_score_adj", O_WRONLY);
    if (fd < 0) {
        fprintf(stderr, "fragmem: warning: cannot open oom_score_adj: %s\n", strerror(errno));
        return;
    }
    char buf[16];
    int n = snprintf(buf, sizeof(buf), "%d", score);
    if (write(fd, buf, n) < 0) {
        fprintf(stderr, "fragmem: warning: cannot write oom_score_adj: %s\n", strerror(errno));
    }
    close(fd);
}

/*
 * Parse /proc/buddyinfo for a given zone, return sum of orders >= min_order.
 * Returns -1 on failure.
 */
static long read_buddyinfo_sum(const char *zone_name, int min_order) {
    FILE *f = fopen("/proc/buddyinfo", "r");
    if (!f) return -1;

    char line[512];
    long total = 0;
    int found = 0;

    while (fgets(line, sizeof(line), f)) {
        /* Format: "Node N, zone   ZoneName   o0 o1 o2 ... o10" */
        char *zp = strstr(line, "zone");
        if (!zp) continue;
        zp += 4; /* skip "zone" */
        while (*zp == ' ') zp++;

        /* Extract zone name */
        char zname[64] = {0};
        int i = 0;
        while (*zp && *zp != ' ' && *zp != '\t' && i < 63) {
            zname[i++] = *zp++;
        }
        zname[i] = 0;

        if (strcmp(zname, zone_name) != 0) continue;
        found = 1;

        /* Parse order values */
        int order = 0;
        while (*zp) {
            while (*zp == ' ' || *zp == '\t') zp++;
            if (*zp == '\0' || *zp == '\n') break;
            long val = strtol(zp, &zp, 10);
            if (order >= min_order) {
                total += val * (1L << (order - min_order));
            }
            order++;
        }
    }
    fclose(f);
    return found ? total : -1;
}

/* Print full buddyinfo to stderr for diagnostics */
static void dump_buddyinfo(void) {
    FILE *f = fopen("/proc/buddyinfo", "r");
    if (!f) return;
    char line[512];
    fprintf(stderr, "--- /proc/buddyinfo ---\n");
    while (fgets(line, sizeof(line), f)) {
        fprintf(stderr, "%s", line);
    }
    fprintf(stderr, "---\n");
    fclose(f);
}

/* ---- main ---- */

static void usage(void) {
    fprintf(stderr,
        "Usage: fragmem --alloc-mb <MB> [options]\n"
        "Options:\n"
        "  --alloc-mb <MB>       Total mmap size (required)\n"
        "  --chunk-kb <KB>       Chunk size for pattern (default: 16)\n"
        "  --stride <N>          Keep 1 chunk, release N-1 (default: 2)\n"
        "  --threshold <N>       Target sum(order>=2); 0=skip check (default: 0)\n"
        "  --zone <name>         Buddyinfo zone (default: Normal)\n"
        "  --min-order <N>       Min order to sum (default: 2)\n"
        "  --quiet               Suppress progress output\n"
        "\n"
        "Example:\n"
        "  fragmem --alloc-mb 3000 --threshold 2000\n"
    );
    exit(1);
}

int main(int argc, char **argv) {
    size_t alloc_mb = 0;
    size_t chunk_kb = DEFAULT_CHUNK / 1024;
    int stride = DEFAULT_STRIDE;
    long threshold = 0;
    int min_order = 2;
    const char *zone = "Normal";
    int quiet = 0;

    /* Parse args */
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--alloc-mb") == 0 && i + 1 < argc) {
            alloc_mb = (size_t)atol(argv[++i]);
        } else if (strcmp(argv[i], "--chunk-kb") == 0 && i + 1 < argc) {
            chunk_kb = (size_t)atol(argv[++i]);
        } else if (strcmp(argv[i], "--stride") == 0 && i + 1 < argc) {
            stride = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--threshold") == 0 && i + 1 < argc) {
            threshold = atol(argv[++i]);
        } else if (strcmp(argv[i], "--zone") == 0 && i + 1 < argc) {
            zone = argv[++i];
        } else if (strcmp(argv[i], "--min-order") == 0 && i + 1 < argc) {
            min_order = atoi(argv[++i]);
        } else if (strcmp(argv[i], "--quiet") == 0) {
            quiet = 1;
        } else {
            fprintf(stderr, "Unknown option: %s\n", argv[i]);
            usage();
        }
    }

    if (alloc_mb == 0) {
        fprintf(stderr, "fragmem: --alloc-mb is required\n");
        usage();
    }
    if (stride < 2) stride = 2;

    size_t chunk_size = chunk_kb * 1024;
    size_t total_size = alloc_mb * 1024UL * 1024UL;
    size_t num_chunks = total_size / chunk_size;

    if (!quiet) {
        fprintf(stderr, "fragmem: alloc=%zuMB chunk=%zuKB stride=%d chunks=%zu\n",
                alloc_mb, chunk_kb, stride, num_chunks);
        fprintf(stderr, "fragmem: will retain %zu/%zu chunks (~%zuMB)\n",
                num_chunks / stride, num_chunks,
                (num_chunks / stride * chunk_size) / (1024 * 1024));
    }

    /* Step 1: mmap large anonymous region */
    void *base = mmap(NULL, total_size, PROT_READ | PROT_WRITE,
                      MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
    if (base == MAP_FAILED)
        die("mmap");

    if (!quiet)
        fprintf(stderr, "fragmem: mmap OK at %p (%zu MB)\n", base, alloc_mb);

    /* Step 2: Touch every page to force physical allocation */
    volatile char *p = (volatile char *)base;
    for (size_t off = 0; off < total_size; off += PAGE_SIZE) {
        p[off] = (char)(off & 0xFF);
    }

    if (!quiet)
        fprintf(stderr, "fragmem: touch done\n");

    /* Step 3: Release chunks in checkerboard pattern.
     * Keep chunk index % stride == 0, release all others.
     * This fragments the physical memory: retained chunks prevent coalescing.
     */
    size_t released = 0;
    for (size_t i = 0; i < num_chunks; i++) {
        if (i % stride != 0) {
            /* Release physical pages but keep VMA intact */
            void *addr = (char *)base + i * chunk_size;
            if (madvise(addr, chunk_size, MADV_DONTNEED) < 0) {
                fprintf(stderr, "fragmem: madvise chunk %zu failed: %s\n", i, strerror(errno));
            }
            released++;
        }
    }

    if (!quiet) {
        fprintf(stderr, "fragmem: released %zu chunks, retained %zu chunks (~%zu MB held)\n",
                released, num_chunks - released,
                ((num_chunks - released) * chunk_size) / (1024 * 1024));
    }

    /* Step 4: Set OOM score to -1000 (immune) */
    set_oom_adj(-1000);

    /* Step 5: Check buddyinfo */
    long sum = read_buddyinfo_sum(zone, min_order);
    if (!quiet) {
        fprintf(stderr, "fragmem: buddyinfo sum(order>=%d, zone=%s) = %ld\n",
                min_order, zone, sum);
        dump_buddyinfo();
    }

    if (threshold > 0 && sum > threshold) {
        fprintf(stderr, "fragmem: WARNING: sum %ld > threshold %ld (may need more alloc-mb)\n",
                sum, threshold);
    }

    /* Step 6: Signal ready and sleep forever */
    printf("FRAGMEM_READY alloc_mb=%zu held_mb=%zu sum_order%d=%ld threshold=%ld\n",
           alloc_mb,
           ((num_chunks - released) * chunk_size) / (1024 * 1024),
           min_order, sum, threshold);
    fflush(stdout);

    /* Infinite sleep — zero CPU */
    for (;;) {
        pause();
    }

    return 0;  /* unreachable */
}
