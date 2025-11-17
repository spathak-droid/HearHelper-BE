from django.shortcuts import render
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt

@csrf_exempt  # For testing purposes only
@require_http_methods(["GET"])
def websocket_test(request):
    return render(request, 'websocket_test.html')