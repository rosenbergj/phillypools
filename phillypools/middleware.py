from django.http import HttpResponsePermanentRedirect


class WWWRedirectMiddleware:
    """301-redirects the www host to the bare apex domain, which is canonical everywhere else
    (canonical/og tags, robots.txt, sitemap.xml) but previously had no enforcing redirect."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host = request.get_host()
        if host.startswith('www.'):
            apex_host = host[len('www.'):]
            scheme = 'https' if request.is_secure() else 'http'
            return HttpResponsePermanentRedirect(f'{scheme}://{apex_host}{request.get_full_path()}')
        return self.get_response(request)
