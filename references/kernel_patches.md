# Kernel Patches for 16KB mTHP Split Analysis

## Files Changed (8 files, ~180 lines)

### 1. include/linux/huge_mm.h

- Add `DSR_NR` to `enum deferred_split_reason` (sentinel)
- Replace `DECLARE_PER_CPU(enum deferred_split_reason, ...)` with `DECLARE_PER_CPU(unsigned long, deferred_split_reason_counts[DSR_NR])`

### 2. include/trace/events/huge_memory.h

Add 4 new TRACE_EVENTs:

| Trace | Purpose |
|-------|---------|
| `mm_shrink_partial_split` | partially_mapped folio split in shrink_folio_list |
| `mm_shrink_swap_split` | swap fallback split in shrink_folio_list |
| `mm_madvise_dontneed` | MADV_DONTNEED addr+len before zap |
| `mm_folio_partial_unmap` | folio PFN + page PFN on partial unmap |

Fix `mm_folio_split` to record pre-split `order` instead of `folio_order(folio)`.

### 3. mm/huge_memory.c

- Replace per-CPU scratch variable with `deferred_split_reason_counts[DSR_NR]`
- Add `this_cpu_inc(deferred_split_reason_counts[DSR_ZAP])` at THP fault caller
- Add `trace_mm_folio_split(folio, order, ...)` — pass original order
- Add debugfs `deferred_split_reasons` (read-only) showing cumulative per-reason counts

### 4. mm/khugepaged.c

- Add `this_cpu_inc(deferred_split_reason_counts[DSR_KHUGEPAGED])` before `deferred_split_folio` call

### 5. mm/rmap.c

- Add `#include <trace/events/huge_memory.h>`
- Add `this_cpu_inc(deferred_split_reason_counts[DSR_PARTIALLY_MAPPED])` before `deferred_split_folio`
- Add `trace_mm_folio_partial_unmap(folio, page, nr, nr_pmdmapped)` for partial unmap analysis

### 6. mm/vmscan.c

- Add `#include <trace/events/huge_memory.h>`
- In `shrink_folio_list`, add `trace_mm_shrink_partial_split(folio)` before partially_mapped split
- Add `trace_mm_shrink_swap_split(folio)` before swap fallback split

### 7. mm/page_alloc.c

- Add `enum alloc_fail_reason { AFR_WMARK, AFR_FRAGMENT, AFR_NR }` and per-CPU counters
- Add `DEFINE_PER_CPU(int, last_alloc_fail_reason)` — records why `get_page_from_freelist` failed
- In `get_page_from_freelist`: set `last_alloc_fail_reason` at watermark fail and rmqueue fail
- In `__alloc_pages_slowpath`: before `__alloc_pages_direct_reclaim`, read reason and increment counter
- Add debugfs `alloc_fail_reasons` (read-only) showing wmark vs fragment counts

### 8. mm/madvise.c

- Add `#include <trace/events/huge_memory.h>`
- Add `trace_mm_madvise_dontneed(vma, range->start, range->end - range->start)` in `madvise_dontneed_single_vma`

## DebugFS Nodes

| Node | Content |
|------|---------|
| `/sys/kernel/debug/deferred_split_reasons` | PARTIALLY_MAPPED / ZAP / KHUGEPAGED counts |
| `/sys/kernel/debug/alloc_fail_reasons` | wmark / fragment allocation failure counts |

## Verification

- `wmark + fragment ≈ allocstall` (both sides use delta from start/end)
- `PARTIALLY_MAPPED ≈ mm_folio_deferred_split trace count`
- `THP stats split ≈ mm_folio_split (order=2) trace count`
