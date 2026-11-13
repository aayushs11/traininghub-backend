import os

from django.db.models import Prefetch
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.decorators import action
from rest_framework.filters import SearchFilter
from rest_framework.generics import (
    CreateAPIView,
    GenericAPIView,
    RetrieveAPIView,
    RetrieveUpdateAPIView,
    RetrieveUpdateDestroyAPIView,
    UpdateAPIView,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.throttling import IdentityScopedRateThrottle
from apps.common.viewsets import (
    ActionAPIView,
    CreateListDestroyViewSet,
    CreateListRetrieveUpdateViewSet,
    CreateListUpdateDestroyViewSet,
    CreateListUpdateViewSet,
    DestroyViewSet,
    ListViewSet,
    ReadOnlyViewSet,
)
from apps.institutes import services
from apps.institutes.api.v1.serializers import (
    AcceptInvitationSerializer,
    GalleryImageSerializer,
    GalleryReorderSerializer,
    InstituteActionRequiredSerializer,
    InstituteCEOSerializer,
    InstituteContactSerializer,
    InstituteCoverSerializer,
    InstituteDocumentSerializer,
    InstituteLocationSerializer,
    InstituteProfileSerializer,
    InstituteRegistrationSerializer,
    InstituteSocialLinkSerializer,
    InvitationSerializer,
    PublicInstituteDetailSerializer,
    PublicInstituteListSerializer,
    StaffSerializer,
)
from apps.institutes.constants import InstituteStatus
from apps.institutes.models import (
    Institute,
    InstituteCEO,
    InstituteContact,
    InstituteDocument,
    InstituteGalleryImage,
    InstituteInvitation,
    InstituteLocation,
    InstituteMember,
    InstituteSocialLink,
)
from apps.institutes.permissions import (
    InstituteScopedMixin,
    IsInstituteMember,
    IsInstituteOwner,
)
from apps.training.services import annotate_institute_public_counts
from apps.users.api.v1.serializers import (
    RegistrationOTPSerializer,
    RegistrationOTPVerifySerializer,
)


def private_file_response(document):
    """Stream a private document. Documents never get a URL."""
    return FileResponse(
        document.file.open("rb"),
        as_attachment=True,
        filename=os.path.basename(document.file.name),
    )


# public
class RegisterView(CreateAPIView):
    serializer_class = InstituteRegistrationSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "institute_register"


class RegisterSendOTPView(ActionAPIView):
    """Step 1: email a verification code to the institute's own address."""

    serializer_class = RegistrationOTPSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [IdentityScopedRateThrottle]
    throttle_scope = "register_otp"


class RegisterVerifyOTPView(ActionAPIView):
    """Optional step 2: check the code before the form is submitted (it is checked again, and
    used up, by RegisterView)."""

    serializer_class = RegistrationOTPVerifySerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [IdentityScopedRateThrottle]
    throttle_scope = "register_otp_verify"


class AcceptInvitationView(CreateAPIView):
    serializer_class = AcceptInvitationSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_scope = "invitation_accept"


class PublicInstituteViewSet(ReadOnlyViewSet):
    """Approved institutes only. A constant number of queries, whatever the page size."""

    permission_classes = []  # public
    lookup_field = "slug"
    filter_backends = (DjangoFilterBackend, SearchFilter)
    filterset_fields = ("type",)
    search_fields = ("name",)

    def get_serializer_class(self):
        if self.action == "retrieve":
            return PublicInstituteDetailSerializer
        return PublicInstituteListSerializer

    def get_queryset(self):
        queryset = (
            Institute.objects.filter(status=InstituteStatus.APPROVED)
            .prefetch_related(
                Prefetch(
                    "locations",
                    queryset=InstituteLocation.objects.filter(is_active=True)
                    .select_related(
                        "location", "location__district", "location__province"
                    )
                    .order_by("-is_main", "pk"),
                ),
                Prefetch(
                    "gallery",
                    queryset=InstituteGalleryImage.objects.order_by("position", "pk"),
                ),
            )
            .order_by("name", "pk")
        )
        if self.action == "retrieve":  # only the detail page shows these
            queryset = queryset.select_related("contact", "ceo").prefetch_related(
                "social_links"
            )
        return annotate_institute_public_counts(queryset)


# portal (the institute's own staff)
class InstituteProfileView(RetrieveUpdateAPIView):
    serializer_class = InstituteProfileSerializer
    permission_classes = [IsInstituteMember]
    http_method_names = ["get", "patch", "head", "options"]
    queryset = Institute.objects.select_related("contact", "ceo").prefetch_related(
        "social_links"
    )

    def get_object(self):
        return get_object_or_404(
            self.get_queryset(), pk=self.request.user.membership.institute_id
        )


class PortalCoverView(UpdateAPIView):
    """Upload (or replace) the institute's cover image: PUT multipart `cover_img`."""

    serializer_class = InstituteCoverSerializer
    permission_classes = [IsInstituteMember]
    http_method_names = ["put", "head", "options"]

    def get_object(self):
        return self.request.user.membership.institute


class ResubmitView(GenericAPIView):
    """Answer an info request, or re-apply after a rejection."""

    permission_classes = [IsInstituteMember]
    serializer_class = InstituteProfileSerializer

    def post(self, request):
        institute = services.resubmit(
            request.user.membership.institute, by=request.user
        )
        return Response(self.get_serializer(institute).data)


class PortalLocationViewSet(InstituteScopedMixin, CreateListRetrieveUpdateViewSet):
    serializer_class = InstituteLocationSerializer
    permission_classes = [IsInstituteMember]
    lookup_value_regex = r"[0-9]+"
    http_method_names = ["get", "post", "patch", "head", "options"]
    queryset = InstituteLocation.objects.select_related(
        "location", "location__district", "location__province"
    ).order_by("-is_main", "pk")

    @action(detail=True, methods=["post"], url_path="set-main")
    def set_main(self, request, pk=None):
        location = services.set_main_location(self.get_object())
        return Response(self.get_serializer(location).data)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        location = services.deactivate_location(self.get_object())
        return Response(self.get_serializer(location).data)


class PortalDocumentViewSet(InstituteScopedMixin, CreateListUpdateViewSet):
    serializer_class = InstituteDocumentSerializer
    permission_classes = [IsInstituteMember]
    lookup_value_regex = r"[0-9]+"
    http_method_names = ["get", "post", "patch", "head", "options"]
    queryset = InstituteDocument.objects.order_by("-created_at", "-pk")


class PortalDocumentDownloadView(APIView):
    permission_classes = [IsInstituteMember]

    def get(self, request, pk):
        return private_file_response(
            get_object_or_404(
                InstituteDocument,
                pk=pk,
                institute_id=request.user.membership.institute_id,
            )
        )


class PortalGalleryViewSet(InstituteScopedMixin, CreateListUpdateDestroyViewSet):
    serializer_class = GalleryImageSerializer
    permission_classes = [IsInstituteMember]
    lookup_value_regex = r"[0-9]+"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    queryset = InstituteGalleryImage.objects.order_by("position", "pk")

    @action(detail=False, methods=["post"])
    def reorder(self, request):
        serializer = GalleryReorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.reorder_gallery(
            request.user.membership.institute, serializer.validated_data["ids"]
        )
        return Response(self.get_serializer(self.get_queryset(), many=True).data)


class PortalStaffViewSet(InstituteScopedMixin, ListViewSet, DestroyViewSet):
    serializer_class = StaffSerializer
    permission_classes = [IsInstituteOwner]
    lookup_value_regex = r"[0-9]+"
    queryset = InstituteMember.objects.select_related("user").order_by("role", "pk")

    def perform_destroy(self, instance):
        services.remove_staff(instance, by=self.request.user)


class PortalInvitationViewSet(InstituteScopedMixin, CreateListDestroyViewSet):
    serializer_class = InvitationSerializer
    permission_classes = [IsInstituteOwner]
    lookup_value_regex = r"[0-9]+"
    queryset = InstituteInvitation.objects.order_by("-created_at", "-pk")

    def perform_destroy(self, instance):
        services.revoke_invitation(instance, by=self.request.user)


class PortalContactView(RetrieveUpdateAPIView):
    serializer_class = InstituteContactSerializer
    permission_classes = [IsInstituteMember]
    http_method_names = ["get", "patch", "head", "options"]

    def get_object(self):
        # no prefetch of the social links: the website is read after an update, and a prefetch
        # made before it would still hold the old link
        return get_object_or_404(
            InstituteContact.objects.select_related("institute"),
            institute_id=self.request.user.membership.institute_id,
        )


class PortalCEOView(RetrieveUpdateDestroyAPIView):
    serializer_class = InstituteCEOSerializer
    permission_classes = [IsInstituteMember]
    http_method_names = ["get", "put", "delete", "head", "options"]

    def get_object(self):
        institute_id = self.request.user.membership.institute_id
        if self.request.method == "PUT":  # PUT creates the row when there is none
            return InstituteCEO.objects.filter(
                institute_id=institute_id
            ).first() or InstituteCEO(institute_id=institute_id)
        return get_object_or_404(InstituteCEO, institute_id=institute_id)


class PortalSocialLinkViewSet(InstituteScopedMixin, CreateListUpdateDestroyViewSet):
    serializer_class = InstituteSocialLinkSerializer
    permission_classes = [IsInstituteMember]
    lookup_value_regex = r"[0-9]+"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    queryset = InstituteSocialLink.objects.order_by("pk")


class InstituteActionRequiredView(RetrieveAPIView):
    """Profile completeness of the caller's own institute, all sections in one query. Each key is
    `true` when the section is filled in and `false` when the institute still has to fill it."""

    serializer_class = InstituteActionRequiredSerializer
    permission_classes = [IsInstituteMember]
    queryset = services.annotate_profile_completion(Institute.objects.only("id"))

    def get_object(self):
        return get_object_or_404(
            self.get_queryset(), pk=self.request.user.membership.institute_id
        )
