// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @notice Minimal ERC-20 surface used by the escrow. transferFrom/transfer/balanceOf.
interface IERC20 {
    function transferFrom(address from, address to, uint256 value) external returns (bool);
    function transfer(address to, uint256 value) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

/// @title TokenVesting
/// @notice Cliff + linear token vesting escrow with revocation support.
/// @dev All amounts/timestamps are uint256. Solidity 0.8 built-in checked
///      arithmetic guarantees that the vesting math (amount * elapsed / duration)
///      cannot silently overflow; any out-of-range product reverts.
///
///      Vesting curve:
///        - now < cliff                          -> vested = 0
///        - cliff <= now < end                   -> total * (now - start) / (end - start)
///        - now >= end (or revoked and past it)  -> the final vested target
///      On revocation the curve freezes at `revokedAt`: the unvested remainder
///      is refunded to the contract owner, the already-vested portion is kept
///      for the beneficiary and remains claimable via {release}.
contract TokenVesting {
    struct Schedule {
        address beneficiary; // receives vested tokens
        address token;       // escrowed ERC-20
        uint256 totalAmount; // amount locked at creation
        uint256 start;       // vesting curve reference start (unix seconds)
        uint256 cliff;       // nothing is releasable before this timestamp
        uint256 end;         // vesting completes at this timestamp
        uint256 released;    // amount already paid to the beneficiary
        uint256 revokedAt;   // nonzero once revoked (freeze timestamp)
    }

    /// @dev monotonically increasing id; scheduleId == id (1-based).
    uint256 public nextScheduleId;

    address public owner;

    mapping(uint256 => Schedule) private _schedules;

    event ScheduleCreated(
        uint256 indexed scheduleId,
        address indexed beneficiary,
        address indexed token,
        uint256 totalAmount,
        uint256 start,
        uint256 cliff,
        uint256 end
    );
    event TokensReleased(uint256 indexed scheduleId, address indexed beneficiary, uint256 amount);
    event ScheduleRevoked(uint256 indexed scheduleId, uint256 vestedAmount, uint256 refundAmount, uint256 revokedAt);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    error Unauthorized();
    error InvalidBeneficiary();
    error InvalidTimeline();
    error ZeroAmount();
    error ScheduleNotFound();
    error AlreadyRevoked();
    error NothingToRelease();
    error TokenTransferFailed();

    modifier onlyOwner() {
        if (msg.sender != owner) revert Unauthorized();
        _;
    }

    constructor() {
        owner = msg.sender;
        emit OwnershipTransferred(address(0), msg.sender);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert InvalidBeneficiary();
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }

    /// @notice Lock `totalAmount` of `token` for `beneficiary` under a new schedule.
    /// @dev The caller must have approved this contract for at least `totalAmount`.
    function createSchedule(
        address beneficiary,
        address token,
        uint256 totalAmount,
        uint256 start,
        uint256 cliff,
        uint256 end
    ) external returns (uint256 scheduleId) {
        if (beneficiary == address(0)) revert InvalidBeneficiary();
        if (token == address(0)) revert InvalidBeneficiary();
        if (totalAmount == 0) revert ZeroAmount();
        if (!(start <= cliff && cliff <= end && start < end)) revert InvalidTimeline();

        scheduleId = ++nextScheduleId;
        _schedules[scheduleId] = Schedule({
            beneficiary: beneficiary,
            token: token,
            totalAmount: totalAmount,
            start: start,
            cliff: cliff,
            end: end,
            released: 0,
            revokedAt: 0
        });

        if (!IERC20(token).transferFrom(msg.sender, address(this), totalAmount)) {
            revert TokenTransferFailed();
        }

        emit ScheduleCreated(scheduleId, beneficiary, token, totalAmount, start, cliff, end);
    }

    /// @notice Full schedule record. Reverts for unknown ids.
    function scheduleOf(uint256 scheduleId) external view returns (Schedule memory) {
        if (scheduleId == 0 || scheduleId > nextScheduleId) revert ScheduleNotFound();
        return _schedules[scheduleId];
    }

    /// @dev Amount that has vested at timestamp `now` (passed in for testability).
    ///      Integer division rounds down; dust smaller than one step stays in the
    ///      escrow until `end`, where the full remainder vests at once.
    function vestedAmount(uint256 scheduleId, uint256 nowTs) public view returns (uint256) {
        Schedule storage s = _schedules[scheduleId];
        if (s.totalAmount == 0) revert ScheduleNotFound();

        // After revocation the curve is frozen at revokedAt; never grows beyond it.
        uint256 effectiveNow = nowTs;
        if (s.revokedAt != 0 && effectiveNow > s.revokedAt) {
            effectiveNow = s.revokedAt;
        }

        if (effectiveNow < s.cliff) {
            return 0;
        }
        if (effectiveNow >= s.end) {
            return s.totalAmount;
        }

        uint256 elapsed = effectiveNow - s.start;
        uint256 duration = s.end - s.start;
        return (s.totalAmount * elapsed) / duration; // checked multiply -> reverts on overflow
    }

    /// @notice Vested amount not yet withdrawn.
    function releasable(uint256 scheduleId) external view returns (uint256) {
        Schedule storage s = _schedules[scheduleId];
        if (s.totalAmount == 0) revert ScheduleNotFound();
        return vestedAmount(scheduleId, block.timestamp) - s.released;
    }

    /// @notice Pay out all currently vested-and-unreleased tokens to the beneficiary.
    /// @dev Permissionless: anyone may trigger the transfer, but funds only ever go
    ///      to the recorded beneficiary.
    function release(uint256 scheduleId) external returns (uint256 amount) {
        Schedule storage s = _schedules[scheduleId];
        if (s.totalAmount == 0) revert ScheduleNotFound();

        uint256 vested = vestedAmount(scheduleId, block.timestamp);
        amount = vested - s.released;
        if (amount == 0) revert NothingToRelease();

        s.released += amount;
        if (!IERC20(s.token).transfer(s.beneficiary, amount)) revert TokenTransferFailed();

        emit TokensReleased(scheduleId, s.beneficiary, amount);
    }

    /// @notice Revoke a schedule. Vested-so-far is retained for the beneficiary;
    ///         the unvested remainder is refunded immediately to the owner.
    /// @dev The beneficiary may still {release} the vested portion afterwards.
    ///      Refund = totalAmount - vestedAt(revoke). Conserving identity after
    ///      revoke + full release:
    ///         beneficiary (released before + vestedAt(revoke)) + owner refund
    ///         == totalAmount.
    function revoke(uint256 scheduleId) external onlyOwner returns (uint256 refund) {
        Schedule storage s = _schedules[scheduleId];
        if (s.totalAmount == 0) revert ScheduleNotFound();
        if (s.revokedAt != 0) revert AlreadyRevoked();

        s.revokedAt = block.timestamp;

        uint256 vested = vestedAmount(scheduleId, block.timestamp);
        // Unvested remainder; vestedAmount is capped at totalAmount, no underflow.
        refund = s.totalAmount - vested;

        if (refund != 0) {
            if (!IERC20(s.token).transfer(owner, refund)) revert TokenTransferFailed();
        }

        emit ScheduleRevoked(scheduleId, vested, refund, block.timestamp);
    }
}
