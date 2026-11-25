<?php declare(strict_types=1);

namespace OmniCore\Connector\Service;

use Doctrine\DBAL\Connection;
use Shopware\Core\Defaults;
use Shopware\Core\Framework\Uuid\Uuid;
use Symfony\Component\Messenger\MessageBusInterface;
use OmniCore\Connector\Message\ProcessAiImportJobMessage;

class AiImportJobService
{
    public function __construct(
        private readonly Connection $connection,
        private readonly MiddlewareApiClient $middleware,
        private readonly MessageBusInterface $bus,
    ) {
    }

    /** @return array<string,mixed> */
    public function createJob(string $mode, array $skus, array $filters, string $requestedBy): array
    {
        $mode = in_array($mode, ['selected', 'filter', 'retry_failed'], true) ? $mode : 'selected';
        $skus = $this->normalizeSkus($skus);
        $resolveFiltersAsync = ($mode === 'filter' || ($mode === 'selected' && $skus === [] && $filters !== []));
        if ($resolveFiltersAsync) {
            // Resolve filter in background handler to keep admin response <2s.
            $skus = [];
        }
        if (!$resolveFiltersAsync && $skus === []) {
            throw new \InvalidArgumentException('No SKUs selected for import');
        }

        $now = (new \DateTimeImmutable())->format(Defaults::STORAGE_DATE_TIME_FORMAT);
        $jobId = Uuid::randomBytes();
        $jobIdHex = Uuid::fromBytesToHex($jobId);
        $jobFilters = $filters;
        $jobFilters['_resolve_async'] = $resolveFiltersAsync;
        $this->connection->insert('omnicore_ai_import_job', [
            'id' => $jobId,
            'status' => 'queued',
            'mode' => $mode,
            'filters_json' => json_encode($jobFilters, JSON_UNESCAPED_UNICODE),
            'requested_by' => mb_substr($requestedBy, 0, 255),
            'total_items' => count($skus),
            'processed_items' => 0,
            'success_items' => 0,
            'failed_items' => 0,
            'skipped_items' => 0,
            'error_summary' => null,
            'result_json' => json_encode(['resolve_filters_async' => $resolveFiltersAsync], JSON_UNESCAPED_UNICODE),
            'started_at' => null,
            'finished_at' => null,
            'created_at' => $now,
            'updated_at' => $now,
        ]);

        foreach ($skus as $sku) {
            $this->insertJobItem($jobId, $jobIdHex, $sku, $now);
        }

        $this->bus->dispatch(new ProcessAiImportJobMessage($jobIdHex));
        return $this->getJob($jobIdHex);
    }

    private function insertJobItem(string $jobId, string $jobIdHex, string $sku, string $now): void
    {
        $itemId = Uuid::randomBytes();
        $idem = hash('sha256', $jobIdHex . '|' . $sku . '|import');
        $this->connection->insert('omnicore_ai_import_job_item', [
            'id' => $itemId,
            'job_id' => $jobId,
            'sku' => $sku,
            'status' => 'pending',
            'attempts' => 0,
            'idempotency_key' => $idem,
            'middleware_status' => null,
            'shopware_product_id' => null,
            'error_message' => null,
            'result_json' => null,
            'processed_at' => null,
            'created_at' => $now,
            'updated_at' => $now,
        ]);
    }

    /** @return array<string,mixed> */
    public function retryFailed(string $jobIdHex): array
    {
        $job = $this->getJob($jobIdHex);
        $jobId = Uuid::fromHexToBytes($jobIdHex);
        $items = $this->connection->fetchAllAssociative(
            'SELECT sku FROM omnicore_ai_import_job_item WHERE job_id = :jobId AND status = :status',
            ['jobId' => $jobId, 'status' => 'failed']
        );
        $skus = $this->normalizeSkus(array_map(fn($r) => (string) ($r['sku'] ?? ''), $items));
        if ($skus === []) {
            return $job;
        }
        return $this->createJob('retry_failed', $skus, ['source_job_id' => $jobIdHex], (string) ($job['requested_by'] ?? 'admin'));
    }

    /** @return array<string,mixed> */
    public function getJob(string $jobIdHex): array
    {
        $row = $this->connection->fetchAssociative('SELECT * FROM omnicore_ai_import_job WHERE id = :id', ['id' => Uuid::fromHexToBytes($jobIdHex)]);
        if (!$row) {
            throw new \RuntimeException('Job not found');
        }
        return $this->hydrateJob($row);
    }

    /** @return array<int,array<string,mixed>> */
    public function getJobItems(string $jobIdHex, int $limit = 100, int $offset = 0): array
    {
        $rows = $this->connection->fetchAllAssociative(
            'SELECT * FROM omnicore_ai_import_job_item WHERE job_id = :jobId ORDER BY created_at ASC LIMIT :limit OFFSET :offset',
            ['jobId' => Uuid::fromHexToBytes($jobIdHex), 'limit' => max(1, min($limit, 500)), 'offset' => max(0, $offset)],
            ['limit' => \Doctrine\DBAL\ParameterType::INTEGER, 'offset' => \Doctrine\DBAL\ParameterType::INTEGER]
        );
        return array_map([$this, 'hydrateItem'], $rows);
    }

    /** @return array<string,mixed> */
    public function processJob(string $jobIdHex): array
    {
        $jobId = Uuid::fromHexToBytes($jobIdHex);
        $now = fn() => (new \DateTimeImmutable())->format(Defaults::STORAGE_DATE_TIME_FORMAT);
        $this->connection->executeStatement(
            'UPDATE omnicore_ai_import_job SET status = :status, started_at = COALESCE(started_at, :startedAt), updated_at = :updatedAt WHERE id = :id AND status IN ("queued","running")',
            ['status' => 'running', 'startedAt' => $now(), 'updatedAt' => $now(), 'id' => $jobId]
        );

        $job = $this->getJob($jobIdHex);
        $filters = is_array($job['filters_json'] ?? null) ? $job['filters_json'] : [];
        if (($filters['_resolve_async'] ?? false) === true) {
            $resolved = $this->resolveFilterSkus($filters);
            foreach ($resolved as $sku) {
                $this->insertJobItem($jobId, $jobIdHex, $sku, $now());
            }
            $filters['_resolve_async'] = false;
            $this->connection->update('omnicore_ai_import_job', [
                'filters_json' => json_encode($filters, JSON_UNESCAPED_UNICODE),
                'total_items' => count($resolved),
                'updated_at' => $now(),
            ], ['id' => $jobId]);
        }

        $items = $this->connection->fetchAllAssociative(
            'SELECT id, sku, status, attempts, idempotency_key FROM omnicore_ai_import_job_item WHERE job_id = :jobId AND status IN ("pending","retry_scheduled","failed") ORDER BY created_at ASC',
            ['jobId' => $jobId]
        );

        $success = 0; $failed = 0; $skipped = 0; $processed = 0;
        foreach ($items as $item) {
            $itemId = $item['id'];
            $sku = (string) $item['sku'];
            $attempts = ((int) $item['attempts']) + 1;
            $this->connection->update('omnicore_ai_import_job_item', [
                'status' => 'processing', 'attempts' => $attempts, 'updated_at' => $now(),
            ], ['id' => $itemId]);

            try {
                $validation = $this->middleware->validateCandidates([$sku]);
                $blocked = $this->findSkuInList($validation['blocked'] ?? [], $sku);
                if ($blocked !== null) {
                    $msg = 'Validation blocked: ' . implode(', ', array_map('strval', $blocked['errors'] ?? ['unknown']));
                    $this->markItem($itemId, 'failed', $msg, ['validation' => $validation], null, null);
                    $failed++; $processed++;
                    $this->updateJobCounts($jobId, $processed, $success, $failed, $skipped);
                    continue;
                }

                $import = $this->middleware->importCandidates([$sku], 1);
                $imported = $this->findSkuInList($import['imported'] ?? [], $sku);
                $importErr = $this->findSkuInList($import['errors'] ?? [], $sku);
                if ($imported !== null) {
                    $this->markItem($itemId, 'imported', null, ['validation' => $validation, 'import' => $imported], (string) ($imported['status'] ?? 'IMPORTED'), (string) ($imported['shopware_product_id'] ?? ''));
                    $success++; $processed++;
                } elseif ($importErr !== null) {
                    $this->markItem($itemId, 'failed', (string) ($importErr['error'] ?? 'Import failed'), ['validation' => $validation, 'import_error' => $importErr], 'IMPORT_BLOCKED', (string) ($importErr['shopware_product_id'] ?? ''));
                    $failed++; $processed++;
                } else {
                    $this->markItem($itemId, 'skipped', 'No import result returned for SKU', ['validation' => $validation, 'import' => $import], null, null);
                    $skipped++; $processed++;
                }
            } catch (\Throwable $e) {
                $this->markItem($itemId, 'failed', mb_substr($e->getMessage(), 0, 2000), ['exception' => get_class($e)], null, null);
                $failed++; $processed++;
            }
            $this->updateJobCounts($jobId, $processed, $success, $failed, $skipped);
        }

        $final = $failed > 0 ? ($success > 0 || $skipped > 0 ? 'completed_with_errors' : 'failed') : 'completed';
        $summary = $failed > 0 ? sprintf('%d failed, %d succeeded, %d skipped', $failed, $success, $skipped) : null;
        $this->connection->update('omnicore_ai_import_job', [
            'status' => $final,
            'processed_items' => $processed,
            'success_items' => $success,
            'failed_items' => $failed,
            'skipped_items' => $skipped,
            'error_summary' => $summary,
            'finished_at' => $now(),
            'updated_at' => $now(),
            'result_json' => json_encode(['success' => $success, 'failed' => $failed, 'skipped' => $skipped, 'processed' => $processed], JSON_UNESCAPED_UNICODE),
        ], ['id' => $jobId]);

        return $this->getJob($jobIdHex);
    }

    private function updateJobCounts(string $jobId, int $processed, int $success, int $failed, int $skipped): void
    {
        $this->connection->update('omnicore_ai_import_job', [
            'processed_items' => $processed,
            'success_items' => $success,
            'failed_items' => $failed,
            'skipped_items' => $skipped,
            'updated_at' => (new \DateTimeImmutable())->format(Defaults::STORAGE_DATE_TIME_FORMAT),
        ], ['id' => $jobId]);
    }

    private function markItem(string $itemId, string $status, ?string $error, array $result, ?string $middlewareStatus, ?string $shopwareProductId): void
    {
        $this->connection->update('omnicore_ai_import_job_item', [
            'status' => $status,
            'error_message' => $error !== null ? mb_substr($error, 0, 4000) : null,
            'result_json' => json_encode($result, JSON_UNESCAPED_UNICODE),
            'middleware_status' => $middlewareStatus,
            'shopware_product_id' => $shopwareProductId,
            'processed_at' => (new \DateTimeImmutable())->format(Defaults::STORAGE_DATE_TIME_FORMAT),
            'updated_at' => (new \DateTimeImmutable())->format(Defaults::STORAGE_DATE_TIME_FORMAT),
        ], ['id' => $itemId]);
    }

    private function findSkuInList(mixed $list, string $sku): ?array
    {
        if (!is_array($list)) return null;
        foreach ($list as $entry) {
            if (is_array($entry) && (string) ($entry['sku'] ?? '') === $sku) return $entry;
        }
        return null;
    }

    /** @return string[] */
    private function normalizeSkus(array $skus): array
    {
        $out = [];
        foreach ($skus as $sku) {
            $s = trim((string) $sku);
            if ($s !== '') $out[$s] = $s;
        }
        return array_values($out);
    }

    private function candidateQueryFromFilters(array $filters): array
    {
        $q = [];
        foreach (['status', 'search', 'min_score', 'limit', 'offset'] as $k) {
            if (isset($filters[$k]) && $filters[$k] !== '' && $filters[$k] !== null) $q[$k] = $filters[$k];
        }
        if (!isset($q['limit'])) $q['limit'] = min(500, max(1, (int) ($filters['limit'] ?? 100)));
        return $q;
    }

    /** @return string[] */
    private function resolveFilterSkus(array $filters): array
    {
        $candidates = $this->middleware->getCandidates($this->candidateQueryFromFilters($filters));
        $items = is_array($candidates['items'] ?? null) ? $candidates['items'] : [];
        return $this->normalizeSkus(array_map(fn($i) => is_array($i) ? (string) ($i['sku'] ?? '') : '', $items));
    }

    /** @param array<string,mixed> $row @return array<string,mixed> */
    private function hydrateJob(array $row): array
    {
        $row['id'] = Uuid::fromBytesToHex($row['id']);
        foreach (['filters_json', 'result_json'] as $k) {
            if (isset($row[$k]) && is_string($row[$k])) {
                $decoded = json_decode($row[$k], true);
                $row[$k] = is_array($decoded) ? $decoded : $row[$k];
            }
        }
        foreach (['total_items','processed_items','success_items','failed_items','skipped_items'] as $k) {
            $row[$k] = (int) ($row[$k] ?? 0);
        }
        return $row;
    }

    /** @param array<string,mixed> $row @return array<string,mixed> */
    private function hydrateItem(array $row): array
    {
        $row['id'] = Uuid::fromBytesToHex($row['id']);
        $row['job_id'] = Uuid::fromBytesToHex($row['job_id']);
        if (isset($row['result_json']) && is_string($row['result_json'])) {
            $decoded = json_decode($row['result_json'], true);
            $row['result_json'] = is_array($decoded) ? $decoded : $row['result_json'];
        }
        $row['attempts'] = (int) ($row['attempts'] ?? 0);
        return $row;
    }
}
