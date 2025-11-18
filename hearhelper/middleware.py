from django.http import HttpResponse

class SimpleCORSHeadersMiddleware:
    """Add permissive CORS headers for local development."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == "OPTIONS":
            response = HttpResponse()
        else:
            response = self.get_response(request)

        response.setdefault("Access-Control-Allow-Origin", "*")
        response.setdefault(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, X-Requested-With",
        )
        response.setdefault(
            "Access-Control-Allow-Methods",
            "GET, POST, PUT, PATCH, DELETE, OPTIONS",
        )
        response.setdefault("Access-Control-Allow-Credentials", "true")
        return response
