// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {UUPSUpgradable} from "../core/UUPSUpgradable.sol";
import {InitialStorageV0} from "../core/InitialStorageV0.sol";

/// @title BoxV3 — INCOMPATIBLE upgrade: fields are REORDERED.
/// @notice Compared with V1, `name` moved from slot 63 to slot 61 and the
///         packed group (y/z/w) moved to slot 63/64. The checker must flag the
///         slot moves and reject this upgrade.
contract BoxV3 is UUPSUpgradable, InitialStorageV0 {
    string internal name;
    bytes internal flags;
    uint256 internal x;
    uint32 internal y;
    uint16 internal z;
    uint16 internal w;
    uint256[] internal counts;
    mapping(uint256 => uint256) internal values;

    function initialize() external {
        __UUPS_init();
    }

    function version() external pure returns (string memory) {
        return "v3";
    }

    function setX(uint256 v) external {
        x = v;
    }

    function getX() external view returns (uint256) {
        return x;
    }

    function setName(string calldata s) external {
        name = s;
    }

    function getName() external view returns (string memory) {
        return name;
    }
}
