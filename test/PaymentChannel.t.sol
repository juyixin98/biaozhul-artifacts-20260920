// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Test.sol";
import "../src/PaymentChannel.sol";
import "../src/TestToken.sol";

contract PaymentChannelTest is Test {
    uint256 constant PAYER_KEY = 0xA11CE;
    uint256 constant PAYEE_KEY = 0xB0B;
    uint256 constant STRANGER_KEY = 0xDEAD;
    uint256 constant CHALLENGE_PERIOD = 1 days;
    uint256 constant COLLATERAL = 100 ether;

    TestToken token;
    PaymentChannel channel;
    address payer = vm.addr(PAYER_KEY);
    address payee = vm.addr(PAYEE_KEY);
    bytes32 channelId;

    function setUp() public {
        token = new TestToken();
        channel = new PaymentChannel(address(token), CHALLENGE_PERIOD);
        token.mint(payer, 1000 ether);
        vm.prank(payer);
        token.approve(address(channel), type(uint256).max);
        vm.prank(payer);
        channelId = channel.openChannel(payee, COLLATERAL, bytes32("salt-1"));
    }

    // ---------------------------------------------------------------- helpers

    function _state(bytes32 id, uint256 amount, uint64 nonce) internal pure returns (PaymentChannel.State memory) {
        return PaymentChannel.State({channelId: id, amount: amount, nonce: nonce});
    }

    function _sign(PaymentChannel.State memory s, uint256 k1, uint256 k2)
        internal
        view
        returns (bytes memory sig1, bytes memory sig2)
    {
        bytes32 digest = channel.hashState(s);
        (uint8 v, bytes32 r, bytes32 sv) = vm.sign(k1, digest);
        sig1 = abi.encodePacked(r, sv, v);
        (v, r, sv) = vm.sign(k2, digest);
        sig2 = abi.encodePacked(r, sv, v);
    }

    function _signBoth(PaymentChannel.State memory s) internal view returns (bytes memory, bytes memory) {
        return _sign(s, PAYER_KEY, PAYEE_KEY);
    }

    /// 用双方签名的状态关闭通道（由 payee 提交），返回截止时间。
    function _closeWith(uint256 amount, uint64 nonce) internal returns (uint64 challengeEnd) {
        PaymentChannel.State memory s = _state(channelId, amount, nonce);
        (bytes memory sp, bytes memory sq) = _signBoth(s);
        vm.prank(payee);
        channel.closeChannel(s, sp, sq);
        return uint64(block.timestamp) + uint64(CHALLENGE_PERIOD);
    }

    // ---------------------------------------------------------------- open

    function test_open_locksCollateral() public view {
        PaymentChannel.Channel memory ch = channel.getChannel(channelId);
        assertEq(ch.payer, payer);
        assertEq(ch.payee, payee);
        assertEq(ch.collateral, COLLATERAL);
        assertEq(uint256(ch.status), uint256(PaymentChannel.Status.Open));
        assertEq(token.balanceOf(address(channel)), COLLATERAL);
        assertEq(token.balanceOf(payer), 1000 ether - COLLATERAL);
    }

    function test_open_revertsOnDuplicateSalt() public {
        vm.prank(payer);
        vm.expectRevert(PaymentChannel.ChannelExists.selector);
        channel.openChannel(payee, COLLATERAL, bytes32("salt-1"));
    }

    function test_open_revertsOnZeroCollateral() public {
        vm.prank(payer);
        vm.expectRevert(PaymentChannel.ZeroCollateral.selector);
        channel.openChannel(payee, 0, bytes32("salt-9"));
    }

    // ---------------------------------------------------------------- close

    function test_close_entersChallengePeriod() public {
        uint64 end = _closeWith(30 ether, 1);
        PaymentChannel.Channel memory ch = channel.getChannel(channelId);
        assertEq(uint256(ch.status), uint256(PaymentChannel.Status.Closing));
        assertEq(ch.nonce, 1);
        assertEq(ch.amount, 30 ether);
        assertEq(ch.challengeEnd, end);
        assertEq(ch.challengeEnd, uint64(block.timestamp) + uint64(CHALLENGE_PERIOD));
    }

    function test_close_revertsForNonParty() public {
        PaymentChannel.State memory s = _state(channelId, 30 ether, 1);
        (bytes memory sp, bytes memory sq) = _signBoth(s);
        address stranger = vm.addr(STRANGER_KEY);
        vm.prank(stranger);
        vm.expectRevert(PaymentChannel.NotParty.selector);
        channel.closeChannel(s, sp, sq);
    }

    function test_close_revertsOnBadSignature() public {
        PaymentChannel.State memory s = _state(channelId, 30 ether, 1);
        // 陌生人代替 payer 签名
        (bytes memory bad, bytes memory sq) = _sign(s, STRANGER_KEY, PAYEE_KEY);
        vm.prank(payee);
        vm.expectRevert(PaymentChannel.BadSignature.selector);
        channel.closeChannel(s, bad, sq);
    }

    function test_close_revertsOnSwappedSignatures() public {
        PaymentChannel.State memory s = _state(channelId, 30 ether, 1);
        (bytes memory sp, bytes memory sq) = _signBoth(s);
        vm.prank(payee);
        vm.expectRevert(PaymentChannel.BadSignature.selector);
        channel.closeChannel(s, sq, sp); // 两人签名对调
    }

    function test_close_revertsOnNonceZero() public {
        PaymentChannel.State memory s = _state(channelId, 0, 0);
        (bytes memory sp, bytes memory sq) = _signBoth(s);
        vm.prank(payee);
        vm.expectRevert(PaymentChannel.StaleNonce.selector);
        channel.closeChannel(s, sp, sq);
    }

    function test_close_revertsOnCrossChannelReplay() public {
        // 开通第二条通道（相同双方）
        vm.prank(payer);
        bytes32 channelId2 = channel.openChannel(payee, COLLATERAL, bytes32("salt-2"));

        // 针对通道 1 的有效签名，被套用到通道 2 的状态上
        PaymentChannel.State memory s1 = _state(channelId, 30 ether, 1);
        (bytes memory sp, bytes memory sq) = _signBoth(s1);
        PaymentChannel.State memory s2 = _state(channelId2, 30 ether, 1);

        vm.prank(payee);
        vm.expectRevert(PaymentChannel.BadSignature.selector);
        channel.closeChannel(s2, sp, sq);
    }

    function test_close_revertsOnCrossChainReplay() public {
        PaymentChannel.State memory s = _state(channelId, 30 ether, 1);
        (bytes memory sp, bytes memory sq) = _signBoth(s); // 在当前链 ID 下签名
        vm.chainId(block.chainid + 1); // 模拟签名被拿到另一条链重放
        vm.prank(payee);
        vm.expectRevert(PaymentChannel.BadSignature.selector);
        channel.closeChannel(s, sp, sq);
    }

    // ---------------------------------------------------------------- challenge

    function test_challenge_acceptsHigherNonce() public {
        _closeWith(30 ether, 1);
        PaymentChannel.State memory newer = _state(channelId, 70 ether, 2);
        (bytes memory sp, bytes memory sq) = _signBoth(newer);
        channel.challenge(newer, sp, sq);
        PaymentChannel.Channel memory ch = channel.getChannel(channelId);
        assertEq(ch.nonce, 2);
        assertEq(ch.amount, 70 ether);
    }

    function test_challenge_revertsOnStaleNonce() public {
        _closeWith(30 ether, 5);
        // 序号相同
        PaymentChannel.State memory same = _state(channelId, 50 ether, 5);
        (bytes memory sp, bytes memory sq) = _signBoth(same);
        vm.expectRevert(PaymentChannel.StaleNonce.selector);
        channel.challenge(same, sp, sq);
        // 序号更低（旧状态不得覆盖最新状态）
        PaymentChannel.State memory older = _state(channelId, 10 ether, 3);
        (sp, sq) = _signBoth(older);
        vm.expectRevert(PaymentChannel.StaleNonce.selector);
        channel.challenge(older, sp, sq);
    }

    function test_challenge_revertsOnAmountRegression() public {
        _closeWith(50 ether, 1);
        PaymentChannel.State memory regression = _state(channelId, 40 ether, 2); // 序号更高但累计额回退
        (bytes memory sp, bytes memory sq) = _signBoth(regression);
        vm.expectRevert(PaymentChannel.AmountRegression.selector);
        channel.challenge(regression, sp, sq);
    }

    function test_challenge_revertsAfterDeadline() public {
        uint64 end = _closeWith(30 ether, 1);
        PaymentChannel.State memory newer = _state(channelId, 70 ether, 2);
        (bytes memory sp, bytes memory sq) = _signBoth(newer);
        vm.warp(end); // 到达截止时刻即不可再挑战
        vm.expectRevert(PaymentChannel.ChallengeOver.selector);
        channel.challenge(newer, sp, sq);
    }

    function test_challenge_worksOneSecondBeforeDeadline() public {
        uint64 end = _closeWith(30 ether, 1);
        PaymentChannel.State memory newer = _state(channelId, 70 ether, 2);
        (bytes memory sp, bytes memory sq) = _signBoth(newer);
        vm.warp(end - 1);
        channel.challenge(newer, sp, sq);
        assertEq(channel.getChannel(channelId).nonce, 2);
    }

    // ---------------------------------------------------------------- settle

    function test_settle_revertsBeforeDeadline() public {
        uint64 end = _closeWith(30 ether, 1);
        vm.warp(end - 1); // 截止前 1 秒
        vm.expectRevert(PaymentChannel.ChallengeActive.selector);
        channel.settle(channelId);
    }

    function test_settle_succeedsExactlyAtDeadline() public {
        uint64 end = _closeWith(30 ether, 1);
        vm.warp(end); // 恰好在截止时刻
        channel.settle(channelId);
        assertEq(token.balanceOf(payee), 30 ether);
        assertEq(token.balanceOf(payer), 1000 ether - 30 ether);
        assertEq(token.balanceOf(address(channel)), 0);
    }

    function test_settle_capsPayoutAtCollateral() public {
        // 累计支付额超过抵押额：收款方最多拿走全部抵押，付款方无退款
        uint64 end = _closeWith(150 ether, 1);
        vm.warp(end);
        channel.settle(channelId);
        assertEq(token.balanceOf(payee), COLLATERAL);
        assertEq(token.balanceOf(payer), 1000 ether - COLLATERAL);
    }

    function test_settle_revertsOnSecondSettle() public {
        uint64 end = _closeWith(30 ether, 1);
        vm.warp(end);
        channel.settle(channelId);
        vm.expectRevert(PaymentChannel.NotClosing.selector);
        channel.settle(channelId);
    }

    function test_settle_revertsWhenChannelOpen() public {
        vm.expectRevert(PaymentChannel.NotClosing.selector);
        channel.settle(channelId);
    }

    // ---------------------------------------------------------------- full flow

    /// 完整流程：开通 → 用旧状态(nonce=1, 30)关闭 → 对手方用新状态(nonce=2, 70)挑战 → 到期结算。
    function test_fullFlow_oldStateCloseChallengedThenSettled() public {
        uint64 end = _closeWith(30 ether, 1); // 旧状态关闭

        PaymentChannel.State memory newer = _state(channelId, 70 ether, 2);
        (bytes memory sp, bytes memory sq) = _signBoth(newer);
        vm.warp(end - 10);
        channel.challenge(newer, sp, sq); // 挑战成功，覆盖旧状态

        vm.warp(end);
        channel.settle(channelId);

        assertEq(token.balanceOf(payee), 70 ether); // 按最新状态结算，而非旧的 30
        assertEq(token.balanceOf(payer), 1000 ether - 70 ether);
        assertEq(uint256(channel.getChannel(channelId).status), uint256(PaymentChannel.Status.Settled));
    }
}
