<?php declare(strict_types=1);

namespace OmniCore\Connector\Message;

class ProcessAiImportJobMessage
{
    public function __construct(public readonly string $jobId)
    {
    }
}
