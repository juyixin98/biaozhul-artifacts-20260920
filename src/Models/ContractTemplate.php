<?php

declare(strict_types=1);

namespace TravelOps\Models;

class ContractTemplate extends BaseModel
{
    protected $table = 'contract_templates';

    public function decodeVariablesPlaceholders(): array
    {
        preg_match_all('/\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}/', $this->body_template, $m);
        return array_values(array_unique($m[1]));
    }
}
