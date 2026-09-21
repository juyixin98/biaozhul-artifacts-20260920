<?php

declare(strict_types=1);

namespace Meridian\Services;

use Meridian\DomainException;

/**
 * Weight-based cost allocation over integer minor units.
 *
 * Each target gets floor(total * weight / totalWeight); the leftover
 * (rounding remainder) is assigned in full to the designated remainder sink
 * (rule's remainder_target, defaulting to the first allocation). The sum of
 * the allocated shares therefore always equals the original total exactly,
 * and the remainder's owner is explicit and recorded.
 */
final class AllocationService
{
    /**
     * @param array<int,array{target:string,weight:int}> $allocations
     * @return array<int,array{target:string,weight:int,amount_minor:int,is_remainder_sink:bool}>
     */
    public function allocate(int $totalMinor, array $allocations, ?string $remainderTarget = null): array
    {
        if ($allocations === []) {
            throw new DomainException('allocation rule has no targets', 422, 'invalid_rule');
        }

        $totalWeight = 0;
        foreach ($allocations as $a) {
            $weight = (int) ($a['weight'] ?? 0);
            if ($weight <= 0 || !isset($a['target']) || $a['target'] === '') {
                throw new DomainException('allocation targets need a name and a positive weight', 422, 'invalid_rule');
            }
            $totalWeight += $weight;
        }

        $sink = $remainderTarget ?? $allocations[0]['target'];
        $targets = array_column($allocations, 'target');
        if (!in_array($sink, $targets, true)) {
            throw new DomainException("remainder_target '{$sink}' is not one of the allocation targets", 422, 'invalid_rule');
        }

        $result = [];
        $distributed = 0;
        foreach ($allocations as $a) {
            $share = self::floorDiv($totalMinor * (int) $a['weight'], $totalWeight);
            $distributed += $share;
            $result[] = [
                'target' => $a['target'],
                'weight' => (int) $a['weight'],
                'amount_minor' => $share,
                'is_remainder_sink' => false,
            ];
        }

        $remainder = $totalMinor - $distributed;
        foreach ($result as &$row) {
            if ($row['target'] === $sink) {
                $row['amount_minor'] += $remainder;
                $row['is_remainder_sink'] = true;
                break;
            }
        }
        unset($row);

        $sum = array_sum(array_column($result, 'amount_minor'));
        if ($sum !== $totalMinor) {
            // Defensive: the invariant "allocated sum == original amount" must never break.
            throw new \LogicException("allocation invariant violated: {$sum} != {$totalMinor}");
        }

        return $result;
    }

    /** Floor division that also behaves correctly for negative dividends. */
    private static function floorDiv(int $a, int $b): int
    {
        $q = intdiv($a, $b);
        if ($a % $b !== 0 && (($a < 0) !== ($b < 0))) {
            $q--;
        }
        return $q;
    }
}
