<?php declare(strict_types=1);

namespace OmniCore\Connector\Migration;

use Doctrine\DBAL\Connection;
use Shopware\Core\Framework\Migration\MigrationStep;

class Migration1761400000CreateChatWorkflowTables extends MigrationStep
{
    public function getCreationTimestamp(): int
    {
        return 1761400000;
    }

    public function update(Connection $connection): void
    {
        $connection->executeStatement(<<<SQL
CREATE TABLE IF NOT EXISTS `omnicore_chat_plan` (
    `id` BINARY(16) NOT NULL,
    `plan_id` VARCHAR(64) NOT NULL,
    `channel_scope` VARCHAR(32) NOT NULL DEFAULT 'shopware',
    `intent` VARCHAR(64) NOT NULL,
    `status` VARCHAR(32) NOT NULL,
    `idempotency_key` VARCHAR(191) NOT NULL,
    `requested_by` VARCHAR(255) NOT NULL DEFAULT '',
    `prompt` LONGTEXT NULL,
    `plan_json` LONGTEXT NOT NULL,
    `result_json` LONGTEXT NULL,
    `error_message` LONGTEXT NULL,
    `created_at` DATETIME(3) NOT NULL,
    `updated_at` DATETIME(3) NOT NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uniq.omnicore_chat_plan.plan_id` (`plan_id`),
    UNIQUE KEY `uniq.omnicore_chat_plan.idempotency` (`idempotency_key`),
    KEY `idx.omnicore_chat_plan.status` (`status`),
    KEY `idx.omnicore_chat_plan.created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
SQL);

        $connection->executeStatement(<<<SQL
CREATE TABLE IF NOT EXISTS `omnicore_chat_audit` (
    `id` BINARY(16) NOT NULL,
    `plan_id` VARCHAR(64) NOT NULL,
    `action` VARCHAR(64) NOT NULL,
    `actor` VARCHAR(255) NOT NULL DEFAULT '',
    `prompt` LONGTEXT NULL,
    `diff_json` LONGTEXT NULL,
    `result_json` LONGTEXT NULL,
    `created_at` DATETIME(3) NOT NULL,
    PRIMARY KEY (`id`),
    KEY `idx.omnicore_chat_audit.plan_id` (`plan_id`),
    KEY `idx.omnicore_chat_audit.created_at` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
SQL);
    }

    public function updateDestructive(Connection $connection): void
    {
    }
}
