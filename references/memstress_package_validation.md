# 为什么 memstress 需要校验/解析 package（而 monkey 不需要）

## memstress 的语义决定必须前置校验

memstress 工作流是“脚本主动控制 app 集合”：

1) 读入 `--package/--package-file`
2) 过滤出“设备上已安装”的包（推荐用一次 `pm list packages` 拉全量）
3) 对每个包解析可启动入口 activity（`cmd package resolve-activity ...`）
4) 长测循环里重复执行：
   - `am start -W -n <component>`
   - 保活/等待
   - `am force-stop <pkg>`

因此如果不做前置校验会出现：

- 包未安装：每轮都会失败，变成长测“空转 + log spam”
- 包已安装但无 launcher/activity：同样会稳定失败
- 包可启动但入口不稳定：压力形态不可复现（同样的包名跑出来行为不同）

结论：memstress 必须把“可启动集合”固定下来，后续循环才稳定。

## monkey 不做同级别校验的原因

monkey 的语义是“系统/monkey 自己探索并注入事件”：

- `monkey --global`：目标选择和启动/切换由系统负责，脚本不需要提供每个包的入口 activity
- `monkey -p <pkg>`：即使限制在某些包，monkey 也不是脚本逐包 `am start -n`，失败/异常通常由 monkey 自己处理/输出

所以 monkey 入口脚本通常只需要：
- 可选限制 package（`-p`）
- 控制 throttle/events
- 做好 logcat 收集 + sampling

