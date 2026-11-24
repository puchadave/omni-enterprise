<?php declare(strict_types=1);

namespace OmniCore\Connector\Command;

use Doctrine\DBAL\Connection;
use OmniCore\Connector\Service\OmniConfigService;
use Symfony\Component\Console\Attribute\AsCommand;
use Symfony\Component\Console\Command\Command;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Output\OutputInterface;

#[AsCommand(name: 'omnicore:connector:health', description: 'OMNI connector health/config check')]
final class HealthCheckCommand extends Command
{
    public function __construct(private readonly OmniConfigService $config, private readonly Connection $connection)
    {
        parent::__construct();
    }

    protected function execute(InputInterface $input, OutputInterface $output): int
    {
        $dbOk = true;
        try {
            $this->connection->executeQuery('SELECT 1')->fetchOne();
        } catch (\Throwable $e) {
            $dbOk = false;
            $output->writeln('<error>DB error: ' . $e->getMessage() . '</error>');
        }
        $output->writeln('enabled=' . ($this->config->isEnabled() ? '1' : '0'));
        $output->writeln('middleware_base_url_configured=' . ($this->config->getMiddlewareBaseUrl() !== '' ? '1' : '0'));
        $output->writeln('secret_configured=' . ($this->config->getSharedSecret() !== '' ? '1' : '0'));
        $output->writeln('db=' . ($dbOk ? 'ok' : 'error'));
        return $dbOk ? Command::SUCCESS : Command::FAILURE;
    }
}
