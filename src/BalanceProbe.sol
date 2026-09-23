// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "./interfaces/IERC20.sol";

/// @title BalanceProbe
/// @notice A pool-specific measurement address used to actively reject
///         fee-on-transfer / deflationary tokens at first deposit.
/// @dev Balance deltas measured on the pool itself only see the pool's side
///      of a transfer. A token that credits the recipient less than the
///      nominal amount is invisible on the *outgoing* path by balance
///      accounting alone (the sender's balance still drops by the full
///      amount). The probe closes that gap: the pool sends a small probe
///      amount here and measures how much actually arrived, then sweeps it
///      back. Each pool deploys its own probe and only that pool may sweep.
contract BalanceProbe {
    address public immutable owner;

    event ProbeSwept(address indexed token, address indexed to, uint256 amount);

    constructor() {
        owner = msg.sender;
    }

    /// @notice Transfer every unit of `token` held here back to `to`.
    ///         Only the owning pool may call it, and honest tokens move the
    ///         full balance (the probe just relayed a fixed probe amount).
    function sweep(address token, address to) external {
        require(msg.sender == owner, "probe: only owner");
        IERC20 t = IERC20(token);
        uint256 bal = t.balanceOf(address(this));
        if (bal != 0) {
            (bool ok, bytes memory ret) = address(t).call(abi.encodeCall(IERC20.transfer, (to, bal)));
            require(ok && (ret.length == 0 || _isTruthy(ret)), "probe: transfer");
            emit ProbeSwept(token, to, bal);
        }
    }

    function _isTruthy(bytes memory ret) private pure returns (bool) {
        if (ret.length < 32) return false;
        bytes32 word;
        assembly {
            word := mload(add(ret, 32))
        }
        return word == bytes32(uint256(1));
    }
}
