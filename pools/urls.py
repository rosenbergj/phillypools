from django.urls import path
from pools import views

urlpatterns = [
    path("", views.index, name="index"),
    path("neighborhood-at/", views.neighborhood_at, name="neighborhood_at"),
    path("pools/<int:pk>/", views.pool_detail, name="pool_detail"),
    path("submit/thanks/", views.submit_thanks, name="submit_thanks"),
    path("submit/", views.submit, name="submit"),
]
