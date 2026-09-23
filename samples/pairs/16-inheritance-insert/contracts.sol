// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

// Base: uint256 a; Derived is Base + uint256 b
// V2 在 Base 与 Derived 之间插入 Middle(uint256 m)，b 被推移 1 slot
