# ManagementUI

`ManagementUI` is a Django web application for the top-level management API.

## Features

- authenticates users against the Keycloak `master` realm
- serves the tenant console at `https://dil.collab-cloud.eu/`
- uses the `ManagementAPI` under `https://dil.collab-cloud.eu/management`
- supports listing, creating, and deleting tenants

## Build

```bash
docker build -t ghcr.io/data-space-core/management-ui:latest .
docker push ghcr.io/data-space-core/management-ui:latest
```

## Deploy

1. Create a secret from `deploy/secret.example.yaml` with a real Django secret key and Keycloak client secret.
2. Commit the repo and point Argo CD at `deploy/`.

## Keycloak client

Import [keycloak/management-ui-client.json](/home/vmuser/ManagementUI/keycloak/management-ui-client.json) into the `master` realm, then copy the generated client secret into the Kubernetes secret used by the deployment.
