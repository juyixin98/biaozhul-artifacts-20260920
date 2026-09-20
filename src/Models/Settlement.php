<?php

declare(strict_types=1);

namespace TravelOps\Models;

class Settlement extends BaseModel
{
    protected $table = 'settlements';
    public const UPDATED_AT = null;

    public function allocations()
    {
        return $this->hasMany(SettlementAllocation::class)->orderBy('position');
    }
}
