from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import (
    Case,
    Count,
    F,
    Func,
    IntegerField,
    OuterRef,
    Prefetch,
    Subquery,
    Value,
    When,
)
from django.db.models.functions import Coalesce
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.filters import OrderingFilter, SearchFilter
from rest_framework.generics import GenericAPIView, get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.models.category import Category
from apps.common.viewsets import CreateListRetrieveUpdateViewSet
from apps.control_panel.api.v1 import selectors
from apps.control_panel.api.v1.filters import (
    AWAITING_REVIEW,
    AdminCategoryFilter,
    AdminInstituteFilter,
    AdminTrainingFilter,
)
from apps.control_panel.api.v1.serializers import (
    AdminCategoryOptionSerializer,
    AdminCategorySerializer,
    AdminDocumentSerializer,
    AdminInstituteListSerializer,
    AdminInstituteSerializer,
    AdminSerializer,
    AdminTrainingListSerializer,
    AdminTrainingSerializer,
    DocumentReviewSerializer,
    FeatureSerializer,
    ReasonSerializer,
)
from apps.enquiries import services as enquiry_services
from apps.enquiries.models import Enquiry
from apps.institutes import services as institute_services
from apps.institutes.api.v1.views import private_file_response
from apps.institutes.constants import InstituteStatus, MemberRole
from apps.institutes.models import (
    Institute,
    InstituteDocument,
    InstituteLocation,
    InstituteMember,
)
from apps.training import services as training_services
from apps.training.api.v1.views import with_content
from apps.training.constants import TrainingStatus
from apps.training.models import Training, TrainingStatusLog
from apps.users.constants import Role
from apps.users.permissions import HasPlatformPermission

User = get_user_model()


# admins
class AdminViewSet(CreateListRetrieveUpdateViewSet):
    """Super Admin only. No DELETE: suspend through user/users/{id}/status/."""

    serializer_class = AdminSerializer
    permission_classes = [HasPlatformPermission]
    required_permission = "users.manage_admins"
    queryset = (
        User.objects.filter(role=Role.ADMIN)
        .prefetch_related("user_permissions")
        .order_by("-created_at", "-pk")
    )
    http_method_names = ["get", "post", "patch", "head", "options"]


# categories
class AdminCategoryViewSet(CreateListRetrieveUpdateViewSet):
    """No DELETE: deactivate with PATCH {"is_active": false}."""

    serializer_class = AdminCategorySerializer
    permission_classes = [HasPlatformPermission]
    required_permission = "users.manage_categories"
    lookup_field = "slug"
    lookup_value_regex = r"[-a-zA-Z0-9_]+"
    http_method_names = ["get", "post", "patch", "head", "options"]
    filter_backends = (DjangoFilterBackend, SearchFilter)
    filterset_class = AdminCategoryFilter
    search_fields = ("name",)
    queryset = Category.objects.select_related("parent").order_by("name", "pk")

    def get_serializer_class(self):
        if self.action == "options_list":
            return AdminCategoryOptionSerializer
        return super().get_serializer_class()

    # named options_list: a method called `options` would replace the HTTP OPTIONS handler
    @action(detail=False, methods=["get"], url_path="options", pagination_class=None)
    def options_list(self, request):
        """Every category, hidden ones too, unpaginated, for dropdowns. Same filters as the list."""
        queryset = self.filter_queryset(self.get_queryset())
        return Response(self.get_serializer(queryset, many=True).data)


# institutes
def _trainings_count():
    """All trainings of the institute, any status. A correlated subquery: no join fan-out."""
    counted = (
        Training.objects.filter(institute=OuterRef("pk"))
        .order_by()
        .annotate(n=Func(F("pk"), function="COUNT", output_field=IntegerField()))
    )
    return Coalesce(Subquery(counted.values("n"), output_field=IntegerField()), 0)


def _pending_documents_count():
    """Documents of the institute waiting for an admin (new, or rejected and uploaded again)."""
    counted = (
        InstituteDocument.objects.filter(
            institute=OuterRef("pk"), status__in=AWAITING_REVIEW
        )
        .order_by()
        .annotate(n=Func(F("pk"), function="COUNT", output_field=IntegerField()))
    )
    return Coalesce(Subquery(counted.values("n"), output_field=IntegerField()), 0)


def _owner_prefetch():
    return Prefetch(
        "members",
        queryset=InstituteMember.objects.filter(role=MemberRole.OWNER).select_related(
            "user"
        ),
        to_attr="owner_members",
    )


def _locations_queryset():
    return InstituteLocation.objects.select_related(
        "location", "location__district", "location__province"
    ).order_by("-is_main", "pk")


class AdminInstituteViewSet(viewsets.ReadOnlyModelViewSet):
    """List: ?status=PENDING,INFO_REQUESTED (the Pending tab), ?type=, ?search=, ?ordering=.
    Tab counts: GET summary/. Review history: GET {slug}/activity/.
    Default order: waiting for review first, then newest."""

    permission_classes = [HasPlatformPermission]
    required_permission = "users.manage_institutes"
    lookup_field = "slug"
    lookup_value_regex = r"[-a-zA-Z0-9_]+"
    filter_backends = (DjangoFilterBackend, SearchFilter, OrderingFilter)
    filterset_class = AdminInstituteFilter
    search_fields = ("name", "registration_number", "pan_vat_number")
    ordering_fields = ("name", "created_at", "status_rank")
    ordering = ("status_rank", "-created_at", "-pk")

    def get_serializer_class(self):
        if self.action == "list":
            return AdminInstituteListSerializer
        return AdminInstituteSerializer

    def get_queryset(self):
        queryset = Institute.objects.annotate(
            # PENDING first, then INFO_REQUESTED, then the rest
            status_rank=Case(
                When(status=InstituteStatus.PENDING, then=Value(0)),
                When(status=InstituteStatus.INFO_REQUESTED, then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            )
        )
        if self.action == "list":
            return (
                queryset.select_related("contact")
                .prefetch_related(
                    Prefetch(
                        "locations", queryset=_locations_queryset().filter(is_active=True)
                    )
                )
                .annotate(
                    trainings_count=_trainings_count(),
                    pending_documents_count=_pending_documents_count(),
                )
            )
        return queryset.select_related("contact", "ceo").prefetch_related(
            "documents",
            "social_links",
            Prefetch("locations", queryset=_locations_queryset()),
            _owner_prefetch(),
        )

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Counts for the tabs. `pending` includes the institutes waiting for the owner's reply
        (INFO_REQUESTED). `by_status` has the raw counts. `documents_to_verify` is the number of
        approved institutes with an uploaded document still to review."""
        rows = Institute.objects.order_by().values("status").annotate(n=Count("pk"))
        by_status = {code: 0 for code, _ in InstituteStatus.CHOICES}
        by_status.update({row["status"]: row["n"] for row in rows})
        tabs = {
            "all": sum(by_status.values()),
            "pending": by_status[InstituteStatus.PENDING]
            + by_status[InstituteStatus.INFO_REQUESTED],
            "approved": by_status[InstituteStatus.APPROVED],
            "rejected": by_status[InstituteStatus.REJECTED],
            "suspended": by_status[InstituteStatus.SUSPENDED],
        }
        # approved institutes that uploaded a document after approval and wait for a review
        documents_to_verify = (
            InstituteDocument.objects.filter(
                status__in=AWAITING_REVIEW, institute__status=InstituteStatus.APPROVED
            )
            .values("institute")
            .distinct()
            .count()
        )
        return Response(
            {
                "tabs": tabs,
                "by_status": by_status,
                "documents_to_verify": documents_to_verify,
            }
        )

    @action(detail=True, methods=["get"])
    def activity(self, request, *args, **kwargs):
        """Registration, training submissions and status changes of the institute, newest first."""
        institute = get_object_or_404(
            Institute.objects.only("id", "created_at"), slug=kwargs["slug"]
        )
        return Response({"results": selectors.institute_activity(institute)})

    def _review(self, request, service, *, with_reason):
        kwargs = {}
        if with_reason:
            serializer = ReasonSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            kwargs["reason"] = serializer.validated_data["reason"]
        try:
            institute = service(self.get_object(), by=request.user, **kwargs)
        except DjangoValidationError as exc:  # a status rule or approval check -> a 400
            raise DRFValidationError(
                exc.message_dict if hasattr(exc, "error_dict") else {"detail": exc.messages}
            )
        # re-read through the viewset queryset so documents and owner are prefetched
        return Response(
            self.get_serializer(self.get_queryset().get(pk=institute.pk)).data
        )

    @action(detail=True, methods=["post"])
    def approve(self, request, *args, **kwargs):
        return self._review(request, institute_services.approve, with_reason=False)

    @action(detail=True, methods=["post"])
    def reject(self, request, *args, **kwargs):
        return self._review(request, institute_services.reject, with_reason=True)

    @action(detail=True, methods=["post"], url_path="request-info")
    def request_info(self, request, *args, **kwargs):
        return self._review(request, institute_services.request_info, with_reason=True)

    @action(detail=True, methods=["post"])
    def suspend(self, request, *args, **kwargs):
        return self._review(request, institute_services.suspend, with_reason=True)

    # Disabled until it is decided what suspending an institute means and whether it can be undone.
    # `institute_services.reinstate` and `InstituteStatus.TRANSITIONS[SUSPENDED]` are unchanged.
    # @action(detail=True, methods=["post"])
    # def reinstate(self, request, pk=None):
    #     return self._review(request, institute_services.reinstate, with_reason=False)


class AdminDocumentView(GenericAPIView):
    permission_classes = [HasPlatformPermission]
    required_permission = "users.manage_institutes"
    serializer_class = DocumentReviewSerializer
    queryset = (
        InstituteDocument.objects.none()
    )  # only for schema generation; patch() looks the row up itself

    def patch(self, request, institute_slug, pk):
        document = get_object_or_404(
            InstituteDocument, pk=pk, institute__slug=institute_slug
        )
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        document = institute_services.review_document(
            document, status=serializer.validated_data["status"], by=request.user
        )
        return Response(
            AdminDocumentSerializer(
                document, context=self.get_serializer_context()
            ).data
        )


class AdminDocumentDownloadView(APIView):
    permission_classes = [HasPlatformPermission]
    required_permission = "users.manage_institutes"

    def get(self, request, institute_slug, pk):
        return private_file_response(
            get_object_or_404(InstituteDocument, pk=pk, institute__slug=institute_slug)
        )


# trainings
class AdminTrainingViewSet(viewsets.ReadOnlyModelViewSet):
    """Trainings screen: all tabs. Training Approvals uses the same list with ?status=SUBMITTED,
    CHANGES_REQUESTED, REJECTED or APPROVED. Tab counts: GET summary/. History: GET {slug}/activity/.
    Filters: ?status= (comma separated), ?category=<slug>, ?institute=<slug>, ?mode=, ?is_featured=,
    ?search= (title or institute). Default order: waiting for review first, then newest."""

    permission_classes = [HasPlatformPermission]
    required_permission = "users.manage_trainings"
    lookup_field = "slug"
    lookup_value_regex = r"[-a-zA-Z0-9_]+"
    filter_backends = (DjangoFilterBackend, SearchFilter, OrderingFilter)
    filterset_class = AdminTrainingFilter
    search_fields = ("title", "institute__name")
    ordering_fields = ("created_at", "title", "status_rank")
    ordering = ("status_rank", "-created_at", "-pk")

    def get_serializer_class(self):
        if self.action == "list":
            return AdminTrainingListSerializer
        if self.action in ("request_changes", "reject"):
            return ReasonSerializer
        if self.action == "feature":
            return FeatureSerializer
        return AdminTrainingSerializer

    def get_queryset(self):
        queryset = Training.objects.annotate(
            # SUBMITTED (waiting for review) first, then the rest
            status_rank=Case(
                When(status=TrainingStatus.SUBMITTED, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        ).select_related("institute", "category__parent")
        if self.action == "list":
            return queryset
        submitted = (
            TrainingStatusLog.objects.filter(
                training=OuterRef("pk"), to_status=TrainingStatus.SUBMITTED
            )
            .order_by("-created_at")
            .values("created_at")[:1]
        )
        return with_content(
            queryset.select_related("venue__location").annotate(
                submitted_at=Subquery(submitted)
            )
        )

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """Counts for the tabs. `all` counts every status, so it can be more than the tabs added up
        (unpublished and cancelled trainings have no tab of their own). `by_status` has the raw counts."""
        rows = Training.objects.order_by().values("status").annotate(n=Count("pk"))
        by_status = {code: 0 for code, _ in TrainingStatus.CHOICES}
        by_status.update({row["status"]: row["n"] for row in rows})
        tabs = {
            "all": sum(by_status.values()),
            "draft": by_status[TrainingStatus.DRAFT],
            "pending_review": by_status[TrainingStatus.SUBMITTED],
            "approved": by_status[TrainingStatus.APPROVED],
            "changes_requested": by_status[TrainingStatus.CHANGES_REQUESTED],
            "rejected": by_status[TrainingStatus.REJECTED],
            "expired": by_status[TrainingStatus.EXPIRED],
        }
        featured = Training.objects.filter(is_featured=True).count()
        return Response({"tabs": tabs, "by_status": by_status, "featured": featured})

    @action(detail=True, methods=["get"])
    def activity(self, request, *args, **kwargs):
        """The training's creation and every status change, newest first."""
        training = get_object_or_404(
            Training.objects.only("id", "title", "created_at"), slug=kwargs["slug"]
        )
        return Response({"results": selectors.training_activity(training)})

    def _review(self, request, service, *, with_reason, **extra):
        kwargs = dict(extra)
        if with_reason:
            serializer = ReasonSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            kwargs["reason"] = serializer.validated_data["reason"]
        try:
            training = service(self.get_object(), by=request.user, **kwargs)
        except DjangoValidationError as exc:  # a status rule or check -> a 400
            raise DRFValidationError(
                exc.message_dict if hasattr(exc, "error_dict") else {"detail": exc.messages}
            )
        return Response(
            AdminTrainingSerializer(
                self.get_queryset().get(pk=training.pk),
                context=self.get_serializer_context(),
            ).data
        )

    @action(detail=True, methods=["post"])
    def approve(self, request, *args, **kwargs):
        return self._review(request, training_services.approve, with_reason=False)

    @action(detail=True, methods=["post"], url_path="request-changes")
    def request_changes(self, request, *args, **kwargs):
        return self._review(
            request, training_services.request_changes, with_reason=True
        )

    @action(detail=True, methods=["post"])
    def reject(self, request, *args, **kwargs):
        return self._review(request, training_services.reject, with_reason=True)

    @action(detail=True, methods=["post"])
    def feature(self, request, *args, **kwargs):
        """Body {"is_featured": true} features an approved training, false removes it."""
        serializer = FeatureSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._review(
            request,
            training_services.set_featured,
            with_reason=False,
            featured=serializer.validated_data["is_featured"],

# enquiries
class AdminEnquiryViewSet(ListViewSet):
    """Monitor: every enquiry of every institute. Read-only; the institutes' own staff
    change statuses in the portal. ?status= ?type= ?institute=<slug> ?category=<slug>
    filter, ?search= matches the visitor's name, the training title or the institute."""

    serializer_class = AdminEnquiryListSerializer
    permission_classes = [HasPlatformPermission]
    required_permission = "users.manage_enquiries"
    filter_backends = (DjangoFilterBackend, SearchFilter)
    filterset_class = AdminEnquiryFilter
    search_fields = ("name", "training__title", "institute__name")
    queryset = Enquiry.objects.select_related(
        "institute", "training__category__parent"
    ).order_by("-created_at", "-pk")

    @action(detail=False, methods=["get"])
    def summary(self, request):
        """{"total", "converted"} of whatever the filters select."""
        return Response(
            enquiry_services.counts(self.filter_queryset(self.get_queryset()))
        )
