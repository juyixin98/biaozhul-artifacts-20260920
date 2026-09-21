<?php

declare(strict_types=1);

namespace Meridian\Models;

use Illuminate\Database\Eloquent\Model;

class SettlementAllocation extends Model
{
    protected $table = 'settlement_allocations';
    public const UPDATED_AT = null;
    protected $fillable = ['settlement_id', 'target', 'weight', 'amount_minor', 'is_remainder_sink'];
    protected $casts = ['is_remainder_sink' => 'boolean'];
}
