from django.urls import path

from .views import DevMockLoginView, TokenExchangeView, UserDeactivateView

urlpatterns = [
    path("auth/token", TokenExchangeView.as_view(), name="auth-token"),
    path("auth/dev-mock-login", DevMockLoginView.as_view(), name="auth-dev-mock-login"),
    path(
        "admin/users/<uuid:pk>/deactivate",
        UserDeactivateView.as_view(),
        name="user-deactivate",
    ),
]
