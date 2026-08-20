from django.urls import path

from .views import (
    BookingCancelView,
    BookingCollectionView,
    BookingDetailView,
    BookingHistoryView,
)

urlpatterns = [
    path("bookings", BookingCollectionView.as_view(), name="booking-collection"),
    path("bookings/<uuid:pk>", BookingDetailView.as_view(), name="booking-detail"),
    path("bookings/<uuid:pk>/cancel", BookingCancelView.as_view(), name="booking-cancel"),
    path("bookings/<uuid:pk>/history", BookingHistoryView.as_view(), name="booking-history"),
]
