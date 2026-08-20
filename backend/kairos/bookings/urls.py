from django.urls import path

from .views import BookingCreateView

urlpatterns = [
    path("bookings", BookingCreateView.as_view(), name="booking-create"),
]
