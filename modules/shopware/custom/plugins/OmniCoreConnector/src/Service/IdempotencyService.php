<?php declare(strict_types=1);

namespace OmniCore\Connector\Service;

use Doctrine\DBAL\Connection;

final class IdempotencyService
{
    public const TABLE = 'omnicore_connector_event_outbox';

    public function __construct(private readonly Connection $connection)
    {
    }

    public function ensureSchema(): void
    {
        $this->connection->executeStatement(sprintf('CREATE TABLE IF NOT EXISTS %s (
            id BINARY(16) NOT NULL,
            idempotency_key VARCHAR(191) NOT NULL,
            event_name VARCHAR(190) NOT NULL,
            payload_hash CHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL,
            response_code INT NULL,
            response_body LONGTEXT NULL,
            error_message LONGTEXT NULL,
            created_at DATETIME(3) NOT NULL,
            updated_at DATETIME(3) NOT NULL,
            PRIMARY KEY(id),
            UNIQUE KEY uniq_omnicore_idempotency (idempotency_key),
            KEY idx_omnicore_status_created (status, created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci', self::TABLE));
    }

    public function begin(string $idempotencyKey, string $eventName, string $payloadHash): bool
    {
        $this->ensureSchema();
        $now = (new \DateTimeImmutable())->format('Y-m-d H:i:s.v');
        $id = random_bytes(16);
        $affected = $this->connection->executeStatement(
            sprintf('INSERT IGNORE INTO %s (id,idempotency_key,event_name,payload_hash,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)', self::TABLE),
            [$id, $idempotencyKey, $eventName, $payloadHash, 'pending', $now, $now],
            [\PDO::PARAM_LOB]
        );

        return $affected === 1;
    }

    public function markSent(string $idempotencyKey, int $code, string $body): void
    {
        $this->update($idempotencyKey, 'sent', $code, $body, null);
    }

    public function markFailed(string $idempotencyKey, int $code, string $body, string $error): void
    {
        $this->update($idempotencyKey, 'failed', $code, $body, $error);
    }

    private function update(string $idempotencyKey, string $status, int $code, ?string $body, ?string $error): void
    {
        $now = (new \DateTimeImmutable())->format('Y-m-d H:i:s.v');
        $this->connection->executeStatement(
            sprintf('UPDATE %s SET status=?, response_code=?, response_body=?, error_message=?, updated_at=? WHERE idempotency_key=?', self::TABLE),
            [$status, $code, $body, $error, $now, $idempotencyKey]
        );
    }
}
