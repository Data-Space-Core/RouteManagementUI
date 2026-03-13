# RouteManagementUI

`RouteManagementUI` is a Django web application for tenant-scoped route management.

## Features

- authenticates users against the tenant Keycloak realm
- serves the tenant console at `https://tenant-a.dil.collab-cloud.eu/route`
- uses the `ManagementAPI` under `https://dil.collab-cloud.eu/management`
- supports listing, creating, and deleting tenant routes

## Build

```bash
docker build -t ghcr.io/data-space-core/route-management-ui:latest .
docker push ghcr.io/data-space-core/route-management-ui:latest
```

## Deploy

1. Create a secret from `deploy/secret.example.yaml` with a real Django secret key and Keycloak client secret.
2. Commit the repo and point Argo CD at `deploy/`, or let `ManagementAPI` create the tenant Argo CD `Application`.

## Keycloak client

Import [keycloak/management-ui-client.json](/home/vmuser/RouteManagementUI/keycloak/management-ui-client.json) into a tenant realm, then copy the generated client secret into the Kubernetes secret used by the deployment.
