// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {UUPSUpgradable} from "../core/UUPSUpgradable.sol";
import {InitialStorageV0} from "../core/InitialStorageV0.sol";

/// @title BoxV4 — INCOMPATIBLE upgrade in two independent ways:
///   1. TYPE WIDENING: packed `y` goes uint32 -> uint256, which reshuffles the
///      packing (z/w move to later slots);
///   2. DYNAMIC TYPE CHANGE: `counts` at slot 65 changes from uint256[] to
///      mapping(uint256 => uint256). Both occupy one "head" slot but use
///      incompatible keccak data encodings, so existing data would be
///      corrupted. The checker must flag both.
contract BoxV4 is UUPSUpgradable, InitialStorageV0 {
    uint256 internal x;
    uint256 internal y;
    uint16 internal z;
    uint16 internal w;
    string internal name;
    bytes internal flags;
    mapping(uint256 => uint256) internal counts;
    mapping(uint256 => uint256) internal values;

    function initialize() external {
        __UUPS_init();
    }

    function version() external pure returns (string memory) {
        return "v4";
    }

    function setX(uint256 v) external {
        x = v;
    }

    function getX() external view returns (uint256) {
        return x;
    }

    function setCount(uint256 k, uint256 v) external {
        counts[k] = v;
    }

    function getCount(uint256 k) external view returns (uint256) {
        return counts[k];
    }
}
