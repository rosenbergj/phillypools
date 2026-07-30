from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from pools.models import Pool


class PoolSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.8

    def items(self):
        return Pool.objects.filter(is_active=True)

    def lastmod(self, obj):
        return obj.last_updated


class StaticViewSitemap(Sitemap):
    """The hand-written pages. Without this the homepage — the most important URL
    on the site — was absent from the sitemap entirely."""

    changefreq = "daily"
    priority = 1.0

    def items(self):
        return ["index"]

    def location(self, item):
        return reverse(item)
