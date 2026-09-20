<?php

declare(strict_types=1);

namespace TravelOps\Models;

class AllocationRuleVersion extends BaseModel
{
    protected $table = 'allocation_rule_versions';

    public const STATUS_ACTIVE = 'active';
    public const STATUS_SUPERSEDED = 'superseded';

    /** @return list<array{target:string,weight:int,remainder_owner?:bool}> */
    public function rules(): array
    {
        return json_decode((string) $this->rules_json, true, 512, JSON_THROW_ON_ERROR);
    }
}
