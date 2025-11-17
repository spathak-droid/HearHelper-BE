import os
import django
from django.core.asgi import get_asgi_application
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from django.urls import re_path
from api import consumers

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'hearhelper.settings')
django.setup()

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": AuthMiddlewareStack(
        URLRouter([
            re_path(r'^ws/hat/?$', consumers.ChatConsumer.as_asgi()),
        ])
    ),
})