// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title WidenV1 — `a` and `b` pack into slot 0.
contract WidenV1 {
    uint128 public a; // slot 0, offset 0
    uint128 public b; // slot 0, offset 16
    uint256 public c; // slot 1
}
