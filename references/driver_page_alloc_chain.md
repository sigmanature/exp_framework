# Pixel Kernel 驱动大页分配知识库

## Trace 方法

trace_page_alloc.py 使用 ftrace `mm_page_alloc` event + `options/stacktrace` 全局栈记录：
- filter: `order >= 2`
- 停 trace 后读 `/sys/kernel/tracing/trace` 全文
- 解析 event → 关联 stack → 按栈签名分组

---

## 1. dma-heap (Samsung system heap) — order 9/8/4

### 源码调用链

```
用户态 ioctl(DMA_HEAP_IOC_ALLOC)
  ↓
dma_heap_ioctl()
  → dma_heap_buffer_alloc()                     // common/drivers/dma-buf/dma-heap.c:79-96
    len = PAGE_ALIGN(len)                        // line 92
    return heap->ops->allocate(heap, len, ...)   // line 96
  ↓
system_heap_allocate()                           // private/google-modules/soc/gs/drivers/dma-buf/heaps/samsung/system_heap.c:111-184
  max_order = orders[0]  // = 9                  // line 121
  while (size_remaining > 0):
    page = alloc_largest_available(size_remaining, max_order)  // line 148
    max_order = compound_order(page)             // line 154
  ↓
alloc_largest_available()                        // system_heap.c:63-82
  for (i = 0; i < NUM_ORDERS; i++):  // orders[] = {9, 8, 4, 0}
    if size < (PAGE_SIZE << orders[i]): continue
    if max_order < orders[i]: continue
    page = dmabuf_page_pool_alloc(pools[i])      // line 75
  ↓
dmabuf_page_pool_alloc()                         // page_pool.c:112-124
  page = dmabuf_page_pool_fetch(pool)            // try cache first
  if (!page):
    page = dmabuf_page_pool_alloc_pages(pool)    // line 122
  ↓
dmabuf_page_pool_alloc_pages()                   // page_pool.c:52-57
  return alloc_pages(pool->gfp_mask, pool->order)  // line 56 ← 实际 alloc_pages(order=N)
```
  - gfp = HIGH_ORDER_GFP (__GFP_COMP | __GFP_NOWARN | __GFP_NORETRY, page_pool.c:158)
  - LOW_ORDER_GFP for order=0 fallback (system_heap.c:33)

### trace 栈（确认命中）

```
__traceiter_mm_page_alloc
__alloc_frozen_pages_noprof
__alloc_pages_noprof
dmabuf_page_pool_alloc                         <-- page_pool.c:122
system_heap_allocate                           <-- system_heap.c:148
dma_heap_ioctl                                 <-- dma-heap.c:96
__arm64_sys_ioctl
invoke_syscall
el0_svc_common
do_el0_svc
el0_svc
el0t_64_sync_handler
el0t_64_sync
```

### 触发条件
- 相机 HAL、视频解码器、任何通过 dma-heap 分配 buffer 的进程
- 每笔 allocation 尝试 order 9 → 8 → 4 降级
- gfp 含 `__GFP_COMP` → 返回 compound page

---

## 2. GPU Mali 大页池 — order 9

### 源码调用链

```
kbase_mem_pool_group_init()                      // mali_kbase_mem_pool_group.c:42-67
  kbase_mem_pool_init(&small, ..., KBASE_MEM_POOL_SMALL_PAGE_TABLE_ORDER=0, ...) // line 50-51
  kbase_mem_pool_init(&large, ..., KBASE_MEM_POOL_2MB_PAGE_TABLE_ORDER=9, ...)   // line 55-56
      ↓
      pool->order = order  // = 9              // mali_kbase_mem_pool.c:572

KBASE_MEM_POOL_2MB_PAGE_TABLE_ORDER              // mali_kbase_mem.h:986
  = __builtin_ffs(512) - 1 = 9

NORMAL allocation path:
  kbase_mem_alloc_page(pool)                    // mali_kbase_mem_pool.c:310-352
    gfp = __GFP_ZERO | GFP_HIGHUSER | __GFP_NOWARN  // line 320-321 (high-order)
    p = kbdev->mgm_dev->ops.mgm_alloc_page(...)      // line 325
    ↓
    mgm_alloc_page()                             // memory_group_manager.c:411-441
      p = alloc_pages(gfp_mask, order)           // line 431 ← order=9
```

### trace 栈（确认命中）

```
__traceiter_mm_page_alloc
__alloc_frozen_pages_noprof
__alloc_pages_noprof
mgm_alloc_page                                 <-- memory_group_manager.c:431
kbase_mem_alloc_page                           <-- mali_kbase_mem_pool.c:325
kbase_mem_pool_alloc_pages                     <-- mali_kbase_mem_pool.c:818
...
```

### 触发条件
- 任何 GPU 渲染请求超过 2MB 连续内存 → 从 large pool 分配
- **未设置 `__GFP_COMP`** (mgm_alloc_page 接收的 gfp 不含 COMP)
- 抖音 swipe/视频渲染/游戏都会触发

---

## 3. Video codec (MFC) — via dma-heap order 8/4

### 源码调用链

```
mfc_mem_special_buf_alloc()                     // mfc_mem.c
  → mfc_mem_dma_heap_alloc(dev, special_buf)   // mfc_mem.c:81-160
    heapname = "system-uncached"/"mfc_fw-secure"/"vframe-secure"  // mfc_mem.c:87-96
    special_buf->dma_buf = dma_heap_buffer_alloc(dma_heap, special_buf->size, 0, 0) // line 109-110
    ↓ 之后路径同 §1 dma-heap
```

MFC buffer sizes (mfc.c:1302-1316):
```
firmware_code = PAGE_ALIGN(0x100000)  // 1MB → order 8
cpb_buf       = PAGE_ALIGN(0x300000)  // 3MB → order 9
h264_dec_ctx  = PAGE_ALIGN(0x200000)  // 1.6MB → order 8
```

### trace 栈

Video 进程 (`video`) 出现在抖音 trace 中，order 3，来自解码器 buffer，非 MFC 专用 buffer。MFC 专用 buffer 在首次启动时会走 dma-heap 产生 order 8/9。

---

## 4. WiFi BCM4389 (page_frag_cache) — order 3

### 源码调用链

```
dhd_msgbuf_rxbuf_post()                         // BCM4389 收包 buffer 投递
  → linux_pktget()                              // Broadcom OS 适配层
  → __netdev_alloc_skb()                        // common/net/core/skbuff.c:718-781
    page_frag_alloc(nc, len, gfp_mask)           // line 768
    ↓
    __page_frag_cache_refill(nc, gfp_mask)      // common/mm/page_frag_cache.c:49-71
      unsigned long order = PAGE_FRAG_CACHE_MAX_ORDER;  // line 52
      gfp = (gfp_mask & ~__GFP_DIRECT_RECLAIM) | __GFP_COMP | ...  // line 57-58
      page = __alloc_pages(gfp_mask, PAGE_FRAG_CACHE_MAX_ORDER, ...)  // line 59 ← order=3
    ↓
PAGE_FRAG_CACHE_MAX_ORDER                        // include/linux/mm_types_task.h:47-48
  #define PAGE_FRAG_CACHE_MAX_SIZE   __ALIGN_MASK(32768, ~PAGE_MASK)  // = 32KB
  #define PAGE_FRAG_CACHE_MAX_ORDER  get_order(PAGE_FRAG_CACHE_MAX_SIZE)
  // get_order(32768) = get_order(8 pages) = 3 (PAGE_SIZE=4KB)
```

### trace 栈（确认命中）

```
__traceiter_mm_page_alloc
__alloc_frozen_pages_noprof
__alloc_pages_noprof
__page_frag_cache_refill                       <-- page_frag_cache.c:59
__page_frag_alloc_align
__netdev_alloc_skb                             <-- skbuff.c:718
linux_pktget
dhd_msgbuf_rxbuf_post                          <-- BCM4389 收包
```

### 说明
- 这不是**驱动自己的预分配**（BCM4389 驱动预分配 SKB 最高 order 2, dhd_custom_memprealloc.c:68-79）
- 这是**内核协议栈**的 page_frag_cache 运行时分配，固定 order 3 (32KB)
- WiFi 每次收包触发 page_frag_cache 扩容时分配
- 含 `__GFP_COMP` → compound page

---

## 5. GPU G2D — order 2

### 源码调用链

```
g2d_create_task()                               // gs/drivers/gpu/exynos/g2d/g2d_task.c:465-482
  task->cmd_page = alloc_pages(GFP_KERNEL, get_order(G2D_CMD_LIST_SIZE))  // line 478
  ↓
G2D_CMD_LIST_SIZE                               // g2d_task.h:24-25
  = G2D_MAX_COMMAND * sizeof(struct g2d_reg)    // g2d_uapi.h:61-64
  = 2048 * 8 = 16384 bytes = 16KB               // struct g2d_reg = {u32 offset; u32 value;} = 8 bytes
  get_order(16384) = get_order(4 pages) = 2
```

### trace 栈

抖音 trace 中未单独出现 G2D 栈（被更大量的 page_cache/dma-heap 淹没），但源码确认 order=2。

---

## 6. Camera LWIS — order 9/8/4 (via dma-heap)

### 源码调用链

```
用户态 Camera HAL ioctl(LWIS_IOC_BUFFER_ALLOC)
  ↓
lwis_buffer_enroll()                            // lwis_buffer.c:111-112
  alloc_info->size = PAGE_ALIGN(alloc_info->size)
  dma_buf = lwis_platform_dma_buffer_alloc(alloc_info->size, alloc_info->flags)
  ↓
lwis_platform_dma_buffer_alloc()                // lwis_platform_casablanca_dma.c:15-44
  heap_name = "system" / "system-uncached" / "farawimg-secure"   // dma.c:21-26
  heap = dma_heap_find(heap_name)
  dmabuf = dma_heap_buffer_alloc(heap, len, O_RDWR, 0)           // dma.c:34
  ↓ 之后路径同 §1 dma-heap
```

### trace 栈状态

**当前 `interact_camera()` 使用 KEYCODE_CAMERA fallback，未触发此路径。**
需改用 uiautomator 找快门按钮 + 长按，或直接用 `am start` 打开 Google Camera
让预览流自动启动（预览流即触发 LWIS buffer allocation）。

---

## 7. Page cache / filemap (F2FS) — order 2

### 源码调用链

```
filemap_fault() → f2fs_filemap_fault()
  → page_cache_ra_order()                       // do_sync_mmap_readahead / page_cache_async_ra
  → __folio_alloc_noprof(order=2)
```

### trace 栈（抖音 trace 中占 62%）

```
page_cache_ra_order
  → do_sync_mmap_readahead
    → filemap_fault
      → f2fs_filemap_fault          // 抖音视频文件 page-in
  OR
page_cache_async_ra
  → filemap_fault
    → f2fs_filemap_fault
```

### 说明
- 抖音 swipe 读取新视频的 f2fs 文件页会产生大量 order=2 分配
- 这是文件 IO 产生的 buddy allocator 压力，与驱动 DMA 无关
- 但会和 THP anon folio (order=2) 直接竞争

---

## 8. Slab allocator — order 2/3

### 源码调用链

```
kmem_cache_alloc_lru_noprof()
  → ___slab_alloc → __slab_alloc → allocate_slab
  → alloc_pages(order=2~3)  // 新 slab 扩容
```

### trace 栈（抖音 trace 中占 11%）

```
allocate_slab
___slab_alloc → __slab_alloc
kmem_cache_alloc_lru_noprof
f2fs_alloc_inode                               // F2FS inode slab 扩容
alloc_inode
```

### 说明
- F2FS inode cache slab 扩容时触发 order=2~3 分配
- 抖音产生大量文件访问 → f2fs inode 分配频繁

---

## 总结：各负载触发驱动大页分配的能力

| 负载 | dma-heap(8/9) | GPU Mali(9) | WiFi(3) | Camera LWIS | Page cache(2) |
|------|:---:|:---:|:---:|:---:|:---:|
| 抖音 swipe | ★★★ | ★ | ★★ | — | ★★★★★ |
| 相机 KEYCODE_CAMERA | ★ | — | ★ | ✗ | ★★★ |
| 纯 memstress (am start+HOME) | ★ | — | ★ | — | ★★ |

需要改进相机的交互方式，使 `lwis_platform_dma_buffer_alloc` 路径被触发。
