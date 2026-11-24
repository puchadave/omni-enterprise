<?php declare(strict_types=1);

namespace OmniCore\Connector\Service;

use Shopware\Core\System\SystemConfig\SystemConfigService;

final class OmniConfigService
{
    public const PREFIX = 'OmniCoreConnector.config.';

    public function __construct(private readonly SystemConfigService $systemConfigService)
    {
    }

    public function isEnabled(?string $salesChannelId = null): bool
    {
        return (bool) $this->systemConfigService->get(self::PREFIX . 'enabled', $salesChannelId);
    }

    public function getMiddlewareBaseUrl(?string $salesChannelId = null): string
    {
        return trim((string) $this->systemConfigService->get(self::PREFIX . 'middlewareBaseUrl', $salesChannelId));
    }

    public function getSharedSecret(?string $salesChannelId = null): string
    {
        return (string) $this->systemConfigService->get(self::PREFIX . 'sharedSecret', $salesChannelId);
    }

    public function getSourceId(?string $salesChannelId = null): string
    {
        $value = trim((string) $this->systemConfigService->get(self::PREFIX . 'sourceId', $salesChannelId));
        return $value !== '' ? $value : 'shopware';
    }

    public function getTimeoutSeconds(?string $salesChannelId = null): int
    {
        $v = (int) $this->systemConfigService->get(self::PREFIX . 'timeoutSeconds', $salesChannelId);
        return $v > 0 ? $v : 10;
    }
}
