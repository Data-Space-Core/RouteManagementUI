from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import requests
from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST


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


def user_label(user: dict) -> str:
    return user.get("preferred_username") or user.get("email") or user.get("name") or "authenticated-user"


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
            "user": request.session.get("user", {}),
            "user_label": user_label(request.session.get("user", {})),
        },
    )


@require_POST
def create_route(request: HttpRequest) -> HttpResponse:
    token = ensure_authenticated(request)
    if not token:
        return redirect("login")

    payload = {
        "application": request.POST.get("application", "").strip(),
        "service_name": request.POST.get("service_name", "").strip(),
        "service_namespace": request.POST.get("service_namespace", "default").strip() or "default",
        "service_port": int(request.POST.get("service_port", "80").strip() or "80"),
    }
    path_prefix = request.POST.get("path_prefix", "").strip()
    host_service_name = request.POST.get("host_service_name", "").strip()
    if path_prefix:
        payload["path_prefix"] = path_prefix
    if host_service_name:
        payload["host_service_name"] = host_service_name

    response = management_api_request("POST", "/routes", token, json=payload)
    if response.status_code == 201:
        messages.success(request, f"Route {payload['application']} saved.")
    else:
        messages.error(request, f"Route save failed: {response.text}")
    return redirect("index")


@require_POST
def delete_route(request: HttpRequest, application: str) -> HttpResponse:
    token = ensure_authenticated(request)
    if not token:
        return redirect("login")

    response = management_api_request("DELETE", f"/routes/{application}", token)
    if response.status_code == 200:
        messages.success(request, f"Route {application} deleted.")
    else:
        messages.error(request, f"Route delete failed: {response.text}")
    return redirect("index")

