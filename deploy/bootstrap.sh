#!/usr/bin/env bash
# 在一台 Ubuntu 宿主上开出一个 larkflow 租户。**幂等**：重复跑不会覆盖已有的 env 与凭证。
#
#   sudo ./deploy/bootstrap.sh <租户名> [仓库路径]
#
# 干六件事，每件都先查再做：建 Unix 用户与目录 → 建 venv 装 larkflow → 放一份 env 模板
# → 放 lark-cli 包装器 → 有 LARK_APP_SECRET 就顺手建 profile → 装 systemd 模板单元。
# **不自动 enable**：env 还没填，起来必然红，那种红会教人忽略红色。
#
# 2026-07-27 在 alicloud-sh（Ubuntu 22.04.5）上真跑通过，脚本里带「真机上踩过」字样的
# 注释都是那一趟换来的。仍未在别的发行版 / 别的客户机器上验过。
set -euo pipefail

die() { echo "✗ $*" >&2; exit 1; }
say() { echo "→ $*"; }

TENANT="${1:-}"
REPO="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

[[ -n "$TENANT" ]] || die "用法：sudo $0 <租户名> [仓库路径]"
# 租户名会变成 Unix 用户名和路径段，所以口径要窄。Linux 用户名上限 32 字符，减去 lf- 前缀。
[[ "$TENANT" =~ ^[a-z0-9][a-z0-9-]{0,28}$ ]] \
  || die "租户名只允许小写字母/数字/连字符、字母数字开头、不超过 29 字符：$TENANT"
[[ "$(id -u)" == "0" ]] || die "需要 root（建用户 + 写 /etc/systemd）"
[[ -f "$REPO/pyproject.toml" ]] || die "$REPO 看着不像 larkflow 仓库（没有 pyproject.toml）"

USER_NAME="lf-$TENANT"
HOME_DIR="/srv/larkflow/$TENANT"
ENV_FILE="$HOME_DIR/larkflow.env"
VENV="$HOME_DIR/venv"
UNIT_SRC="$REPO/deploy/larkflow@.service"

# ---- 0. 前置：外部依赖 ----
command -v python3 >/dev/null || die "没有 python3"
# lark-cli 是出入站的唯一通道，没有它整个服务是聋哑的。不替用户装 node，只把话说清。
command -v lark-cli >/dev/null || die "PATH 上没有 lark-cli。先 npm i -g @larksuite/cli"
# Ubuntu 24.04+ 的系统 python 是 externally-managed（PEP 668），pip 直装会被拦。
# 这也是这里一律走 venv 的原因，不要图省事往系统 python 里装。
# **别用 `python3 -m venv --help` 判**（真机上踩过）：Ubuntu 22.04 把 ensurepip 拆进了
# 单独的包，`venv` 模块在、`--help` 也过，建 venv 时才报 "ensurepip is not available"。
python3 -c 'import ensurepip' 2>/dev/null \
  || die "缺 ensurepip：apt install python3-venv（22.04 上可能要 python3.10-venv）"

# ---- 1. 用户与目录 ----
if id -u "$USER_NAME" >/dev/null 2>&1; then
  say "用户 $USER_NAME 已存在，跳过"
else
  say "建用户 $USER_NAME（无登录 shell、无密码）"
  useradd --system --create-home --home-dir "$HOME_DIR" --shell /usr/sbin/nologin "$USER_NAME"
fi
install -d -o "$USER_NAME" -g "$USER_NAME" -m 700 "$HOME_DIR"
# lark-cli 在 Linux 上**没有 OS keyring**（它自己的说法：「on this platform the keychain
# layer already uses local files」），app secret 是「本地文件 + 同目录 master.key」。
# 也就是说谁读得到这个目录谁就解得开凭证，0700 与独立 uid 就是这里的全部防线。
# lark-cli 自己会按需建 ~/.lark-cli 与 ~/.local/share/lark-cli，这里不预建：
# 只要 HOME 是这个租户专属的，两样就都在里面（真机三组对照实测，见 larkflow@.service 的注释）。

# ---- 2. venv + larkflow ----
if [[ -x "$VENV/bin/larkflow" ]]; then
  say "venv 已存在，就地升级"
else
  say "建 venv $VENV"
  sudo -u "$USER_NAME" python3 -m venv "$VENV"
fi
# **先把源码复制进租户自己的目录再装**（真机上踩过）：`pip install /home/<你>/repo` 是以
# 租户身份跑的，而家目录默认 0750，租户用户连进都进不去，pip 报的却是「Invalid requirement /
# File does not exist」，看着完全像路径写错了。放权限是错的解法（等于让每个租户都能读部署者
# 的家目录）。顺带的好处：每个租户手边都留着它此刻正在跑的那份源码，出事时不用猜版本。
SRC_DIR="$HOME_DIR/src"
say "把源码复制到 $SRC_DIR"
rm -rf "$SRC_DIR"
install -d -o "$USER_NAME" -g "$USER_NAME" -m 700 "$SRC_DIR"
tar -C "$REPO" --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache \
    --exclude='*.egg-info' --exclude=.env --exclude='*.sqlite*' -cf - . \
  | tar -C "$SRC_DIR" -xf -
chown -R "$USER_NAME:$USER_NAME" "$SRC_DIR"

# 国内宿主上 PyPI 直连会超时（阿里云上海实测：github / npm / 飞书都通，唯独 pypi.org 不通）。
# 用 PIP_INDEX_URL 覆盖，默认给阿里云镜像；跑在能直连的地方就换成官方源。
PIP_INDEX="${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}"
say "装 larkflow（pip 源：$PIP_INDEX）"
sudo -u "$USER_NAME" "$VENV/bin/pip" install --quiet --upgrade pip -i "$PIP_INDEX"
sudo -u "$USER_NAME" "$VENV/bin/pip" install --quiet -i "$PIP_INDEX" "$SRC_DIR"

# ---- 3. env 模板 ----
if [[ -f "$ENV_FILE" ]]; then
  say "$ENV_FILE 已存在，**不覆盖**（里面是凭证）"
else
  say "放一份 env 模板到 $ENV_FILE（0600）"
  install -o "$USER_NAME" -g "$USER_NAME" -m 600 "$REPO/deploy/tenant.env.example" "$ENV_FILE"
fi

# ---- 4. lark-cli 包装器 ----
# 为什么要有它（真机上踩过两次，症状极其隐蔽）：凭证跟着 HOME 走，而人在敲手工命令时
# 极容易忘了带 HOME，于是读到的是**自己那套**凭证。表现是 profile 建出来看着完全正常、
# `profile list` 里也在，但服务那套 env 下 `auth status` 报
# `bot: not_configured (missing app secret)`，服务照常启动、静默地没有凭证。
# 所以让人只敲 `<租户目录>/lark ...`，env 一个字都不用碰。
say "放 lark-cli 包装器 $HOME_DIR/lark"
cat > "$HOME_DIR/lark" <<EOF
#!/bin/sh
# 用与 systemd 单元**完全相同**的环境跑 lark-cli。改这里就要同步改 larkflow@.service。
# 关键只有 HOME 一个：凭证（config.json + master.key + 密文）全部跟着它走。
# 手敲 lark-cli 而忘了带 HOME，会读到你自己那套凭证，看着能跑，但跟服务用的不是同一份。
exec env HOME="$HOME_DIR" \\
  LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1 \\
  LARK_CLI_NO_PROXY=1 \\
  lark-cli "\$@"
EOF
chown "$USER_NAME:$USER_NAME" "$HOME_DIR/lark"
chmod 750 "$HOME_DIR/lark"

# ---- 5. 有 LARK_APP_SECRET 就顺手把 profile 建了 ----
# 值用 **larkflow 自己的解析器**读，不用 grep / sed / source：这个仓库的规矩是「同一件事只许
# 有一把尺」，而 `.env` 的语法（整体引号、`$` 不展开、行尾注释）已经在 config.load_dotenv
# 里定死并有测试钉着。密钥全程只在变量里、走 stdin，不进 argv（`ps` 看得到 argv）、不进日志。
read_env_key() {
  sudo -u "$USER_NAME" "$VENV/bin/python" - "$ENV_FILE" "$1" <<'PY'
import sys
from larkflow.config import load_dotenv
env = {}
load_dotenv(sys.argv[1], environ=env)
sys.stdout.write(env.get(sys.argv[2], ""))
PY
}

APP_SECRET="$(read_env_key LARK_APP_SECRET || true)"
PROFILE_NAME="$(read_env_key LARK_PROFILE || true)"
PROFILE_NAME="${PROFILE_NAME:-$TENANT}"
APP_ID="$(read_env_key LARKFLOW_APP_ID || true)"

# 占位值是语法合法的，漏填一行就会静默建出一个指向 cli_xxxxxxxxxxxxxxxx 的 profile。
# 宁可在这里停下来，也不要建一个「看着正常、连的却是不存在的 app」的凭证。
if [[ -n "$APP_SECRET" ]] && [[ "$APP_ID" == *xxxx* || "$APP_SECRET" == *在这里填* ]]; then
  die "$ENV_FILE 里还留着占位值（APP_ID=$APP_ID）。先填成真值再跑。"
fi

if [[ -n "$APP_SECRET" && -n "$APP_ID" ]]; then
  if sudo -u "$USER_NAME" "$HOME_DIR/lark" --profile "$PROFILE_NAME" auth status >/dev/null 2>&1; then
    say "profile「$PROFILE_NAME」已存在，跳过（要换凭证先 lark profile remove）"
  else
    say "按 LARK_APP_SECRET 建 profile「$PROFILE_NAME」→ $APP_ID"
    printf '%s' "$APP_SECRET" | sudo -u "$USER_NAME" "$HOME_DIR/lark" \
      profile add --name "$PROFILE_NAME" --app-id "$APP_ID" --app-secret-stdin >/dev/null
  fi
elif [[ -n "$APP_SECRET" ]]; then
  say "有 LARK_APP_SECRET 但没有 LARKFLOW_APP_ID，跳过自动建 profile"
fi
unset APP_SECRET

# ---- 6. systemd 模板单元 ----
[[ -f "$UNIT_SRC" ]] || die "找不到 $UNIT_SRC"
say "装 systemd 模板单元"
install -m 644 "$UNIT_SRC" /etc/systemd/system/larkflow@.service
systemctl daemon-reload

# ---- 完 ----
cat <<EOF

✓ 租户 $TENANT 的位置已经开好了。**还没启动**，因为凭证还没配。

接下来三步（每步都有验收方法，详见 AIREADME/DEPLOYMENT.md）：

  1) 配飞书凭证。**推荐**：把 LARK_APP_SECRET 与 LARKFLOW_APP_ID 填进 $ENV_FILE，
     再跑一次本脚本，profile 会自动建好（幂等，已存在就跳过）。
     手工建也行，但**一律走 $HOME_DIR/lark 这个包装器**，别直接敲 lark-cli：
     凭证跟着 HOME 走，忘了带就会读到你自己那套，而 profile 看着完全正常、
     服务却静默没有凭证。
       printf '%s' '<app secret>' | sudo -u $USER_NAME $HOME_DIR/lark \\
         profile add --name $TENANT --app-id cli_xxx --app-secret-stdin
     验收：sudo -u $USER_NAME $HOME_DIR/lark --profile $TENANT auth status --json
           → appId 对得上，且 identities.bot.status == "ready"（不是 not_configured）。
     注意：auth status 是**纯本地**判断（拿伪造 secret 建的 profile 它照样说 ready），
           它证明「配成了哪个 app」，不证明「凭证还能用」。真正验凭证只能靠第 3 步跑真链路。

  2) 填 $ENV_FILE（LARK_PROFILE / LARKFLOW_APP_ID / LARKFLOW_ROLES / LLM_*）：
       sudoedit $ENV_FILE

  3) 体检 → 起服务：
       sudo -u $USER_NAME env HOME=$HOME_DIR $VENV/bin/larkflow --env-file $ENV_FILE doctor
       # 全绿（或只剩 ⚠️）之后再：
       sudo systemctl enable --now larkflow@$TENANT
       journalctl -u larkflow@$TENANT -f

EOF
