from django.urls import path
from django.views.generic import TemplateView
from pools import views

urlpatterns = [
    path("robots.txt", TemplateView.as_view(template_name="pools/robots.txt", content_type="text/plain")),
    path("favicon.ico", views.favicon_ico, name="favicon"),
    path("", views.index, name="index"),
    path("neighborhood-at/", views.neighborhood_at, name="neighborhood_at"),
    path("pools-json/", views.pools_json, name="pools_json"),
    path("pools/<int:pk>/", views.pool_detail_pk_redirect, name="pool_detail_pk_redirect"),
    path("pools/<slug:slug>/", views.pool_detail, name="pool_detail"),
    path("pools/<int:pk>/like/", views.toggle_like, name="toggle_like"),
    path("submit/thanks/", views.submit_thanks, name="submit_thanks"),
    path("submit/", views.submit, name="submit"),
    path("pin-click/", views.record_pin_click, name="record_pin_click"),
    path("card-click/", views.record_card_click, name="record_card_click"),
    path("nearby-click/", views.record_nearby_click, name="record_nearby_click"),
    path("page-loaded/", views.record_page_view, name="record_page_view"),
    path("stats/", views.stats, name="stats"),
]
