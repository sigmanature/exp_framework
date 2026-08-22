/**
 * exp-lock-gate — 项目级门禁插件（仅在本项目 .opencode/plugins/ 内加载生效）。
 *
 * 机制：
 * - opencode.json 里 permission.bash 对"设备启动类命令模式"配置为 ask
 * - 触发 ask 时本插件的 permission.ask 接管判定：
 *     查 exp_lock 游标（~/.worklog/run_cursor.json）：
 *       state ∈ {running, cleanup_failed} → deny（设备被占用，拒绝裸命令）
 *       空闲 → allow（无感放行，用户不会被打扰）
 * - 判定逻辑复用 scripts/lock_gate.py（python），插件只做桥接。
 */
import { execSync } from "node:child_process"

const GATE = "/home/nzzhao/skills-repos/exp_framework/scripts/lock_gate.py"

export default {
  id: "exp-lock-gate",
  hooks: {
    "permission.ask": async (input: any, output: any) => {
      if (input?.permission !== "bash") return
      // 触发 ask 的命令：优先 metadata.command，退回 patterns 拼接
      const cmd = (input?.metadata && (input.metadata as any).command) || ""
        || (input?.patterns || []).join(" ")
      if (!cmd) return
      try {
        execSync(`python3 ${GATE} --cmd ${JSON.stringify(cmd)}`, {
          encoding: "utf8", stdio: "pipe",
        })
        output.status = "allow" // 放行（空闲或检查类命令）
      } catch {
        output.status = "deny"  // lock_gate 判定拒绝（exit 1）
      }
    },
  },
}
