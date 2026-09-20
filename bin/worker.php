<?php

declare(strict_types=1);

/**
 * Concurrency test worker: runs ONE domain operation in its own process and
 * prints a single-line JSON result. Used by the PHPUnit race-condition tests
 * so contenders truly run in parallel against the same MySQL database.
 *
 * Usage: php bin/worker.php <task> <json-args>
 */

require __DIR__ . '/../vendor/autoload.php';

use TravelOps\ApiException;
use TravelOps\Clock;
use TravelOps\Database;
use TravelOps\Services\CloseService;
use TravelOps\Services\ImportService;
use TravelOps\Services\SigningService;

Database::boot();

$task = $argv[1] ?? '';
$args = json_decode($argv[2] ?? '{}', true, 512, JSON_THROW_ON_ERROR);

if (!empty($args['freeze_clock'])) {
    Clock::freeze(new DateTimeImmutable($args['freeze_clock']));
}

$emit = function (array $payload): void {
    fwrite(STDOUT, json_encode($payload, JSON_UNESCAPED_UNICODE) . "\n");
    exit(0);
};

try {
    switch ($task) {
        case 'sign':
            $r = (new SigningService())->sign((int) $args['version_id'], (string) $args['signer'], (string) $args['content_hash'], (string) ($args['actor'] ?? 'worker'));
            $emit(['ok' => true, 'signature_id' => (int) $r['signature']->id, 'idempotent' => $r['idempotent']]);
            // no break needed; emit exits

        case 'withdraw':
            $v = (new SigningService())->withdraw((int) $args['version_id'], (string) ($args['actor'] ?? 'worker'));
            $emit(['ok' => true, 'status' => $v->status]);
            // no break needed; emit exits

        case 'import':
            $r = (new ImportService())->importBatch($args['entries'], (string) ($args['actor'] ?? 'worker'), null);
            $emit(['ok' => true] + $r);
            // no break needed; emit exits

        case 'close':
            $r = (new CloseService())->close((string) $args['business_date'], (string) ($args['actor'] ?? 'worker'));
            $emit(['ok' => true] + $r);
            // no break needed; emit exits

        default:
            $emit(['ok' => false, 'error' => "unknown task {$task}"]);
    }
} catch (ApiException $e) {
    $emit(['ok' => false, 'status' => $e->status, 'code' => $e->errorCode, 'message' => $e->getMessage()]);
} catch (Throwable $e) {
    $emit(['ok' => false, 'status' => 500, 'code' => 'error', 'message' => $e->getMessage()]);
}
