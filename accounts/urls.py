from django.urls import path

from . import views

urlpatterns = [
    path("signup/", views.signup, name="signup"),
    path("signin/", views.signin, name="signin"),
    path("models/", views.available_models, name="available_models"),
    path("voice/", views.update_voice, name="update_voice"),
    path("profile/photo-url/", views.profile_photo_upload_url, name="profile_photo_upload_url"),
]
