<?php declare(strict_types=1);

namespace OmniCore\Connector\Service;

use Shopware\Core\System\SystemConfig\SystemConfigService;

class OmniConfigService
{
    private const PREFIX = 'OmniCoreConnector.config.';

    public function __construct(private readonly SystemConfigService $systemConfig)
    {
    }

    public function getString(string $key, string $default = ''): string
    {
        $value = $this->systemConfig->get(self::PREFIX . $key);
        return is_string($value) ? trim($value) : $default;
    }

    public function isEnabled(): bool
    {
        return (bool) $this->systemConfig->get(self::PREFIX . 'enabled');
    }

    public function middlewareBaseUrl(): string
    {
        return rtrim($this->getString('middlewareBaseUrl'), '/');
    }

    public function middlewareApiBaseUrl(): string
    {
        return rtrim($this->getString('middlewareApiBaseUrl'), '/');
    }

    public function middlewareApiKey(): string
    {
        return $this->getString('middlewareApiKey');
    }

    public function sharedSecret(): string
    {
        return $this->getString('sharedSecret');
    }

    public function sourceId(): string
    {
        $source = $this->getString('sourceId', 'shopware-connector');
        return $source !== '' ? $source : 'shopware-connector';
    }

    public function timeoutSeconds(): int
    {
        $value = $this->systemConfig->get(self::PREFIX . 'timeoutSeconds');
        if (!is_numeric($value)) {
            return 10;
        }
        $int = (int) $value;
        return $int > 0 ? $int : 10;
    }
}
