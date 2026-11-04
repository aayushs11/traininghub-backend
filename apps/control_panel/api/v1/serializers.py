from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from apps.common import services as common_services
from apps.common.models.category import Category
from apps.common.serializers import (
    DynamicFieldsModelSerializer,
    DynamicFieldsSerializer,
)
from apps.enquiries.models import Enquiry
from apps.institutes.api.v1.serializers import (
    InstituteCEOSerializer,
    InstituteContactSerializer,
    InstituteLocationSerializer,
    InstituteSocialLinkSerializer,
)
from apps.institutes.constants import DocumentStatus, InstituteStatus
from apps.institutes.models import Institute, InstituteDocument
from apps.training.api.v1.serializers import (
    LearningOutcomeSerializer,
    PortalTrainingSerializer,
    TrainingContentFields,
    TrainingModuleSerializer,
    TrainingSessionSerializer,
)
from apps.training.constants import TrainingStatus
from apps.training.models import Training
from apps.users import services as user_services
from apps.users.api.v1.serializers import ProfileSerializer
from apps.users.constants import GRANTABLE_PERMISSIONS

User = get_user_model()


class ReasonSerializer(DynamicFieldsSerializer):
    """The body of every review action that needs a reason (reject, request changes or info, suspend)."""

    reason = serializers.CharField()


# admins
class AdminSerializer(ProfileSerializer):
    """Admin serializer class for admin related work"""

    password = serializers.CharField(write_only=True, required=False)
    permissions = serializers.ListField(
        child=serializers.ChoiceField(choices=GRANTABLE_PERMISSIONS), required=False
    )

    class Meta(ProfileSerializer.Meta):
        fields = ProfileSerializer.Meta.fields + ("password", "is_active")
        read_only_fields = ("id", "role", "created_at", "last_login", "is_active")
        create_only_fields = ("email", "password")  # read-only after password creation
        extra_kwargs = {
            **ProfileSerializer.Meta.extra_kwargs,
            "email": {
                "validators": [
                    UniqueValidator(
                        queryset=User.objects.all(),
                        lookup="iexact",
                        message="A user with that email already exists.",
                    )
                ]
            },
        }

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["permissions"] = self.get_permissions(instance)
        return data

    def validate_password(self, value):
        validate_password(value)
        return value

    def validate(self, attrs):
        if self.instance is None and "password" not in attrs:
            raise serializers.ValidationError({"password": "This field is required."})
        if self.instance is not None and "password" in attrs:
            raise serializers.ValidationError(
                {"password": "The password cannot be changed here."}
            )
        return attrs

    def create(self, validated_data):
        return user_services.create_admin(**validated_data)

    def update(self, instance, validated_data):
        return user_services.update_admin(instance, **validated_data)


# categories
class AdminCategorySerializer(DynamicFieldsModelSerializer):
    parent = serializers.SlugRelatedField(
        slug_field="slug",
        queryset=Category.objects.filter(parent__isnull=True),
        required=False,
        allow_null=True,
    )
    parent_name = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = (
            "id",
            "slug",
            "name",
            "parent",
            "parent_name",
            "icon",
            "hue",
            "is_active",
            "created_at",
        )
        read_only_fields = ("id", "slug", "created_at")
        extra_kwargs = {
            "hue": {"max_value": 360}
        }  # the database constraint says the same

    def get_parent_name(self, obj):
        return obj.parent.name if obj.parent_id else None

    def validate(self, attrs):
        name = attrs.get("name", getattr(self.instance, "name", "")).strip()
        parent = attrs.get("parent", getattr(self.instance, "parent", None))
        clash = Category.objects.filter(name__iexact=name, parent=parent)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                {"name": "A category with this name already exists here."}
            )
        return attrs

    def create(self, validated_data):
        validated_data.pop("is_active", None)
        return common_services.create_category(**validated_data)

    def update(self, instance, validated_data):
        return common_services.update_category(instance, **validated_data)


class AdminCategoryOptionSerializer(serializers.ModelSerializer):
    """Light shape for dropdowns, e.g. the category filter on the Trainings screen."""

    parent = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    parent_name = serializers.CharField(source="parent.name", read_only=True, default=None)

    class Meta:
        model = Category
        fields = ("id", "slug", "name", "parent", "parent_name", "is_active")
        read_only_fields = fields


# institutes
class AdminDocumentSerializer(DynamicFieldsModelSerializer):
    class Meta:
        model = InstituteDocument
        fields = ("id", "name", "status", "created_at")  # never the file path or URL
        read_only_fields = ("id", "name", "created_at")


class DocumentReviewSerializer(serializers.Serializer):
    status = serializers.ChoiceField(
        choices=(DocumentStatus.VERIFIED, DocumentStatus.REJECTED)
    )


# the review actions an admin can take, by the status they move the institute to
_INSTITUTE_ACTIONS = {
    InstituteStatus.APPROVED: "approve",
    InstituteStatus.REJECTED: "reject",
    InstituteStatus.INFO_REQUESTED: "request_info",
    InstituteStatus.SUSPENDED: "suspend",
}


class AdminInstituteSerializer(DynamicFieldsModelSerializer):
    """Read-only view of an institute for the reviewer: what it gave at registration (locations,
    contact person) and what it added since, with every location, retired ones included."""

    documents = AdminDocumentSerializer(many=True, read_only=True)
    owner_email = serializers.SerializerMethodField()
    contact = InstituteContactSerializer(read_only=True, allow_null=True)
    ceo = InstituteCEOSerializer(read_only=True, allow_null=True)
    social_links = InstituteSocialLinkSerializer(many=True, read_only=True)
    locations = InstituteLocationSerializer(many=True, read_only=True)
    available_actions = serializers.SerializerMethodField()

    class Meta:
        model = Institute
        fields = (
            "id",
            "slug",
            "name",
            "registration_number",
            "pan_vat_number",
            "type",
            "status",
            "status_reason",
            "established_year",
            "description",
            "created_at",
            "owner_email",
            "contact",
            "ceo",
            "social_links",
            "locations",
            "documents",
            "available_actions",
        )

    def get_owner_email(self, obj):
        owners = getattr(obj, "owner_members", [])  # prefetched by the admin viewset
        return owners[0].user.email if owners else None

    def get_available_actions(self, obj):
        """The buttons the admin may press now, from the real status rules, so the frontend
        does not hard-code them. A rejected or info-requested institute waits for its owner."""
        targets = InstituteStatus.TRANSITIONS.get(obj.status, ())
        return [
            action for status, action in _INSTITUTE_ACTIONS.items() if status in targets
        ]


class AdminInstituteListSerializer(DynamicFieldsModelSerializer):
    """One row of the institutes list: only what the table shows (no documents, social links or
    CEO). Reads the annotations and prefetches set by AdminInstituteViewSet.get_queryset."""

    contact_person = serializers.CharField(
        source="contact.contact_person", read_only=True, default=None
    )
    contact_email = serializers.CharField(
        source="contact.contact_email", read_only=True, default=None
    )
    contact_phone = serializers.CharField(
        source="contact.contact_phone", read_only=True, default=None
    )
    city = serializers.SerializerMethodField()
    district = serializers.SerializerMethodField()
    province = serializers.SerializerMethodField()
    trainings_count = serializers.IntegerField(read_only=True)
    # documents waiting for review: new uploads, and rejected ones uploaded again
    pending_documents_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Institute
        fields = (
            "id",
            "slug",
            "name",
            "type",
            "status",
            "created_at",
            "contact_person",
            "contact_email",
            "contact_phone",
            "city",
            "district",
            "province",
            "trainings_count",
            "pending_documents_count",
        )

    def _main_location(self, obj):
        locations = list(obj.locations.all())  # prefetched: active ones, main first
        return locations[0].location if locations else None

    def get_city(self, obj):
        location = self._main_location(obj)
        return location.name if location else None

    def get_district(self, obj):
        location = self._main_location(obj)
        return location.district.name if location else None

    def get_province(self, obj):
        location = self._main_location(obj)
        return location.province.name if location else None


# trainings
class FeatureSerializer(serializers.Serializer):
    is_featured = serializers.BooleanField()


def _category_data(category):
    """The stored category is the leaf: a sub-category has a parent, a main category does not."""
    parent = category.parent
    return {
        "id": category.pk,
        "slug": category.slug,
        "name": category.name,
        "parent": parent.name if parent else None,
        "parent_slug": parent.slug if parent else None,
    }


class AdminTrainingListSerializer(DynamicFieldsModelSerializer):
    """One row of the Trainings and Training Approvals lists. Reads select_related rows only."""

    institute = serializers.SerializerMethodField()
    category = serializers.SerializerMethodField()

    class Meta:
        model = Training
        fields = (
            "id",
            "slug",
            "title",
            "status",
            "is_featured",
            "mode",
            "fee_npr",
            "start_date",
            "end_date",
            "review_feedback",
            "created_at",
            "institute",
            "category",
        )

    def get_institute(self, obj):
        return {
            "slug": obj.institute.slug,
            "name": obj.institute.name,
            "status": obj.institute.status,
        }

    def get_category(self, obj):
        return _category_data(obj.category)


class AdminTrainingSerializer(TrainingContentFields, DynamicFieldsModelSerializer):
    """Read-only view of a whole training for the reviewer."""

    institute = serializers.SerializerMethodField()
    category = serializers.SerializerMethodField()
    sub_category = serializers.SerializerMethodField()
    venue = serializers.SerializerMethodField()
    sessions = TrainingSessionSerializer(many=True, read_only=True)
    modules = TrainingModuleSerializer(many=True, read_only=True)
    outcomes = LearningOutcomeSerializer(many=True, read_only=True)
    submitted_at = serializers.SerializerMethodField()
    available_actions = serializers.SerializerMethodField()

    class Meta:
        model = Training
        fields = PortalTrainingSerializer.Meta.fields + (
            "institute",
            "is_featured",
            "submitted_at",
            "available_actions",
        )
        read_only_fields = fields

    def get_institute(self, obj):
        return {
            "id": obj.institute_id,
            "slug": obj.institute.slug,
            "name": obj.institute.name,
            "status": obj.institute.status,
        }

    def get_category(self, obj):
        return _category_data(obj.category)

    def get_venue(self, obj):
        venue = getattr(obj, "venue", None)
        if venue is None:
            return None
        return {
            "municipality": venue.location.name if venue.location_id else None,
            "address": venue.address,
            "map_url": venue.map_url,
            "contact_phone": venue.contact_phone,
        }

    def get_sub_category(self, obj):
        if obj.category.parent_id is None:
            return None
        return {
            "id": obj.category_id,
            "slug": obj.category.slug,
            "name": obj.category.name,
        }

    def get_submitted_at(self, obj):
        """When it last went to review (annotated by the viewset); the creation date if never."""
        return getattr(obj, "submitted_at", None) or obj.created_at

    def get_available_actions(self, obj):
        """The buttons the admin may press now, so the frontend does not hard-code them. Approve
        needs an approved institute (the design disables it otherwise)."""
        institute_approved = obj.institute.status == InstituteStatus.APPROVED
        actions = []
        if obj.status == TrainingStatus.SUBMITTED:
            if institute_approved:
                actions.append("approve")
            actions += ["request_changes", "reject"]
        if obj.is_featured:
            actions.append("unfeature")
        elif obj.status == TrainingStatus.APPROVED and institute_approved:
            actions.append("feature")
        return actions
