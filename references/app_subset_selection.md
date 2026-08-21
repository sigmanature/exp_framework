# Selecting a small “heavy” app subset (≤20) for flash-kill runs

When you want a very aggressive churn workload (start → short dwell → force-stop), using top100 packages is often unnecessary and slows iteration. A curated subset (≤20) is faster and more controlled.

## Recommended types to include

- Social/IM: WeChat, QQ
- Commerce: Taobao
- Video/streaming: Bilibili, Kuaishou, TikTok/Douyin
- Browser class: Chrome / Edge / UC / Quark / QQBrowser
- Pixel/system: YouTube, Camera, Maps

## Practical rule

Always validate **installed + launchable** on *all* target devices:

- installed: `pm path <pkg>` returns `package:...`
- launchable: `cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER <pkg>` returns `pkg/activity`

Then use that validated list as `--package-file`.

