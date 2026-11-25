<?php declare(strict_types=1);

namespace OmniCore\Connector\Service;

use Doctrine\DBAL\Connection;
use Shopware\Core\Defaults;
use Shopware\Core\Framework\Uuid\Uuid;

class ShopwareChatService
{
    public function __construct(
        private readonly Connection $connection,
        private readonly MiddlewareApiClient $middleware,
        private readonly ?\Symfony\Component\Messenger\MessageBusInterface $bus = null,
    ) {
    }

    /** @return array<string,mixed> */
    public function plan(string $prompt, array $skus, string $actor): array
    {
        $plan = $this->middleware->postShopwareChatPlan($prompt, $skus);
        $this->persistPlan($plan, $actor, $prompt);
        $this->audit((string) ($plan['plan_id'] ?? ''), 'plan', $actor, $prompt, $plan['diff'] ?? null, $plan);
        return $plan;
    }

    /** @return array<string,mixed> */
    public function getPlan(string $planId): array
    {
        $plan = $this->middleware->getShopwareChatPlan($planId);
        $this->persistPlan($plan, '', (string) ($plan['prompt'] ?? ''));
        return $plan;
    }

    /** @return array<string,mixed> */
    public function enqueueExecution(string $planId, string $idempotencyKey, string $actor): array
    {
        $plan = $this->getPlan($planId);
        $key = $idempotencyKey !== '' ? $idempotencyKey : (string) ($plan['idempotency_key'] ?? '');
        if ($this->bus !== null) {
            $this->bus->dispatch(new \OmniCore\Connector\Message\ExecuteShopwareChatPlanMessage($planId, $key, $actor));
            $plan['status'] = 'queued_for_execution';
            $this->persistPlan($plan, $actor, (string) ($plan['prompt'] ?? ''));
            $this->audit($planId, 'enqueue_execute', $actor, (string) ($plan['prompt'] ?? ''), $plan['diff'] ?? null, ['queued' => true, 'idempotency_key' => $key]);
            return $plan;
        }
        return $this->execute($planId, $key, $actor);
    }

    /** @return array<string,mixed> */
    public function execute(string $planId, string $idempotencyKey, string $actor): array
    {
        $plan = $this->middleware->executeShopwareChatPlan($planId, $idempotencyKey);
        $this->persistPlan($plan, $actor, (string) ($plan['prompt'] ?? ''));
        $this->audit($planId, 'execute', $actor, (string) ($plan['prompt'] ?? ''), $plan['diff'] ?? null, $plan['result'] ?? $plan);
        return $plan;
    }

    /** @return array<string,mixed> */
    public function rollback(string $planId, string $actor): array
    {
        $plan = $this->middleware->rollbackShopwareChatPlan($planId);
        $this->persistPlan($plan, $actor, (string) ($plan['prompt'] ?? ''));
        $this->audit($planId, 'rollback', $actor, (string) ($plan['prompt'] ?? ''), $plan['diff'] ?? null, $plan['result'] ?? $plan);
        return $plan;
    }

    private function persistPlan(array $plan, string $actor, string $prompt): void
    {
        $planId = (string) ($plan['plan_id'] ?? '');
        if ($planId === '') return;
        $now = (new \DateTimeImmutable())->format(Defaults::STORAGE_DATE_TIME_FORMAT);
        $existing = $this->connection->fetchOne('SELECT id FROM omnicore_chat_plan WHERE plan_id = :pid', ['pid' => $planId]);
        $data = [
            'plan_id' => $planId,
            'channel_scope' => (string) ($plan['channel_scope'] ?? 'shopware'),
            'intent' => (string) ($plan['intent'] ?? ''),
            'status' => (string) ($plan['status'] ?? 'draft'),
            'idempotency_key' => (string) ($plan['idempotency_key'] ?? hash('sha256', $planId)),
            'requested_by' => mb_substr($actor !== '' ? $actor : (string) ($plan['created_by'] ?? ''), 0, 255),
            'prompt' => $prompt,
            'plan_json' => json_encode($plan, JSON_UNESCAPED_UNICODE),
            'result_json' => isset($plan['result']) ? json_encode($plan['result'], JSON_UNESCAPED_UNICODE) : null,
            'error_message' => isset($plan['error']) ? mb_substr((string) $plan['error'], 0, 4000) : null,
            'updated_at' => $now,
        ];
        if ($existing) {
            $this->connection->update('omnicore_chat_plan', $data, ['plan_id' => $planId]);
        } else {
            $data['id'] = Uuid::randomBytes();
            $data['created_at'] = $now;
            $this->connection->insert('omnicore_chat_plan', $data);
        }
    }

    private function audit(string $planId, string $action, string $actor, string $prompt, mixed $diff, mixed $result): void
    {
        if ($planId === '') return;
        $now = (new \DateTimeImmutable())->format(Defaults::STORAGE_DATE_TIME_FORMAT);
        $this->connection->insert('omnicore_chat_audit', [
            'id' => Uuid::randomBytes(),
            'plan_id' => $planId,
            'action' => $action,
            'actor' => mb_substr($actor, 0, 255),
            'prompt' => $prompt,
            'diff_json' => $diff !== null ? json_encode($diff, JSON_UNESCAPED_UNICODE) : null,
            'result_json' => $result !== null ? json_encode($result, JSON_UNESCAPED_UNICODE) : null,
            'created_at' => $now,
        ]);
    }
}
