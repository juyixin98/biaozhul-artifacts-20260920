<?php

declare(strict_types=1);

namespace Meridian\Models;

use Illuminate\Database\Eloquent\Model;

class SettlementLine extends Model
{
    protected $table = 'settlement_lines';
    protected $fillable = [
        'batch_id', 'contract_id', 'currency', 'business_date', 'external_ref',
        'amount_minor', 'description', 'content_hash', 'status',
    ];
}
