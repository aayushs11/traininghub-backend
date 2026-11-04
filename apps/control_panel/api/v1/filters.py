import django_filters
from django.db.models import Exists, OuterRef, Q

from apps.common.models.category import Category
from apps.institutes.constants import DocumentStatus
from apps.institutes.models import Institute, InstituteDocument
from apps.training.models import Training

# documents an admin still has to look at: new uploads, and rejected ones uploaded again
AWAITING_REVIEW = (DocumentStatus.PENDING, DocumentStatus.RE_UPLOADED)


class CharInFilter(django_filters.BaseInFilter, django_filters.CharFilter):
    """Comma separated values: ?status=PENDING,INFO_REQUESTED"""


class AdminCategoryFilter(django_filters.FilterSet):
    parent = django_filters.CharFilter(field_name="parent__slug")

    class Meta:
        model = Category
        fields = ("parent", "is_active")


class AdminTrainingFilter(django_filters.FilterSet):
    # ?status=SUBMITTED, or several: ?status=SUBMITTED,CHANGES_REQUESTED
    status = CharInFilter(field_name="status", lookup_expr="in")
    institute = django_filters.CharFilter(field_name="institute__slug")
    # a main category also matches the trainings filed under its sub-categories
    category = django_filters.CharFilter(method="filter_category")

    class Meta:
        model = Training
        fields = ("status", "mode", "institute", "category", "is_featured")

    def filter_category(self, queryset, name, value):
        return queryset.filter(Q(category__slug=value) | Q(category__parent__slug=value))


class AdminInstituteFilter(django_filters.FilterSet):
    status = CharInFilter(field_name="status", lookup_expr="in")
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
