"""GET/POST /me/pets and GET/PATCH/DELETE /me/pets/{id}. Moved from listings/views.py in My Pets
review step 10; the URLs and responses are unchanged. Writes go through `pets.service`."""
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from pets import service
from pets.representations import pet_fields, pet_summary, rehomed_row
from pets.serializers import PetCreateSerializer, PetPatchSerializer, PetRemoveSerializer


def _refused(e):
    error = {"code": e.code, "message": e.message}
    if e.details is not None:
        error["details"] = e.details
    return Response({"error": error}, status=e.status)


class MyPetsView(APIView):
    """GET /me/pets — US-H3. The owner's own pets, newest first, each with its primary
    photo (or earliest-uploaded if none is primary) for the mobile My-pets tab.
    Each row carries `open_listing` and `lost_report_id` (PP14) so the card can say what the pet is
    in the middle of. `rehomed` (PQ2) is the read-only trace of pets this account rehomed through
    Kupkop: the name and photo as they were at the transfer, never the new owner's later edits.
    POST /me/pets — AQ8 · an owner adds a pet they already have (before: the only Pet writer was
    a placement Accept), so they can keep its details and rehome it later. PQ3 · not a shelter:
    its animals are listings (D7)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        pets = list(request.user.pets.current().order_by("-created_at").prefetch_related("photos"))
        states = service.pet_states(pets)
        return Response({"results": [{**pet_summary(p), **states[p.pk]} for p in pets],
                         "rehomed": [rehomed_row(h) for h in service.rehomed_by(request.user)]})

    def post(self, request):
        if request.user.account_type == "shelter":        # refused before the body is read
            return _refused(service.PetRefused("not_for_shelters", "A shelter's animals are its listings", 403))
        s = PetCreateSerializer(data=request.data, context={"request": request})
        s.is_valid(raise_exception=True)
        try:
            pet = service.create_pet(request.user, s.validated_data)
        except service.PetRefused as e:
            return _refused(e)
        return Response({"pet_id": str(pet.pk)}, status=201)


class MyPetDetailView(APIView):
    """GET /me/pets/{id} — AQ8 · everything the owner entered, for the pet screen and for
    prefilling a rehoming listing, plus its open rehoming listing and open lost report, if any.
    Private: another account's pet, or an archived one, is a 404.
    PATCH — PP12 · any subset; `photos` present replaces the set. The species can't change while
    an open listing describes the animal (PP3).
    DELETE — PP12 / PQ1 · archive with a reason (passed_away, given_away, other); never while an
    open rehoming listing exists. Locking is described in pets/service.py."""
    permission_classes = [IsAuthenticated]

    def get(self, request, pet_id):
        pet = request.user.pets.current().prefetch_related("photos").filter(pk=pet_id).first()
        if pet is None:
            return _refused(service.no_such_pet())
        return Response({**pet_fields(pet), **service.pet_states([pet])[pet.pk]})

    def patch(self, request, pet_id):
        def validate(pet):
            s = PetPatchSerializer(data=request.data, context={"request": request, "pet": pet})
            s.is_valid(raise_exception=True)
            return s.validated_data
        try:
            pet = service.update_pet(request.user, pet_id, validate)
        except service.PetRefused as e:
            return _refused(e)
        return Response({"pet_id": str(pet.pk)})

    def delete(self, request, pet_id):
        s = PetRemoveSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        try:
            service.remove_pet(request.user, pet_id, s.validated_data["reason"])
        except service.PetRefused as e:
            return _refused(e)
        return Response(status=204)
