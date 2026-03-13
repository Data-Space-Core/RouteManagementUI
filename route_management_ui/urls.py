from django.contrib import admin
from django.urls import path

from routes_ui import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.index, name="index"),
    path("login/", views.login_view, name="login"),
    path("oidc/callback/", views.oidc_callback, name="oidc_callback"),
    path("logout/", views.logout_view, name="logout"),
    path("routes/create/", views.create_route, name="create_route"),
    path("routes/<str:application>/delete/", views.delete_route, name="delete_route"),
]

