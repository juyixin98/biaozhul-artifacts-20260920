// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {UUPSUpgradable} from "../core/UUPSUpgradable.sol";
import {InitialStorageV0} from "../core/InitialStorageV0.sol";

/// @title BoxV1 — first logic version.
/// @notice Storage layout (after the two base contracts):
///   slot 61  x        uint256
///   slot 62  y uint32 / z uint16 / w uint16   (single packed slot)
///   slot 63  name     string  (dynamic)
///   slot 64  flags    bytes   (dynamic)
///   slot 65  counts   uint256[] (dynamic array)
///   slot 66  values   mapping(uint256 => uint256)
contract BoxV1 is UUPSUpgradable, InitialStorageV0 {
    uint256 internal x;
    uint32 internal y;
    uint16 internal z;
    uint16 internal w;
    string internal name;
    bytes internal flags;
    uint256[] internal counts;
    mapping(uint256 => uint256) internal values;

    function initialize() external {
        __UUPS_init();
    }

    function version() external pure virtual returns (string memory) {
        return "v1";
    }

    function setX(uint256 v) external {
        x = v;
    }

    function setPacked(uint32 a, uint16 b, uint16 c) external {
        y = a;
        z = b;
        w = c;
    }

    function setName(string calldata s) external {
        name = s;
    }

    function setFlags(bytes calldata b) external {
        flags = b;
    }

    function pushCount(uint256 v) external {
        counts.push(v);
    }

    function setValue(uint256 k, uint256 v) external {
        values[k] = v;
    }

    function getX() external view returns (uint256) {
        return x;
    }

    function getPacked()
        external
        view
        returns (uint32, uint16, uint16)
    {
        return (y, z, w);
    }

    function getName() external view returns (string memory) {
        return name;
    }

    function getFlags() external view returns (bytes memory) {
        return flags;
    }

    function countLength() external view returns (uint256) {
        return counts.length;
    }

    function countAt(uint256 i) external view returns (uint256) {
        return counts[i];
    }

    function getValue(uint256 k) external view returns (uint256) {
        return values[k];
    }

    function getBaseCounter() external view returns (uint64) {
        return InitialStorageV0.baseCounter;
    }

    function setBaseCounter(uint64 v) external {
        InitialStorageV0.baseCounter = v;
    }
}
