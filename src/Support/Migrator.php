<?php

declare(strict_types=1);

namespace Meridian\Support;

use Illuminate\Database\Capsule\Manager as DB;

/**
 * Tiny migration runner: executes database/migrations/*.php in name order,
 * tracking applied names in the `migrations` table. Each migration file
 * returns an object with up()/down() methods.
 */
final class Migrator
{
    public function __construct(private readonly string $dir)
    {
    }

    /** @return string[] names of migrations applied in this run */
    public function run(): array
    {
        if (!DB::schema()->hasTable('migrations')) {
            DB::schema()->create('migrations', function ($table) {
                $table->id();
                $table->string('name')->unique();
                $table->timestamp('ran_at')->useCurrent();
            });
        }

        $applied = DB::table('migrations')->pluck('name')->all();
        $ran = [];

        $files = glob(rtrim($this->dir, '/') . '/*.php') ?: [];
        sort($files);
        foreach ($files as $file) {
            $name = basename($file, '.php');
            if (in_array($name, $applied, true)) {
                continue;
            }
            $migration = require $file;
            $migration->up();
            DB::table('migrations')->insert(['name' => $name]);
            $ran[] = $name;
        }

        return $ran;
    }
}
