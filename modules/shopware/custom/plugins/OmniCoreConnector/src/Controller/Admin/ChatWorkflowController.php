<?php declare(strict_types=1);

namespace OmniCore\Connector\Controller\Admin;

use OmniCore\Connector\Service\ShopwareChatService;
use Shopware\Core\Framework\Context;
use Symfony\Bundle\FrameworkBundle\Controller\AbstractController;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\Routing\Attribute\Route;

#[Route(defaults: ['_routeScope' => ['api']])]
class ChatWorkflowController extends AbstractController
{
    public function __construct(private readonly ShopwareChatService $chat)
    {
    }

    #[Route(path: '/api/_action/omnicore/chat/plan', name: 'api.action.omnicore.chat.plan', methods: ['POST'])]
    public function plan(Request $request, Context $context): JsonResponse
    {
        $this->denyUnlessAcl($context, 'omnicore_connector:read');
        $payload = json_decode($request->getContent(), true);
        if (!is_array($payload)) {
            return new JsonResponse(['error' => 'invalid_json'], 400);
        }
        $prompt = trim((string) ($payload['message'] ?? $payload['prompt'] ?? ''));
        $skus = is_array($payload['skus'] ?? null) ? $payload['skus'] : [];
        if ($prompt === '') {
            return new JsonResponse(['error' => 'message_required'], 400);
        }
        try {
            $plan = $this->chat->plan($prompt, $skus, $this->currentUser($context));
            return new JsonResponse(['status' => 'ok', 'plan' => $plan]);
        } catch (\Throwable $e) {
            return new JsonResponse(['error' => 'plan_failed', 'message' => $e->getMessage()], 500);
        }
    }

    #[Route(path: '/api/_action/omnicore/chat/plans/{planId}', name: 'api.action.omnicore.chat.plan.get', methods: ['GET'])]
    public function getPlan(string $planId, Context $context): JsonResponse
    {
        $this->denyUnlessAcl($context, 'omnicore_connector:read');
        try {
            return new JsonResponse(['status' => 'ok', 'plan' => $this->chat->getPlan($planId)]);
        } catch (\Throwable $e) {
            return new JsonResponse(['error' => 'not_found', 'message' => $e->getMessage()], 404);
        }
    }

    #[Route(path: '/api/_action/omnicore/chat/plans/{planId}/execute', name: 'api.action.omnicore.chat.plan.execute', methods: ['POST'])]
    public function execute(string $planId, Request $request, Context $context): JsonResponse
    {
        $this->denyUnlessAcl($context, 'omnicore_connector:import');
        $payload = json_decode($request->getContent(), true);
        $idempotencyKey = is_array($payload) ? (string) ($payload['idempotency_key'] ?? '') : '';
        try {
            $plan = $this->chat->enqueueExecution($planId, $idempotencyKey, $this->currentUser($context));
            return new JsonResponse(['status' => 'accepted', 'plan' => $plan], 202);
        } catch (\Throwable $e) {
            return new JsonResponse(['error' => 'execute_failed', 'message' => $e->getMessage()], 500);
        }
    }

    #[Route(path: '/api/_action/omnicore/chat/plans/{planId}/rollback', name: 'api.action.omnicore.chat.plan.rollback', methods: ['POST'])]
    public function rollback(string $planId, Context $context): JsonResponse
    {
        $this->denyUnlessAcl($context, 'omnicore_connector:import');
        try {
            $plan = $this->chat->rollback($planId, $this->currentUser($context));
            return new JsonResponse(['status' => 'ok', 'plan' => $plan]);
        } catch (\Throwable $e) {
            return new JsonResponse(['error' => 'rollback_failed', 'message' => $e->getMessage()], 500);
        }
    }

    private function denyUnlessAcl(Context $context, string $privilege): void
    {
        $source = $context->getSource();
        if (method_exists($source, 'isAllowed') && !$source->isAllowed($privilege)) {
            throw $this->createAccessDeniedException('Missing privilege: ' . $privilege);
        }
    }

    private function currentUser(Context $context): string
    {
        $source = $context->getSource();
        if (method_exists($source, 'getUserId') && $source->getUserId()) {
            return (string) $source->getUserId();
        }
        return 'admin';
    }
}
