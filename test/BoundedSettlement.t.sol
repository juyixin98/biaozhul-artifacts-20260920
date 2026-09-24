// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {BoundedSettlement} from "../src/BoundedSettlement.sol";
import {MockERC20} from "../src/MockERC20.sol";

/// @notice 最小 Vm cheatcode 接口（不依赖 forge-std，离线可编译）
interface Vm {
    function sign(uint256 privateKey, bytes32 digest) external returns (uint8 v, bytes32 r, bytes32 s);
    function addr(uint256 privateKey) external returns (address);
    function warp(uint256 newTimestamp) external;
    function prank(address msgSender) external;
    function expectRevert() external;
    function expectRevert(bytes4 revertData) external;
    function expectRevert(bytes calldata revertData) external;
}

contract BoundedSettlementTest {
    Vm internal constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);

    // Anvil 标准测试私钥
    uint256 internal constant DEPLOYER_PK = 0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80;
    uint256 internal constant MAKER_PK = 0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d;
    uint256 internal constant TAKER_PK = 0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a;

    BoundedSettlement internal settlement;
    BoundedSettlement internal settlement2; // 用于域名绑定测试
    MockERC20 internal tokenA; // maker 卖出
    MockERC20 internal tokenB; // taker 支付
    address internal deployer;
    address internal maker;
    address internal taker;
    address internal constant FEE_RECV = address(0xFEE);

    struct Balances {
        uint256 aMaker;
        uint256 aTaker;
        uint256 aFee;
        uint256 bMaker;
        uint256 bTaker;
        uint256 bFee;
    }

    function setUp() public {
        deployer = vm.addr(DEPLOYER_PK);
        maker = vm.addr(MAKER_PK);
        taker = vm.addr(TAKER_PK);

        vm.prank(deployer);
        tokenA = new MockERC20("Token A", "TKA");
        vm.prank(deployer);
        tokenB = new MockERC20("Token B", "TKB");

        vm.prank(deployer);
        settlement = new BoundedSettlement(FEE_RECV);
        vm.prank(deployer);
        settlement2 = new BoundedSettlement(FEE_RECV);

        tokenA.mint(maker, 1_000_000 ether);
        tokenB.mint(taker, 1_000_000 ether);

        vm.prank(maker);
        tokenA.approve(address(settlement), type(uint256).max);
        vm.prank(taker);
        tokenB.approve(address(settlement), type(uint256).max);
        vm.prank(maker);
        tokenA.approve(address(settlement2), type(uint256).max);
    }

    // ---- 辅助函数 ----

    function _order(uint256 sellAmt, uint256 buyAmt, uint256 feeCap, uint256 nonce, uint256 expiry)
        internal
        view
        returns (BoundedSettlement.Order memory o)
    {
        o = BoundedSettlement.Order({
            maker: maker,
            sellToken: address(tokenA),
            buyToken: address(tokenB),
            sellAmount: sellAmt,
            buyAmount: buyAmt,
            feeCap: feeCap,
            nonce: nonce,
            expiry: expiry == 0 ? block.timestamp + 1 hours : expiry
        });
    }

    function _sign(BoundedSettlement.Order memory o, uint256 pk, BoundedSettlement verifier)
        internal
        returns (bytes memory)
    {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, verifier.hashOrder(o));
        return abi.encodePacked(r, s, v);
    }

    function _fill(BoundedSettlement.Order memory o, uint256 sellFill, uint256 fee) internal returns (uint256) {
        bytes memory sig = _sign(o, MAKER_PK, settlement);
        vm.prank(taker);
        return settlement.fillOrder(o, sellFill, fee, sig);
    }

    /// @dev 与 _fill 相同但用外部预生成的签名（避免在 vm.expectRevert 之后调用 vm.sign）
    function _fillWithSig(BoundedSettlement.Order memory o, uint256 sellFill, uint256 fee, bytes memory sig)
        internal
        returns (uint256)
    {
        vm.prank(taker);
        return settlement.fillOrder(o, sellFill, fee, sig);
    }

    function _snap() internal view returns (Balances memory b) {
        b.aMaker = tokenA.balanceOf(maker);
        b.aTaker = tokenA.balanceOf(taker);
        b.aFee = tokenA.balanceOf(FEE_RECV);
        b.bMaker = tokenB.balanceOf(maker);
        b.bTaker = tokenB.balanceOf(taker);
        b.bFee = tokenB.balanceOf(FEE_RECV);
    }

    function _assertConserved(Balances memory x, Balances memory y, string memory tag) internal pure {
        require(x.aMaker + x.aTaker + x.aFee == y.aMaker + y.aTaker + y.aFee, string.concat(tag, ": tokenA"));
        require(x.bMaker + x.bTaker + x.bFee == y.bMaker + y.bTaker + y.bFee, string.concat(tag, ": tokenB"));
    }

    // ---- 1. 完全成交 + 费用 ----

    function testFullFillWithFee() public {
        BoundedSettlement.Order memory o = _order(100 ether, 200 ether, 5 ether, 1, 0);
        Balances memory before = _snap();

        uint256 buyPaid = _fill(o, 100 ether, 5 ether);

        Balances memory aft = _snap();
        require(buyPaid == 200 ether, "buyPaid");
        require(aft.aTaker - before.aTaker == 100 ether, "taker received 100 A");
        require(before.aMaker - aft.aMaker == 100 ether, "maker sold 100 A");
        require(aft.bMaker - before.bMaker == 200 ether, "maker received 200 B");
        require(before.bTaker - aft.bTaker == 205 ether, "taker paid 200 B + 5 fee");
        require(aft.bFee - before.bFee == 5 ether, "fee receiver got 5 B");
        _assertConserved(before, aft, "full fill");

        bytes32 h = settlement.hashOrder(o);
        require(settlement.filledSellAmount(h) == 100 ether, "filledSell");
        require(settlement.filledBuyAmount(h) == 200 ether, "filledBuy");
        require(settlement.filledFeeAmount(h) == 5 ether, "filledFee");
    }

    // ---- 2. 两次部分成交舍入 + 末笔补足 ----

    function testTwoPartialFillsRounding() public {
        // 3 A -> 100 B，不能整除
        BoundedSettlement.Order memory o = _order(3, 100, 0, 7, 0);
        Balances memory before = _snap();

        uint256 p1 = _fill(o, 1, 0); // 部分：floor(100*1/3)=33
        uint256 p2 = _fill(o, 1, 0); // 部分：floor(100*1/3)=33
        uint256 p3 = _fill(o, 1, 0); // 最后一笔（remaining 归零）：补足 100-66=34

        require(p1 == 33, "partial1");
        require(p2 == 33, "partial2");
        require(p3 == 34, "final fill makes whole");

        Balances memory aft = _snap();
        require(before.aMaker - aft.aMaker == 3, "maker sold exactly 3");
        require(aft.aTaker - before.aTaker == 3, "taker got exactly 3");
        require(aft.bMaker - before.bMaker == 100, "maker got exactly 100 (= signed buyAmount)");
        require(before.bTaker - aft.bTaker == 100, "taker paid exactly 100");
        _assertConserved(before, aft, "rounding");

        bytes32 h = settlement.hashOrder(o);
        require(settlement.filledSellAmount(h) == 3, "filled sell == quota");
        require(settlement.filledBuyAmount(h) == 100, "filled buy == signed amount");
    }

    // ---- 3. 多次部分成交不超过签名额度 ----

    function testPartialFillsNeverExceedSignedQuota() public {
        // 100 A -> 99 B
        BoundedSettlement.Order memory o = _order(100, 99, 0, 2, 0);

        require(_fill(o, 30, 0) == 29, "p1 floor 29.7");
        require(_fill(o, 30, 0) == 29, "p2 floor 29.7");
        // 剩余 40：普通取整会得 39，但这是最后一笔，补足 99-58=41
        require(_fill(o, 40, 0) == 41, "p3 final");

        bytes32 h = settlement.hashOrder(o);
        require(settlement.filledSellAmount(h) == 100, "sell fully consumed");
        require(settlement.filledBuyAmount(h) == 99, "buy exactly signed");

        // 再填必须回滚：成交量不得超过签名额度
        bytes memory sigQ = _sign(o, MAKER_PK, settlement);
        vm.expectRevert(
            abi.encodeWithSelector(BoundedSettlement.FillExceedsRemaining.selector, uint256(1), uint256(0))
        );
        _fillWithSig(o, 1, 0, sigQ);
    }

    // ---- 4. 超量部分成交回滚 ----

    function testFillExceedsRemainingReverts() public {
        BoundedSettlement.Order memory o = _order(100, 100, 0, 3, 0);
        require(_fill(o, 60, 0) == 60, "first fill 60");
        bytes memory sig = _sign(o, MAKER_PK, settlement);
        vm.expectRevert(
            abi.encodeWithSelector(BoundedSettlement.FillExceedsRemaining.selector, uint256(50), uint256(40))
        );
        _fillWithSig(o, 50, 0, sig);
    }

    // ---- 5. 重放：完全成交后同一签名再来一次 ----

    function testReplayAfterFullFillReverts() public {
        BoundedSettlement.Order memory o = _order(100 ether, 100 ether, 0, 4, 0);
        _fill(o, 100 ether, 0);

        bytes memory sig = _sign(o, MAKER_PK, settlement);
        vm.expectRevert(
            abi.encodeWithSelector(BoundedSettlement.FillExceedsRemaining.selector, 100 ether, uint256(0))
        );
        vm.prank(taker);
        settlement.fillOrder(o, 100 ether, 0, sig);
    }

    // ---- 6. 取消竞争：先取消后成交 ----

    function testCancelThenFillReverts() public {
        BoundedSettlement.Order memory o = _order(100 ether, 100 ether, 0, 11, 0);

        vm.prank(maker);
        settlement.cancelNonce(11);

        require(settlement.cancelledNonces(maker, 11), "nonce marked cancelled");
        bytes memory sig11 = _sign(o, MAKER_PK, settlement);
        vm.expectRevert(
            abi.encodeWithSelector(BoundedSettlement.NonceAlreadyCancelled.selector, maker, uint256(11))
        );
        _fillWithSig(o, 100 ether, 0, sig11);
    }

    // ---- 6b. 取消竞争：先成交（部分），再取消，后续成交被拒，已成交部分保留 ----

    function testFillThenCancelKillsRemainingFill() public {
        BoundedSettlement.Order memory o = _order(100 ether, 100 ether, 0, 12, 0);
        Balances memory before = _snap();
        require(_fill(o, 40 ether, 0) == 40 ether, "partial 40");

        vm.prank(maker);
        settlement.cancelNonce(12);

        bytes32 h = settlement.hashOrder(o);
        require(settlement.filledSellAmount(h) == 40 ether, "filled part preserved");
        Balances memory mid = _snap();
        require(mid.aTaker - before.aTaker == 40 ether, "partial delivery preserved");

        bytes memory sig12 = _sign(o, MAKER_PK, settlement);
        vm.expectRevert(
            abi.encodeWithSelector(BoundedSettlement.NonceAlreadyCancelled.selector, maker, uint256(12))
        );
        _fillWithSig(o, 60 ether, 0, sig12);
        require(settlement.filledSellAmount(h) == 40 ether, "still 40 after rejected fill");
    }

    // ---- 7. 到期 ----

    function testExpiredOrderReverts() public {
        uint256 expiry = block.timestamp + 1 hours;
        BoundedSettlement.Order memory o = _order(100 ether, 100 ether, 0, 5, expiry);
        bytes memory sig = _sign(o, MAKER_PK, settlement);
        vm.warp(expiry + 1);
        vm.expectRevert(
            abi.encodeWithSelector(BoundedSettlement.OrderExpired.selector, expiry, expiry + 1)
        );
        _fillWithSig(o, 100 ether, 0, sig);
    }

    function testExpiryBoundaryStillValid() public {
        uint256 expiry = block.timestamp + 1 hours;
        BoundedSettlement.Order memory o = _order(100 ether, 100 ether, 0, 6, expiry);
        vm.warp(expiry); // block.timestamp == expiry 仍可成交
        require(_fill(o, 100 ether, 0) == 100 ether, "boundary fill ok");
    }

    // ---- 8. 费用累计上限 ----

    function testFeeCapCumulative() public {
        BoundedSettlement.Order memory o = _order(100 ether, 100 ether, 10 ether, 8, 0);
        require(_fill(o, 50 ether, 5 ether) == 50 ether, "fill 1 fee 5");

        // 6 会使累计 11 > 10
        bytes memory sigOver = _sign(o, MAKER_PK, settlement);
        vm.expectRevert(
            abi.encodeWithSelector(BoundedSettlement.FeeCapExceeded.selector, 11 ether, 10 ether)
        );
        _fillWithSig(o, 50 ether, 6 ether, sigOver);

        // 5 正好打满
        require(_fill(o, 50 ether, 5 ether) == 50 ether, "fill 2 fee 5 caps exactly");
        bytes32 h = settlement.hashOrder(o);
        require(settlement.filledFeeAmount(h) == 10 ether, "fee total == cap");
    }

    // ---- 9. 转账失败：sellToken 返回 false，整体回滚、状态不变 ----

    function testSellTokenTransferFailureRollsBack() public {
        BoundedSettlement.Order memory o = _order(100 ether, 100 ether, 0, 9, 0);
        Balances memory before = _snap();

        tokenA.setTransfersFail(true);
        bytes memory sigFailA = _sign(o, MAKER_PK, settlement);
        vm.expectRevert(
            abi.encodeWithSelector(
                BoundedSettlement.TransferFailed.selector,
                address(tokenA),
                maker,
                taker,
                100 ether
            )
        );
        _fillWithSig(o, 100 ether, 0, sigFailA);
        tokenA.setTransfersFail(false);

        bytes32 h = settlement.hashOrder(o);
        require(settlement.filledSellAmount(h) == 0, "no fill recorded");
        require(settlement.filledBuyAmount(h) == 0, "no buy recorded");
        Balances memory aft = _snap();
        require(before.aMaker == aft.aMaker && before.aTaker == aft.aTaker, "A balances unchanged");
        require(before.bMaker == aft.bMaker && before.bTaker == aft.bTaker, "B balances unchanged");
    }

    // ---- 9b. 转账失败：buyToken（taker 付款）失败同样回滚 ----

    function testBuyTokenTransferFailureRollsBack() public {
        BoundedSettlement.Order memory o = _order(100 ether, 100 ether, 0, 10, 0);
        tokenB.setTransfersFail(true);
        bytes memory sigFailB = _sign(o, MAKER_PK, settlement);
        vm.expectRevert(
            abi.encodeWithSelector(
                BoundedSettlement.TransferFailed.selector,
                address(tokenB),
                taker,
                maker,
                100 ether
            )
        );
        _fillWithSig(o, 100 ether, 0, sigFailB);
        tokenB.setTransfersFail(false);
        require(settlement.filledSellAmount(settlement.hashOrder(o)) == 0, "no fill recorded");
    }

    // ---- 10. 非法签名 ----

    function testWrongSignerReverts() public {
        BoundedSettlement.Order memory o = _order(100 ether, 100 ether, 0, 13, 0);
        bytes memory sig = _sign(o, TAKER_PK, settlement); // 非 maker 签名
        vm.expectRevert(
            abi.encodeWithSelector(BoundedSettlement.InvalidSignature.selector, taker, maker)
        );
        vm.prank(taker);
        settlement.fillOrder(o, 100 ether, 0, sig);
    }

    function testBadSignatureLengthReverts() public {
        BoundedSettlement.Order memory o = _order(100 ether, 100 ether, 0, 14, 0);
        bytes memory sig = new bytes(64);
        vm.expectRevert(abi.encodeWithSelector(BoundedSettlement.BadSignatureLength.selector, uint256(64)));
        vm.prank(taker);
        settlement.fillOrder(o, 100 ether, 0, sig);
    }

    function testBadVReverts() public {
        BoundedSettlement.Order memory o = _order(100 ether, 100 ether, 0, 15, 0);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(MAKER_PK, settlement.hashOrder(o));
        v = 29;
        bytes memory sig = abi.encodePacked(r, s, v);
        vm.expectRevert(abi.encodeWithSelector(BoundedSettlement.BadSignatureV.selector, uint8(29)));
        vm.prank(taker);
        settlement.fillOrder(o, 100 ether, 0, sig);
    }

    function testHighSReverts() public {
        BoundedSettlement.Order memory o = _order(100 ether, 100 ether, 0, 16, 0);
        (, bytes32 r, bytes32 s) = vm.sign(MAKER_PK, settlement.hashOrder(o));
        // 翻成高位 s（secp256k1n/2 之上）
        s = bytes32(
            0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141 - uint256(s) + 1
        );
        bytes memory sig = abi.encodePacked(r, s, uint8(27));
        vm.expectRevert(BoundedSettlement.BadSignatureS.selector);
        vm.prank(taker);
        settlement.fillOrder(o, 100 ether, 0, sig);
    }

    // ---- 11. 签名域绑定：为合约 A 签的名不能在合约 B 结算 ----

    function testSignatureDomainBoundToContract() public {
        require(settlement.DOMAIN_SEPARATOR() != settlement2.DOMAIN_SEPARATOR(), "domains differ");
        BoundedSettlement.Order memory o = _order(100 ether, 100 ether, 0, 17, 0);
        bytes memory sig = _sign(o, MAKER_PK, settlement); // 针对 settlement 的域签名

        vm.expectRevert(); // recovered 不等于 maker（或为零地址）
        vm.prank(taker);
        settlement2.fillOrder(o, 100 ether, 0, sig);
    }

    // ---- 12. 签名后篡改订单字段 ----

    function testTamperedOrderRejects() public {
        BoundedSettlement.Order memory o = _order(100 ether, 100 ether, 0, 18, 0);
        bytes memory sig = _sign(o, MAKER_PK, settlement);
        o.buyAmount = 99 ether; // 篡改价格
        vm.expectRevert();
        vm.prank(taker);
        settlement.fillOrder(o, 100 ether, 0, sig);
    }

    // ---- 13. 奇数比例多次成交的守恒综合检查 ----

    function testConservationWithOddRatios() public {
        // 7 A -> 13 B：2,2,3
        BoundedSettlement.Order memory o = _order(7, 13, 0, 19, 0);
        Balances memory before = _snap();

        require(_fill(o, 2, 0) == 3, "p1"); // floor(26/7)=3
        require(_fill(o, 2, 0) == 3, "p2"); // floor(26/7)=3
        require(_fill(o, 3, 0) == 7, "p3 final"); // 13-6=7

        Balances memory aft = _snap();
        require(aft.aTaker - before.aTaker == 7, "taker got 7 A");
        require(aft.bMaker - before.bMaker == 13, "maker got exactly 13 B");
        _assertConserved(before, aft, "odd ratios");

        bytes32 h = settlement.hashOrder(o);
        require(settlement.filledSellAmount(h) <= o.sellAmount, "filled <= signed sell quota");
        require(settlement.filledBuyAmount(h) <= o.buyAmount, "bought <= signed buy amount");
    }

    // ---- 14. 只有 maker 能取消自己的 nonce；重复取消回滚 ----

    function testCancelIdempotencyReverts() public {
        vm.prank(maker);
        settlement.cancelNonce(20);
        vm.expectRevert(
            abi.encodeWithSelector(BoundedSettlement.NonceAlreadyCancelled.selector, maker, uint256(20))
        );
        vm.prank(maker);
        settlement.cancelNonce(20);
    }
}
