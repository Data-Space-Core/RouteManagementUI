from __future__ import annotations

import base64
import hashlib
import json
import secrets
from pathlib import Path
from urllib.parse import urlparse
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.client import ApiException


def keycloak_realm_base() -> str:
    return f"{settings.KEYCLOAK_BASE_URL}/realms/{settings.KEYCLOAK_REALM}"


def callback_url() -> str:
    return f"{settings.SITE_URL}/oidc/callback/"


def build_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def access_token(request: HttpRequest) -> str | None:
    return request.session.get("access_token")


def ensure_authenticated(request: HttpRequest) -> str | None:
    token = access_token(request)
    if token:
        return token
    return None


def management_api_request(method: str, path: str, token: str, **kwargs: object) -> requests.Response:
    headers = kwargs.pop("headers", {})
    merged_headers = {"Authorization": f"Bearer {token}", **headers}
    return requests.request(
        method,
        f"{settings.MANAGEMENT_API_BASE_URL}{path}",
        headers=merged_headers,
        timeout=60,
        **kwargs,
    )


def load_routes(token: str) -> tuple[list[dict], str | None]:
    response = management_api_request("GET", "/routes", token)
    if response.status_code == 200:
        return response.json(), None
    return [], response.text


SYSTEM_NAMESPACES = {"kube-system", "argocd", "route-management-ui"}
KUBERNETES_DISCOVERY_ERROR = "Unable to load tenant cluster applications and services."
_core_api: k8s_client.CoreV1Api | None = None
_apps_api: k8s_client.AppsV1Api | None = None
_version_text: str | None = None


def core_api() -> k8s_client.CoreV1Api:
    global _core_api
    if _core_api is None:
        k8s_config.load_incluster_config()
        _core_api = k8s_client.CoreV1Api()
    return _core_api


def apps_api() -> k8s_client.AppsV1Api:
    global _apps_api
    if _apps_api is None:
        k8s_config.load_incluster_config()
        _apps_api = k8s_client.AppsV1Api()
    return _apps_api


def include_namespace(name: str) -> bool:
    return name not in SYSTEM_NAMESPACES


def preferred_app_name(metadata: object) -> str:
    labels = getattr(metadata, "labels", {}) or {}
    return (
        labels.get("app.kubernetes.io/instance")
        or labels.get("app.kubernetes.io/name")
        or labels.get("app")
        or labels.get("k8s-app")
        or getattr(metadata, "name", "")
    )


def load_cluster_catalog() -> tuple[dict[str, object], str | None]:
    try:
        namespaces = sorted(
            item.metadata.name
            for item in core_api().list_namespace().items
            if item.metadata and item.metadata.name and include_namespace(item.metadata.name)
        )

        applications_by_key: dict[tuple[str, str], dict[str, str]] = {}
        for workload in apps_api().list_deployment_for_all_namespaces().items:
            namespace = workload.metadata.namespace if workload.metadata else ""
            if not include_namespace(namespace):
                continue
            app_name = preferred_app_name(workload.metadata)
            if app_name:
                applications_by_key[(namespace, app_name)] = {
                    "name": app_name,
                    "namespace": namespace,
                    "kind": "Deployment",
                }
        for workload in apps_api().list_stateful_set_for_all_namespaces().items:
            namespace = workload.metadata.namespace if workload.metadata else ""
            if not include_namespace(namespace):
                continue
            app_name = preferred_app_name(workload.metadata)
            if app_name:
                applications_by_key[(namespace, app_name)] = {
                    "name": app_name,
                    "namespace": namespace,
                    "kind": "StatefulSet",
                }

        services: list[dict[str, object]] = []
        for service in core_api().list_service_for_all_namespaces().items:
            namespace = service.metadata.namespace if service.metadata else ""
            service_name = service.metadata.name if service.metadata else ""
            if not include_namespace(namespace) or not service_name or service_name == "kubernetes":
                continue
            selector = service.spec.selector or {}
            app_name = (
                selector.get("app.kubernetes.io/instance")
                or selector.get("app.kubernetes.io/name")
                or selector.get("app")
                or selector.get("k8s-app")
                or service_name
            )
            ports = [
                {"name": port.name or str(port.port), "port": int(port.port)}
                for port in (service.spec.ports or [])
            ]
            services.append(
                {
                    "name": service_name,
                    "namespace": namespace,
                    "application": app_name,
                    "ports": ports,
                    "default_port": ports[0]["port"] if ports else 80,
                }
            )
            applications_by_key.setdefault(
                (namespace, app_name),
                {"name": app_name, "namespace": namespace, "kind": "Service"},
            )

        applications = sorted(
            applications_by_key.values(),
            key=lambda item: (str(item["namespace"]), str(item["name"])),
        )
        services.sort(key=lambda item: (str(item["namespace"]), str(item["name"])))
        initial_namespace = services[0]["namespace"] if services else (namespaces[0] if namespaces else "")
        initial_service = next(
            (service for service in services if service["namespace"] == initial_namespace),
            services[0] if services else None,
        )
        initial_ports = initial_service["ports"] if initial_service else []
        return {
            "namespaces": namespaces,
            "applications": applications,
            "services": services,
            "initial_namespace": initial_namespace,
            "initial_service_name": initial_service["name"] if initial_service else "",
            "initial_ports": initial_ports,
            "applications_json": json.dumps(applications),
            "services_json": json.dumps(services),
        }, None
    except (ApiException, RuntimeError, ValueError) as exc:
        return {
            "namespaces": [],
            "applications": [],
            "services": [],
            "initial_namespace": "",
            "initial_service_name": "",
            "initial_ports": [],
            "applications_json": "[]",
            "services_json": "[]",
        }, f"{KUBERNETES_DISCOVERY_ERROR} {exc}"


def app_version() -> str:
    global _version_text
    if _version_text is None:
        version_path = Path(__file__).resolve().parents[1] / "VERSION"
        try:
            _version_text = version_path.read_text(encoding="utf-8").strip() or "unknown"
        except OSError:
            _version_text = "unknown"
    return _version_text


def default_hostname() -> str:
    return urlparse(settings.SITE_URL).hostname or ""


def user_label(user: dict) -> str:
    return user.get("preferred_username") or user.get("email") or user.get("name") or "authenticated-user"


def common_template_context(request: HttpRequest) -> dict[str, object]:
    return {
        "app_version": app_version(),
        "default_hostname": default_hostname(),
        "user": request.session.get("user", {}),
        "user_label": user_label(request.session.get("user", {})),
    }


def build_route_form_context(
    request: HttpRequest,
    token: str,
    route_name: str | None = None,
) -> tuple[dict[str, object], str | None]:
    cluster_catalog, discovery_error = load_cluster_catalog()
    routes, api_error = load_routes(token)
    form_route: dict[str, object] | None = None
    if route_name:
        form_route = next((route for route in routes if route.get("route_name") == route_name), None)
        if form_route is None:
            return {}, f"Route {route_name} was not found."

    route_definition_text = ""
    if form_route and form_route.get("route_definition") is not None:
        route_definition_text = json.dumps(form_route["route_definition"], indent=2)

    context = {
        "api_error": api_error,
        "discovery_error": discovery_error,
        "is_edit": form_route is not None,
        "form_route": form_route or {},
        "route_definition_text": route_definition_text,
        **common_template_context(request),
        **cluster_catalog,
    }
    return context, None


@require_GET
def login_view(request: HttpRequest) -> HttpResponse:
    state = secrets.token_urlsafe(24)
    code_verifier, code_challenge = build_pkce_pair()
    request.session["oidc_state"] = state
    request.session["oidc_code_verifier"] = code_verifier
    params = {
        "client_id": settings.KEYCLOAK_CLIENT_ID,
        "response_type": "code",
        "scope": settings.KEYCLOAK_SCOPE,
        "redirect_uri": callback_url(),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return redirect(f"{keycloak_realm_base()}/protocol/openid-connect/auth?{urlencode(params)}")


@require_GET
def oidc_callback(request: HttpRequest) -> HttpResponse:
    expected_state = request.session.get("oidc_state")
    code_verifier = request.session.get("oidc_code_verifier")
    returned_state = request.GET.get("state")
    code = request.GET.get("code")
    if not expected_state or returned_state != expected_state or not code or not code_verifier:
        messages.error(request, "OIDC login failed.")
        return redirect("index")

    token_response = requests.post(
        f"{keycloak_realm_base()}/protocol/openid-connect/token",
        data={
            "grant_type": "authorization_code",
            "client_id": settings.KEYCLOAK_CLIENT_ID,
            "client_secret": settings.KEYCLOAK_CLIENT_SECRET,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": callback_url(),
        },
        timeout=30,
    )
    if token_response.status_code != 200:
        messages.error(request, "Token exchange failed.")
        return redirect("index")

    token_data = token_response.json()
    token = token_data["access_token"]
    userinfo = requests.get(
        f"{keycloak_realm_base()}/protocol/openid-connect/userinfo",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    if userinfo.status_code != 200:
        messages.error(request, "Unable to load user profile.")
        return redirect("index")

    request.session["access_token"] = token
    request.session["refresh_token"] = token_data.get("refresh_token")
    request.session["user"] = userinfo.json()
    request.session.pop("oidc_state", None)
    request.session.pop("oidc_code_verifier", None)
    return redirect("index")


@require_GET
def logout_view(request: HttpRequest) -> HttpResponse:
    request.session.flush()
    params = {
        "client_id": settings.KEYCLOAK_CLIENT_ID,
        "post_logout_redirect_uri": settings.SITE_URL + "/",
    }
    return redirect(f"{keycloak_realm_base()}/protocol/openid-connect/logout?{urlencode(params)}")


@require_GET
def index(request: HttpRequest) -> HttpResponse:
    token = ensure_authenticated(request)
    if not token:
        return render(request, "routes_ui/login.html")

    routes, api_error = load_routes(token)
    return render(
        request,
        "routes_ui/index.html",
        {
            "routes": routes,
            "api_error": api_error,
            **common_template_context(request),
        },
    )


@require_GET
def route_form(request: HttpRequest, route_name: str | None = None) -> HttpResponse:
    token = ensure_authenticated(request)
    if not token:
        return render(request, "routes_ui/login.html")

    context, route_error = build_route_form_context(request, token, route_name)
    if route_error:
        messages.error(request, route_error)
        return redirect("index")
    return render(request, "routes_ui/route_form.html", context)


@require_POST
def create_route(request: HttpRequest) -> HttpResponse:
    token = ensure_authenticated(request)
    if not token:
        return redirect("login")

    payload = {
        "application": request.POST.get("application", "").strip(),
        "route_name": request.POST.get("route_name", "").strip(),
        "original_route_name": request.POST.get("original_route_name", "").strip(),
        "service_name": request.POST.get("service_name", "").strip(),
        "service_namespace": request.POST.get("service_namespace", "default").strip() or "default",
        "service_port": int(request.POST.get("service_port", "80").strip() or "80"),
    }
    path_prefix = request.POST.get("path_prefix", "").strip()
    hostname = request.POST.get("hostname", "").strip()
    host_service_name = request.POST.get("host_service_name", "").strip()
    route_definition_text = request.POST.get("route_definition_text", "").strip()
    if path_prefix:
        payload["path_prefix"] = path_prefix
    if hostname:
        payload["hostname"] = hostname
    if host_service_name:
        payload["host_service_name"] = host_service_name
    if route_definition_text:
        payload["route_definition_text"] = route_definition_text

    if not payload["route_name"]:
        payload.pop("route_name")
    if not payload["original_route_name"]:
        payload.pop("original_route_name")

    route_label = payload.get("route_name") or payload["application"]
    if request.POST.get("original_route_name", "").strip():
        response = management_api_request(
            "PUT",
            f"/routes/{request.POST.get('original_route_name', '').strip()}",
            token,
            json=payload,
        )
    else:
        response = management_api_request("POST", "/routes", token, json=payload)

    if response.status_code in (200, 201):
        messages.success(request, f"Route {route_label} saved.")
    else:
        messages.error(request, f"Route save failed: {response.text}")
        redirect_name = "edit_route" if request.POST.get("original_route_name", "").strip() else "new_route"
        if redirect_name == "edit_route":
            return redirect(redirect_name, route_name=request.POST.get("original_route_name", "").strip())
        return redirect(redirect_name)
    return redirect("index")


@require_POST
def delete_route(request: HttpRequest, route_name: str) -> HttpResponse:
    token = ensure_authenticated(request)
    if not token:
        return redirect("login")

    response = management_api_request("DELETE", f"/routes/{route_name}", token)
    if response.status_code == 200:
        messages.success(request, f"Route {route_name} deleted.")
    else:
        messages.error(request, f"Route delete failed: {response.text}")
    return redirect("index")
