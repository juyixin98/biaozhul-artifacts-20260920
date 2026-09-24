// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title DynTypeV1 — dynamic array in storage.
contract DynTypeV1 {
    uint256[] public items; // slot 0 (dynamic array head)
    uint256 public count; // slot 1
}
