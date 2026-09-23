// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// 结构体内 __gap 原地缩小 1 个 slot，承接尾部新成员 c（末端结构体，可增长）
// V1: struct Cfg { uint256 a; uint256[3] __gap; }
// V2: struct Cfg { uint256 a; uint256 c; uint256[2] __gap; }
