<?php

declare(strict_types=1);

namespace TravelOpsTests;

use PHPUnit\Framework\TestCase;
use TravelOps\Clock;
use TravelOps\Database;

abstract class DbTestCase extends TestCase
{
    protected function setUp(): void
    {
        parent::setUp();
        Database::reset();
        Clock::reset();
        $this->migrate();
    }

    protected function migrate(): void
    {
        $schema = file_get_contents(__DIR__ . '/../database/schema.sql');
        $lines = [];
        foreach (preg_split('/\r?\n/', $schema) as $line) {
            if (preg_match('/^\s*--/', $line)) {
                continue;
            }
            $lines[] = $line;
        }
        $conn = \Illuminate\Database\Capsule\Manager::connection();
        foreach (array_filter(array_map('trim', explode(';', implode("\n", $lines)))) as $stmt) {
            if ($stmt !== '') {
                $conn->statement($stmt);
            }
        }
    }

    /**
     * Run bin/worker.php N times in parallel, all started before any is
     * awaited, so they genuinely contend. Returns decoded JSON lines.
     *
     * @param list<array{task:string,args:array<string,mixed>}> $jobs
     * @return list<array<string,mixed>>
     */
    protected function runParallel(array $jobs): array
    {
        $pipes = [];
        $handles = [];
        foreach ($jobs as $i => $job) {
            $cmd = sprintf(
                'php %s %s %s 2>&1',
                escapeshellarg(__DIR__ . '/../bin/worker.php'),
                escapeshellarg($job['task']),
                escapeshellarg(json_encode($job['args'], JSON_UNESCAPED_UNICODE))
            );
            $handles[$i] = proc_open($cmd, [1 => ['pipe', 'w']], $pipes[$i]);
        }
        $results = [];
        foreach ($handles as $i => $h) {
            $out = stream_get_contents($pipes[$i][1]);
            fclose($pipes[$i][1]);
            proc_close($h);
            // Worker prints exactly one JSON line; ignore any stray warnings.
            $line = '';
            foreach (explode("\n", trim((string) $out)) as $candidate) {
                if (str_starts_with(ltrim($candidate), '{')) {
                    $line = trim($candidate);
                }
            }
            $decoded = json_decode($line, true);
            if (!is_array($decoded)) {
                $decoded = ['ok' => false, 'raw' => $out];
            }
            $results[$i] = $decoded;
        }
        ksort($results);
        return array_values($results);
    }
}
