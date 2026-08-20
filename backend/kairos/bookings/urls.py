from django.urls import path

from .views import BookingCollectionView, BookingDetailView

urlpatterns = [
    path("bookings", BookingCollectionView.as_view(), name="booking-collection"),
    path("bookings/<uuid:pk>", BookingDetailView.as_view(), name="booking-detail"),
]
