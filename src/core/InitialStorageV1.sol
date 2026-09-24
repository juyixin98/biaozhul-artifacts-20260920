// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title InitialStorageV1 — UNSAFE base evolution.
/// @notice `baseCounter` is WIDENED uint64 -> uint256, which breaks the packed
///         slot it shared with `baseAdmin`: baseAdmin is pushed to the next
///         slot. The gap is shrunk by one to keep the derived contract's own
///         fields at the same slots, proving the checker catches the change
///         *inside* the base even when downstream slot numbers line up.
contract InitialStorageV1 {
    uint256 internal baseCounter;
    address internal baseAdmin;
    uint256[9] private __baseGap;
}
