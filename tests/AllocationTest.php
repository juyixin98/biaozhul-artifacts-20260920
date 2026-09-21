<?php

declare(strict_types=1);

namespace Meridian\Tests;

use Meridian\DomainException;
use Meridian\Services\AllocationService;
use Meridian\Support\Money;

final class AllocationTest extends TestCase
{
    private AllocationService $alloc;

    protected function setUp(): void
    {
        parent::setUp();
        $this->alloc = new AllocationService();
    }

    public function testSharesSumToTotalWithExplicitRemainderSink(): void
    {
        // 10000 minor units across three equal weights cannot split evenly.
        $result = $this->alloc->allocate(10000, [
            ['target' => 'a', 'weight' => 1],
            ['target' => 'b', 'weight' => 1],
            ['target' => 'c', 'weight' => 1],
        ], 'c');

        $this->assertSame(10000, array_sum(array_column($result, 'amount_minor')));
        $this->assertSame(3333, $result[0]['amount_minor']);
        $this->assertSame(3333, $result[1]['amount_minor']);
        $this->assertSame(3334, $result[2]['amount_minor']); // sink owns the remainder
        $this->assertTrue($result[2]['is_remainder_sink']);
        $this->assertFalse($result[0]['is_remainder_sink']);
    }

    public function testRemainderDefaultsToFirstTarget(): void
    {
        $result = $this->alloc->allocate(101, [
            ['target' => 'x', 'weight' => 1],
            ['target' => 'y', 'weight' => 1],
        ], null);

        $this->assertSame(51, $result[0]['amount_minor']);
        $this->assertSame(50, $result[1]['amount_minor']);
        $this->assertSame(101, array_sum(array_column($result, 'amount_minor')));
    }

    public function testNegativeTotalsAlsoBalance(): void
    {
        $result = $this->alloc->allocate(-10000, [
            ['target' => 'a', 'weight' => 1],
            ['target' => 'b', 'weight' => 1],
            ['target' => 'c', 'weight' => 1],
        ], 'a');

        $this->assertSame(-10000, array_sum(array_column($result, 'amount_minor')));
        // floor(-3333.33) = -3334 each, so the +2 remainder goes to the sink.
        $this->assertSame(-3332, $result[0]['amount_minor']);
        $this->assertSame(-3334, $result[1]['amount_minor']);
    }

    public function testWeightedSplit(): void
    {
        $result = $this->alloc->allocate(100000, [
            ['target' => 'ops', 'weight' => 60],
            ['target' => 'fin', 'weight' => 30],
            ['target' => 'sup', 'weight' => 10],
        ], 'ops');

        $this->assertSame([60000, 30000, 10000], array_column($result, 'amount_minor'));
    }

    public function testZeroWeightIsRejected(): void
    {
        $this->expectException(DomainException::class);
        $this->alloc->allocate(100, [['target' => 'a', 'weight' => 0]], null);
    }

    public function testUnknownRemainderTargetIsRejected(): void
    {
        $this->expectException(DomainException::class);
        $this->alloc->allocate(100, [['target' => 'a', 'weight' => 1]], 'nobody');
    }

    public function testMoneyRoundTrip(): void
    {
        $this->assertSame(123456, Money::toMinor('1234.56', 'USD'));
        $this->assertSame(-50, Money::toMinor('-0.5', 'USD'));
        $this->assertSame(1234, Money::toMinor('1234', 'JPY'));
        $this->assertSame('1234.56', Money::fromMinor(123456, 'USD'));
        $this->assertSame('-0.50', Money::fromMinor(-50, 'USD'));
        $this->assertSame('1234', Money::fromMinor(1234, 'JPY'));
    }

    public function testMoneyRejectsExcessPrecisionAndFloats(): void
    {
        $this->expectException(DomainException::class);
        Money::toMinor('1.001', 'USD');
    }

    public function testMoneyRejectsDecimalsForZeroScaleCurrency(): void
    {
        $this->expectException(DomainException::class);
        Money::toMinor('100.5', 'JPY');
    }
}
