// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @notice 最小化的 Foundry 作弊码接口（只声明本项目测试用到的方法）。
///         forge 在测试 EVM 中于固定地址注入 Vm 实现，无需引入 forge-std。
interface Vm {
    function warp(uint256) external;
    function roll(uint256) external;
    function addr(uint256) external returns (address);
    function sign(uint256, bytes32) external returns (uint8, bytes32, bytes32);
    function prank(address) external;
    function startPrank(address) external;
    function stopPrank() external;
    function expectRevert(bytes4) external;
    function expectRevert(bytes calldata) external;
    function expectEmit(bool, bool, bool, bool, address) external;
}
