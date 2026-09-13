#!/usr/bin/env bash
# 把 expf（expf_ui 本地 GUI）安装为用户级直接命令：~/.local/bin/expf
# 仓库移动/更名后重跑本脚本即可刷新路径。幂等。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${HOME}/.local/bin"
TARGET="${BIN_DIR}/expf"

mkdir -p "${BIN_DIR}"
cat > "${TARGET}" <<EOF
#!/usr/bin/env bash
# expf — exp_framework 本地 GUI（expf_ui）用户级命令
# 由 ${REPO_ROOT}/scripts/install_expf_cli.sh 安装
exec python3 "${REPO_ROOT}/scripts/expf_ui.py" "\$@"
EOF
chmod +x "${TARGET}"

case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) echo "提示: ${BIN_DIR} 不在 PATH，请把下面这行加进 ~/.bashrc："
       echo "  export PATH=\"${BIN_DIR}:\$PATH\"" ;;
esac
echo "已安装: ${TARGET} -> python3 ${REPO_ROOT}/scripts/expf_ui.py"
echo "直接敲 expf 即可启动（参数原样透传，如 expf --screenshot x.png）"
