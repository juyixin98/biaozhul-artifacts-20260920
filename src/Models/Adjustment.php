<?php

declare(strict_types=1);

namespace Meridian\Models;

use Illuminate\Database\Eloquent\Model;

class Adjustment extends Model
{
    public const UPDATED_AT = null;
    protected $fillable = [
        'target_type', 'target_id', 'type', 'amount_minor',
        'business_date', 'reason', 'operator',
    ];
}
