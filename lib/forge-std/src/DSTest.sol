// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice Tiny assertion/logger base, API-compatible with the subset of
///         DSTest used by the repo's tests. Failures set the `failed` flag and
///         emit a log event so forge reports them as test failures.
contract DSTest {
    event log(string text);
    event log_named_uint(string key, uint256 val);
    event log_named_address(string key, address val);
    event log_named_bytes32(string key, bytes32 val);
    event log_named_decimal_uint(string key, uint256 val, uint256 decimals);
    event log_named_string(string key, string val);
    event log_named_bytes(string key, bytes val);

    bool public IS_TEST = true;
    bool public failed;

    function fail() internal {
        failed = true;
    }

    function assertEq(uint256 a, uint256 b) internal virtual {
        if (a != b) {
            emit log("Error: a == b not satisfied [uint]");
            emit log_named_uint("  Expected", b);
            emit log_named_uint("    Actual", a);
            fail();
        }
    }

    function assertEq(uint256 a, uint256 b, string memory err) internal virtual {
        if (a != b) {
            emit log(err);
            fail();
        }
    }

    function assertEq(address a, address b) internal virtual {
        if (a != b) {
            emit log("Error: a == b not satisfied [address]");
            emit log_named_address("  Expected", b);
            emit log_named_address("    Actual", a);
            fail();
        }
    }

    function assertEq(bool a, bool b) internal virtual {
        if (a != b) {
            emit log("Error: a == b not satisfied [bool]");
            fail();
        }
    }

    function assertEq32(bytes32 a, bytes32 b) internal virtual {
        if (a != b) {
            emit log("Error: a == b not satisfied [bytes32]");
            fail();
        }
    }

    function assertLe(uint256 a, uint256 b) internal virtual {
        if (a > b) {
            emit log("Error: a <= b not satisfied [uint]");
            emit log_named_uint("  Expected max", b);
            emit log_named_uint("    Actual", a);
            fail();
        }
    }

    function assertLt(uint256 a, uint256 b) internal virtual {
        if (a >= b) {
            emit log("Error: a < b not satisfied [uint]");
            fail();
        }
    }

    function assertGe(uint256 a, uint256 b) internal virtual {
        if (a < b) {
            emit log("Error: a >= b not satisfied [uint]");
            fail();
        }
    }

    function assertTrue(bool condition) internal virtual {
        if (!condition) {
            emit log("Error: expected true");
            fail();
        }
    }

    function assertTrue(bool condition, string memory err) internal virtual {
        if (!condition) {
            emit log(err);
            fail();
        }
    }

    function assertFalse(bool condition) internal virtual {
        if (condition) {
            emit log("Error: expected false");
            fail();
        }
    }
}
