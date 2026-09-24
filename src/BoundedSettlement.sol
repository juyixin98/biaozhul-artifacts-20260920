// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

interface IERC20 {
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

/// @title BoundedSettlement
/// @notice 有界订单签名结算合约（仅用于本地测试链）。
///         maker 对订单做 EIP-712 签名，taker 提交签名结算。
///         支持部分成交（向下取整、最后一笔补足）、nonce 取消、到期、
///         按订单累计的费用上限。签名域绑定 chainId 与本合约地址。
contract BoundedSettlement {
    string public constant NAME = "BoundedSettlement";
    string public constant VERSION = "1";

    bytes32 public constant ORDER_TYPEHASH = keccak256(
        "Order(address maker,address sellToken,address buyToken,uint256 sellAmount,uint256 buyAmount,uint256 feeCap,uint256 nonce,uint256 expiry)"
    );

    bytes32 private constant EIP712_DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");

    struct Order {
        address maker;
        address sellToken; // maker 卖出、taker 收到的代币
        address buyToken; //  taker 支付、maker 收到的代币（费用也用该代币计价）
        uint256 sellAmount; // 签名额度：maker 最多卖出的 sellToken 总量
        uint256 buyAmount; //  完全成交时 taker 支付的 buyToken 总量
        uint256 feeCap; //    该订单累计费用上限（buyToken 计价）
        uint256 nonce; //    maker 维度的取消 nonce
        uint256 expiry; //   过期时间戳（含边界：block.timestamp > expiry 时拒绝）
    }

    // ---- 错误 ----
    error ZeroAddress();
    error SameToken();
    error ZeroAmount();
    error OrderExpired(uint256 expiry, uint256 nowTs);
    error NonceAlreadyCancelled(address maker, uint256 nonce);
    error InvalidSignature(address recovered, address expected);
    error BadSignatureLength(uint256 length);
    error BadSignatureV(uint8 v);
    error BadSignatureS();
    error FillExceedsRemaining(uint256 requested, uint256 remaining);
    error FeeCapExceeded(uint256 cumulative, uint256 cap);
    error TransferFailed(address token, address from, address to, uint256 amount);
    error NotOwner();

    // ---- 事件 ----
    event OrderFilled(
        bytes32 indexed orderHash,
        address indexed maker,
        address indexed taker,
        uint256 sellFilled,
        uint256 buyPaid,
        uint256 feePaid,
        uint256 totalSellFilled
    );
    event NonceCancelled(address indexed maker, uint256 indexed nonce);
    event FeeReceiverChanged(address indexed oldReceiver, address indexed newReceiver);

    // ---- 存储 ----
    bytes32 public immutable DOMAIN_SEPARATOR;
    address public owner;
    address public feeReceiver;

    mapping(bytes32 => uint256) public filledSellAmount; // orderHash => 已成交 sellToken 累计
    mapping(bytes32 => uint256) public filledBuyAmount; //  orderHash => 已支付 buyToken 累计
    mapping(bytes32 => uint256) public filledFeeAmount; //  orderHash => 已收费用累计
    mapping(address => mapping(uint256 => bool)) public cancelledNonces; // maker => nonce => 已取消

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    constructor(address feeReceiver_) {
        if (feeReceiver_ == address(0)) revert ZeroAddress();
        owner = msg.sender;
        feeReceiver = feeReceiver_;
        DOMAIN_SEPARATOR = keccak256(
            abi.encode(
                EIP712_DOMAIN_TYPEHASH,
                keccak256(bytes(NAME)),
                keccak256(bytes(VERSION)),
                block.chainid,
                address(this)
            )
        );
        emit FeeReceiverChanged(address(0), feeReceiver_);
    }

    // ---- 视图 ----

    /// @notice 订单的 EIP-712 摘要（绑定本合约 DOMAIN_SEPARATOR）
    function hashOrder(Order calldata order) public view returns (bytes32) {
        return keccak256(abi.encodePacked("\x19\x01", DOMAIN_SEPARATOR, _structHash(order)));
    }

    function _structHash(Order calldata order) internal pure returns (bytes32) {
        return keccak256(
            abi.encode(
                ORDER_TYPEHASH,
                order.maker,
                order.sellToken,
                order.buyToken,
                order.sellAmount,
                order.buyAmount,
                order.feeCap,
                order.nonce,
                order.expiry
            )
        );
    }

    // ---- 成交 ----

    /// @notice 结算一笔（部分）成交。msg.sender 即 taker，需已对本合约授权两种代币支出。
    /// @param sellFillAmount 本次成交的 sellToken 数量（<= 剩余额度）
    /// @param feeAmount 本次收取的费用（buyToken），订单累计不得超过 feeCap
    /// @return buyPaid 本次 taker 实际支付的 buyToken 数量
    function fillOrder(Order calldata order, uint256 sellFillAmount, uint256 feeAmount, bytes calldata signature)
        external
        returns (uint256 buyPaid)
    {
        // ---- 校验 ----
        if (order.maker == address(0) || order.sellToken == address(0) || order.buyToken == address(0)) {
            revert ZeroAddress();
        }
        if (order.sellToken == order.buyToken) revert SameToken();
        if (order.sellAmount == 0 || order.buyAmount == 0 || sellFillAmount == 0) revert ZeroAmount();
        if (block.timestamp > order.expiry) revert OrderExpired(order.expiry, block.timestamp);
        if (cancelledNonces[order.maker][order.nonce]) revert NonceAlreadyCancelled(order.maker, order.nonce);

        bytes32 orderHash = hashOrder(order);
        address recovered = _recover(orderHash, signature);
        if (recovered != order.maker) revert InvalidSignature(recovered, order.maker);

        uint256 filledSell = filledSellAmount[orderHash];
        uint256 remaining = order.sellAmount - filledSell; // 不变式：filledSell <= sellAmount
        if (sellFillAmount > remaining) revert FillExceedsRemaining(sellFillAmount, remaining);

        uint256 newFeeTotal = filledFeeAmount[orderHash] + feeAmount;
        if (newFeeTotal > order.feeCap) revert FeeCapExceeded(newFeeTotal, order.feeCap);

        // ---- 定价：按比例向下取整；最后一笔补足全部剩余，保证完全成交时总额精确 ----
        if (sellFillAmount == remaining) {
            buyPaid = order.buyAmount - filledBuyAmount[orderHash];
        } else {
            buyPaid = (order.buyAmount * sellFillAmount) / order.sellAmount;
        }

        // ---- 先更新状态（CEI），再做外部调用 ----
        filledSellAmount[orderHash] = filledSell + sellFillAmount;
        filledBuyAmount[orderHash] = filledBuyAmount[orderHash] + buyPaid;
        filledFeeAmount[orderHash] = newFeeTotal;

        // ---- 资产转移：任一失败整体回滚 ----
        _transferFrom(order.sellToken, order.maker, msg.sender, sellFillAmount);
        _transferFrom(order.buyToken, msg.sender, order.maker, buyPaid);
        if (feeAmount > 0) {
            _transferFrom(order.buyToken, msg.sender, feeReceiver, feeAmount);
        }

        emit OrderFilled(
            orderHash, order.maker, msg.sender, sellFillAmount, buyPaid, feeAmount, filledSell + sellFillAmount
        );
    }

    /// @notice maker 取消某个 nonce；此后该 maker 所有带此 nonce 的订单签名失效
    function cancelNonce(uint256 nonce) external {
        if (cancelledNonces[msg.sender][nonce]) revert NonceAlreadyCancelled(msg.sender, nonce);
        cancelledNonces[msg.sender][nonce] = true;
        emit NonceCancelled(msg.sender, nonce);
    }

    function setFeeReceiver(address newReceiver) external onlyOwner {
        if (newReceiver == address(0)) revert ZeroAddress();
        emit FeeReceiverChanged(feeReceiver, newReceiver);
        feeReceiver = newReceiver;
    }

    // ---- 内部 ----

    function _transferFrom(address token, address from, address to, uint256 amount) internal {
        (bool ok, bytes memory data) =
            token.call(abi.encodeWithSelector(IERC20.transferFrom.selector, from, to, amount));
        if (!ok || (data.length != 0 && !abi.decode(data, (bool)))) {
            revert TransferFailed(token, from, to, amount);
        }
    }

    function _recover(bytes32 digest, bytes calldata sig) internal pure returns (address) {
        if (sig.length != 65) revert BadSignatureLength(sig.length);
        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly {
            r := calldataload(sig.offset)
            s := calldataload(add(sig.offset, 32))
            v := byte(0, calldataload(add(sig.offset, 64)))
        }
        if (v < 27) v += 27;
        if (v != 27 && v != 28) revert BadSignatureV(v);
        // EIP-2：拒绝高位 s，防签名可塑性
        if (uint256(s) > 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0) revert BadSignatureS();
        address signer = ecrecover(digest, v, r, s);
        if (signer == address(0)) revert InvalidSignature(address(0), address(0));
        return signer;
    }
}
