<?php declare(strict_types=1);

namespace OmniCore\Connector\Subscriber;

use OmniCore\Connector\Service\MiddlewareWebhookClient;
use Shopware\Core\Content\Product\ProductEvents;
use Shopware\Core\Framework\DataAbstractionLayer\Event\EntityWrittenEvent;
use Symfony\Component\EventDispatcher\EventSubscriberInterface;

final class ProductEventSubscriber implements EventSubscriberInterface
{
    public function __construct(private readonly MiddlewareWebhookClient $client)
    {
    }

    public static function getSubscribedEvents(): array
    {
        return [
            ProductEvents::PRODUCT_WRITTEN_EVENT => 'onProductWritten',
        ];
    }

    public function onProductWritten(EntityWrittenEvent $event): void
    {
        foreach ($event->getWriteResults() as $result) {
            $payload = [
                'id' => $result->getPrimaryKey(),
                'entity' => 'product',
                'operation' => $result->getOperation(),
                'payload' => $result->getPayload(),
            ];
            $this->client->send('shopware.product.written', $payload);
        }
    }
}
