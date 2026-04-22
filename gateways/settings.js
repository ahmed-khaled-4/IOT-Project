// Node-RED settings for the Phase 2 Floor Gateway containers.
//
// Each gateway-fNN container mounts this file at /data/settings.js.
// FLOOR_ID is injected via the docker-compose environment.

module.exports = {
    flowFile: 'flows.json',
    flowFilePretty: true,

    uiPort: process.env.PORT || 1880,
    uiHost: '0.0.0.0',

    credentialSecret: 'campus-phase2-credential-secret-change-me',

    logging: {
        console: {
            level: 'info',
            metrics: false,
            audit: false,
        },
    },

    functionGlobalContext: {
        floorId: process.env.FLOOR_ID || '01',
        hivemqHost: process.env.HIVEMQ_HOST || 'hivemq',
        hivemqPort: parseInt(process.env.HIVEMQ_PORT || '1883', 10),
        engineHost: process.env.ENGINE_HOST || 'engine',
        dupCacheSize: 1000,
    },

    editorTheme: {
        projects: { enabled: false },
        palette: { editable: false },
        header: {
            title: `Phase 2 Gateway F${process.env.FLOOR_ID || '01'}`,
        },
    },

    contextStorage: {
        default: 'memoryOnly',
        memoryOnly: { module: 'memory' },
    },
};
