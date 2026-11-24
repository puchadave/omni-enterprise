<?php declare(strict_types=1);

namespace OmniCore\Connector\Service;

use Psr\Log\LoggerInterface;
use Symfony\Contracts\HttpClient\HttpClientInterface;

final class MiddlewareWebhookClient
{
    public function __construct(
        private readonly OmniConfigService $config,
        private readonly IdempotencyService $idempotency,
        private readonly HttpClientInterface $httpClient,
        private readonly LoggerInterface $logger
    ) {
    }

    /** @param array<string,mixed> $payload */
    public function send(string $eventName, array $payload, ?string $salesChannelId = null): array
    {
        if (!$this->config->isEnabled($salesChannelId)) {
            return ['status' => 'skipped', 'reason' => 'disabled'];
        }

        $url = $this->config->getMiddlewareBaseUrl($salesChannelId);
        $secret = $this->config->getSharedSecret($salesChannelId);
        if ($url === '' || $secret === '') {
            return ['status' => 'skipped', 'reason' => 'missing_config'];
        }

        $envelope = [
            'event' => $eventName,
            'source' => $this->config->getSourceId($salesChannelId),
            'occurred_at' => (new \DateTimeImmutable())->format(DATE_ATOM),
            'payload' => $payload,
        ];
        $body = json_encode($envelope, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
        if ($body === false) {
            return ['status' => 'error', 'reason' => 'json_encode_failed'];
        }

        $payloadHash = hash('sha256', $body);
        $idempotencyKey = hash('sha256', $eventName . '|' . $payloadHash . '|' . ($payload['id'] ?? ''));
        if (!$this->idempotency->begin($idempotencyKey, $eventName, $payloadHash)) {
            return ['status' => 'skipped', 'reason' => 'duplicate', 'idempotency_key' => $idempotencyKey];
        }

        $signature = hash_hmac('sha256', $body, $secret);
        try {
            $response = $this->httpClient->request('POST', $url, [
                'headers' => [
                    'Content-Type' => 'application/json',
                    'X-Omni-Event' => $eventName,
                    'X-Omni-Idempotency-Key' => $idempotencyKey,
                    'X-Omni-Signature' => 'sha256=' . $signature,
                ],
                'body' => $body,
                'timeout' => $this->config->getTimeoutSeconds($salesChannelId),
            ]);
            $code = $response->getStatusCode();
            $respBody = $response->getContent(false);
            if ($code >= 200 && $code < 300) {
                $this->idempotency->markSent($idempotencyKey, $code, $respBody);
                return ['status' => 'sent', 'code' => $code, 'idempotency_key' => $idempotencyKey];
            }
            $this->idempotency->markFailed($idempotencyKey, $code, $respBody, 'non_2xx');
            $this->logger->warning('OMNI webhook failed', ['event' => $eventName, 'code' => $code]);
            return ['status' => 'failed', 'code' => $code, 'idempotency_key' => $idempotencyKey];
        } catch (\Throwable $e) {
            $this->idempotency->markFailed($idempotencyKey, 0, '', $e->getMessage());
            $this->logger->error('OMNI webhook exception', ['event' => $eventName, 'error' => $e->getMessage()]);
            return ['status' => 'error', 'reason' => $e->getMessage(), 'idempotency_key' => $idempotencyKey];
        }
    }
}
