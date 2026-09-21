<?php

declare(strict_types=1);

namespace Meridian\Support;

use Meridian\DomainException;

/**
 * Fixed-point money handling. Amounts are stored as integer minor units
 * (cents for most currencies). API payloads carry decimal strings; all
 * conversion goes through here so no float ever touches an amount.
 */
final class Money
{
    /** ISO-4217 minor-unit exponents for the currencies we care about. */
    private const SCALES = [
        'JPY' => 0, 'KRW' => 0, 'VND' => 0,
        'BHD' => 3, 'JOD' => 3, 'KWD' => 3, 'OMR' => 3,
    ];

    public static function scale(string $currency): int
    {
        return self::SCALES[strtoupper($currency)] ?? 2;
    }

    public static function isKnownCurrency(string $currency): bool
    {
        return (bool) preg_match('/^[A-Z]{3}$/', strtoupper($currency));
    }

    /**
     * Parse a decimal string like "1234.56" into minor units (123456).
     *
     * @throws DomainException on malformed input or excess precision
     */
    public static function toMinor(string $amount, string $currency): int
    {
        $currency = strtoupper($currency);
        $scale = self::scale($currency);
        $pattern = $scale === 0
            ? '/^-?\d+$/'
            : '/^-?\d+(\.\d{1,' . $scale . '})?$/';

        if (!preg_match($pattern, $amount)) {
            throw new DomainException(
                "invalid amount '{$amount}' for currency {$currency} (scale {$scale})",
                422,
                'invalid_amount'
            );
        }

        $minor = bcmul($amount, (string) (10 ** $scale), 0);

        return (int) $minor;
    }

    public static function fromMinor(int $minor, string $currency): string
    {
        $scale = self::scale($currency);
        $negative = $minor < 0;
        $abs = abs($minor);
        $int = intdiv($abs, 10 ** $scale);
        $frac = $scale === 0 ? '' : '.' . str_pad((string) ($abs % (10 ** $scale)), $scale, '0', STR_PAD_LEFT);

        return ($negative ? '-' : '') . $int . $frac;
    }
}
