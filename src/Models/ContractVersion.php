<?php

declare(strict_types=1);

namespace Meridian\Models;

use Illuminate\Database\Eloquent\Model;

class ContractVersion extends Model
{
    protected $fillable = [
        'contract_id', 'version_no', 'variables', 'content', 'content_hash',
        'status', 'sign_initiated_at', 'expires_at',
    ];
    protected $casts = [
        'variables' => 'array',
        'sign_initiated_at' => 'datetime',
        'expires_at' => 'datetime',
    ];

    public function contract()
    {
        return $this->belongsTo(Contract::class, 'contract_id');
    }

    public function signers()
    {
        return $this->hasMany(ContractSigner::class, 'version_id')->orderBy('seq');
    }
}
