<?php declare(strict_types=1);

namespace OmniCore\Connector\MessageHandler;

use OmniCore\Connector\Message\ExecuteShopwareChatPlanMessage;
use OmniCore\Connector\Service\ShopwareChatService;
use Psr\Log\LoggerInterface;
use Symfony\Component\Messenger\Attribute\AsMessageHandler;

#[AsMessageHandler]
class ExecuteShopwareChatPlanHandler
{
    public function __construct(
        private readonly ShopwareChatService $chat,
        private readonly LoggerInterface $logger,
    ) {
    }

    public function __invoke(ExecuteShopwareChatPlanMessage $message): void
    {
        try {
            $this->chat->execute($message->planId, $message->idempotencyKey, $message->actor);
        } catch (\Throwable $e) {
            $this->logger->error('OmniCore Shopware chat plan execution failed', [
                'plan_id' => $message->planId,
                'error' => $e->getMessage(),
            ]);
            throw $e;
        }
    }
}
