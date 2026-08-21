from django.urls import path

from .views import WaitlistEntryCancelView, WaitlistEntryCollectionView

urlpatterns = [
    path(
        "waitlist-entries", WaitlistEntryCollectionView.as_view(), name="waitlist-entry-collection"
    ),
    path(
        "waitlist-entries/<uuid:pk>/cancel",
        WaitlistEntryCancelView.as_view(),
        name="waitlist-entry-cancel",
    ),
]
