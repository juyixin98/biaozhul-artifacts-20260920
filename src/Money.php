<?php

declare(strict_types=1);

namespace TravelOps;

use InvalidArgumentException;

/**
 * Fixed-point money helpers. All amounts use 4-decimal DECIMAL(18,4) storage
 * and bcmath in PHP; floating point is never used.
 */
final class Money
{
    public const SCALE = 4;

    /**
     * Accepts int|numeric-string with at most 4 decimal places.
     * Returns a canonical 4-decimal string; throws on floats / too many digits.
     */
    public static function normalize(mixed $value): string
    {
        if (is_int($value)) {
            $value = (string) $value;
        }
        if (!is_string($value) || !preg_match('/^-?\d+(\.\d+)?$/', $value)) {
            throw new InvalidArgumentException('amount must be a decimal string, got: ' . self::debug($value));
        }
        if (preg_match('/\.(-?\d+)$/', $value, $m) && strlen($m[1]) > self::SCALE) {
            throw new InvalidArgumentException('amount may have at most 4 decimal places: ' . $value);
        }
        return bcadd($value, '0', self::SCALE);
    }

    /** Convert a normalized 4-decimal string to integer minor units (×10000). */
    public static function toMinor(string $amount): int
    {
        [$whole, $frac] = array_pad(explode('.', $amount), 2, '');
        $frac = str_pad($frac, self::SCALE, '0');
        $sign = str_starts_with($whole, '-') ? -1 : 1;
        $wholeDigits = ltrim($whole, '-');
        return $sign * ((int) $wholeDigits * 10 ** self::SCALE + (int) $frac);
    }

    public static function fromMinor(int $minor): string
    {
        $sign = $minor < 0 ? '-' : '';
        $abs = abs($minor);
        $whole = intdiv($abs, 10 ** self::SCALE);
        $frac = str_pad((string) ($abs % 10 ** self::SCALE), self::SCALE, '0');
        return $sign . $whole . '.' . $frac;
    }

    public static function add(string ...$amounts): string
    {
        $acc = '0';
        foreach ($amounts as $a) {
            $acc = bcadd($acc, self::normalize($a), self::SCALE);
        }
        return $acc;
    }

    /**
     * Allocate $amount by integer $weights using the Hare / largest-remainder
     * method, so the parts always sum exactly to the original amount.
     *
     * @param list<array{target:string,weight:int,remainder_owner?:bool}> $rules
     * @return list<array{target:string,weight:int,amount:string,remainder:int,is_remainder_owner:bool,position:int}>
     */
    public static function allocate(string $amount, array $rules): array
    {
        $totalWeight = 0;
        foreach ($rules as $r) {
            $totalWeight += $r['weight'];
        }
        if ($totalWeight <= 0) {
            throw new InvalidArgumentException('allocation weights must sum to a positive integer');
        }

        $totalMinor = self::toMinorString(self::normalize($amount));
        $parts = [];
        $allocated = '0';
        foreach ($rules as $i => $r) {
            // Exact integer quotient/remainder of total*weight/totalWeight.
            // bcmath keeps this exact even when the product exceeds PHP_INT_MAX
            // (a native $total * $weight would silently cast to float).
            $product = bcmul($totalMinor, (string) $r['weight'], 0);
            $exactS = bcdiv($product, (string) $totalWeight, 0);
            $rem = (int) bcmod($product, (string) $totalWeight, 0);
            $parts[] = [
                'target' => $r['target'],
                'weight' => $r['weight'],
                'exact' => $exactS,
                'remainder' => $rem,
                'remainder_owner' => !empty($r['remainder_owner']),
                'position' => $i,
            ];
            $allocated = bcadd($allocated, $exactS, 0);
        }

        // Leftover units: integer division of each line discards < 1 unit, so
        // the total left over is strictly less than the number of lines.
        $left = (int) bcsub($totalMinor, $allocated, 0);

        // Distribute leftover units one per line, prioritising:
        //   1. a line explicitly marked remainder_owner (deterministic tie-break target)
        //   2. the largest fractional remainder (Hare / largest remainder)
        //   3. declared order (first in list)
        // Every unit is therefore assigned to a named line: the rounding
        // difference ownership is always explicit, never implicit.
        if ($left > 0) {
            usort($parts, function ($a, $b) {
                if ($a['remainder_owner'] !== $b['remainder_owner']) {
                    return $b['remainder_owner'] <=> $a['remainder_owner'];
                }
                if ($a['remainder'] !== $b['remainder']) {
                    return $b['remainder'] <=> $a['remainder'];
                }
                return $a['position'] <=> $b['position'];
            });
            foreach ($parts as &$p) {
                if ($left <= 0) {
                    break;
                }
                $p['exact'] = bcadd($p['exact'], '1', 0);
                $left--;
            }
            unset($p);
        }

        usort($parts, fn ($a, $b) => $a['position'] <=> $b['position']);

        return array_map(static fn ($p) => [
            'target' => $p['target'],
            'weight' => $p['weight'],
            'amount' => self::fromMinorString($p['exact']),
            'remainder' => $p['remainder'],
            'is_remainder_owner' => $p['remainder_owner'],
            'position' => $p['position'],
        ], $parts);
    }

    /** Like toMinor but returns a decimal string, immune to int overflow. */
    public static function toMinorString(string $amount): string
    {
        $negative = str_starts_with($amount, '-');
        $abs = $negative ? substr($amount, 1) : $amount;
        [$whole, $frac] = array_pad(explode('.', $abs), 2, '');
        $frac = str_pad($frac, self::SCALE, '0');
        $minor = bcadd(bcmul($whole, (string) (10 ** self::SCALE), 0), $frac, 0);
        return $negative && bccomp($minor, '0', 0) !== 0 ? '-' . $minor : $minor;
    }

    /** Like fromMinor but accepts a decimal string. */
    public static function fromMinorString(string $minor): string
    {
        $negative = str_starts_with($minor, '-');
        $abs = $negative ? substr($minor, 1) : $minor;
        if (bccomp($abs, (string) (10 ** self::SCALE), 0) < 0) {
            $value = '0.' . str_pad($abs, self::SCALE, '0', STR_PAD_LEFT);
        } else {
            $whole = bcdiv($abs, (string) (10 ** self::SCALE), 0);
            $frac = bcmod($abs, (string) (10 ** self::SCALE), 0);
            $value = $whole . '.' . str_pad($frac, self::SCALE, '0', STR_PAD_LEFT);
        }
        return $negative && bccomp($abs, '0', 0) !== 0 ? '-' . $value : $value;
    }

    private static function debug(mixed $v): string
    {
        return is_float($v) ? 'float' : (is_scalar($v) ? (string) $v : get_debug_type($v));
    }
}
