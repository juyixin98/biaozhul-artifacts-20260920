// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {UUPSUpgradable} from "../core/UUPSUpgradable.sol";
import {InitialStorageV1} from "../core/InitialStorageV1.sol";

/// @title BoxV5 — INCOMPATIBLE upgrade through a BASE-CLASS CHANGE.
/// @notice Storage looks identical to V1 at the derived level (x/y/z/w/...),
/// and the gap shrink keeps x at slot 61, but the base contract
/// InitialStorageV0 was replaced by InitialStorageV1 whose baseCounter was
/// widened from uint64 to uint256 — this silently corrupts baseAdmin and all
/// packed data in that base slot. The checker must flag the base change even
/// though downstream field slots are unchanged.
contract BoxV5 is UUPSUpgradable, InitialStorageV1 {
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

    function version() external pure returns (string memory) {
        return "v5";
    }

    function getBaseCounter() external view returns (uint256) {
        return InitialStorageV1.baseCounter;
    }

    function setBaseCounter(uint256 v) external {
        InitialStorageV1.baseCounter = v;
    }
}
