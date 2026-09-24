// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice 极简断言基类，替代 forge-std/Test.sol，避免外部依赖。
contract Assert {
    event log_named_uint(string name, uint256 value);
    event log_named_string(string name, string value);
    event log_string(string value);

    function assertTrue(bool condition, string memory err) internal pure {
        if (!condition) revert(err);
    }

    function assertFalse(bool condition, string memory err) internal pure {
        if (condition) revert(err);
    }

    function assertEq(address a, address b, string memory err) internal pure {
        if (a != b) revert(err);
    }

    function assertEq(bool a, bool b, string memory err) internal pure {
        if (a != b) revert(err);
    }

    function assertEq(uint256 a, uint256 b, string memory err) internal pure {
        if (a != b) revert(err);
    }

    function assertEq(bytes32 a, bytes32 b, string memory err) internal pure {
        if (a != b) revert(err);
    }
}
