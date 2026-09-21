<?php

declare(strict_types=1);

namespace Meridian\Models;

use Illuminate\Database\Eloquent\Model;

class ContractSigner extends Model
{
    protected $table = 'contract_signers';
    protected $fillable = [
        'version_id', 'seq', 'signer_name', 'signer_role',
        'status', 'confirmed_at', 'confirmed_by',
    ];
    protected $casts = ['confirmed_at' => 'datetime'];
}
