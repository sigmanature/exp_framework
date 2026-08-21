#include <jni.h>
#include <android/log.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include <atomic>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#ifndef PR_SET_VMA
#define PR_SET_VMA 0x53564d41
#endif
#ifndef PR_SET_VMA_ANON_NAME
#define PR_SET_VMA_ANON_NAME 0
#endif

#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, "ZZMthpSynthNative", __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, "ZZMthpSynthNative", __VA_ARGS__)

struct Region {
    uint8_t* ptr;
    size_t size;
    size_t pages;
};

struct Config {
    int profile_index = 0;
    int process_count = 1;
    int vma_count = 256;
    int vma_size_kb = 64;
    int parent_touch_pages = 1024;
    int scudo_threads = 2;
    int scudo_live_mb = 32;
    int small_alloc_bytes = 128;
    int large_alloc_bytes = 65536;
    int dlopen_lib_count = 0;
    int fork_children = 0;
    int cow_pages_per_child = 0;
    int filemap_threads = 0;
    int filemap_file_mb = 0;
};

static std::once_flag g_once;
static std::atomic<bool> g_started{false};
static std::atomic<uint64_t> g_fork_rounds{0};
static std::atomic<uint64_t> g_cow_pages_written{0};
static std::atomic<uint64_t> g_dlopen_ok{0};
static std::atomic<uint64_t> g_anon_pages_written{0};
static std::atomic<uint64_t> g_vma_name_failures{0};
static std::vector<Region> g_regions;
static std::string g_status;

static int find_int(const std::string& json, const char* key, int def) {
    std::string needle = std::string("\"") + key + "\"";
    size_t pos = json.find(needle);
    if (pos == std::string::npos) return def;
    pos = json.find(':', pos + needle.size());
    if (pos == std::string::npos) return def;
    pos++;
    while (pos < json.size() && (json[pos] == ' ' || json[pos] == '\t')) pos++;
    char* end = nullptr;
    long v = strtol(json.c_str() + pos, &end, 10);
    if (end == json.c_str() + pos) return def;
    return static_cast<int>(v);
}

static void name_vma(void* ptr, size_t size, const char* name) {
    if (prctl(PR_SET_VMA, PR_SET_VMA_ANON_NAME, ptr, size, name) != 0) {
        g_vma_name_failures.fetch_add(1, std::memory_order_relaxed);
    }
}

static void setup_regions(const Config& cfg, int process_index) {
    int vma_count = cfg.vma_count;
    if (process_index > 0 && vma_count > 0) {
        vma_count = std::max(32, vma_count / 3);
    }
    size_t region_size = static_cast<size_t>(std::max(4, cfg.vma_size_kb)) * 1024ULL;
    size_t page_size = static_cast<size_t>(sysconf(_SC_PAGESIZE));
    size_t guard_size = 16ULL * 1024ULL;
    for (int i = 0; i < vma_count; i++) {
        size_t mapping_size = region_size + guard_size;
        void* p = mmap(nullptr, mapping_size, PROT_READ | PROT_WRITE,
                       MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE, -1, 0);
        if (p == MAP_FAILED) continue;
        void* guard = reinterpret_cast<uint8_t*>(p) + region_size;
        if (mprotect(guard, guard_size, PROT_NONE) != 0) {
            munmap(p, mapping_size);
            continue;
        }
        char name[64];
        snprintf(name, sizeof(name), "mthp_vma_%05d", i);
        name_vma(p, region_size, name);
        snprintf(name, sizeof(name), "mthp_guard_%05d", i);
        name_vma(guard, guard_size, name);
        g_regions.push_back({reinterpret_cast<uint8_t*>(p), region_size, region_size / page_size});
    }
    uint64_t touched = 0;
    for (Region& r : g_regions) {
        for (size_t off = 0; off < r.size; off += page_size) {
            r.ptr[off] = static_cast<uint8_t>(touched);
            touched++;
        }
    }
    g_anon_pages_written.fetch_add(touched, std::memory_order_relaxed);
    LOGI("regions=%zu region_size=%zu guard_size=%zu anon_pages_written=%llu process_index=%d vma_name_failures=%llu", g_regions.size(), region_size, guard_size, (unsigned long long)touched, process_index, (unsigned long long)g_vma_name_failures.load(std::memory_order_relaxed));
}

static void scudo_worker(Config cfg, int worker_id, int process_index) {
    int live_mb = std::max(0, cfg.scudo_live_mb);
    if (process_index > 0) live_mb = live_mb / 3;
    size_t target = static_cast<size_t>(live_mb) * 1024ULL * 1024ULL / std::max(1, cfg.scudo_threads);
    size_t small = static_cast<size_t>(std::max(16, cfg.small_alloc_bytes));
    size_t large = static_cast<size_t>(std::max(4096, cfg.large_alloc_bytes));
    std::vector<void*> blocks;
    std::vector<size_t> sizes;
    uint64_t salt = worker_id + 1;
    size_t live = 0;
    while (live < target) {
        size_t sz = ((salt++ % 17) == 0) ? large : small + ((salt % 16) * 16);
        void* p = malloc(sz);
        if (!p) break;
        for (size_t off = 0; off < sz; off += 4096) {
            reinterpret_cast<uint8_t*>(p)[off] = static_cast<uint8_t>(salt);
        }
        blocks.push_back(p);
        sizes.push_back(sz);
        live += sz;
    }
    LOGI("scudo_worker process=%d worker=%d live_bytes=%zu blocks=%zu", process_index, worker_id, live, blocks.size());
}

using touch_fn_t = size_t (*)(size_t, size_t);

static void dlopen_libs(const Config& cfg, const std::string& native_dir) {
    int count = std::max(0, cfg.dlopen_lib_count);
    size_t page_size = static_cast<size_t>(sysconf(_SC_PAGESIZE));
    for (int i = 0; i < count; i++) {
        char path[1024];
        snprintf(path, sizeof(path), "%s/libmthppad%03d.so", native_dir.c_str(), i);
        void* h = dlopen(path, RTLD_NOW | RTLD_LOCAL);
        if (!h) {
            LOGE("dlopen failed %s: %s", path, dlerror());
            continue;
        }
        auto fn = reinterpret_cast<touch_fn_t>(dlsym(h, "mthp_pad_touch"));
        if (fn) {
            fn(page_size, 0);
        }
        g_dlopen_ok.fetch_add(1, std::memory_order_relaxed);
    }
}

static void child_write_cow(const Config& cfg) {
    size_t page_size = static_cast<size_t>(sysconf(_SC_PAGESIZE));
    int target = cfg.cow_pages_per_child;
    int written = 0;
    for (size_t pass = 0; written < target && pass < 1048576; pass++) {
        for (Region& r : g_regions) {
            if (written >= target) break;
            if (pass < r.pages) {
                r.ptr[pass * page_size] = static_cast<uint8_t>(written + 3);
                written++;
            }
        }
    }
    g_cow_pages_written.fetch_add(static_cast<uint64_t>(written), std::memory_order_relaxed);
}

static void fork_worker(Config cfg) {
    if (cfg.fork_children <= 0 || cfg.cow_pages_per_child <= 0) return;
    std::vector<pid_t> pids;
    for (int i = 0; i < cfg.fork_children; i++) {
        pid_t pid = fork();
        if (pid == 0) {
            child_write_cow(cfg);
            _exit(0);
        }
        if (pid > 0) pids.push_back(pid);
    }
    for (pid_t pid : pids) {
        int status = 0;
        waitpid(pid, &status, 0);
    }
    uint64_t cow_pages = static_cast<uint64_t>(pids.size()) * static_cast<uint64_t>(cfg.cow_pages_per_child);
    g_cow_pages_written.fetch_add(cow_pages, std::memory_order_relaxed);
    uint64_t rounds = g_fork_rounds.fetch_add(1, std::memory_order_relaxed) + 1;
    LOGI("fork_round=%llu children=%zu cow_pages_target=%llu",
         (unsigned long long)rounds, pids.size(), (unsigned long long)cow_pages);
}

static void filemap_worker(Config cfg, std::string files_dir, int id) {
    if (cfg.filemap_threads <= 0 || cfg.filemap_file_mb <= 0) return;
    char path[1024];
    snprintf(path, sizeof(path), "%s/mthp_filemap_%d.bin", files_dir.c_str(), id);
    int fd = open(path, O_RDWR | O_CREAT, 0600);
    if (fd < 0) return;
    size_t len = static_cast<size_t>(cfg.filemap_file_mb) * 1024ULL * 1024ULL;
    ftruncate(fd, static_cast<off_t>(len));
    size_t stride = static_cast<size_t>(sysconf(_SC_PAGESIZE));
    uint8_t* p = reinterpret_cast<uint8_t*>(mmap(nullptr, len, PROT_READ, MAP_SHARED, fd, 0));
    if (p != MAP_FAILED) {
        volatile uint8_t sink = 0;
        size_t pages_read = 0;
        for (size_t off = 0; off < len; off += stride) {
            sink ^= p[off];
            pages_read++;
        }
        (void)sink;
        LOGI("filemap_worker id=%d bytes=%zu pages_read=%zu", id, len, pages_read);
        munmap(p, len);
    }
}

static Config parse_config(const std::string& json) {
    Config c;
    c.profile_index = find_int(json, "profile_index", c.profile_index);
    c.process_count = find_int(json, "process_count", c.process_count);
    c.vma_count = find_int(json, "vma_count", c.vma_count);
    c.vma_size_kb = find_int(json, "vma_size_kb", c.vma_size_kb);
    c.parent_touch_pages = find_int(json, "parent_touch_pages", c.parent_touch_pages);
    c.scudo_threads = find_int(json, "scudo_threads", c.scudo_threads);
    c.scudo_live_mb = find_int(json, "scudo_live_mb", c.scudo_live_mb);
    c.small_alloc_bytes = find_int(json, "small_alloc_bytes", c.small_alloc_bytes);
    c.large_alloc_bytes = find_int(json, "large_alloc_bytes", c.large_alloc_bytes);
    c.dlopen_lib_count = find_int(json, "dlopen_lib_count", c.dlopen_lib_count);
    c.fork_children = find_int(json, "fork_children", c.fork_children);
    c.cow_pages_per_child = find_int(json, "cow_pages_per_child", c.cow_pages_per_child);
    c.filemap_threads = find_int(json, "filemap_threads", c.filemap_threads);
    c.filemap_file_mb = find_int(json, "filemap_file_mb", c.filemap_file_mb);
    return c;
}

extern "C" JNIEXPORT jstring JNICALL
Java_com_zzhao_mthp_synthetic_WorkloadRuntime_nativeStart(JNIEnv* env, jclass, jstring jjson, jstring jnative_dir, jstring jfiles_dir, jint process_index, jstring jlabel) {
    const char* json_chars = env->GetStringUTFChars(jjson, nullptr);
    const char* native_dir_chars = env->GetStringUTFChars(jnative_dir, nullptr);
    const char* files_dir_chars = env->GetStringUTFChars(jfiles_dir, nullptr);
    const char* label_chars = env->GetStringUTFChars(jlabel, nullptr);
    std::string json = json_chars ? json_chars : "{}";
    std::string native_dir = native_dir_chars ? native_dir_chars : "";
    std::string files_dir = files_dir_chars ? files_dir_chars : "/data/local/tmp";
    std::string label = label_chars ? label_chars : "unknown";
    env->ReleaseStringUTFChars(jjson, json_chars);
    env->ReleaseStringUTFChars(jnative_dir, native_dir_chars);
    env->ReleaseStringUTFChars(jfiles_dir, files_dir_chars);
    env->ReleaseStringUTFChars(jlabel, label_chars);

    Config cfg = parse_config(json);
    std::call_once(g_once, [&]() {
        setup_regions(cfg, static_cast<int>(process_index));
        dlopen_libs(cfg, native_dir);
        Config process_cfg = cfg;
        if (process_index > 0) {
            process_cfg.cow_pages_per_child = std::max(0, cfg.cow_pages_per_child / 3);
        }
        int scudo_threads = std::max(0, cfg.scudo_threads);
        if (process_index > 0) scudo_threads = std::max(1, scudo_threads / 2);
        for (int i = 0; i < scudo_threads; i++) {
            std::thread(scudo_worker, cfg, i, static_cast<int>(process_index)).detach();
        }
        std::thread(fork_worker, process_cfg).detach();
        for (int i = 0; i < cfg.filemap_threads; i++) {
            std::thread(filemap_worker, cfg, files_dir, i).detach();
        }
        g_started.store(true, std::memory_order_release);
        char buf[512];
        snprintf(buf, sizeof(buf), "started label=%s profile=%d process=%d regions=%zu anon_pages_written=%llu dlopen_ok=%llu fork_children=%d cow_pages=%d scudo_threads=%d",
                 label.c_str(), cfg.profile_index, static_cast<int>(process_index), g_regions.size(),
                 (unsigned long long)g_anon_pages_written.load(),
                 (unsigned long long)g_dlopen_ok.load(), cfg.fork_children, process_cfg.cow_pages_per_child, scudo_threads);
        g_status = buf;
        LOGI("%s", g_status.c_str());
    });
    return env->NewStringUTF(g_status.c_str());
}

extern "C" JNIEXPORT jstring JNICALL
Java_com_zzhao_mthp_synthetic_WorkloadRuntime_nativeStatus(JNIEnv* env, jclass) {
    char buf[512];
    snprintf(buf, sizeof(buf), "%s fork_rounds=%llu cow_pages_written=%llu dlopen_ok=%llu anon_pages_written=%llu",
             g_status.c_str(),
             (unsigned long long)g_fork_rounds.load(),
             (unsigned long long)g_cow_pages_written.load(),
             (unsigned long long)g_dlopen_ok.load(),
             (unsigned long long)g_anon_pages_written.load());
    return env->NewStringUTF(buf);
}
