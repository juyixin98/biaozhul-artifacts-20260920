// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "../interfaces/IERC20.sol";
import {ConstantProductPool} from "../ConstantProductPool.sol";
import {CallbackToken, ICallbackReceiver} from "./CallbackToken.sol";
import {TestERC20} from "./TestERC20.sol";

/// @title ReentrancyAttacker
/// @notice Attempts to reenter every state-changing pool entry point from the
///         CallbackToken transfer hook. All inner calls must revert with
///         ReentrancyLocked; the attacker records what actually happened so
///         tests can assert it.
contract ReentrancyAttacker is ICallbackReceiver {
    enum Mode {
        NONE,
        REENTER_SWAP,
        REENTER_ADD,
        REENTER_REMOVE
    }

    ConstantProductPool public pool;
    TestERC20 public tokenA;
    CallbackToken public tokenB;

    Mode public mode;
    bool public innerRevertedAsExpected;
    bytes4 public lastRevertSelector;
    uint256 public innerCalls;

    constructor(ConstantProductPool _pool, TestERC20 _tokenA, CallbackToken _tokenB) {
        pool = _pool;
        tokenA = _tokenA;
        tokenB = _tokenB;
    }

    // ------------------------------------------------------------------
    // Legit setup: deposit initial liquidity honestly (hooks armed off).
    // ------------------------------------------------------------------
    function seedLiquidity(uint256 amountA, uint256 amountB) external {
        tokenA.approve(address(pool), type(uint256).max);
        tokenB.approve(address(pool), type(uint256).max);
        pool.addLiquidity(amountA, amountB, 0, address(this), block.timestamp + 1);
    }

    /// @dev Prime balances/approvals used by the malicious inner call.
    function arm(Mode _mode) external {
        mode = _mode;
        // Funds for an attempted reentrant swap/add.
        tokenB.approve(address(pool), type(uint256).max);
        tokenA.approve(address(pool), type(uint256).max);
    }

    function attackSwap() external {
        mode = Mode.REENTER_SWAP;
        // Sell tokenB; input transfer invokes the hook (from == attacker).
        pool.swapExactInput(address(tokenB), 1e18, 0, address(this), block.timestamp + 1);
    }

    function attackSwapViaOutput() external {
        mode = Mode.REENTER_ADD;
        // Sell tokenA so tokenB is pushed out; output transfer invokes the
        // hook (to == attacker).
        pool.swapExactInput(address(tokenA), 1e18, 0, address(this), block.timestamp + 1);
    }

    function attackRemove() external {
        mode = Mode.REENTER_REMOVE;
        uint256 shares = pool.balanceOf(address(this));
        pool.removeLiquidity(shares / 2, 0, 0, address(this), block.timestamp + 1);
    }

    // ------------------------------------------------------------------
    // Callback, executed mid-transfer while the pool lock is held.
    // ------------------------------------------------------------------
    function cpmmTokenCallback() external override {
        require(msg.sender == address(tokenB), "attacker: only hook token");
        innerCalls++;
        Mode m = mode;
        if (m == Mode.REENTER_SWAP) {
            try pool.swapExactInput(address(tokenB), 1e15, 0, address(this), block.timestamp + 1) {
                innerRevertedAsExpected = false;
            } catch (bytes memory low) {
                _record(low);
            }
        } else if (m == Mode.REENTER_ADD) {
            try pool.addLiquidity(1e15, 1e15, 0, address(this), block.timestamp + 1) {
                innerRevertedAsExpected = false;
            } catch (bytes memory low) {
                _record(low);
            }
        } else if (m == Mode.REENTER_REMOVE) {
            try pool.swapExactInput(address(tokenA), 1e15, 0, address(this), block.timestamp + 1) {
                innerRevertedAsExpected = false;
            } catch (bytes memory low) {
                _record(low);
            }
        }
    }

    function _record(bytes memory low) private {
        lastRevertSelector = bytes4(low);
        innerRevertedAsExpected = lastRevertSelector == ConstantProductPool.ReentrancyLocked.selector;
    }

    receive() external payable {}
}

/// @title AttackerDeployer
/// @notice Deploys a ReentrancyAttacker at a predictable CREATE address, so
///         the CallbackToken (which must know its hook target at
///         construction) can be deployed BEFORE the attacker.
contract AttackerDeployer {
    function predictAddress(address deployer, uint64 nonce) external pure returns (address) {
        // Expose the standard CREATE formula (keccak(rlp([deployer,nonce]))[12:])
        // without relying on cheatcodes, for documentation.
        return _predict(deployer, nonce);
    }

    function deploy(ConstantProductPool _pool, TestERC20 _tokenA, CallbackToken _tokenB)
        external
        returns (ReentrancyAttacker)
    {
        return new ReentrancyAttacker(_pool, _tokenA, _tokenB);
    }

    function _predict(address deployer, uint64 nonce) private pure returns (address) {
        bytes memory data;
        if (nonce == 0x00) {
            data = abi.encodePacked(bytes1(0xd6), bytes1(0x94), deployer, bytes1(0x80));
        } else if (nonce <= 0x7f) {
            data = abi.encodePacked(bytes1(0xd6), bytes1(0x94), deployer, uint8(nonce));
        } else if (nonce <= 0xff) {
            data = abi.encodePacked(bytes1(0xd7), bytes1(0x94), deployer, bytes1(0x81), uint8(nonce));
        } else {
            data = abi.encodePacked(bytes1(0xd8), bytes1(0x94), deployer, bytes1(0x82), uint16(nonce));
        }
        return address(uint160(uint256(keccak256(data))));
    }
}
