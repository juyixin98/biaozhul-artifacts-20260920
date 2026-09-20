<?php

declare(strict_types=1);

namespace TravelOps\Models;

use Illuminate\Database\Eloquent\Model;

abstract class BaseModel extends Model
{
    public const CREATED_AT = 'created_at';
    public const UPDATED_AT = 'updated_at';

    protected $guarded = [];

    public static function freshAt(): string
    {
        return \TravelOps\Clock::now()->format('Y-m-d H:i:s.u');
    }

    /** @return array<string,mixed> */
    protected static function stamp(): array
    {
        return ['created_at' => self::freshAt(), 'updated_at' => self::freshAt()];
    }
}
