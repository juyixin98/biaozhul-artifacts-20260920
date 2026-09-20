<?php

declare(strict_types=1);

namespace TravelOps;

use RuntimeException;

/** Domain error rendered as a 4xx JSON response. */
class ApiException extends RuntimeException
{
    /**
     * @param array<string,mixed> $details
     */
    public function __construct(
        string $message,
        public readonly int $status = 400,
        public readonly string $errorCode = 'invalid_request',
        public readonly array $details = []
    ) {
        parent::__construct($message);
    }

    public static function notFound(string $what): self
    {
        return new self($what . ' not found', 404, 'not_found');
    }

    public static function conflict(string $message, string $code = 'conflict', array $details = []): self
    {
        return new self($message, 409, $code, $details);
    }

    public static function state(string $message, array $details = []): self
    {
        return new self($message, 409, 'invalid_state', $details);
    }
}
