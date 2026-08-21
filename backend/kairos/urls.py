from django.urls import URLPattern, URLResolver, include, path

from kairos.core.views import AdminDashboardPageView

urlpatterns: list[URLPattern | URLResolver] = [
    path("api/v1/", include("kairos.identity.urls")),
    path("api/v1/", include("kairos.bookings.urls")),
    path("api/v1/", include("kairos.resources.urls")),
    path("api/v1/", include("kairos.waitlist.urls")),
    path("api/v1/", include("kairos.core.urls")),
    # Deliberately outside /api/v1 — a browser-rendered HTML page (self-
    # contained client-side poller against the JSON API above), not
    # another JSON endpoint (Implementation Plan Phase 21).
    path("admin/dashboard/", AdminDashboardPageView.as_view(), name="admin-dashboard-page"),
]
