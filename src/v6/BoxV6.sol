// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {UUPSUpgradable} from "../core/UUPSUpgradable.sol";
import {InitialStorageV2} from "../core/InitialStorageV2.sol";

/// @title BoxV6 — COMPATIBLE upgrade via a SAFE BASE-CLASS CHANGE.
/// @notice Standalone implementation compiled for the storage checker: the
/// base V0 is replaced with V2 which adds `baseName` by consuming one gap
/// slot. Every V1 field keeps label+slot+offset+type; the only storage delta
/// inside the base is a gap shrinking by one alongside the new field, which
/// the checker recognises as a reserved-gap append and accepts.
contract BoxV6 is UUPSUpgradable, InitialStorageV2 {
    uint256 internal x;
    uint32 internal y;
    uint16 internal z;
    uint16 internal w;
    string internal name;
    bytes internal flags;
    uint256[] internal counts;
    mapping(uint256 => uint256) internal values;
    uint256 internal extra;

    function initialize() external {
        __UUPS_init();
    }

    function version() external pure returns (string memory) {
        return "v6";
    }

    function setBaseName(string calldata s) external {
        InitialStorageV2.baseName = s;
    }

    function getBaseName() external view returns (string memory) {
        return InitialStorageV2.baseName;
    }
}
