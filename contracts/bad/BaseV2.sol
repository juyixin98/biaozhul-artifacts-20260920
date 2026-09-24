// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract NewBase {
    uint256 public inserted; // takes slot 0 in BaseV2
}

/// @title BaseV2 — INCOMPATIBLE with BaseV1: inheriting NewBase inserts a
///        variable before `x`, shifting it from slot 0 to slot 1.
contract BaseV2 is NewBase {
    uint256 public x; // slot 1 (moved)
}
