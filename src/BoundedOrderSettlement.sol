// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {MockERC20} from "./MockERC20.sol";

/// @title BoundedOrderSettlement
/// @notice Off-chain signed, bounded maker orders settled on a LOCAL test
///         chain only (Anvil + test keys). Not audited, not for real funds.
///
/// A maker signs an EIP-712 order offering `makerAmount` of `makerToken` for
/// `takerAmount` of `takerToken`. Bounds:
///   - partial fills allowed, repeatable until `makerAmount` is exhausted;
///   - pro-rata pricing with floor/ceil rounding that favors the maker;
///   - a fee in maker token, cumulatively capped by `maxFeeAmount`;
///   - an absolute `deadline`;
///   - a maker-chosen `nonce` that the maker can cancel up front;
///   - EIP-712 domain separator binds chain id and this contract address.
contract BoundedOrderSettlement {
    bytes32 public constant EIP712DOMAIN_TYPEHASH = keccak256(
        "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
    );
    bytes32 public constant ORDER_TYPEHASH = keccak256(
        // solhint-disable-next-line max-line-length
        "Order(address maker,address taker,address makerToken,address takerToken,uint256 makerAmount,uint256 takerAmount,uint256 nonce,uint256 deadline,address feeRecipient,uint256 maxFeeAmount)"
    );

    struct Order {
        address maker; // signs and sells makerToken
        address taker; // allowed counterparty; address(0) = anyone
        address makerToken; // token the maker gives
        address takerToken; // token the maker wants
        uint256 makerAmount; // total maker token on offer (gross of fee)
        uint256 takerAmount; // total taker token owed for a full fill
        uint256 nonce; // maker-chosen nonce; cancellation invalidates it
        uint256 deadline; // unix seconds; last valid block timestamp
        address feeRecipient; // paid in maker token; address(0) = fee must be 0
        uint256 maxFeeAmount; // cumulative fee cap for the order's whole life
    }

    /// @notice Cumulative gross maker token spent (proceeds + fees) per hash.
    mapping(bytes32 => uint256) public filledMakerAmount;
    /// @notice Cumulative fees paid per hash.
    mapping(bytes32 => uint256) public cumulativeFee;
    /// @notice Cumulative taker token received by the maker per hash.
    mapping(bytes32 => uint256) public filledTakerAmount;
    /// @notice True once a maker has cancelled this nonce.
    mapping(address => mapping(uint256 => bool)) public cancelledNonce;

    bytes32 public immutable domainSeparator;

    event OrderFilled(
        bytes32 indexed orderHash,
        address indexed maker,
        address indexed taker,
        uint256 makerProceeds,
        uint256 takerPaid,
        uint256 fee,
        uint256 cumulativeMakerSpent
    );
    event OrderCancelled(address indexed maker, uint256 indexed nonce);

    error InvalidSignature();
    error OrderExpired();
    error NonceCancelled();
    error UnauthorizedTaker();
    error ZeroAmount();
    error FillExceedsOrder(uint256 requested, uint256 remaining);
    error FeeExceedsCap(uint256 requested, uint256 remaining);
    error FeeExceedsSpent(uint256 fee, uint256 spent);
    error FeeRecipientRequired();

    constructor() {
        domainSeparator = keccak256(
            abi.encode(
                EIP712DOMAIN_TYPEHASH,
                keccak256(bytes("BoundedOrderSettlement")),
                keccak256(bytes("1")),
                block.chainid,
                address(this)
            )
        );
    }

    // ------------------------------------------------------------------ //
    // Core                                                               //
    // ------------------------------------------------------------------ //

    /// @notice Cancel every order using `nonce` that msg.sender has not yet
    ///         (fully or partially) been filled against. Already-executed
    ///         fills are not reversed.
    function cancelNonce(uint256 nonce) external {
        cancelledNonce[msg.sender][nonce] = true;
        emit OrderCancelled(msg.sender, nonce);
    }

    /// @notice Settle (part of) a signed maker order.
    /// @param order      The maker-signed order.
    /// @param spentInput Gross maker token this fill consumes; the taker
    ///                   receives `spent - fee` of it.
    /// @param fee        Maker-token fee for this fill, paid to the signed
    ///                   fee recipient.
    /// @param signature  Maker EIP-712 signature over `order`.
    /// @return takerDue  Taker token pulled from msg.sender to the maker.
    function fillOrder(
        Order calldata order,
        uint256 spentInput,
        uint256 fee,
        bytes calldata signature
    ) external returns (uint256 takerDue) {
        // --- validation ------------------------------------------------
        if (spentInput == 0) revert ZeroAmount();
        if (block.timestamp > order.deadline) revert OrderExpired();
        if (cancelledNonce[order.maker][order.nonce]) revert NonceCancelled();
        if (order.taker != address(0) && order.taker != msg.sender) {
            revert UnauthorizedTaker();
        }
        if (fee > spentInput) revert FeeExceedsSpent(fee, spentInput);
        if (fee > 0 && order.feeRecipient == address(0)) revert FeeRecipientRequired();

        bytes32 hash = hashOrder(order);
        if (!_isValidSignature(order.maker, hash, signature)) revert InvalidSignature();

        uint256 newSpent = filledMakerAmount[hash] + spentInput;
        if (newSpent > order.makerAmount) {
            revert FillExceedsOrder(spentInput, order.makerAmount - filledMakerAmount[hash]);
        }
        uint256 newFee = cumulativeFee[hash] + fee;
        if (newFee > order.maxFeeAmount) {
            revert FeeExceedsCap(fee, order.maxFeeAmount - cumulativeFee[hash]);
        }

        // --- pricing ---------------------------------------------------
        // Pro-rata: the taker pays ceil(proceeds * takerAmount / makerAmount),
        // so rounding always rounds the maker's incoming amount UP.
        uint256 makerProceeds = spentInput - fee;
        takerDue = _ceilMulDiv(makerProceeds, order.takerAmount, order.makerAmount);

        // The final fill clears the order: the taker pays exactly the
        // remaining signed taker amount, eliminating trailing dust.
        uint256 priorTaker = filledTakerAmount[hash];
        if (newSpent == order.makerAmount) {
            takerDue = order.takerAmount - priorTaker;
        }

        // --- effects ---------------------------------------------------
        filledMakerAmount[hash] = newSpent;
        cumulativeFee[hash] = newFee;
        filledTakerAmount[hash] = priorTaker + takerDue;

        // --- settlement ------------------------------------------------
        // Every transfer reverts the whole fill on failure (mock reverts on
        // short balance/allowance), so partial state never survives a failed
        // token move.
        MockERC20(order.makerToken).transferFrom(order.maker, msg.sender, makerProceeds);
        if (fee > 0) {
            MockERC20(order.makerToken).transferFrom(order.maker, order.feeRecipient, fee);
        }
        MockERC20(order.takerToken).transferFrom(msg.sender, order.maker, takerDue);

        emit OrderFilled(hash, order.maker, msg.sender, makerProceeds, takerDue, fee, newSpent);
    }

    // ------------------------------------------------------------------ //
    // Views                                                              //
    // ------------------------------------------------------------------ //

    function hashOrder(Order calldata order) public view returns (bytes32) {
        bytes32 structHash = keccak256(
            abi.encode(
                ORDER_TYPEHASH,
                order.maker,
                order.taker,
                order.makerToken,
                order.takerToken,
                order.makerAmount,
                order.takerAmount,
                order.nonce,
                order.deadline,
                order.feeRecipient,
                order.maxFeeAmount
            )
        );
        return keccak256(abi.encodePacked("\x19\x01", domainSeparator, structHash));
    }

    // ------------------------------------------------------------------ //
    // Internal                                                           //
    // ------------------------------------------------------------------ //

    function _ceilMulDiv(uint256 a, uint256 b, uint256 denominator)
        internal
        pure
        returns (uint256)
    {
        return (a * b + denominator - 1) / denominator;
    }

    function _isValidSignature(address signer, bytes32 hash, bytes calldata signature)
        internal
        pure
        returns (bool)
    {
        if (signature.length != 65 || signer == address(0)) return false;
        bytes32 r;
        bytes32 s;
        uint8 v;
        assembly {
            r := calldataload(signature.offset)
            s := calldataload(add(signature.offset, 32))
            v := byte(0, calldataload(add(signature.offset, 64)))
        }
        // Low-s / EIP-2 malleability guard.
        if (uint256(s) > 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0) {
            return false;
        }
        if (v < 27) v += 27;
        if (v != 27 && v != 28) return false;
        return ecrecover(hash, v, r, s) == signer;
    }
}
