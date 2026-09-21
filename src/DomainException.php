<?php

declare(strict_types=1);

namespace Meridian;

/**
 * Domain-level failure carrying the HTTP status the API should answer with.
 */
class DomainException extends \RuntimeException
{
    public function __construct(
        string $message,
        public readonly int $status = 400,
        public readonly string $errorCode = 'domain_error',
    ) {
        parent::__construct($message);
    }
}
