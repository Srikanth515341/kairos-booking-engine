from django.urls import path

from .views import (
    ResourceAdminGrantCollectionView,
    ResourceAdminGrantDetailView,
    ResourceAvailabilityView,
    ResourceCollectionView,
    ResourceDetailView,
    ResourceUtilizationView,
)

urlpatterns = [
    path("resources", ResourceCollectionView.as_view(), name="resource-collection"),
    path("resources/<uuid:pk>", ResourceDetailView.as_view(), name="resource-detail"),
    path(
        "resources/<uuid:pk>/availability",
        ResourceAvailabilityView.as_view(),
        name="resource-availability",
    ),
    path(
        "resources/<uuid:pk>/admins",
        ResourceAdminGrantCollectionView.as_view(),
        name="resource-admin-grant-collection",
    ),
    path(
        "resources/<uuid:pk>/admins/<uuid:user_id>",
        ResourceAdminGrantDetailView.as_view(),
        name="resource-admin-grant-detail",
    ),
    path(
        "admin/resources/<uuid:pk>/utilization",
        ResourceUtilizationView.as_view(),
        name="resource-utilization",
    ),
]
