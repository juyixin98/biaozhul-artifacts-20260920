<?php

declare(strict_types=1);

namespace Meridian\Models;

use Illuminate\Database\Eloquent\Model;

class AllocationRuleVersion extends Model
{
    protected $table = 'allocation_rule_versions';
    public const UPDATED_AT = null;
    protected $fillable = ['name', 'version_no', 'rules', 'remainder_target', 'created_by'];
    protected $casts = ['rules' => 'array'];
}
