// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {ICPMMCallee} from "../CPMMPair.sol";
import {CPMMPair} from "../CPMMPair.sol";

interface IERC20Mini {
    function transfer(address, uint256) external returns (bool);
}

/// @notice Flash-swap callee. During the callback it tries to reenter
///         swap/mint/burn (all blocked by the lock). It then repays the
///         required input amount from pre-funded balances when ``repay`` is
///         true; otherwise it repays nothing so the outer swap fails the
///         k-invariant check (atomic revert).
contract ReentrantCallee is ICPMMCallee {
    address public pair;
    uint8 public mode; // 0 = no repayment, 1 = partial (k fails), 2 = full
    bool public repay;

    constructor(address _pair) {
        pair = _pair;
    }

    function setRepay(bool v) external {
        repay = v;
        mode = v ? 2 : 0;
    }

    /// @notice 0 none, 1 partial (insufficient -> k fails), 2 full.
    function setMode(uint8 m) external {
        mode = m;
        repay = m == 2;
    }

    /// @notice Required input to cover an output plus the 30 bps fee (ceil).
    function requiredInput(uint256 amountOut) external pure returns (uint256) {
        return _requiredInput(amountOut);
    }

    function _requiredInput(uint256 amountOut) private pure returns (uint256) {
        // ceil(amountOut * 1000 / 997)
        return (amountOut * 1000 + 996) / 997;
    }

    function cpmmSwapCall(address, uint256 amount0Out, uint256 amount1Out, bytes calldata) external override {
        CPMMPair p = CPMMPair(pair);

        // All reentries must be blocked by the shared lock.
        try p.swap(1, 0, address(this), 0, 0, "") {
            revert("reentry swap succeeded");
        } catch {}
        try p.mint(address(this), 0, 0) {
            revert("reentry mint succeeded");
        } catch {}
        try p.burn(address(this)) {
            revert("reentry burn succeeded");
        } catch {}

        if (mode == 2) {
            // Full fee-adjusted repayment of each borrowed side.
            if (amount0Out > 0) _send(p.token0(), pair, _requiredInput(amount0Out));
            if (amount1Out > 0) _send(p.token1(), pair, _requiredInput(amount1Out));
        } else if (mode == 1) {
            // Repay the principal but not the fee: an input exists, yet k fails.
            if (amount0Out > 0) _send(p.token0(), pair, amount0Out);
            if (amount1Out > 0) _send(p.token1(), pair, amount1Out);
        }
        // mode 0: no input at all -> InsufficientInputAmount.
    }

    function _send(address token, address to, uint256 value) private {
        (bool ok, bytes memory ret) = token.call(abi.encodeWithSignature("transfer(address,uint256)", to, value));
        require(ok && (ret.length == 0 || abi.decode(ret, (bool))), "send failed");
    }
}
