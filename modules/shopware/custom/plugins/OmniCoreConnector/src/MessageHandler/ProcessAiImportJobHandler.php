<?php declare(strict_types=1);

namespace OmniCore\Connector\MessageHandler;

use OmniCore\Connector\Message\ProcessAiImportJobMessage;
use OmniCore\Connector\Service\AiImportJobService;
use Psr\Log\LoggerInterface;
use Symfony\Component\Messenger\Attribute\AsMessageHandler;

#[AsMessageHandler]
class ProcessAiImportJobHandler
{
    public function __construct(
        private readonly AiImportJobService $jobs,
        private readonly LoggerInterface $logger,
    ) {
    }

    public function __invoke(ProcessAiImportJobMessage $message): void
    {
        try {
            $this->jobs->processJob($message->jobId);
        } catch (\Throwable $e) {
            $this->logger->error('OmniCore AI import job failed', [
                'job_id' => $message->jobId,
                'error' => $e->getMessage(),
            ]);
            throw $e;
        }
    }
}
