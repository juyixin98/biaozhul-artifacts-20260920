// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {MockERC20} from "./MockERC20.sol";

/// @notice ERC20 形式的份额接口（金库本身就是份额代币）。
interface IERC20 {
    function totalSupply() external view returns (uint256);
    function balanceOf(address account) external view returns (uint256);
    function allowance(address owner, address spender) external view returns (uint256);
    function approve(address spender, uint256 value) external returns (bool);
    function transfer(address to, uint256 value) external returns (bool);
    function transferFrom(address from, address to, uint256 value) external returns (bool);
}

/// @title ShareVault
/// @notice 模拟资产（MockERC20）的份额金库：存款得到份额，赎回销毁份额拿回资产。
/// @dev 舍入不变量（全部使用整数算术，0.8.x 默认 checked 运算）：
///   1. previewDeposit 用向下取整除法，存款得到的份额“宁少勿多”；
///      存款者最多损失 1 wei 份额（首次另受死份额影响），旧份额持有者不受损。
///   2. previewRedeem 用向下取整除法，赎回拿到的资产“宁少勿多”；
///      未取走的零头(dust) 留在库内、由剩余份额按比例共享，金库永不会被掏空到资不抵债。
///   3. 空库初始化：首次存款按 1:1 铸造，并永久锁死 MIN_LIQUIDITY 份额到 address(1)，
///      这是 Uniswap V2 式“死份额”方案，使经典的首发捐赠/通胀攻击无利可图。
///   4. 资产余额只按“链上真实记账余额”计算(totalAssets = asset.balanceOf(address(this)))，
///      不维护任何可被捐赠污染的内部资产计数器；恶意 ERC20 回调期间发生的重入转账
///      会被重入锁拒绝（存款/赎回均为 nonReentrant + 检查-生效-交互 顺序）。
///   5. maxSlippageBps 由存款人/赎回人按笔给出，兑换结果低于下界则整笔回滚。
contract ShareVault {
    // ---------------------------------------------------------------------------
    // 常量与存储
    // ---------------------------------------------------------------------------

    /// @notice 首次存款永久锁死到死地址的份额数量（Uniswap V2 式最小流动性）。
    uint256 public constant MIN_LIQUIDITY = 1000;

    /// @notice 死份额接收地址。刻意不用 address(0)：避免 ERC20 的零地址检查，
    ///         且与普通转账日志区分开。
    address public constant DEAD = address(1);

    IERC20 public immutable asset;
    uint8 private immutable _underlyingDecimals;

    string public name;
    string public symbol;
    uint8 public constant decimals = 18;
    uint256 public totalSupply;

    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    /// @dev 简单的 0/1 重入锁（不引入库依赖）。
    uint256 private locked = 1;

    // ---------------------------------------------------------------------------
    // 事件与错误
    // ---------------------------------------------------------------------------

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

    error ZeroAddress();
    error ZeroAssets();
    error ZeroShares();
    error FirstDepositTooSmall(uint256 minAssets, uint256 provided);
    error SlippageExceeded(uint256 minAccepted, uint256 actual);
    error InsufficientShares(uint256 balance, uint256 needed);
    error InsufficientAllowance(uint256 allowance, uint256 needed);
    error Reentrancy();

    modifier nonReentrant() {
        if (locked != 1) revert Reentrancy();
        locked = 2;
        _;
        locked = 1;
    }

    constructor(address asset_, string memory name_, string memory symbol_) {
        if (asset_ == address(0)) revert ZeroAddress();
        asset = IERC20(asset_);
        _underlyingDecimals = MockERC20(asset_).decimals();
        name = name_;
        symbol = symbol_;
    }

    // ---------------------------------------------------------------------------
    // 视图函数
    // ---------------------------------------------------------------------------

    function totalAssets() public view returns (uint256) {
        return asset.balanceOf(address(this));
    }

    /// @notice 存入 assets 数量的资产预期得到多少份额（向下取整）。
    function previewDeposit(uint256 assets) public view returns (uint256) {
        if (assets == 0) return 0;
        uint256 supply = totalSupply;
        if (supply == 0) {
            // 1:1 初始化，死份额在存款路径上单独扣除。
            return assets > MIN_LIQUIDITY ? assets - MIN_LIQUIDITY : 0;
        }
        // 向下取整：shares = floor(assets * supply / totalAssets)
        return (assets * supply) / totalAssets();
    }

    /// @notice 赎回 shares 数量的份额预期拿到多少资产（向下取整）。
    function previewRedeem(uint256 shares) public view returns (uint256) {
        if (shares == 0 || totalSupply == 0) return 0;
        // 向下取整：assets = floor(shares * totalAssets / supply)
        return (shares * totalAssets()) / totalSupply;
    }

    function underlyingDecimals() external view returns (uint8) {
        return _underlyingDecimals;
    }

    // ---------------------------------------------------------------------------
    // 存款
    // ---------------------------------------------------------------------------

    /// @notice 存入资产换取份额。
    /// @param assets      存入的资产数量（整数最小单位）。
    /// @param receiver    份额接收人。
    /// @param minShares   调用者接受的最少份额（滑点保护，基于自身整数计算后传入）。
    /// @return shares     实际铸造的份额。
    function deposit(uint256 assets, address receiver, uint256 minShares)
        external
        nonReentrant
        returns (uint256 shares)
    {
        if (receiver == address(0)) revert ZeroAddress();
        if (assets == 0) revert ZeroAssets();

        uint256 supply = totalSupply;
        if (supply == 0) {
            // 空库初始化：要求 assets > MIN_LIQUIDITY，铸出的 MIN_LIQUIDITY 份永久锁死。
            if (assets <= MIN_LIQUIDITY) revert FirstDepositTooSmall(MIN_LIQUIDITY + 1, assets);
            shares = assets - MIN_LIQUIDITY;
            // 先记账死份额与用户份额（检查-生效-交互）。
            totalSupply = assets;
            balanceOf[DEAD] += MIN_LIQUIDITY;
            balanceOf[receiver] += shares;
            emit Transfer(address(0), DEAD, MIN_LIQUIDITY);
            emit Transfer(address(0), receiver, shares);
        } else {
            // 向下取整，结果为 0 直接拒绝，防止“极小存款”铸出 0 份额而资产被吞。
            shares = (assets * supply) / totalAssets();
            if (shares == 0) revert ZeroShares();
            totalSupply = supply + shares;
            balanceOf[receiver] += shares;
            emit Transfer(address(0), receiver, shares);
        }

        if (shares < minShares) revert SlippageExceeded(minShares, shares);

        // 交互放最后：即使资产代币在 transferFrom 中恶意回调，状态已全部结算且有重入锁。
        bool ok = asset.transferFrom(msg.sender, address(this), assets);
        require(ok, "asset transfer failed");

        emit Deposit(msg.sender, receiver, assets, shares);
    }

    // ---------------------------------------------------------------------------
    // 赎回
    // ---------------------------------------------------------------------------

    /// @notice 销毁份额、按当前汇率赎回资产。
    /// @param shares       要销毁的份额数量。
    /// @param receiver     资产接收人。
    /// @param owner        份额持有人（非本人需预先 approve）。
    /// @param minAssetsOut 接受的最少资产数量（滑点保护）。
    /// @return assetsOut   实际转出的资产数量。
    function redeem(uint256 shares, address receiver, address owner, uint256 minAssetsOut)
        external
        nonReentrant
        returns (uint256 assetsOut)
    {
        if (receiver == address(0) || owner == address(0)) revert ZeroAddress();
        if (shares == 0) revert ZeroShares();

        uint256 balance = balanceOf[owner];
        if (balance < shares) revert InsufficientShares(balance, shares);
        if (owner != msg.sender) {
            uint256 allowed = allowance[owner][msg.sender];
            if (allowed < shares) revert InsufficientAllowance(allowed, shares);
            if (allowed != type(uint256).max) {
                unchecked {
                    allowance[owner][msg.sender] = allowed - shares;
                }
            }
        }

        // 向下取整：零头留在库内，保证 sum(份额可兑资产) <= 库内资产。
        assetsOut = (shares * totalAssets()) / totalSupply;

        // 检查-生效-交互：先烧份额，再处理外部转账与回调。
        unchecked {
            balanceOf[owner] = balance - shares;
            totalSupply -= shares;
        }
        emit Transfer(owner, address(0), shares);

        if (assetsOut < minAssetsOut) revert SlippageExceeded(minAssetsOut, assetsOut);

        bool ok = asset.transfer(receiver, assetsOut);
        require(ok, "asset transfer failed");

        emit Withdraw(msg.sender, receiver, owner, assetsOut, shares);
    }

    // ---------------------------------------------------------------------------
    // 份额代币 ERC20（份额转账不会移动底层资产，可随时由持份者赎回）
    // ---------------------------------------------------------------------------

    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function transfer(address to, uint256 value) external returns (bool) {
        _transferShares(msg.sender, to, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        if (allowed != type(uint256).max) {
            if (allowed < value) revert InsufficientAllowance(allowed, value);
            unchecked {
                allowance[from][msg.sender] = allowed - value;
            }
        }
        _transferShares(from, to, value);
        return true;
    }

    function _transferShares(address from, address to, uint256 value) internal {
        if (to == address(0)) revert ZeroAddress();
        uint256 balance = balanceOf[from];
        if (balance < value) revert InsufficientShares(balance, value);
        unchecked {
            balanceOf[from] = balance - value;
        }
        balanceOf[to] += value;
        emit Transfer(from, to, value);
    }
}
