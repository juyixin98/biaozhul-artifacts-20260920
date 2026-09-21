<?php

declare(strict_types=1);

namespace Meridian\Models;

use Illuminate\Database\Eloquent\Model;

class Settlement extends Model
{
    public const UPDATED_AT = null;
    protected $fillable = [
        'contract_id', 'contract_version_id', 'currency', 'business_date',
        'rule_version_id', 'total_minor', 'status', 'created_by',
    ];

    public function allocations()
    {
        return $this->hasMany(SettlementAllocation::class, 'settlement_id');
    }
}
