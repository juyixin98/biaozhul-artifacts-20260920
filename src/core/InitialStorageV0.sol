// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title InitialStorageV0 — plain base contract holding the first fields.
/// @notice Exists specifically so that later versions can demonstrate
///         *base-class changes*:
///           - a SAFE one (adding a new base field out of a reserved gap),
///           - an UNSAFE one (reordering / widening a field inside the base).
contract InitialStorageV0 {
    uint64 internal baseCounter;
    address internal baseAdmin;
    // Reserved space so future base versions can append fields without
    // shifting the derived contract's slots.
    uint256[10] private __baseGap;
}
