// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {BoxV1} from "../v1/BoxV1.sol";

/// @title BoxV2 — COMPATIBLE upgrade: only appends new fields after all
///         existing ones (slots 67 and 68). Every V1 variable keeps its exact
///         slot/offset/type, so storage is preserved through the proxy.
contract BoxV2 is BoxV1 {
    uint256 internal extra;
    uint128 internal a;
    uint128 internal b;

    function version() external pure override returns (string memory) {
        return "v2";
    }

    function setExtra(uint256 v) external {
        extra = v;
    }

    function getExtra() external view returns (uint256) {
        return extra;
    }

    function setAB(uint128 av, uint128 bv) external {
        a = av;
        b = bv;
    }

    function getAB() external view returns (uint128, uint128) {
        return (a, b);
    }
}
