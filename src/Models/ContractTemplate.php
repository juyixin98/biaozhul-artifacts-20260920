<?php

declare(strict_types=1);

namespace Meridian\Models;

use Illuminate\Database\Eloquent\Model;

class ContractTemplate extends Model
{
    protected $table = 'contract_templates';
    protected $fillable = ['name', 'body'];
}
