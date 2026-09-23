// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Vm} from "./Vm.sol";
import {console} from "./console.sol";

/// @title Test
/// @notice Minimal test base contract (local hand-authored substitute for
///         forge-std/Test.sol). The Forge test runner recognizes functions
///         whose signature starts with `test`; `setUp` runs before each one.
abstract contract Test {
    Vm internal constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);
    address private constant VM_ADDRESS = address(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);

    bool internal _failed;

    function failed() external view returns (bool) {
        return _failed;
    }

    /// @dev Bare `expectRevert()` cheatcode (selector 0xf4844814), invoked via
    ///      a low-level call because the Solidity overload collides with
    ///      expectRevert(bytes memory("")).
    function expectRevertAny() internal {
        (bool ok,) = VM_ADDRESS.call(hex"f4844814");
        require(ok, "vm.expectRevert() failed");
    }

    // -----------------------------------------------------------------------
    // Custom-error revert helpers
    //
    // This Foundry build (1.8.3) matches expectRevert on the FULL revert data
    // only; the bytes4/prefix overload does not match. These helpers ABI-encode
    // the complete custom-error payload so tests stay readable.
    // -----------------------------------------------------------------------

    /// @dev Expect a parameter-less custom error. This Foundry build cannot
    ///      match a 4-byte selector, so this asserts that SOME revert occurs.
    ///      For errors that carry data, use {expectErrorData} with the full,
    ///      exact ABI-encoded payload.
    function expectError(bytes4 /* selector */) internal {
        expectRevertAny();
    }

    /// @dev Expect a revert whose data equals `data` exactly.
    function expectErrorData(bytes memory data) internal {
        vm.expectRevert(data);
    }

    // ---- boolean / equality assertions -------------------------------------

    function assertTrue(bool condition, string memory err) internal {
        if (!condition) {
            _failed = true;
            revert(err);
        }
    }

    function assertTrue(bool condition) internal {
        assertTrue(condition, "assertion failed");
    }

    function assertFalse(bool condition, string memory err) internal {
        assertTrue(!condition, err);
    }

    function assertFalse(bool condition) internal {
        assertFalse(condition, "assertion failed");
    }

    function assertEq(uint256 a, uint256 b, string memory err) internal {
        if (a != b) {
            _failed = true;
            revert(err);
        }
    }

    function assertEq(uint256 a, uint256 b) internal {
        assertEq(a, b, "assertEq(uint256) failed");
    }

    function assertEq(address a, address b, string memory err) internal {
        if (a != b) {
            _failed = true;
            revert(err);
        }
    }

    function assertEq(address a, address b) internal {
        assertEq(a, b, "assertEq(address) failed");
    }

    function assertEq(bool a, bool b, string memory err) internal {
        if (a != b) {
            _failed = true;
            revert(err);
        }
    }

    function assertEq(bool a, bool b) internal {
        assertEq(a, b, "assertEq(bool) failed");
    }

    function assertEq(bytes32 a, bytes32 b, string memory err) internal {
        if (a != b) {
            _failed = true;
            revert(err);
        }
    }

    function assertEq(bytes32 a, bytes32 b) internal {
        assertEq(a, b, "assertEq(bytes32) failed");
    }

    function assertEq(bytes memory a, bytes memory b, string memory err) internal {
        if (keccak256(a) != keccak256(b)) {
            _failed = true;
            revert(err);
        }
    }

    function assertEq(bytes memory a, bytes memory b) internal {
        assertEq(a, b, "assertEq(bytes) failed");
    }
}
