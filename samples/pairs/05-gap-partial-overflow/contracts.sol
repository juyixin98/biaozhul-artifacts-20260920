// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// gap 只有 2 个 slot：y,z 吃掉储备，w 越过旧区域末端。
// 平坦追加本身可工作，但 gap 储备已耗尽 -> gap-overflow 警告（结论 unknown）。
// V1: uint256 x; uint256[2] __gap;
// V2: uint256 x,y,z,w; uint256[0] __gap;（声明顺序 x,y,z,w,__gap）
