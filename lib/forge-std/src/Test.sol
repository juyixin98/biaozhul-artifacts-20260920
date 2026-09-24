// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Vm} from "./Vm.sol";
import {DSTest} from "./DSTest.sol";

/// @notice Locally vendored stand-in for forge-std/Test.sol exposing only what
///         this repository uses: the `vm` cheatcode handle plus the DSTest
///         assertions. forge injects the real cheatcode implementation.
abstract contract Test is DSTest {
    Vm internal constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);

    /// @dev Bound a fuzzed value into [min, max] (simple modulo implementation;
    ///      sufficient for this repo's single fuzz test). API mirrors forge-std.
    function bound(uint256 x, uint256 min, uint256 max) internal pure returns (uint256 result) {
        require(min <= max, "Test/bound: min > max");
        if (max == min) {
            return min;
        }
        uint256 size = max - min;
        result = min + (x % (size + 1));
    }
}
