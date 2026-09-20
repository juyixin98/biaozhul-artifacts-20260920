<?php

declare(strict_types=1);

namespace TravelOpsTests;

use TravelOps\Money;

final class AllocationRoundingTest extends DbTestCase
{
    /**
     * @dataProvider conservationProvider
     */
    public function testAllocationPartsAlwaysSumToOriginal(string $amount, array $weights): void
    {
        $rules = [];
        foreach ($weights as $i => $w) {
            $rules[] = ['target' => 't' . $i, 'weight' => $w, 'remainder_owner' => $i === 0];
        }
        $parts = Money::allocate($amount, $rules);
        $sum = Money::add(...array_column($parts, 'amount'));
        self::assertSame(Money::normalize($amount), $sum, "amount={$amount} weights=" . json_encode($weights));
    }

    public static function conservationProvider(): array
    {
        return [
            ['100.0000', [1]],
            ['100.0000', [70, 30]],
            ['0.0100', [3, 3, 3]],      // one unit, three lines
            ['1.0000', [1, 1, 1]],
            ['10.0001', [1, 1]],        // indivisible odd unit
            ['0.0003', [7, 11, 13]],
            ['999999999.9999', [1, 2, 3, 4, 5, 6]],
            ['123.4567', [1]],
            ['333.3333', [1, 1, 1]],    // classic 1/3 tail
        ];
    }

    public function testRemainderIsDeterministicallyOwned(): void
    {
        // 0.01 split over 3 equal weights: one leftover unit must land on the
        // declared remainder owner even though all remainders are equal.
        $parts = Money::allocate('0.0100', [
            ['target' => 'a', 'weight' => 1, 'remainder_owner' => false],
            ['target' => 'b', 'weight' => 1, 'remainder_owner' => true],
            ['target' => 'c', 'weight' => 1, 'remainder_owner' => false],
        ]);
        $byTarget = array_column($parts, 'amount', 'target');
        self::assertSame('0.0034', $byTarget['b']);
        self::assertSame('0.0033', $byTarget['a']);
        self::assertSame('0.0033', $byTarget['c']);
    }

    public function testLargestRemainderWithoutOwner(): void
    {
        // 0.05 (500 minor units) over weights 3,2,2 (total 7):
        //   exact quotas = 214 (rem 2), 142 (rem 6), 142 (rem 6)
        //   500 - 498 = 2 leftover units -> the two largest remainders win.
        $parts = Money::allocate('0.0500', [
            ['target' => 'a', 'weight' => 3],
            ['target' => 'b', 'weight' => 2],
            ['target' => 'c', 'weight' => 2],
        ]);
        $byTarget = array_column($parts, 'amount', 'target');
        self::assertSame('0.0214', $byTarget['a']);
        self::assertSame('0.0143', $byTarget['b']);
        self::assertSame('0.0143', $byTarget['c']);
        self::assertSame('0.0500', Money::add(...array_column($parts, 'amount')));
    }

    public function testRulesWithNonPositiveWeightRejected(): void
    {
        $this->expectException(\InvalidArgumentException::class);
        Money::allocate('10.0000', [['target' => 'x', 'weight' => 0]]);
    }
}
