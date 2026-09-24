// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "./interfaces/IERC20.sol";
import {SafeTransferLib} from "./SafeTransferLib.sol";
import {Math512} from "./Math512.sol";

/// @title ShareVault
/// @notice ERC-4626-shaped share vault with explicit rounding and first-depositor
///         inflation defense. Integer arithmetic only; no external dependencies.
///
/// @dev ROUNDING DIRECTIONS (everything floors; a user is never rounded in
///      their own favor at the pool's expense):
///        * deposit -> shares minted = floor(assets * (S + V) / (A + 1))  DOWN
///        * redeem  -> assets paid  = floor(shares * (A + 1) / (S + V))  DOWN
///      where S = real share supply, A = vault asset balance, V = virtual
///      share offset. Floor-on-redeem leaves sub-unit dust in the vault for
///      remaining holders, so the price is monotonically non-decreasing.
///
/// @dev EMPTY-VAULT INITIALISATION: no bootstrap deposit, no locked liquidity.
///      The exchange rate is defined even when S = 0 by V virtual shares and
///      1 virtual asset: price(0) = 1/V, so depositing 1 unit on an empty
///      vault mints exactly V shares and redeeming them pays 1 unit back.
///
/// @dev FIRST-DEPOSITOR / DONATION (INFLATION) ATTACK:
///      The canonical attack (deposit 1, donate a large amount D so the next
///      depositor's tiny deposit rounds to 0 shares, then redeem to scoop it)
///      is bounded by VIRTUAL_SHARE_OFFSET = 1000: to make a victim deposit of
///      size x round to zero the attacker must donate ~1000*x, and on redeem
///      the attacker owns at most 1/(1 + 1000) of the inflated pool, i.e. they
///      forfeit ~1000 assets for every 1 they could possibly gain. Assets sent
///      directly to the vault (donations) raise the price pro-rata for every
///      share holder and are never mintable into shares.
///      Known residual: a donation to a COMPLETELY empty vault (S = 0, before
///      anyone deposited) worsens the rate of the *first* depositor up to the
///      same 1:1000 bound while the donor, holding no shares, cannot recover
///      it; it is a pure gift to the first depositor, never a loss to an
///      existing holder.
///
/// @dev MAX SLIPPAGE: deposit/redeem take minSharesOut / minAssetsOut. A
///      donation or reorg between an off-chain quote and execution can move
///      the rate; a fill worse than the caller's limit reverts (Slippage).
///
/// @dev REENTRANCY: all share state is updated before the external token call
///      (checks-effects-interactions) and deposit/redeem carry a nonReentrant
///      guard, so malicious ERC-20 callbacks cannot observe a half-updated pool.
///
/// @dev The asset pull is measured by balance delta, so fee-on-transfer tokens
///      are credited for what actually arrives.
contract ShareVault {
    using SafeTransferLib for IERC20;

    /*//////////////////////////////////////////////////////////////
                                 CONSTANTS
    //////////////////////////////////////////////////////////////*/

    /// @notice Virtual shares always counted in the denominator but never
    ///         minted. 10**3 makes the inflation attack cost ~1000x the gain.
    uint256 public constant VIRTUAL_SHARE_OFFSET = 1000;
    /// @notice Virtual asset counted in the denominator; keeps the rate defined
    ///         on an empty vault and floors conversion toward zero.
    uint256 public constant VIRTUAL_ASSET_OFFSET = 1;

    /*//////////////////////////////////////////////////////////////
                                  STORAGE
    //////////////////////////////////////////////////////////////*/

    IERC20 public immutable asset;
    string public name;
    string public symbol;
    uint8 public constant decimals = 18;

    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    uint256 private _locked;

    /*//////////////////////////////////////////////////////////////
                                  EVENTS
    //////////////////////////////////////////////////////////////*/

    event Deposit(address indexed sender, address indexed owner, uint256 assets, uint256 shares);
    event Withdraw(
        address indexed sender,
        address indexed receiver,
        address indexed owner,
        uint256 assets,
        uint256 shares
    );
    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    /*//////////////////////////////////////////////////////////////
                                  ERRORS
    //////////////////////////////////////////////////////////////*/

    error ZeroShares();
    error ZeroAssets();
    error Slippage(uint256 got, uint256 min);
    error InsufficientShares();
    error InsufficientBalance();
    error InsufficientAllowance();
    error Reentrancy();
    error InvalidReceiver();

    modifier nonReentrant() {
        if (_locked == 1) revert Reentrancy();
        _locked = 1;
        _;
        _locked = 0;
    }

    constructor(address _asset, string memory _name, string memory _symbol) {
        asset = IERC20(_asset);
        name = _name;
        symbol = _symbol;
    }

    /*//////////////////////////////////////////////////////////////
                             ACCOUNTING VIEWS
    //////////////////////////////////////////////////////////////*/

    /// @notice Real assets held by the vault. Direct transfers (donations)
    ///         count here, in pro-rata favor of all share holders.
    function totalAssets() public view returns (uint256) {
        return asset.balanceOf(address(this));
    }

    /// @notice Shares received for `assets` deposited. Rounds DOWN.
    function convertToShares(uint256 assets) public view returns (uint256) {
        return Math512.mulDivDown(
            assets, totalSupply + VIRTUAL_SHARE_OFFSET, totalAssets() + VIRTUAL_ASSET_OFFSET
        );
    }

    /// @notice Assets paid for `shares` redeemed. Rounds DOWN.
    function convertToAssets(uint256 shares) public view returns (uint256) {
        return Math512.mulDivDown(
            shares, totalAssets() + VIRTUAL_ASSET_OFFSET, totalSupply + VIRTUAL_SHARE_OFFSET
        );
    }

    /// @notice Exchange rate scaled by 1e18: (virtual) assets per one share.
    ///         Monotonically non-decreasing across every state transition.
    function assetsPerShare() external view returns (uint256) {
        return Math512.mulDivDown(
            1e18,
            totalAssets() + VIRTUAL_ASSET_OFFSET,
            totalSupply + VIRTUAL_SHARE_OFFSET
        );
    }

    function previewDeposit(uint256 assets) external view returns (uint256) {
        return convertToShares(assets);
    }

    function previewRedeem(uint256 shares) external view returns (uint256) {
        return convertToAssets(shares);
    }

    /*//////////////////////////////////////////////////////////////
                                 DEPOSIT
    //////////////////////////////////////////////////////////////*/

    /// @notice Pull `assets` from msg.sender and credit shares to `receiver`.
    /// @param minSharesOut maximum-slippage floor on shares minted; 0 accepts
    ///        any non-zero fill.
    function deposit(uint256 assets, address receiver, uint256 minSharesOut)
        external
        nonReentrant
        returns (uint256 shares)
    {
        if (receiver == address(0)) revert InvalidReceiver();
        if (assets == 0) revert ZeroAssets();

        uint256 balanceBefore = totalAssets();
        asset.safeTransferFrom(msg.sender, address(this), assets);
        // Credit what actually arrived (fee-on-transfer safety).
        uint256 assetsIn = totalAssets() - balanceBefore;
        if (assetsIn == 0) revert ZeroAssets();

        // Shares round DOWN, priced against the PRE-deposit pool: a depositor
        // can never take more than the current (virtual-adjusted) pool ratio.
        shares = Math512.mulDivDown(
            assetsIn,
            totalSupply + VIRTUAL_SHARE_OFFSET,
            balanceBefore + VIRTUAL_ASSET_OFFSET
        );
        if (shares == 0) revert ZeroShares();
        if (shares < minSharesOut) revert Slippage(shares, minSharesOut);

        _mint(receiver, shares);
        emit Deposit(msg.sender, receiver, assetsIn, shares);
    }

    /*//////////////////////////////////////////////////////////////
                                 REDEEM
    //////////////////////////////////////////////////////////////*/

    /// @notice Burn `shares` from msg.sender and pay their floored asset value
    ///         to `receiver`.
    /// @param minAssetsOut maximum-slippage floor on assets paid; 0 accepts any
    ///        non-zero fill.
    function redeem(uint256 shares, address receiver, uint256 minAssetsOut)
        external
        nonReentrant
        returns (uint256 assets)
    {
        if (receiver == address(0)) revert InvalidReceiver();
        if (shares == 0) revert ZeroShares();
        if (shares > balanceOf[msg.sender]) revert InsufficientShares();

        // Assets round DOWN; sub-unit dust stays with the remaining holders.
        assets = convertToAssets(shares);
        if (assets == 0) revert ZeroAssets();
        if (assets < minAssetsOut) revert Slippage(assets, minAssetsOut);

        _burn(msg.sender, shares);
        // External call last (checks-effects-interactions) behind nonReentrant:
        // a malicious token callback cannot re-enter a half-updated pool.
        asset.safeTransfer(receiver, assets);
        emit Withdraw(msg.sender, receiver, msg.sender, assets, shares);
    }

    /*//////////////////////////////////////////////////////////////
                        SHARE ERC-20 (transferable)
    //////////////////////////////////////////////////////////////*/

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        emit Approval(msg.sender, spender, amount);
        return true;
    }

    function transfer(address to, uint256 amount) external returns (bool) {
        _transfer(msg.sender, to, amount);
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 current = allowance[from][msg.sender];
        if (current != type(uint256).max) {
            if (current < amount) revert InsufficientAllowance();
            unchecked {
                allowance[from][msg.sender] = current - amount;
            }
        }
        _transfer(from, to, amount);
        return true;
    }

    function _transfer(address from, address to, uint256 amount) internal {
        if (to == address(0)) revert InvalidReceiver();
        if (balanceOf[from] < amount) revert InsufficientBalance();
        unchecked {
            balanceOf[from] -= amount;
        }
        balanceOf[to] += amount;
        emit Transfer(from, to, amount);
    }

    function _mint(address to, uint256 amount) internal {
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    function _burn(address from, uint256 amount) internal {
        if (balanceOf[from] < amount) revert InsufficientBalance();
        unchecked {
            balanceOf[from] -= amount;
            totalSupply -= amount;
        }
        emit Transfer(from, address(0), amount);
    }
}
