<?php declare(strict_types=1);

namespace OmniCore\Connector\Subscriber;

use OmniCore\Connector\Service\MiddlewareWebhookClient;
use Shopware\Core\Checkout\Order\OrderEvents;
use Shopware\Core\Framework\DataAbstractionLayer\Event\EntityWrittenEvent;
use Symfony\Component\EventDispatcher\EventSubscriberInterface;

final class OrderEventSubscriber implements EventSubscriberInterface
{
    public function __construct(private readonly MiddlewareWebhookClient $client)
    {
    }

    public static function getSubscribedEvents(): array
    {
        return [
            OrderEvents::ORDER_WRITTEN_EVENT => 'onOrderWritten',
        ];
    }

    public function onOrderWritten(EntityWrittenEvent $event): void
    {
        foreach ($event->getWriteResults() as $result) {
            $payload = [
                'id' => $result->getPrimaryKey(),
                'entity' => 'order',
                'operation' => $result->getOperation(),
                'payload' => $result->getPayload(),
                'updated_fields' => $result->getPayload(),
            ];
            $this->client->send('shopware.order.written', $payload, $event->getContext()->getSource() ? null : null);
        }
    }
}
