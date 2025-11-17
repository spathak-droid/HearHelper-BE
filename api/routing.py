from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'^ws/hat/?$', consumers.ChatConsumer.as_asgi()),  # Added ^ to match from start
]