// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title DynTypeV2 — INCOMPATIBLE with DynTypeV1: same label `items` but
///        the dynamic type changed from an array to a mapping.
contract DynTypeV2 {
    mapping(uint256 => uint256) public items; // slot 0, different encoding
    uint256 public count; // slot 1
}
