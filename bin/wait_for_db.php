<?php

declare(strict_types=1);

require __DIR__ . '/../vendor/autoload.php';

use TravelOps\Env;

$host = Env::get('DB_HOST', '127.0.0.1');
$port = (int) Env::get('DB_PORT', '3306');
$user = Env::get('DB_USER', 'root');
$pass = Env::get('DB_PASSWORD', 'travelops');
$db = Env::get('DB_NAME', 'travelops');

// Ensure the database exists before migrations run.
$attempts = 0;
$pdo = null;
while ($attempts < 90) {
    try {
        $pdo = new PDO("mysql:host={$host};port={$port}", $user, $pass, [
            PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
        ]);
        break;
    } catch (Throwable $e) {
        $attempts++;
        echo "  waiting for mysql ({$attempts})...\n";
        sleep(1);
    }
}
if (!$pdo) {
    fwrite(STDERR, "database not reachable at {$host}:{$port}\n");
    exit(1);
}

$pdo->exec("CREATE DATABASE IF NOT EXISTS `{$db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci");
echo "[wait_for_db] database {$db} ready at {$host}:{$port}\n";
