<?php declare(strict_types=1);

namespace OmniCore\Connector\Service;

use Psr\Log\LoggerInterface;
use Symfony\Contracts\HttpClient\HttpClientInterface;

class MiddlewareApiClient
{
    public function __construct(
        private readonly OmniConfigService $config,
        private readonly HttpClientInterface $http,
        private readonly LoggerInterface $logger,
    ) {
    }

    public function isConfigured(): bool
    {
        $base = $this->middlewareApiBaseUrl();
        return $base !== '' && $this->config->middlewareApiKey() !== '';
    }

    public function middlewareApiBaseUrl(): string
    {
        $explicit = trim((string) $this->config->getString('middlewareApiBaseUrl'));
        if ($explicit !== '') {
            return rtrim($explicit, '/');
        }
        $webhook = $this->config->middlewareBaseUrl();
        if ($webhook === '') {
            return '';
        }
        // Accept either API base or webhook URL; normalize to /api root.
        $u = preg_replace('#/api/webhooks/shopware(?:/.*)?$#', '/api', $webhook);
        $u = preg_replace('#/webhooks/shopware(?:/.*)?$#', '/api', (string) $u);
        return rtrim((string) $u, '/');
    }

    /** @return array<string,mixed> */
    public function getCandidates(array $query = []): array
    {
        return $this->request('GET', '/sales-release/candidates', ['query' => $query]);
    }

    /** @return array<string,mixed> */
    public function getCandidate(string $sku): array
    {
        return $this->request('GET', '/sales-release/candidates/' . rawurlencode($sku));
    }

    /** @return array<string,mixed> */
    public function selectCandidates(int $limit = 100, float $minScore = 55.0): array
    {
        return $this->request('POST', '/sales-release/select', ['query' => ['limit' => $limit, 'min_score' => $minScore]]);
    }

    /** @param string[] $skus */
    public function validateCandidates(array $skus = []): array
    {
        return $this->request('POST', '/sales-release/validate', ['json' => ['skus' => array_values($skus)]]);
    }

    /** @param string[] $skus */
    public function importCandidates(array $skus = [], int $limit = 100): array
    {
        return $this->request('POST', '/sales-release/import-shopware', ['json' => ['skus' => array_values($skus), 'limit' => $limit]]);
    }

    /** @return array<string,mixed> */
    private function request(string $method, string $path, array $options = []): array
    {
        $base = $this->middlewareApiBaseUrl();
        if ($base === '') {
            throw new \RuntimeException('Middleware API base URL is not configured');
        }
        $apiKey = $this->config->middlewareApiKey();
        if ($apiKey === '') {
            throw new \RuntimeException('Middleware API key is not configured');
        }

        $headers = array_merge([
            'Accept' => 'application/json',
            'Content-Type' => 'application/json',
            'X-Internal-API-Key' => $apiKey,
        ], $options['headers'] ?? []);
        $options['headers'] = $headers;
        $options['timeout'] = $options['timeout'] ?? $this->config->timeoutSeconds();

        $url = $base . $path;
        $response = $this->http->request($method, $url, $options);
        $status = $response->getStatusCode();
        $body = $response->getContent(false);
        $decoded = json_decode($body, true);
        if ($status >= 400) {
            $this->logger->warning('OmniCore middleware API request failed', [
                'method' => $method,
                'path' => $path,
                'status' => $status,
            ]);
            $msg = is_array($decoded) ? ($decoded['detail'] ?? $decoded['error'] ?? $decoded['message'] ?? $body) : $body;
            throw new \RuntimeException('Middleware API error ' . $status . ': ' . (is_string($msg) ? $msg : json_encode($msg)));
        }
        if (!is_array($decoded)) {
            throw new \RuntimeException('Middleware API returned invalid JSON');
        }
        return $decoded;
    }
}
