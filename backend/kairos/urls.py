from django.urls import URLPattern, URLResolver, include, path

urlpatterns: list[URLPattern | URLResolver] = [
    path("api/v1/", include("kairos.identity.urls")),
    path("api/v1/", include("kairos.bookings.urls")),
    path("api/v1/", include("kairos.resources.urls")),
]
