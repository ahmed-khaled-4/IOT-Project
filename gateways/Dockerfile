# Floor gateway image: Node-RED + CoAP palette (npm install at build; avoids missing `coap request` at runtime).
FROM nodered/node-red:3.1

USER root
COPY package.json /data/package.json
RUN cd /data \
    && npm install --omit=dev --no-audit --no-fund \
    && chown -R node-red:node-red /data/node_modules /data/package.json

USER node-red
