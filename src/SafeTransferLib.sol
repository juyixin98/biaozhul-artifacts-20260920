// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "./interfaces/IERC20.sol";

/// @title SafeTransferLib
/// @notice Minimal safe ERC-20 helpers. Accepts tokens that return no data
/// (USDT-style) and reverts on failed calls or `false` return values.
library SafeTransferLib {
    error TransferFailed();
    error TransferFromFailed();

    function safeTransfer(IERC20 token, address to, uint256 amount) internal {
        (bool ok, bytes memory ret) = address(token).call(
            abi.encodeCall(IERC20.transfer, (to, amount))
        );
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert TransferFailed();
    }

    function safeTransferFrom(IERC20 token, address from, address to, uint256 amount) internal {
        (bool ok, bytes memory ret) = address(token).call(
            abi.encodeCall(IERC20.transferFrom, (from, to, amount))
        );
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert TransferFromFailed();
    }
}
