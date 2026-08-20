from django.urls import path

from .views import DevMockLoginView, TokenExchangeView

urlpatterns = [
    path("auth/token", TokenExchangeView.as_view(), name="auth-token"),
    path("auth/dev-mock-login", DevMockLoginView.as_view(), name="auth-dev-mock-login"),
]
