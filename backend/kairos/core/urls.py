from django.urls import path

from .views import AdminChecksLatestView

urlpatterns = [
    path("admin/checks/latest", AdminChecksLatestView.as_view(), name="admin-checks-latest"),
]
