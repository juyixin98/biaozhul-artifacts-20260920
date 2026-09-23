// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "./IERC20.sol";
import {Ecdsa} from "./Ecdsa.sol";

/// @title PaymentAdjudicator
/// @notice Unidirectional TEST-token payment channel with off-chain, signed
///         balance states and an on-chain challenge/settlement adjudicator.
///
///         Channel life cycle:
///
///         (absent) --openChannel()-------------------------> Open
///         Open    --startChallenge()----------------------> Challenged (payout 0)
///         Open    --close(initial state)------------------> Challenged (payout N)
///         Challenged --challenge(newer state)-------------> Challenged (payout M)
///         Challenged --settle(), after challengeEndsAt ---> Settled (terminal)
///
///         The holder (payer) pre-funds the channel with at most `collateral`
///         tokens; the merchant (payee) receives cumulative payments. Each
///         off-chain state is an EIP-712 message binding the channelId, the
///         chainId, the cumulative paid amount and a strictly increasing
///         sequence number, signed by BOTH parties.
///
/// @dev This is an educational/test implementation. It is deliberately NOT
///      claimed to be compatible with the Lightning Network or any other
///      existing payment-channel protocol.
contract PaymentAdjudicator {
    using Ecdsa for bytes32;

    enum Status {
        None,
        Open,
        Challenged,
        Settled
    }

    /// @notice A mutually signed off-chain payment state.
    struct State {
        bytes32 channelId;
        uint256 chainId;
        uint64 sequence;
        uint256 cumulativePaid;
    }

    struct Channel {
        address payer;
        address payee;
        IERC20 token;
        uint64 challengeDuration;
        Status status;
        uint64 bestSequence;
        uint256 bestCumulativePaid;
        uint256 collateral;
        uint64 challengeEndsAt;
    }

    // keccak256("PaymentState(bytes32 channelId,uint256 chainId,uint64 sequence,uint256 cumulativePaid)")
    bytes32 public constant STATE_TYPEHASH =
        0x99f8cfebe7dfb6e9bdf10e3a7f6574500b8cdf35e31f019a548b0326def4419d;

    /// @dev Minimum challenge window: keeps misconfiguration from producing
    ///      trivially racy channels.
    uint64 public constant MIN_CHALLENGE_DURATION = 1 minutes;

    bytes32 public constant DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    bytes32 public constant DOMAIN_NAME_HASH = keccak256("PaymentAdjudicator");
    bytes32 public constant DOMAIN_VERSION_HASH = keccak256("1");

    mapping(bytes32 => Channel) private _channels;

    event ChannelOpened(
        bytes32 indexed channelId,
        address indexed payer,
        address indexed payee,
        address token,
        uint256 collateral,
        uint64 challengeDuration
    );
    event ChallengeStarted(bytes32 indexed channelId, address indexed by, uint64 challengeEndsAt);
    event ChannelClosed(
        bytes32 indexed channelId, uint64 sequence, uint256 cumulativePaid, uint64 challengeEndsAt
    );
    event ChannelChallenged(
        bytes32 indexed channelId, uint64 sequence, uint256 cumulativePaid, uint64 challengeEndsAt
    );
    event ChannelSettled(
        bytes32 indexed channelId, uint256 payeePayout, uint256 payerRefund
    );

    error ChannelAlreadyExists(bytes32 channelId);
    error UnknownChannel(bytes32 channelId);
    error ZeroAddress();
    error SameParties(address party);
    error ZeroCollateral();
    error ChallengeDurationTooShort(uint64 provided, uint64 minimum);
    error NotChannelParty(address caller);
    error NotOpen(bytes32 channelId, Status status);
    error NotChallenged(bytes32 channelId, Status status);
    error WrongChainId(uint256 stateChainId, uint256 expectedChainId);
    error StaleSequence(uint64 provided, uint64 current);
    error AmountReverted(uint256 provided, uint256 current);
    error WrongChannel(bytes32 stateChannelId, bytes32 expectedChannelId);
    error BadPayerSignature();
    error BadPayeeSignature();
    error ChallengePeriodActive(uint64 endsAt, uint256 nowTs);
    error AlreadySettled(bytes32 channelId);

    // ---------------------------------------------------------------------------
    // Channel lifecycle
    // ---------------------------------------------------------------------------

    /// @notice Open a channel and pull `collateral` test tokens from the payer.
    /// @dev The payer must have approved this contract for at least `collateral`.
    function openChannel(
        address payer,
        address payee,
        IERC20 token,
        uint256 collateral,
        uint64 challengeDuration,
        uint64 nonce
    ) external returns (bytes32 channelId) {
        if (payer == address(0) || payee == address(0) || address(token) == address(0)) {
            revert ZeroAddress();
        }
        if (payer == payee) revert SameParties(payer);
        if (collateral == 0) revert ZeroCollateral();
        if (challengeDuration < MIN_CHALLENGE_DURATION) {
            revert ChallengeDurationTooShort(challengeDuration, MIN_CHALLENGE_DURATION);
        }

        channelId = _channelId(payer, payee, token, collateral, challengeDuration, nonce);
        if (_channels[channelId].status != Status.None) {
            revert ChannelAlreadyExists(channelId);
        }

        // Pull funds BEFORE storing state (checks-effects-interactions).
        if (!token.transferFrom(payer, address(this), collateral)) {
            revert ZeroCollateral();
        }

        _channels[channelId] = Channel({
            payer: payer,
            payee: payee,
            token: token,
            challengeDuration: challengeDuration,
            status: Status.Open,
            bestSequence: 0,
            bestCumulativePaid: 0,
            collateral: collateral,
            challengeEndsAt: 0
        });

        emit ChannelOpened(channelId, payer, payee, address(token), collateral, challengeDuration);
    }

    /// @notice Move an Open channel into the challenge period with a claimed
    ///         payout of zero. Only the payer or the payee may call this.
    /// @dev Use this when the counterparty is offline/uncooperative. The other
    ///      party can overturn the zero payout by submitting a signed state via
    ///      {challenge} before the challenge period ends.
    function startChallenge(bytes32 channelId) external {
        Channel storage ch = _requireChannel(channelId);
        if (ch.status != Status.Open) revert NotOpen(channelId, ch.status);
        if (msg.sender != ch.payer && msg.sender != ch.payee) {
            revert NotChannelParty(msg.sender);
        }

        uint64 endsAt = uint64(block.timestamp) + ch.challengeDuration;
        ch.status = Status.Challenged;
        ch.challengeEndsAt = endsAt;
        ch.bestSequence = 0;
        ch.bestCumulativePaid = 0;

        emit ChallengeStarted(channelId, msg.sender, endsAt);
    }

    /// @notice Close an Open channel by submitting the first mutually signed
    ///         state. Only the payer or the payee may call this.
    function close(
        bytes32 channelId,
        State calldata state,
        bytes calldata payerSignature,
        bytes calldata payeeSignature
    ) external {
        Channel storage ch = _requireChannel(channelId);
        if (ch.status != Status.Open) revert NotOpen(channelId, ch.status);
        if (msg.sender != ch.payer && msg.sender != ch.payee) {
            revert NotChannelParty(msg.sender);
        }
        _verifyState(channelId, ch, state, payerSignature, payeeSignature);

        uint64 endsAt = uint64(block.timestamp) + ch.challengeDuration;
        ch.status = Status.Challenged;
        ch.bestSequence = state.sequence;
        ch.bestCumulativePaid = state.cumulativePaid;
        ch.challengeEndsAt = endsAt;

        emit ChannelClosed(channelId, state.sequence, state.cumulativePaid, endsAt);
    }

    /// @notice Submit a strictly newer, non-reverting state while a channel is
    ///         already in the challenge period. Refreshes the deadline so the
    ///         counterparty gets time to react to every new highest state.
    function challenge(
        State calldata state,
        bytes calldata payerSignature,
        bytes calldata payeeSignature
    ) external {
        Channel storage ch = _requireChannel(state.channelId);
        if (ch.status != Status.Challenged) revert NotChallenged(state.channelId, ch.status);
        if (msg.sender != ch.payer && msg.sender != ch.payee) {
            revert NotChannelParty(msg.sender);
        }
        _verifyState(state.channelId, ch, state, payerSignature, payeeSignature);

        uint64 endsAt = uint64(block.timestamp) + ch.challengeDuration;
        ch.bestSequence = state.sequence;
        ch.bestCumulativePaid = state.cumulativePaid;
        ch.challengeEndsAt = endsAt;

        emit ChannelChallenged(state.channelId, state.sequence, state.cumulativePaid, endsAt);
    }

    /// @notice Settle after the challenge period. The payee receives at most
    ///         the collateral; everything left returns to the payer. Settling
    ///         twice is impossible: Settled is a terminal state.
    function settle(bytes32 channelId) external {
        Channel storage ch = _requireChannel(channelId);
        if (ch.status == Status.Settled) revert AlreadySettled(channelId);
        if (ch.status != Status.Challenged) revert NotChallenged(channelId, ch.status);
        if (block.timestamp < ch.challengeEndsAt) {
            revert ChallengePeriodActive(ch.challengeEndsAt, block.timestamp);
        }

        // Cap the payout at collateral even if the final state promised more.
        uint256 payeePayout = ch.bestCumulativePaid;
        if (payeePayout > ch.collateral) {
            payeePayout = ch.collateral;
        }
        uint256 payerRefund = ch.collateral - payeePayout;

        ch.status = Status.Settled;
        ch.bestSequence = 0;
        ch.bestCumulativePaid = payeePayout;

        emit ChannelSettled(channelId, payeePayout, payerRefund);

        IERC20 token = ch.token;
        // Checks-effects-interactions: status is already Settled, so even a
        // re-entrant token hook cannot settle again or move funds twice.
        if (payeePayout != 0) {
            // The balances are guaranteed by collateral held in this contract.
            require(token.transfer(ch.payee, payeePayout), "payment failed");
        }
        if (payerRefund != 0) {
            require(token.transfer(ch.payer, payerRefund), "refund failed");
        }
    }

    // ---------------------------------------------------------------------------
    // Views
    // ---------------------------------------------------------------------------

    function getChannel(bytes32 channelId) external view returns (Channel memory) {
        return _channels[channelId];
    }

    /// @notice Compute the channel id deterministically from its parameters.
    function computeChannelId(
        address payer,
        address payee,
        address token,
        uint256 collateral,
        uint64 challengeDuration,
        uint64 nonce
    ) external view returns (bytes32) {
        return _channelId(payer, payee, IERC20(token), collateral, challengeDuration, nonce);
    }

    /// @notice EIP-712 domain separator; the chainId binding prevents a signed
    ///         state from being replayed on a different chain.
    function domainSeparator() public view returns (bytes32) {
        return keccak256(
            abi.encode(
                DOMAIN_TYPEHASH,
                DOMAIN_NAME_HASH,
                DOMAIN_VERSION_HASH,
                block.chainid,
                address(this)
            )
        );
    }

    /// @notice Canonical hash of a state struct (EIP-712 struct hash).
    function hashState(State calldata state) public pure returns (bytes32) {
        return keccak256(
            abi.encode(
                STATE_TYPEHASH,
                state.channelId,
                state.chainId,
                state.sequence,
                state.cumulativePaid
            )
        );
    }

    /// @notice Full EIP-712 digest that both parties sign.
    function stateDigest(State calldata state) public view returns (bytes32) {
        return
            keccak256(abi.encodePacked("\x19\x01", domainSeparator(), hashState(state)));
    }

    // ---------------------------------------------------------------------------
    // Internal
    // ---------------------------------------------------------------------------

    function _channelId(
        address payer,
        address payee,
        IERC20 token,
        uint256 collateral,
        uint64 challengeDuration,
        uint64 nonce
    ) internal view returns (bytes32) {
        return keccak256(
            abi.encode(
                block.chainid,
                address(this),
                payer,
                payee,
                address(token),
                collateral,
                challengeDuration,
                nonce
            )
        );
    }

    function _requireChannel(bytes32 channelId) internal view returns (Channel storage ch) {
        ch = _channels[channelId];
        if (ch.status == Status.None) revert UnknownChannel(channelId);
    }

    /// @dev Verify every replay-protection and monotonicity rule, then check
    ///      BOTH signatures against the fixed payer/payee roles.
    function _verifyState(
        bytes32 channelId,
        Channel storage ch,
        State calldata state,
        bytes calldata payerSignature,
        bytes calldata payeeSignature
    ) internal view {
        if (state.channelId != channelId) revert WrongChannel(state.channelId, channelId);
        if (state.chainId != block.chainid) revert WrongChainId(state.chainId, block.chainid);
        if (state.sequence <= ch.bestSequence) {
            revert StaleSequence(state.sequence, ch.bestSequence);
        }
        if (state.cumulativePaid < ch.bestCumulativePaid) {
            revert AmountReverted(state.cumulativePaid, ch.bestCumulativePaid);
        }

        bytes32 digest = stateDigest(state);
        if (digest.recover(payerSignature) != ch.payer) revert BadPayerSignature();
        if (digest.recover(payeeSignature) != ch.payee) revert BadPayeeSignature();
    }
}
