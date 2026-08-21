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

# Implementation Plan Phase 22 — see kairos.core.error_handlers' own
# docstring: a URL that never resolves to any view previously produced a
# bare, unenveloped Django error page, the one gap in kairos.core.drf.
# kairos_exception_handler's "no bare error body" claim.
handler404 = "kairos.core.error_handlers.handler404"
handler500 = "kairos.core.error_handlers.handler500"
