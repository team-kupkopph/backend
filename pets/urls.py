from django.urls import path

from pets.views import MyPetDetailView, MyPetsView

urlpatterns = [
    path("me/pets", MyPetsView.as_view()),
    path("me/pets/<uuid:pet_id>", MyPetDetailView.as_view()),   # AQ8
]
