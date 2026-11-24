<?php declare(strict_types=1);

namespace OmniCore\Connector\Controller\Admin;

use OmniCore\Connector\Service\AiImportJobService;
use OmniCore\Connector\Service\MiddlewareApiClient;
use Shopware\Core\Framework\Context;
use Symfony\Bundle\FrameworkBundle\Controller\AbstractController;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\HttpFoundation\Request;
use Symfony\Component\Routing\Attribute\Route;

#[Route(defaults: ['_routeScope' => ['api']])]
class AiImportController extends AbstractController
{
    public function __construct(
        private readonly MiddlewareApiClient $middleware,
        private readonly AiImportJobService $jobs,
    ) {
    }

    #[Route(path: '/api/_action/omnicore/ai-import/candidates', name: 'api.action.omnicore.ai_import.candidates', methods: ['GET'])]
    public function candidates(Request $request, Context $context): JsonResponse
    {
        $this->denyUnlessAcl($context, 'omnicore_connector:read');
        $query = array_filter([
            'limit' => $request->query->get('limit', 100),
            'offset' => $request->query->get('offset', 0),
            'status' => $request->query->get('status'),
            'search' => $request->query->get('search'),
            'min_score' => $request->query->get('minScore', $request->query->get('min_score')),
        ], fn($v) => $v !== null && $v !== '');
        return new JsonResponse($this->middleware->getCandidates($query));
    }

    #[Route(path: '/api/_action/omnicore/ai-import/candidates/{sku}', name: 'api.action.omnicore.ai_import.candidate_detail', methods: ['GET'])]
    public function candidateDetail(string $sku, Context $context): JsonResponse
    {
        $this->denyUnlessAcl($context, 'omnicore_connector:read');
        return new JsonResponse($this->middleware->getCandidate($sku));
    }

    #[Route(path: '/api/_action/omnicore/ai-import/refresh', name: 'api.action.omnicore.ai_import.refresh', methods: ['POST'])]
    public function refresh(Request $request, Context $context): JsonResponse
    {
        $this->denyUnlessAcl($context, 'omnicore_connector:update');
        $payload = json_decode($request->getContent(), true);
        $limit = is_array($payload) ? (int) ($payload['limit'] ?? 100) : 100;
        $minScore = is_array($payload) ? (float) ($payload['minScore'] ?? $payload['min_score'] ?? 55.0) : 55.0;
        $result = $this->middleware->selectCandidates(max(1, min($limit, 500)), max(0, min($minScore, 100)));
        return new JsonResponse($result);
    }

    #[Route(path: '/api/_action/omnicore/ai-import/jobs', name: 'api.action.omnicore.ai_import.jobs.create', methods: ['POST'])]
    public function createJob(Request $request, Context $context): JsonResponse
    {
        $this->denyUnlessAcl($context, 'omnicore_connector:import');
        $payload = json_decode($request->getContent(), true);
        if (!is_array($payload)) {
            return new JsonResponse(['error' => 'invalid_json'], 400);
        }
        $mode = (string) ($payload['mode'] ?? 'selected');
        $skus = is_array($payload['skus'] ?? null) ? $payload['skus'] : [];
        $filters = is_array($payload['filters'] ?? null) ? $payload['filters'] : [];
        $confirm = (bool) ($payload['confirm'] ?? false);
        if ($mode === 'filter' && !$confirm) {
            return new JsonResponse(['error' => 'confirm_required_for_filter_import', 'message' => 'Filter-based import requires confirm=true'], 409);
        }
        try {
            $job = $this->jobs->createJob($mode, $skus, $filters, $this->currentUser($context));
            return new JsonResponse(['status' => 'ok', 'job' => $job], 202);
        } catch (\InvalidArgumentException $e) {
            return new JsonResponse(['error' => 'invalid_request', 'message' => $e->getMessage()], 400);
        } catch (\Throwable $e) {
            return new JsonResponse(['error' => 'job_create_failed', 'message' => $e->getMessage()], 500);
        }
    }

    #[Route(path: '/api/_action/omnicore/ai-import/jobs/{jobId}', name: 'api.action.omnicore.ai_import.jobs.get', methods: ['GET'])]
    public function getJob(string $jobId, Context $context): JsonResponse
    {
        $this->denyUnlessAcl($context, 'omnicore_connector:read');
        try {
            return new JsonResponse(['status' => 'ok', 'job' => $this->jobs->getJob($jobId)]);
        } catch (\Throwable $e) {
            return new JsonResponse(['error' => 'not_found', 'message' => $e->getMessage()], 404);
        }
    }

    #[Route(path: '/api/_action/omnicore/ai-import/jobs/{jobId}/items', name: 'api.action.omnicore.ai_import.jobs.items', methods: ['GET'])]
    public function getJobItems(string $jobId, Request $request, Context $context): JsonResponse
    {
        $this->denyUnlessAcl($context, 'omnicore_connector:read');
        $limit = max(1, min((int) $request->query->get('limit', 100), 500));
        $offset = max(0, (int) $request->query->get('offset', 0));
        try {
            return new JsonResponse(['status' => 'ok', 'items' => $this->jobs->getJobItems($jobId, $limit, $offset)]);
        } catch (\Throwable $e) {
            return new JsonResponse(['error' => 'not_found', 'message' => $e->getMessage()], 404);
        }
    }

    #[Route(path: '/api/_action/omnicore/ai-import/jobs/{jobId}/retry-failed', name: 'api.action.omnicore.ai_import.jobs.retry_failed', methods: ['POST'])]
    public function retryFailed(string $jobId, Context $context): JsonResponse
    {
        $this->denyUnlessAcl($context, 'omnicore_connector:import');
        try {
            $job = $this->jobs->retryFailed($jobId);
            return new JsonResponse(['status' => 'ok', 'job' => $job], 202);
        } catch (\Throwable $e) {
            return new JsonResponse(['error' => 'retry_failed', 'message' => $e->getMessage()], 500);
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
