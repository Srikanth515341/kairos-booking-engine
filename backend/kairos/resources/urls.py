from django.urls import path

from .views import ResourceAvailabilityView, ResourceCollectionView, ResourceDetailView

urlpatterns = [
    path("resources", ResourceCollectionView.as_view(), name="resource-collection"),
    path("resources/<uuid:pk>", ResourceDetailView.as_view(), name="resource-detail"),
    path(
        "resources/<uuid:pk>/availability",
        ResourceAvailabilityView.as_view(),
        name="resource-availability",
    ),
]
