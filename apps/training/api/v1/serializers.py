from django.db.models import Q
from rest_framework import serializers

from apps.common.api.v1.serializers import (
    LocationInputSerializer,
    active_municipalities,
)
from apps.common.models.category import Category
from apps.common.serializers import (
    DynamicFieldsModelSerializer,
    DynamicFieldsSerializer,
)
from apps.common.utils.helpers import institute_experience
from apps.common.validators import validate_phone_number
from apps.institutes.validators import validate_image_file
from apps.training import services
from apps.training.constants import WeekDay
from apps.training.models import (
    LearningOutcome,
    Training,
    TrainingModule,
    TrainingSession,
)

DAY_LABELS = dict(WeekDay.CHOICES)


def format_class_days(days):
    """['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI'] gives 'Sun – Fri'; a gap gives 'Sun, Tue, Thu'."""
    indexes = sorted(WeekDay.ORDER.index(d) for d in set(days))
    if len(indexes) > 2 and indexes == list(range(indexes[0], indexes[-1] + 1)):
        return f"{DAY_LABELS[WeekDay.ORDER[indexes[0]]]} – {DAY_LABELS[WeekDay.ORDER[indexes[-1]]]}"
    return ", ".join(DAY_LABELS[WeekDay.ORDER[i]] for i in indexes)


def duration_text(obj):
    if not obj.duration_value:
        return ""
    unit = obj.duration_unit.lower()
    return f"{obj.duration_value} {unit[:-1] if obj.duration_value == 1 else unit}"


# public
class PublicTrainingListSerializer(DynamicFieldsModelSerializer):
    """Everything here comes from select_related rows: no per-row queries."""

    duration = serializers.SerializerMethodField()
    institute = serializers.SerializerMethodField()
    category = serializers.SerializerMethodField()
    location = serializers.SerializerMethodField()
    skills = serializers.ListField(source="detail.skills", read_only=True)

    class Meta:
        model = Training
        fields = (
            "id",
            "slug",
            "title",
            "short_description",
            "mode",
            "level",
            "shifts",
            "duration",
            "fee_npr",
            "start_date",
            "end_date",
            "registration_deadline",
            "cover_image",
            "skills",
            "institute",
            "category",
            "location",
            "published_at",
            "is_featured",
        )

    def get_duration(self, obj):
        return {
            "value": obj.duration_value,
            "unit": obj.duration_unit,
            "weeks": obj.duration_weeks,
            "text": duration_text(obj),
        }

    def get_institute(self, obj):
        institute = obj.institute
        return {
            "slug": institute.slug,
            "name": institute.name,
            "logo": institute.logo.url if institute.logo else None,
        }

    def get_category(self, obj):
        parent = obj.category.parent
        return {
            "id": obj.category_id,
            "slug": obj.category.slug,
            "name": obj.category.name,
            "parent": (
                {"id": parent.id, "slug": parent.slug, "name": parent.name}
                if parent
                else None
            ),
        }

    def get_location(self, obj):
        venue = getattr(obj, "venue", None)
        if venue is None:
            return None
        place = (
            venue.location
        )  # empty for an online training: only the address text is left
        return {
            "municipality": place.name if place else None,
            "district": place.district.name if place else None,
            "province": place.province.name if place else None,
            "address": venue.address,
            "map_url": venue.map_url,
        }


class PublicTrainingDetailSerializer(PublicTrainingListSerializer):
    """sessions, modules and outcomes are prefetched in order by the view."""

    overview = serializers.CharField(source="detail.overview", read_only=True)
    eligibility = serializers.CharField(source="detail.eligibility", read_only=True)
    certification = serializers.CharField(source="detail.certification", read_only=True)
    schedule = serializers.SerializerMethodField()
    modules = serializers.SerializerMethodField()
    outcomes = serializers.SerializerMethodField()
    contact = serializers.SerializerMethodField()
    target_audience = serializers.ListField(
        source="detail.target_audience", read_only=True
    )

    class Meta(PublicTrainingListSerializer.Meta):
        fields = PublicTrainingListSerializer.Meta.fields + (
            "overview",
            "eligibility",
            "certification",
            "target_audience",
            "seats",
            "schedule",
            "modules",
            "outcomes",
            "contact",
        )

    def get_schedule(self, obj):
        return [
            {
                "class_days": s.class_days,
                "class_days_text": format_class_days(s.class_days),
                "start_time": s.start_time,
                "end_time": s.end_time,
            }
            for s in obj.sessions.all()
        ]

    def get_modules(self, obj):
        return [
            {"title": m.title, "description": m.description} for m in obj.modules.all()
        ]

    def get_outcomes(self, obj):
        return [o.text for o in obj.outcomes.all()]

    def get_contact(self, obj):
        # a missing row raises an AttributeError subclass, so getattr falls back to the default
        own = getattr(obj, "contact", None)
        institute = getattr(obj.institute, "contact", None)
        return {
            "person": own.contact_person if own else "",
            "phone": (own.contact_phone if own else "")
            or (institute.contact_phone if institute else ""),
            "email": (own.contact_email if own else "")
            or (institute.contact_email if institute else ""),
        }

    def get_institute(self, obj):
        data = super().get_institute(obj)
        data["experience"] = institute_experience(obj.institute.established_year)
        data["training_count"] = obj.institute_training_count
        return data


# portal (the institute's own staff)
class TrainingSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrainingSession
        fields = ("class_days", "start_time", "end_time")


class TrainingModuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrainingModule
        fields = ("title", "description")


class LearningOutcomeSerializer(serializers.ModelSerializer):
    class Meta:
        model = LearningOutcome
        fields = ("text",)


class VenueSerializer(LocationInputSerializer):
    """Physical: `location` (a municipality) and `address`. Online: `address` only, as plain text."""

    location = serializers.PrimaryKeyRelatedField(
        queryset=active_municipalities(), required=False, allow_null=True
    )


def available_categories():
    return Category.objects.filter(is_active=True).filter(
        Q(parent__isnull=True) | Q(parent__is_active=True)
    )


class TrainingContentFields(serializers.Serializer):
    """The flat fields that live on `TrainingDetail` and `TrainingContact`, so the API keeps one
    flat training. Writes arrive nested (`validated_data["detail"]`), as `services` takes them."""

    overview = serializers.CharField(
        source="detail.overview", required=False, allow_blank=True
    )
    eligibility = serializers.CharField(
        source="detail.eligibility", required=False, allow_blank=True
    )
    certification = serializers.CharField(
        source="detail.certification", required=False, allow_blank=True, max_length=255
    )
    skills = serializers.ListField(
        source="detail.skills",
        child=serializers.CharField(max_length=60),
        required=False,
    )
    contact_person = serializers.CharField(
        source="contact.contact_person",
        required=False,
        allow_blank=True,
        max_length=150,
    )
    contact_phone = serializers.CharField(
        source="contact.contact_phone",
        required=False,
        allow_blank=True,
        max_length=25,
        validators=[validate_phone_number],
    )
    contact_email = serializers.EmailField(
        source="contact.contact_email", required=False, allow_blank=True
    )
    target_audience = serializers.ListField(
        source="detail.target_audience",
        child=serializers.CharField(max_length=300),
        required=False,
        max_length=10,
    )


class PortalTrainingSerializer(TrainingContentFields, DynamicFieldsModelSerializer):
    """A nested list replaces the whole list; leave it out to keep it."""

    category = serializers.SlugRelatedField(
        slug_field="slug",
        queryset=available_categories().filter(parent__isnull=True),
    )
    sub_category = serializers.SlugRelatedField(
        slug_field="slug",
        queryset=available_categories().filter(parent__isnull=False),
        required=False,
        allow_null=True,
    )

    venue = VenueSerializer(required=False, allow_null=True)
    sessions = TrainingSessionSerializer(many=True, required=False)
    modules = TrainingModuleSerializer(many=True, required=False)
    outcomes = LearningOutcomeSerializer(many=True, required=False)

    class Meta:
        model = Training
        fields = (
            "id",
            "slug",
            "title",
            "category",
            "sub_category",
            "short_description",
            "overview",
            "mode",
            "level",
            "duration_value",
            "duration_unit",
            "duration_weeks",
            "fee_npr",
            "seats",
            "start_date",
            "end_date",
            "shifts",
            "registration_deadline",
            "venue",
            "eligibility",
            "certification",
            "skills",
            "target_audience",
            "contact_person",
            "contact_phone",
            "contact_email",
            "cover_image",
            "status",
            "review_feedback",
            "published_at",
            "sessions",
            "modules",
            "outcomes",
            "created_at",
        )
        read_only_fields = (
            "id",
            "slug",
            "duration_weeks",
            "cover_image",
            "status",
            "review_feedback",
            "published_at",
            "created_at",
        )

    def validate(self, attrs):
        """The stored category is the leaf: the sub-category if one is given, else the category.
        Sending `category` alone therefore clears the sub-category."""
        attrs = super().validate(attrs)
        if "sub_category" in attrs:
            sub_category = attrs.pop("sub_category")
            category = attrs.get("category") or self._current_top_category()
            if sub_category and sub_category.parent_id != category.pk:
                raise serializers.ValidationError(
                    {"sub_category": "Choose a sub-category of the selected category."}
                )
            attrs["category"] = sub_category or category
        return attrs

    def _current_top_category(self):
        leaf = (
            self.instance.category
        )  # PATCH only: the viewset selects category__parent
        return leaf.parent if leaf.parent_id else leaf

    def to_representation(self, instance):
        data = super().to_representation(instance)
        leaf = instance.category  # split the stored leaf back into the two inputs
        if "category" in data:
            data["category"] = leaf.parent.slug if leaf.parent_id else leaf.slug
        if "sub_category" in data:
            data["sub_category"] = leaf.slug if leaf.parent_id else None
        return data

    @staticmethod
    def _children(data):
        return {
            key: data.pop(key, None)
            for key in ("sessions", "modules", "outcomes", "detail", "contact", "venue")
        }

    def create(self, validated_data):
        children = self._children(validated_data)
        return services.create_training(
            self.request.user.membership.institute, **children, **validated_data
        )

    def update(self, instance, validated_data):
        children = self._children(validated_data)
        return services.update_training(instance, **children, **validated_data)


class PortalTrainingListSerializer(DynamicFieldsModelSerializer):
    category_name = serializers.CharField(source="category.name", read_only=True)
    municipality = serializers.SerializerMethodField()

    class Meta:
        model = Training
        fields = (
            "id",
            "slug",
            "title",
            "status",
            "review_feedback",
            "category_name",
            "mode",
            "fee_npr",
            "start_date",
            "end_date",
            "municipality",
            "created_at",
        )

    def get_municipality(self, obj):
        venue = getattr(obj, "venue", None)
        return venue.location.name if venue and venue.location_id else None


class CoverSerializer(DynamicFieldsSerializer):
    cover_image = serializers.ImageField(validators=[validate_image_file])

    def update(self, instance, validated_data):
        return services.set_cover(instance, validated_data["cover_image"])
