#!/usr/bin/env bash
# 可选辅助脚本: 把 Rust 工具链安装到项目内 ./.toolchain (与系统其他工具链隔离)。
# 一般情况下直接用系统已装的 stable Rust 即可, 无需运行本脚本。
#
# 网络访问官方源慢时, 可指定镜像, 例如:
#   RUSTUP_DIST_SERVER=https://rsproxy.cn \
#   RUSTUP_UPDATE_ROOT=https://rsproxy.cn/rustup \
#   bash scripts/install_toolchain.sh
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLCHAIN_VERSION="${TOOLCHAIN_VERSION:-1.98.1}"
export PATH="$HOME/.cargo/bin:$PATH"
export RUSTUP_HOME="$PROJECT_DIR/.toolchain/rustup"
export CARGO_HOME="$PROJECT_DIR/.toolchain/cargo"
export RUSTUP_DIST_SERVER="${RUSTUP_DIST_SERVER:-https://static.rust-lang.org}"
export RUSTUP_UPDATE_ROOT="${RUSTUP_UPDATE_ROOT:-https://static.rust-lang.org/rustup}"
mkdir -p "$RUSTUP_HOME" "$CARGO_HOME"
rustup toolchain install "$TOOLCHAIN_VERSION" --profile minimal --no-self-update
"$RUSTUP_HOME/toolchains/$TOOLCHAIN_VERSION-x86_64-unknown-linux-gnu/bin/rustc" --version
"$RUSTUP_HOME/toolchains/$TOOLCHAIN_VERSION-x86_64-unknown-linux-gnu/bin/cargo" --version

# 之后构建/测试(以 1.98.1 为例):
#   export RUSTUP_HOME="$PROJECT_DIR/.toolchain/rustup"
#   export CARGO_HOME="$PROJECT_DIR/.toolchain/cargo"
#   export PATH="$PROJECT_DIR/.toolchain/rustup/toolchains/1.98.1-x86_64-unknown-linux-gnu/bin:$PATH"
#   cargo test
