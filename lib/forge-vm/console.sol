// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @title console
/// @notice Minimal logging helper. Forge decodes a Hardhat-compatible
///         `log(string)` call sent to the well-known console address
///         0x000000000000000000636F6e736F6c652e6c6f67 (selector 0x41304fac).
///         Only this one verified selector is used; callers pre-format values
///         with string.concat and vm.toString.
library console {
    address constant CONSOLE_ADDRESS = address(0x000000000000000000636F6e736F6c652e6c6f67);

    function log(string memory p0) internal view {
        _log(0x41304fac, abi.encode(p0));
    }

    /// @dev Prepend the 4-byte selector to the ABI-encoded arguments and call
    ///      the console address. `data` is a standard abi.encode blob: it
    ///      already begins with the 32-byte offset and includes the length word.
    function _log(uint256 selector, bytes memory data) private view {
        address consoleAddr = CONSOLE_ADDRESS;
        assembly ("memory-safe") {
            let argLength := mload(data)
            let fullLength := add(argLength, 4)
            let free := mload(0x40)
            let words := div(add(argLength, 31), 32)
            for { let i := 0 } lt(i, words) { i := add(i, 1) } {
                mstore(add(free, add(4, mul(i, 32))), mload(add(data, add(32, mul(i, 32)))))
            }
            mstore(free, shl(224, selector))
            pop(staticcall(gas(), consoleAddr, free, fullLength, 0, 0))
        }
    }
}
