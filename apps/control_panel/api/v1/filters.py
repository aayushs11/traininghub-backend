from django_filters import BaseInFilter
import django_filters
from django.db.models import Exists, OuterRef, Q

from apps.common.models.category import Category
from apps.institutes.constants import DocumentStatus, AWAITING_REVIEW
from apps.institutes.models import Institute, InstituteDocument

from apps.enquiries.models import Enquiry
from apps.training.models import Training


class AdminCategoryFilter(django_filters.FilterSet):
    parent = django_filters.CharFilter(field_name="parent__slug")

    class Meta:
        model = Category
        fields = ("parent", "is_active")


class AdminTrainingFilter(django_filters.FilterSet):
    institute = django_filters.CharFilter(field_name="institute__slug")

    class Meta:
        model = Training
        fields = ("status", "mode", "institute")


class ChoiceInFilter(django_filters.BaseInFilter, django_filters.ChoiceFilter):
    """Comma separated values, each checked against `choices`: ?status=PENDING,INFO_REQUESTED"""


class AdminInstituteFilter(django_filters.FilterSet):
    status = ChoiceInFilter(field_name="status", lookup_expr="in, choices=InstituteStatus.CHOICES")
    # ?has_pending_documents=true -> institutes with a document waiting for review (new or re-uploaded)
    has_pending_documents = django_filters.BooleanFilter(
        method="filter_has_pending_documents"
    )

    class Meta:
        model = Institute
        fields = ("status", "type", "has_pending_documents")

    def filter_has_pending_documents(self, queryset, name, value):
        pending = Exists(
            InstituteDocument.objects.filter(
                institute=OuterRef("pk"), status__in=AWAITING_REVIEW
            )
        )
        return queryset.filter(pending) if value else queryset.exclude(pending)


class AdminEnquiryFilter(django_filters.FilterSet):
    institute = django_filters.CharFilter(field_name="institute__slug")
    category = django_filters.CharFilter(method="filter_category")

    class Meta:
        model = Enquiry
        fields = ("status", "type", "institute")

        def filter_category(self, queryset, name, value):
            """The top-level category's slug: the training's own category or its parents."""
            return queryset.filter(
                Q(training__category__slug=value)
                | Q(training__category__parent__slug=value)
            )
