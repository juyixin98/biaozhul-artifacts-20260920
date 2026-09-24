// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title WidenV2 — INCOMPATIBLE with WidenV1: widening `a` from uint128
///        to uint256 pushes `b` and `c` into new slots.
contract WidenV2 {
    uint256 public a; // slot 0 (type widened)
    uint128 public b; // slot 1 (moved)
    uint256 public c; // slot 2 (moved)
}
