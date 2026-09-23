// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// OpenZeppelin 模式：__gap 原地缩小 1，释放 slot 承接新变量 y
// V1: uint256 x; uint256[49] __gap;
// V2: uint256 x; uint256 y; uint256[48] __gap;
