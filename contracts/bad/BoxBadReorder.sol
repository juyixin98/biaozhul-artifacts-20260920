// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title BoxBadReorder — INCOMPATIBLE with BoxV1: `owner` and `value`
///        swapped places, so both land in the wrong slots.
contract BoxBadReorder {
    address public owner; // was slot 1, now slot 0
    uint256 public value; // was slot 0, now slot 1
    string public name;
}
