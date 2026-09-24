// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice Minimal subset of the HEVM/Vm cheatcode interface used by this repo.
/// @dev This is a deliberately small, locally vendored stand-in for
///      forge-std/Vm.sol so the project builds and tests without downloading the
///      full forge-std library over a throttled network. The real cheatcode
///      contract is provided by the EVM (forge) at the well-known address below.
interface Vm {
    // execution context
    function startPrank(address msgSender) external;
    function startPrank(address msgSender, address txOrigin) external;
    function stopPrank() external;
    function prank(address msgSender) external;
    function prank(address msgSender, address txOrigin) external;

    // time
    function warp(uint256 newTimestamp) external;
    function roll(uint256 newNumber) external;

    // expectations
    function expectRevert() external;
    function expectRevert(bytes4 msgData) external;
    function expectEmit() external;
    function expectEmit(bool, bool, bool, bool) external;

    // environment
    function envUint(string calldata name) external;
    function envOr(string calldata name, uint256 defaultValue) external returns (uint256);
    function envAddress(string calldata name) external;

    // logging
    function label(address addr, string calldata newLabel) external;
}
