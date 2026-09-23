// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {ERC20} from "./ERC20.sol";
import {CPMMMath} from "./CPMMMath.sol";

interface ICPMMCallee {
    /// @notice Flash-swap callback. Must repay the swapped token so that the
    ///         constant-product invariant holds after return.
    function cpmmSwapCall(address sender, uint256 amount0Out, uint256 amount1Out, bytes calldata data) external;
}

/// @title CPMM Pair — two-token constant-product (x*y=k) pool.
/// @notice Uniswap-V2-style accounting with reserves cached separately from
///         real token balances. All state-changing entry points share one
///         reentrancy lock; a 30 bps fee applies to swaps; the first
///         MINIMUM_LIQUIDITY (1_000) LP shares are permanently locked at
///         address(0).
contract CPMMPair is ERC20 {
    uint256 public constant MINIMUM_LIQUIDITY = 1_000;
    /// @notice Address that can never withdraw the bootstrap shares.
    address public constant MINIMUM_LIQUIDITY_LOCKED_TO = address(0);

    address public immutable token0;
    address public immutable token1;

    uint112 private reserve0;
    uint112 private reserve1;
    uint32 private blockTimestampLast;

    uint256 private unlocked = 1;

    event Mint(address indexed sender, uint256 amount0, uint256 amount1);
    event Burn(address indexed sender, uint256 amount0, uint256 amount1, address indexed to);
    event Swap(
        address indexed sender,
        uint256 amount0In,
        uint256 amount1In,
        uint256 amount0Out,
        uint256 amount1Out,
        address indexed to
    );
    event Sync(uint112 reserve0, uint112 reserve1);

    error Locked();
    error TokenIdentical();
    error ZeroAddress();
    error InsufficientOutputAmount();
    error InsufficientLiquidity();
    error InvalidTo();
    error KInvariant();
    error InsufficientInputAmount();
    error InsufficientFirstLiquidity();
    error TransferFailed();
    error BalanceOverflow();

    modifier lock() {
        if (unlocked == 0) revert Locked();
        unlocked = 0;
        _;
        unlocked = 1;
    }

    constructor(address _token0, address _token1) ERC20("Constant-Product LP", "CPMM-LP") {
        if (_token0 == _token1) revert TokenIdentical();
        if (_token0 == address(0) || _token1 == address(0)) revert ZeroAddress();
        token0 = _token0;
        token1 = _token1;
    }

    function getReserves() external view returns (uint112 _reserve0, uint112 _reserve1, uint32 _blockTimestampLast) {
        _reserve0 = reserve0;
        _reserve1 = reserve1;
        _blockTimestampLast = blockTimestampLast;
    }

    // ------------------------------------------------------------------
    // Liquidity
    // ------------------------------------------------------------------

    /// @notice Mints LP shares for the tokens already transferred in.
    /// @param expected0 / expected1 MUST equal the actual balance increase.
    ///        They reject fee-on-transfer tokens: any shortfall reverts.
    function mint(address to, uint256 expected0, uint256 expected1) external lock returns (uint256 liquidity) {
        (uint112 _reserve0, uint112 _reserve1,) = this.getReserves();
        uint256 balance0 = _erc20Balance(token0);
        uint256 balance1 = _erc20Balance(token1);
        uint256 amount0 = balance0 - _reserve0;
        uint256 amount1 = balance1 - _reserve1;

        if (amount0 == 0 || amount1 == 0) revert InsufficientInputAmount();
        // Reject tokens that deduct a fee (or otherwise short-change) on transfer.
        if (amount0 != expected0 || amount1 != expected1) revert TransferFailed();

        uint256 _totalSupply = totalSupply;
        if (_totalSupply == 0) {
            uint256 root = CPMMMath.sqrt(amount0 * amount1);
            // Explicit guard before subtraction so the revert is semantic
            // (not an arithmetic panic) at the minimum-liquidity boundary.
            if (root <= MINIMUM_LIQUIDITY) revert InsufficientFirstLiquidity();
            liquidity = root - MINIMUM_LIQUIDITY;
            // Permanently lock the bootstrap shares at address(0).
            _mint(MINIMUM_LIQUIDITY_LOCKED_TO, MINIMUM_LIQUIDITY);
        } else {
            liquidity = CPMMMath.mintedLiquidity(amount0, amount1, _reserve0, _reserve1, _totalSupply);
            if (liquidity == 0) revert InsufficientFirstLiquidity();
        }
        _mint(to, liquidity);

        _update(balance0, balance1);
        emit Mint(msg.sender, amount0, amount1);
    }

    /// @notice Burns LP shares already transferred in and returns both tokens pro-rata.
    function burn(address to) external lock returns (uint256 amount0, uint256 amount1) {
        uint256 liquidity = _erc20Balance(address(this)); // LP shares were sent here
        if (liquidity == 0) revert InsufficientLiquidity();
        uint256 _totalSupply = totalSupply; // uses total including address(0) lock

        (uint112 _reserve0, uint112 _reserve1,) = this.getReserves();
        (amount0, amount1) = CPMMMath.burnedAmounts(liquidity, _totalSupply, _reserve0, _reserve1);
        if (amount0 == 0 || amount1 == 0) revert InsufficientFirstLiquidity();

        _burn(address(this), liquidity);
        _safeTransfer(token0, to, amount0);
        _safeTransfer(token1, to, amount1);

        uint256 balance0 = _erc20Balance(token0);
        uint256 balance1 = _erc20Balance(token1);
        _update(balance0, balance1);
        emit Burn(msg.sender, amount0, amount1, to);
    }

    // ------------------------------------------------------------------
    // Swap
    // ------------------------------------------------------------------

    /// @notice Constant-product swap. Input tokens must already be transferred in.
    /// @param expectedInput0 / expectedInput1 MUST equal the actual balance
    ///        increase, rejecting fee-on-transfer input tokens.
    /// @param data When non-empty, a flash-swap callback is invoked on the
    ///        caller before the k-invariant is checked.
    function swap(
        uint256 amount0Out,
        uint256 amount1Out,
        address to,
        uint256 expectedInput0,
        uint256 expectedInput1,
        bytes calldata data
    ) external lock {
        if (amount0Out == 0 && amount1Out == 0) revert InsufficientOutputAmount();
        (uint112 _reserve0, uint112 _reserve1,) = this.getReserves();
        if (amount0Out >= _reserve0 || amount1Out >= _reserve1) revert InsufficientLiquidity();
        if (to == token0 || to == token1 || to == address(0)) revert InvalidTo();

        if (amount0Out > 0) _safeTransfer(token0, to, amount0Out);
        if (amount1Out > 0) _safeTransfer(token1, to, amount1Out);
        if (data.length > 0) {
            ICPMMCallee(to).cpmmSwapCall(msg.sender, amount0Out, amount1Out, data);
        }

        uint256 balance0 = _erc20Balance(token0);
        uint256 balance1 = _erc20Balance(token1);
        uint256 amount0In = balance0 > _reserve0 - amount0Out ? balance0 - (_reserve0 - amount0Out) : 0;
        uint256 amount1In = balance1 > _reserve1 - amount1Out ? balance1 - (_reserve1 - amount1Out) : 0;
        if (amount0In == 0 && amount1In == 0) revert InsufficientInputAmount();
        // Fee-on-transfer inputs would show up here as a shortfall.
        if (amount0In != expectedInput0 || amount1In != expectedInput1) revert TransferFailed();

        // x*y must not decrease after the 30 bps fee:
        // balance0Adjusted = balance0*1000 - amount0In*3
        uint256 balance0Adjusted = balance0 * 1000 - amount0In * 3;
        uint256 balance1Adjusted = balance1 * 1000 - amount1In * 3;
        if (balance0Adjusted * balance1Adjusted < uint256(_reserve0) * _reserve1 * 1_000_000) {
            revert KInvariant();
        }

        _update(balance0, balance1);
        emit Swap(msg.sender, amount0In, amount1In, amount0Out, amount1Out, to);
    }

    // ------------------------------------------------------------------
    // Reserve maintenance
    // ------------------------------------------------------------------

    /// @notice Redeem accidental token donations (reserves are never trusted
    ///         from balances until explicitly synced).
    function skim(address to) external lock {
        if (to == address(0)) revert ZeroAddress();
        _safeTransfer(token0, to, _erc20Balance(token0) - reserve0);
        _safeTransfer(token1, to, _erc20Balance(token1) - reserve1);
    }

    /// @notice Force reserves to match real balances.
    function sync() external lock {
        _update(_erc20Balance(token0), _erc20Balance(token1));
    }

    // ------------------------------------------------------------------
    // Internal
    // ------------------------------------------------------------------

    function _update(uint256 balance0, uint256 balance1) private {
        if (balance0 > type(uint112).max || balance1 > type(uint112).max) {
            revert BalanceOverflow();
        }
        reserve0 = uint112(balance0);
        reserve1 = uint112(balance1);
        blockTimestampLast = uint32(block.timestamp);
        emit Sync(reserve0, reserve1);
    }

    function _erc20Balance(address token) private view returns (uint256 value) {
        (bool ok, bytes memory ret) = token.staticcall(abi.encodeWithSignature("balanceOf(address)", address(this)));
        if (!ok || ret.length < 32) revert TransferFailed();
        value = abi.decode(ret, (uint256));
    }

    function _safeTransfer(address token, address to, uint256 value) private {
        (bool ok, bytes memory ret) = token.call(abi.encodeWithSignature("transfer(address,uint256)", to, value));
        if (!ok || (ret.length != 0 && !abi.decode(ret, (bool)))) revert TransferFailed();
    }
}
