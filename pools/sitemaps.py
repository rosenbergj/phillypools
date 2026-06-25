from django.contrib.sitemaps import Sitemap

from pools.models import Pool


class PoolSitemap(Sitemap):
    changefreq = "weekly"
    priority = 0.8

    def items(self):
        return Pool.objects.filter(is_active=True)

    def lastmod(self, obj):
        return obj.last_updated
