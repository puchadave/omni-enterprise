import './page/omnicore-chat-workflow';
import deDE from '../../snippet/de-DE.json';
import enGB from '../../snippet/en-GB.json';

Shopware.Module.register('omnicore-chat', {
    type: 'plugin',
    name: 'omnicore-chat',
    title: 'omnicore-chat.general.mainMenuItemGeneral',
    description: 'omnicore-chat.general.descriptionTextModule',
    color: '#2563eb',
    icon: 'regular-comments',
    snippets: { 'de-DE': deDE, 'en-GB': enGB },
    routes: { index: { component: 'omnicore-chat-workflow', path: 'index', meta: { privilege: 'omnicore_connector:read' } } },
    navigation: [{ id: 'omnicore-chat', label: 'omnicore-chat.general.mainMenuItemGeneral', color: '#2563eb', path: 'omnicore.chat.index', icon: 'regular-comments', parent: 'sw-catalogue', position: 101, privilege: 'omnicore_connector:read' }],
});
