<?php

declare(strict_types=1);

namespace Meridian\Models;

use Illuminate\Database\Eloquent\Model;

class ContractEvent extends Model
{
    protected $table = 'contract_events';
    public const UPDATED_AT = null;
    protected $fillable = ['contract_id', 'version_id', 'action', 'operator', 'detail', 'created_at'];
    protected $casts = ['detail' => 'array', 'created_at' => 'datetime'];
}
