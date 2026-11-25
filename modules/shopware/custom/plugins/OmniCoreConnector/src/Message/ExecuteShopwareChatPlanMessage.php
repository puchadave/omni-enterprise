<?php declare(strict_types=1);

namespace OmniCore\Connector\Message;

class ExecuteShopwareChatPlanMessage
{
    public function __construct(
        public readonly string $planId,
        public readonly string $idempotencyKey,
        public readonly string $actor,
    ) {
    }
}
