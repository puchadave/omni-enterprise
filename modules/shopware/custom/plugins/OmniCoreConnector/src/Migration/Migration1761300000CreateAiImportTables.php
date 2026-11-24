<?php declare(strict_types=1);

namespace OmniCore\Connector\Migration;

use Doctrine\DBAL\Connection;
use Shopware\Core\Framework\Migration\MigrationStep;

class Migration1761300000CreateAiImportTables extends MigrationStep
{
    public function getCreationTimestamp(): int
    {
        return 1761300000;
    }

    public function update(Connection $connection): void
    {
        $connection->executeStatement(<<<SQL
CREATE TABLE IF NOT EXISTS `omnicore_ai_import_job` (
    `id` BINARY(16) NOT NULL,
    `status` VARCHAR(32) NOT NULL,
    `mode` VARCHAR(32) NOT NULL,
    `filters_json` JSON NULL,
    `requested_by` VARCHAR(255) NULL,
    `total_items` INT NOT NULL DEFAULT 0,
    `processed_items` INT NOT NULL DEFAULT 0,
    `success_items` INT NOT NULL DEFAULT 0,
    `failed_items` INT NOT NULL DEFAULT 0,
    `skipped_items` INT NOT NULL DEFAULT 0,
    `error_summary` LONGTEXT NULL,
    `result_json` JSON NULL,
    `started_at` DATETIME(3) NULL,
    `finished_at` DATETIME(3) NULL,
    `created_at` DATETIME(3) NOT NULL,
    `updated_at` DATETIME(3) NOT NULL,
    PRIMARY KEY (`id`),
    KEY `idx.omnicore_ai_import_job.status_created` (`status`, `created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
SQL);

        $connection->executeStatement(<<<SQL
CREATE TABLE IF NOT EXISTS `omnicore_ai_import_job_item` (
    `id` BINARY(16) NOT NULL,
    `job_id` BINARY(16) NOT NULL,
    `sku` VARCHAR(255) NOT NULL,
    `status` VARCHAR(32) NOT NULL,
    `attempts` INT NOT NULL DEFAULT 0,
    `idempotency_key` VARCHAR(191) NOT NULL,
    `middleware_status` VARCHAR(64) NULL,
    `shopware_product_id` VARCHAR(64) NULL,
    `error_message` LONGTEXT NULL,
    `result_json` JSON NULL,
    `processed_at` DATETIME(3) NULL,
    `created_at` DATETIME(3) NOT NULL,
    `updated_at` DATETIME(3) NOT NULL,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uniq.omnicore_ai_import_job_item.job_sku` (`job_id`, `sku`),
    UNIQUE KEY `uniq.omnicore_ai_import_job_item.idempotency` (`idempotency_key`),
    KEY `idx.omnicore_ai_import_job_item.job_status` (`job_id`, `status`),
    CONSTRAINT `fk.omnicore_ai_import_job_item.job_id`
        FOREIGN KEY (`job_id`) REFERENCES `omnicore_ai_import_job` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
SQL);
    }

    public function updateDestructive(Connection $connection): void
    {
    }
}
