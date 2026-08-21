from django.urls import path

from .views import (
    WaitlistEntryCancelView,
    WaitlistEntryCollectionView,
    WaitlistOfferConfirmView,
    WaitlistOfferDeclineView,
)

urlpatterns = [
    path(
        "waitlist-entries", WaitlistEntryCollectionView.as_view(), name="waitlist-entry-collection"
    ),
    path(
        "waitlist-entries/<uuid:pk>/cancel",
        WaitlistEntryCancelView.as_view(),
        name="waitlist-entry-cancel",
    ),
    path(
        "waitlist-offers/<uuid:pk>/confirm",
        WaitlistOfferConfirmView.as_view(),
        name="waitlist-offer-confirm",
    ),
    path(
        "waitlist-offers/<uuid:pk>/decline",
        WaitlistOfferDeclineView.as_view(),
        name="waitlist-offer-decline",
    ),
]
