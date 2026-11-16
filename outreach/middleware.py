from django.contrib.auth.middleware import LoginRequiredMiddleware
from django.http import HttpResponse
from django.shortcuts import resolve_url


class HealthCheckMiddleware:
    """
    Serve /healthz/ before SecurityMiddleware and CommonMiddleware run.

    Platform health probes (Render, Docker, load balancers) may arrive over plain
    HTTP or with an internal Host header that is not in ALLOWED_HOSTS. Answering
    here keeps the probe working without loosening ALLOWED_HOSTS or the HTTPS redirect.
    """

    path = "/healthz/"

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == self.path:
            from outreach.views import healthcheck_view

            return healthcheck_view(request)
        return self.get_response(request)


class HtmxLoginRequiredMiddleware(LoginRequiredMiddleware):
    """
    Django's LoginRequiredMiddleware, plus a clean redirect for HTMX requests.

    Without this, an expired session would make HTMX swap the login page *into*
    a contact card. HX-Redirect tells HTMX to do a full-page navigation instead.
    """

    def handle_no_permission(self, request, view_func):
        if request.headers.get("HX-Request") == "true":
            response = HttpResponse(status=401)
            response["HX-Redirect"] = resolve_url(self.get_login_url(view_func))
            return response
        return super().handle_no_permission(request, view_func)
