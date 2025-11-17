from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from api.views import websocket_test

urlpatterns = [
    path('admin/', admin.site.urls),
    path('test/', websocket_test, name='websocket_test'),
    path('ws/', include('api.routing.websocket_urlpatterns')),  # Updated this line
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)