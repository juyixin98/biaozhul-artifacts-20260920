// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

interface IERC20Min {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

/// @title LinearTokenVesting
/// @notice Cliff + linear (pro-rata) token vesting escrow with owner revocation.
/// @dev All time/amount math stays in uint256 under Solidity >=0.8 checked
///      arithmetic: any overflow reverts instead of wrapping. The vested
///      fraction is computed as `total * elapsed / duration`
///      (multiplication before division) so non-divisible totals are exact up
///      to integer rounding (dust at most 1 base unit, released at the end).
///
///      Invariant for every schedule, at and after its lifecycle:
///          releasedToBeneficiary + refundedToOwner == totalLocked
///      (possibly with the not-yet-released vested remainder still held by
///      this contract in between).
contract LinearTokenVesting {
    struct Schedule {
        address beneficiary;
        uint256 totalAmount;      // tokens locked into the schedule
        uint256 releasedAmount;   // tokens already paid to the beneficiary
        uint256 start;            // vesting start timestamp
        uint256 cliff;            // timestamp before which nothing vests
        uint256 end;              // timestamp at which everything is vested
        bool revocable;
        bool revoked;
        uint256 revokedAt;        // freeze point once revoked (0 = live)
        uint256 refundedAmount;   // unvested tokens returned to the owner
    }

    IERC20Min public immutable token;
    address public immutable owner;

    uint256 public nextScheduleId;
    mapping(uint256 => Schedule) private _schedules;

    event ScheduleCreated(
        uint256 indexed id,
        address indexed beneficiary,
        uint256 amount,
        uint256 start,
        uint256 cliff,
        uint256 end,
        bool revocable
    );
    event Released(uint256 indexed id, address indexed beneficiary, uint256 amount);
    event Revoked(uint256 indexed id, uint256 vestedAtRevocation, uint256 refundedAmount);

    error NotOwner();
    error NotBeneficiaryOrOwner();
    error ZeroAddress();
    error ZeroAmount();
    error InvalidTiming();
    error ScheduleNotFound();
    error NotRevocable();
    error AlreadyRevoked();
    error NothingToRelease();
    error TransferFailed();

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    constructor(address _token) {
        if (_token == address(0)) revert ZeroAddress();
        token = IERC20Min(_token);
        owner = msg.sender;
    }

    // ---------------------------------------------------------------------
    // Creation
    // ---------------------------------------------------------------------

    /// @notice Pull `amount` of tokens from the owner into escrow and open a
    ///         vesting schedule. The owner must have approved this contract.
    function createSchedule(
        address beneficiary,
        uint256 amount,
        uint256 startTimestamp,
        uint256 cliffDuration,
        uint256 vestingDuration,
        bool revocable
    ) external onlyOwner returns (uint256 id) {
        if (beneficiary == address(0)) revert ZeroAddress();
        if (amount == 0) revert ZeroAmount();
        if (vestingDuration == 0) revert InvalidTiming();
        if (startTimestamp + cliffDuration < startTimestamp) revert InvalidTiming();
        if (startTimestamp + vestingDuration < startTimestamp) revert InvalidTiming();
        if (cliffDuration > vestingDuration) revert InvalidTiming();

        id = nextScheduleId++;
        Schedule storage s = _schedules[id];
        s.beneficiary = beneficiary;
        s.totalAmount = amount;
        s.start = startTimestamp;
        s.cliff = startTimestamp + cliffDuration;
        s.end = startTimestamp + vestingDuration;
        s.revocable = revocable;

        if (!token.transferFrom(msg.sender, address(this), amount)) revert TransferFailed();

        emit ScheduleCreated(id, beneficiary, amount, s.start, s.cliff, s.end, revocable);
    }

    // ---------------------------------------------------------------------
    // Release
    // ---------------------------------------------------------------------

    /// @notice Transfer all currently releasable (vested, not yet released)
    ///         tokens to the beneficiary. Callable by beneficiary or owner.
    function release(uint256 id) external returns (uint256 payment) {
        Schedule storage s = _requireSchedule(id);
        if (msg.sender != s.beneficiary && msg.sender != owner) {
            revert NotBeneficiaryOrOwner();
        }

        uint256 vested = _vestedAmount(s, block.timestamp);
        payment = vested - s.releasedAmount; // checked: vested is always >= released
        if (payment == 0) revert NothingToRelease();

        s.releasedAmount += payment;
        if (!token.transfer(s.beneficiary, payment)) revert TransferFailed();
        emit Released(id, s.beneficiary, payment);
    }

    // ---------------------------------------------------------------------
    // Revocation
    // ---------------------------------------------------------------------

    /// @notice Freeze vesting immediately. Everything vested so far (minus
    ///         what was already released) remains claimable by the
    ///         beneficiary; the unvested remainder is refunded to the owner.
    function revoke(uint256 id) external onlyOwner returns (uint256 refunded) {
        Schedule storage s = _requireSchedule(id);
        if (!s.revocable) revert NotRevocable();
        if (s.revoked) revert AlreadyRevoked();

        uint256 vested = _vestedAmount(s, block.timestamp);
        refunded = s.totalAmount - vested; // unvested portion

        s.revoked = true;
        s.revokedAt = block.timestamp;
        s.refundedAmount += refunded;

        if (refunded != 0) {
            if (!token.transfer(owner, refunded)) revert TransferFailed();
        }
        emit Revoked(id, vested, refunded);
    }

    // ---------------------------------------------------------------------
    // Views
    // ---------------------------------------------------------------------

    function getSchedule(uint256 id) external view returns (Schedule memory) {
        return _requireSchedule(id);
    }

    /// @notice Vested amount at an arbitrary timestamp `at`.
    function vestedAmount(uint256 id, uint256 at) external view returns (uint256) {
        Schedule storage s = _requireSchedule(id);
        return _vestedAmount(s, at);
    }

    /// @notice Vested but not yet withdrawn, evaluated now.
    function releasableAmount(uint256 id) external view returns (uint256) {
        Schedule storage s = _requireSchedule(id);
        uint256 vested = _vestedAmount(s, block.timestamp);
        return vested - s.releasedAmount;
    }

    // ---------------------------------------------------------------------
    // Internal
    // ---------------------------------------------------------------------

    function _requireSchedule(uint256 id) internal view returns (Schedule storage s) {
        s = _schedules[id];
        if (s.beneficiary == address(0)) revert ScheduleNotFound();
    }

    /// @dev Pure vesting curve:
    ///   t < cliff          -> 0
    ///   cliff <= t < end   -> total * (t - start) / (end - start)
    ///   t >= end           -> total
    /// After revocation the evaluation time is clamped to revokedAt.
    function _vestedAmount(Schedule storage s, uint256 at) internal view returns (uint256) {
        if (s.revoked && at > s.revokedAt) {
            at = s.revokedAt;
        }
        if (at < s.cliff) {
            return 0;
        }
        if (at >= s.end) {
            return s.totalAmount;
        }
        // Duration and total are constant; checked multiply reverts on overflow.
        return (s.totalAmount * (at - s.start)) / (s.end - s.start);
    }
}
