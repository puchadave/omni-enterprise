<?php declare(strict_types=1);

namespace OmniCore\Connector\Controller;

use Doctrine\DBAL\Connection;
use OmniCore\Connector\Service\IdempotencyService;
use OmniCore\Connector\Service\OmniConfigService;
use Shopware\Core\Framework\Context;
use Symfony\Bundle\FrameworkBundle\Controller\AbstractController;
use Symfony\Component\HttpFoundation\JsonResponse;
use Symfony\Component\Routing\Attribute\Route;

#[Route(defaults: ['_routeScope' => ['api']])]
final class HealthController extends AbstractController
{
    public function __construct(
        private readonly OmniConfigService $config,
        private readonly IdempotencyService $idempotency,
        private readonly Connection $connection
    ) {
    }

    #[Route(path: '/api/_action/omnicore/connector/health', name: 'api.action.omnicore.connector.health', methods: ['GET'])]
    public function health(Context $context): JsonResponse
    {
        $this->idempotency->ensureSchema();
        $dbOk = true;
        try {
            $this->connection->executeQuery('SELECT 1')->fetchOne();
        } catch (\Throwable) {
            $dbOk = false;
        }

        return new JsonResponse([
            'status' => $dbOk ? 'ok' : 'degraded',
            'plugin' => 'OmniCoreConnector',
            'enabled' => $this->config->isEnabled(),
            'middleware_base_url_configured' => $this->config->getMiddlewareBaseUrl() !== '',
            'secret_configured' => $this->config->getSharedSecret() !== '',
            'db' => $dbOk ? 'ok' : 'error',
            'time' => (new \DateTimeImmutable())->format(DATE_ATOM),
        ]);
    }
}
