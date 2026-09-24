// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title InitialStorageV2 — SAFE base evolution.
/// @notice A new field `baseName` is added by consuming exactly one slot of
/// the reserved gap (gap shrinks 10 -> 9). No existing field's slot/offset
/// changes, so the checker treats gap-consumption appends as compatible.
contract InitialStorageV2 {
    uint64 internal baseCounter;
    address internal baseAdmin;
    string internal baseName;
    uint256[9] private __baseGap;
}
