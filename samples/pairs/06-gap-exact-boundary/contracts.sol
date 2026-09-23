// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// gap 边界但安全标记消失：旧 __gap[2] 在新版本中被整体删除，
// y,z 虽然精确落在释放区间，但检查器无法确认“删除 gap”是有意为之，
// 报 gap-removed 警告，结论 unknown（推荐写法见 06b：保留同名 gap 缩到 0）。
