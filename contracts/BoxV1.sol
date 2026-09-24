// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title BoxV1 — first implementation behind the proxy.
contract BoxV1 {
    uint256 public value; // slot 0
    address public owner; // slot 1
    string public name; // slot 2 (dynamic)

    function initialize(uint256 v, string calldata n) external {
        require(owner == address(0), "already initialized");
        owner = msg.sender;
        value = v;
        name = n;
    }

    function setValue(uint256 v) external {
        value = v;
    }

    function version() external pure virtual returns (string memory) {
        return "v1";
    }
}
