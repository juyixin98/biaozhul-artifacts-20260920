// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @title Vm
/// @notice Hand-authored subset of the Foundry cheatcode interface
///         (https://book.getfoundry.sh/cheatcodes/). This is a local interface
///         declaration, not an external dependency: calls are dispatched by the
///         Foundry EVM to the cheatcode entry point at a fixed address.
interface Vm {
    // Calls / pranking
    function prank(address msgSender) external;
    function startPrank(address msgSender) external;
    function stopPrank() external;

    // Chain environment
    function warp(uint256 newTimestamp) external;
    function roll(uint256 newHeight) external;
    function chainId(uint256 newChainId) external;

    // Revert expectations. NOTE: the bare expectRevert() (selector 0xf4844814)
    // is invoked via a low-level call in Test.sol. Solidity cannot distinguish
    // expectRevert() from expectRevert("") at the interface level, so only the
    // parameterized overloads are declared here.
    function expectRevert(bytes4 revertData) external;
    function expectRevert(bytes calldata revertData) external;

    // Real secp256k1 key derivation / signing (executed by Foundry, not mocked)
    function addr(uint256 privateKey) external returns (address keyAddr);
    function sign(uint256 privateKey, bytes32 digest)
        external
        returns (uint8 v, bytes32 r, bytes32 s);

    function label(address account, string calldata newLabel) external;

    // Formatting
    function toString(address value) external pure returns (string memory);
    function toString(uint256 value) external pure returns (string memory);
    function toString(bytes32 value) external pure returns (string memory);
    function toString(bytes memory value) external pure returns (string memory);

    // Scripting
    function startBroadcast() external;
    function stopBroadcast() external;

    // Environment
    function envOr(string calldata name, string calldata defaultValue)
        external
        returns (string memory value);
    function envOr(string calldata name, uint256 defaultValue)
        external
        returns (uint256 value);
    function envOr(string calldata name, address defaultValue)
        external
        returns (address value);

    // Filesystem (paths are gated by fs_permissions in foundry.toml)
    function readFile(string calldata path) external view returns (string memory content);
    function writeJson(string calldata json, string calldata path) external;
    function parseJsonUint(string calldata json, string calldata key)
        external
        pure
        returns (uint256 value);
    function parseJsonAddress(string calldata json, string calldata key)
        external
        pure
        returns (address value);
    function parseJsonBytes32(string calldata json, string calldata key)
        external
        pure
        returns (bytes32 value);
    function parseJsonBytes(string calldata json, string calldata key)
        external
        pure
        returns (bytes memory value);
}
