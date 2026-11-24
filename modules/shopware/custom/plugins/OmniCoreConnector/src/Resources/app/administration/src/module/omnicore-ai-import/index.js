import './page/omnicore-ai-import-list';
import deDE from '../../snippet/de-DE.json';
import enGB from '../../snippet/en-GB.json';

Shopware.Module.register('omnicore-ai-import', {
    type: 'plugin',
    name: 'omnicore-ai-import',
    title: 'omnicore-ai-import.general.mainMenuItemGeneral',
    description: 'omnicore-ai-import.general.descriptionTextModule',
    color: '#0f766e',
    icon: 'regular-products',

    snippets: {
        'de-DE': deDE,
        'en-GB': enGB,
    },

    routes: {
        index: {
            component: 'omnicore-ai-import-list',
            path: 'index',
            meta: {
                privilege: 'omnicore_connector:read',
            },
        },
    },

    navigation: [{
        id: 'omnicore-ai-import',
        label: 'omnicore-ai-import.general.mainMenuItemGeneral',
        color: '#0f766e',
        path: 'omnicore.ai.import.index',
        icon: 'regular-products',
        parent: 'sw-catalogue',
        position: 100,
        privilege: 'omnicore_connector:read',
    }],
});
