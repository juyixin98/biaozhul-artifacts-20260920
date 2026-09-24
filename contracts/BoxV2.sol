// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title BoxV2 — layout-compatible upgrade of BoxV1.
/// @dev All V1 variables keep their slot/offset/type; new state is only
///      appended after the last V1 slot.
contract BoxV2 {
    uint256 public value; // slot 0 (unchanged)
    address public owner; // slot 1 (unchanged)
    string public name; // slot 2 (unchanged)
    uint256 public extra; // slot 3 (appended)
    mapping(address => uint256) public balances; // slot 4 (appended, dynamic)

    function initialize(uint256 v, string calldata n) external {
        require(owner == address(0), "already initialized");
        owner = msg.sender;
        value = v;
        name = n;
    }

    function setValue(uint256 v) external {
        value = v;
    }

    function setExtra(uint256 e) external {
        extra = e;
    }

    function deposit() external payable {
        balances[msg.sender] += msg.value;
    }

    function version() external pure returns (string memory) {
        return "v2";
    }
}
