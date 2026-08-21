from django.urls import path

from .views import AdminChecksLatestView, AdminDashboardView

urlpatterns = [
    path("admin/checks/latest", AdminChecksLatestView.as_view(), name="admin-checks-latest"),
    path("admin/dashboard", AdminDashboardView.as_view(), name="admin-dashboard"),
]
