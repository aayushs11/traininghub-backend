from datetime import timedelta

from django.contrib.postgres.search import SearchVector
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, IntegerField, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.institutes.constants import InstituteStatus
from apps.institutes.models import Institute
from apps.training.constants import (
    SEARCH_CONFIG,
    DurationUnit,
    StartBucket,
    TrainingMode,
    TrainingShift,
    TrainingStatus,
)
from apps.training.models import (
    LearningOutcome,
    Training,
    TrainingContact,
    TrainingDetail,
    TrainingLocation,
    TrainingModule,
    TrainingSession,
    TrainingStatusLog,
)


# derived values and search
def duration_in_weeks(value, unit):
    if not value or unit not in DurationUnit.WEEKS_PER_UNIT:
        return None
    # half up and at least one week, like the portal's Math.round
    return max(1, int(value * DurationUnit.WEEKS_PER_UNIT[unit] + 0.5))


def _build_search_vector(training):
    category = training.category
    category_names = (
        f"{category.parent.name if category.parent_id else ''} {category.name}"
    )
    venue = getattr(training, "venue", None)  # a draft may not have one yet
    place = venue.location if venue else None
    place_names = f"{place.name} {place.district.name}" if place else ""
    detail = getattr(
        training, "detail", None
    )  # a row made without the services has none
    skills = " ".join(detail.skills) if detail else ""
    overview = detail.overview if detail else ""
    return (
        SearchVector(Value(training.title), weight="A", config=SEARCH_CONFIG)
        + SearchVector(
            Value(f"{training.institute.name} {category_names} {skills}"),
            weight="B",
            config=SEARCH_CONFIG,
        )
        + SearchVector(
            Value(f"{training.short_description} {overview} {place_names}"),
            weight="C",
            config=SEARCH_CONFIG,
        )
        + SearchVector(
            Value(" ".join(training.modules.values_list("title", flat=True))),
            weight="D",
            config=SEARCH_CONFIG,
        )
    )


def refresh_search_vector(training):
    """The vector is built from Python values, so one UPDATE and no joins in the database."""
    training = Training.objects.select_related(
        "institute",
        "category__parent",
        "venue__location__district",
        "detail",
    ).get(pk=training.pk)
    Training.objects.filter(pk=training.pk).update(
        search_vector=_build_search_vector(training)
    )


def refresh_search_vectors(queryset):
    for pk in queryset.values_list("pk", flat=True).iterator():
        refresh_search_vector(Training(pk=pk))


def registration_open_q(today=None):
    """A training still takes registrations: its deadline has not passed, or it has no deadline
    and has not started. One definition for the public `registration_open` filter and for
    enquiries (a plain enquiry is refused once this is false)."""
    today = today or timezone.localdate()
    return Q(registration_deadline__gte=today) | Q(
        registration_deadline__isnull=True, start_date__gte=today
    )


# checks
def _require_approved_institute(institute):
    if not Institute.objects.filter(
        pk=institute.pk, status=InstituteStatus.APPROVED
    ).exists():
        raise ValidationError("Only an approved institute can do this.")


def _check_category(category):
    if not category.is_active or (category.parent_id and not category.parent.is_active):
        raise ValidationError({"category": "This category is not available."})


def _check_venue(mode, location, address):
    """`location` is the municipality or None; `address` is the street address, or the plain text
    for an online training."""
    if mode == TrainingMode.ONLINE:
        if location is not None:
            raise ValidationError(
                {
                    "venue": "An online training has no municipality: send only the address text."
                }
            )
    elif location is None:
        raise ValidationError(
            {"venue": "Choose the municipality where the training is held."}
        )
    if not address:
        raise ValidationError({"venue": "Enter the address."})


def _save_venue(training, venue):
    """The one-to-one row holds where it is held. None leaves it alone; a dict replaces it whole."""
    if venue is None:
        return
    training.venue, _ = (
        TrainingLocation.objects.update_or_create(  # keeps the cached row current
            training=training,
            defaults={"location": None, "map_url": "", "contact_phone": "", **venue},
        )
    )


def _check_dates(start, end, deadline):
    errors = {}
    if start and end and end < start:
        errors["end_date"] = "The end date cannot be before the start date."
    if deadline and start and deadline > start:
        errors["registration_deadline"] = (
            "The registration deadline cannot be after the start date."
        )
    if errors:
        raise ValidationError(errors)


def _check_sessions(sessions):
    for session in sessions:
        days = session["class_days"]
        if not days or len(set(days)) != len(days):
            raise ValidationError(
                {"sessions": "Choose at least one class day, each day once."}
            )
        if session["end_time"] <= session["start_time"]:
            raise ValidationError({"sessions": "A session must end after it starts."})


def _clean_shifts(shifts):
    """Each shift once, in Morning, Day, Evening order."""
    return sorted(set(shifts), key=TrainingShift.ORDER.index)


def _replace_children(training, sessions, modules, outcomes):
    """None leaves a list alone; a list (even an empty one) replaces it."""
    if sessions is not None:
        training.sessions.all().delete()
        TrainingSession.objects.bulk_create(
            [TrainingSession(training=training, **s) for s in sessions]
        )
    if modules is not None:
        training.modules.all().delete()
        TrainingModule.objects.bulk_create(
            [TrainingModule(training=training, **m) for m in modules]
        )
    if outcomes is not None:
        training.outcomes.all().delete()
        LearningOutcome.objects.bulk_create(
            [LearningOutcome(training=training, **o) for o in outcomes]
        )


def _save_extras(training, detail, contact):
    """The one-to-one rows hold the long text, skills and contact. None leaves a row alone; a dict
    changes only the fields in it (and creates the row if the training was made without one)."""
    for name, model, values in (
        ("detail", TrainingDetail, detail),
        ("contact", TrainingContact, contact),
    ):
        if values is None:
            continue
        row = getattr(training, name, None) or model(training=training)
        for field, value in values.items():
            setattr(row, field, value)
        row.save()


def _ready_for_submission(training):
    errors = {}
    required = {
        "short_description": training.short_description,
        "level": training.level,
        "shifts": training.shifts,
        "duration_value": training.duration_value,
        "duration_unit": training.duration_unit,
        "fee_npr": training.fee_npr,
        "start_date": training.start_date,
        "end_date": training.end_date,
    }
    for name, value in required.items():
        if value in (None, "", []):
            errors[name] = "This is required before submitting."
    venue = getattr(training, "venue", None)
    detail = getattr(training, "detail", None)
    if detail is None or not detail.target_audience:
        errors["target_audience"] = "This is required before submitting."
    if venue is None or not venue.address:
        errors["venue"] = "Add where the training is held."
    elif training.mode == TrainingMode.PHYSICAL:
        if venue.location_id is None:
            errors["venue"] = "Choose where the training is held."
        elif not venue.location.is_active:
            errors["venue"] = "The selected location is no longer active."
    if not training.sessions.exists():
        errors["sessions"] = "Add at least one class day and time."
    if training.start_date and training.start_date < timezone.localdate():
        errors["start_date"] = "The start date is in the past."
    if errors:
        raise ValidationError(errors)


# workflow
def _transition(training, to, *, only_from=None, reason="", check=None, by=None):
    """Move a training to a new status and log it. Caller must be inside transaction.atomic()."""
    training = (
        Training.objects.select_for_update(of=("self",))
        .select_related("institute", "venue__location")
        .get(pk=training.pk)
    )
    # The table alone is not enough: UNPUBLISHED -> APPROVED is a republish, not an admin approval.
    if only_from is not None and training.status not in only_from:
        raise ValidationError(
            f"A {training.get_status_display().lower()} training cannot do this."
        )
    if to not in TrainingStatus.TRANSITIONS[training.status]:
        raise ValidationError(f"Cannot move from {training.status} to {to}.")
    if to in TrainingStatus.REASON_REQUIRED and not reason.strip():
        raise ValidationError({"reason": "A reason is required."})
    if check:
        check(training)
    previous = training.status
    training.status = to
    if to in TrainingStatus.REASON_REQUIRED:
        training.review_feedback = reason
    elif to == TrainingStatus.APPROVED:
        training.review_feedback = ""
    if previous == TrainingStatus.APPROVED and to != TrainingStatus.APPROVED:
        training.is_featured = False  # no longer live, so no longer featured
    training.save(
        update_fields=["status", "review_feedback", "is_featured", "modified_at"]
    )
    TrainingStatusLog.objects.create(
        training=training,
        from_status=previous,
        to_status=to,
        reason=reason.strip(),
        changed_by=by,
    )
    return training


# institute actions
@transaction.atomic
def submit(training, *, by):
    _require_approved_institute(training.institute)
    # TODO(notify): admins with manage_trainings
    return _transition(
        training,
        TrainingStatus.SUBMITTED,
        check=_ready_for_submission,
        only_from=TrainingStatus.EDITABLE,
        by=by,
    )


@transaction.atomic
def withdraw(training, *, by):
    """Take a submitted training back to DRAFT while it waits for review."""
    return _transition(
        training, TrainingStatus.DRAFT, only_from={TrainingStatus.SUBMITTED}, by=by
    )


@transaction.atomic
def unpublish(training, *, by):
    return _transition(
        training,
        TrainingStatus.UNPUBLISHED,
        only_from={TrainingStatus.APPROVED},
        by=by,
    )


@transaction.atomic
def republish(training, *, by):
    _require_approved_institute(training.institute)
    return _transition(
        training,
        TrainingStatus.APPROVED,
        only_from={TrainingStatus.UNPUBLISHED},
        by=by,
    )


@transaction.atomic
def cancel(training, *, by):
    # TODO(notify): people who sent an enquiry (once enquiries exist)
    return _transition(
        training,
        TrainingStatus.CANCELLED,
        only_from=TrainingStatus.LIVE,
        by=by,
    )


# admin actions (a training that waits for review)
@transaction.atomic
def approve(training, *, by):
    # the design disables Approve while the institute is not approved
    _require_approved_institute(training.institute)
    training = _transition(
        training,
        TrainingStatus.APPROVED,
        only_from={TrainingStatus.SUBMITTED},
        by=by,
    )
    if training.published_at is None:
        training.published_at = timezone.now()
        training.save(update_fields=["published_at", "modified_at"])
    # TODO(notify): the institute
    return training


@transaction.atomic
def request_changes(training, *, by, reason):
    return _transition(
        training,
        TrainingStatus.CHANGES_REQUESTED,
        reason=reason,
        only_from={TrainingStatus.SUBMITTED},
        by=by,
    )


@transaction.atomic
def reject(training, *, by, reason):
    return _transition(
        training,
        TrainingStatus.REJECTED,
        reason=reason,
        only_from={TrainingStatus.SUBMITTED},
        by=by,
    )


@transaction.atomic
def set_featured(training, *, featured, by):
    """Feature or un-feature a training on the front page. Only an approved training of an
    approved institute can be featured; un-featuring is always allowed."""
    training = (
        Training.objects.select_for_update(of=("self",))
        .select_related("institute")
        .get(pk=training.pk)
    )
    if featured:
        if training.status != TrainingStatus.APPROVED:
            raise ValidationError("Only an approved training can be featured.")
        _require_approved_institute(training.institute)
    if training.is_featured != featured:
        training.is_featured = featured
        training.save(update_fields=["is_featured", "modified_at"])
    return training


# the system
@transaction.atomic
def expire_trainings(today=None):
    """Set EXPIRED on every live training whose end date has passed, and log each change.
    Safe to run often. Returns how many were expired."""
    today = today or timezone.localdate()
    due = list(
        Training.objects.select_for_update()
        .filter(status__in=TrainingStatus.LIVE, end_date__lt=today)
        .values_list("pk", "status")
    )
    if not due:
        return 0
    Training.objects.filter(pk__in=[pk for pk, _ in due]).update(
        status=TrainingStatus.EXPIRED, is_featured=False, modified_at=timezone.now()
    )
    TrainingStatusLog.objects.bulk_create(
        TrainingStatusLog(
            training_id=pk, from_status=status, to_status=TrainingStatus.EXPIRED
        )
        for pk, status in due
    )
    return len(due)

# content
@transaction.atomic
def create_training(
    institute,
    *,
    sessions=None,
    modules=None,
    outcomes=None,
    detail=None,
    contact=None,
    venue=None,
    **fields,
):
    Institute.objects.select_for_update().get(
        pk=institute.pk
    )  # an admin may be suspending it right now
    _require_approved_institute(institute)
    _check_category(fields["category"])
    venue = venue or {}
    _check_venue(fields["mode"], venue.get("location"), venue.get("address"))
    _check_dates(
        fields.get("start_date"),
        fields.get("end_date"),
        fields.get("registration_deadline"),
    )
    if "shifts" in fields:
        fields["shifts"] = _clean_shifts(fields["shifts"])
    if sessions is not None:
        _check_sessions(sessions)
    fields["duration_weeks"] = duration_in_weeks(
        fields.get("duration_value"), fields.get("duration_unit")
    )
    training = Training.objects.create(institute=institute, **fields)
    _save_extras(training, detail or {}, contact or {})  # every training has both rows
    _save_venue(training, venue)  # create requires one, so this is never skipped
    _replace_children(training, sessions, modules, outcomes)
    refresh_search_vector(training)
    return training


@transaction.atomic
def update_training(
    training,
    *,
    sessions=None,
    modules=None,
    outcomes=None,
    detail=None,
    contact=None,
    venue=None,
    **fields,
):
    """DRAFT, CHANGES_REQUESTED and REJECTED are saved as they are. APPROVED and UNPUBLISHED go
    straight back to review (hidden until approved), and the edit is refused if the result is not
    ready to submit; the whole edit then rolls back."""
    training = (
        Training.objects.select_for_update(of=("self",))
        .select_related("institute", "category__parent", "venue", "detail", "contact")
        .get(pk=training.pk)
    )
    back_to_review = training.status in TrainingStatus.REVIEWED_EDIT
    if training.status not in TrainingStatus.EDITABLE and not back_to_review:
        raise ValidationError(
            f"A {training.get_status_display().lower()} training cannot be edited."
        )
    mode = fields.get("mode", training.mode)
    if venue is None and mode != training.mode:
        raise ValidationError({"venue": "Send the venue when you change the mode."})
    if (
        venue is not None or "mode" in fields
    ):  # a title-only edit of an old draft stays allowed
        current = getattr(training, "venue", None)
        if venue is not None:
            location, address = venue.get("location"), venue["address"]
        elif current is not None:
            location, address = current.location, current.address
        else:
            location, address = None, ""
        _check_venue(mode, location, address)
    _check_category(fields.get("category", training.category))
    _check_dates(
        fields.get("start_date", training.start_date),
        fields.get("end_date", training.end_date),
        fields.get("registration_deadline", training.registration_deadline),
    )
    if "shifts" in fields:
        fields["shifts"] = _clean_shifts(fields["shifts"])

    if sessions is not None:
        _check_sessions(sessions)
    if "duration_value" in fields or "duration_unit" in fields:
        fields["duration_weeks"] = duration_in_weeks(
            fields.get("duration_value", training.duration_value),
            fields.get("duration_unit", training.duration_unit),
        )
    for name, value in fields.items():
        setattr(training, name, value)
    training.save(update_fields=[*fields, "modified_at"])
    _save_extras(training, detail, contact)
    _save_venue(training, venue)
    _replace_children(training, sessions, modules, outcomes)
    refresh_search_vector(training)
    if back_to_review:
        _require_approved_institute(training.institute)
        training = _transition(
            training, TrainingStatus.SUBMITTED, check=_ready_for_submission,
        )
    return training


@transaction.atomic
def set_cover(training, image):
    training = Training.objects.select_for_update().get(pk=training.pk)
    if training.status not in TrainingStatus.EDITABLE:
        raise ValidationError(
            "The cover can only change while the training is a draft or being revised."
        )
    training.cover_image = image
    training.save(update_fields=["cover_image", "modified_at"])
    return training


@transaction.atomic
def delete_training(training):
    training = Training.objects.select_for_update().get(pk=training.pk)
    if training.status != TrainingStatus.DRAFT:
        raise ValidationError(
            "Only a draft can be deleted. Cancel the training instead."
        )
    # a training that was live and went back to a draft may hold enquiries, which the institute keeps
    if training.enquiries.exists():
        raise ValidationError(
            "This training has enquiries, so it cannot be deleted. Cancel it instead."
        )
    training.delete()


def institute_counts(trainings):
    return trainings.aggregate(
        total=Count("pk"),
        active=Count("pk", filter=Q(status=TrainingStatus.APPROVED)),
        pending_review=Count("pk", filter=Q(status=TrainingStatus.SUBMITTED)),
    )


def annotate_category_training_count(categories):
    """Annotate categories with `training_count`: the trainings the public can see (`APPROVED`,
    of an `APPROVED` institute) in the category itself and, for a top-level one, in its
    sub-categories. The same rule as the public `category` filter. One query, however many rows."""
    visible = (
        Training.objects.filter(
            status=TrainingStatus.APPROVED, institute__status=InstituteStatus.APPROVED
        )
        .filter(Q(category=OuterRef("pk")) | Q(category__parent=OuterRef("pk")))
        .order_by()  # no ordering inside the subquery
        .values(group=Value(1))  # one group, so the subquery answers a single row
        .annotate(n=Count("pk"))
        .values("n")
    )
    return categories.annotate(
        training_count=Coalesce(Subquery(visible, output_field=IntegerField()), 0)
    )


def annotate_institute_training_count(trainings):
    """Annotate trainings with `institute_training_count`: the `APPROVED` trainings of the same
    institute. One query, however many rows. The detail view only serves approved institutes, so
    this is the public count."""
    approved = (
        Training.objects.filter(
            institute=OuterRef("institute_id"), status=TrainingStatus.APPROVED
        )
        .order_by()
        .values(group=Value(1))
        .annotate(n=Count("pk"))
        .values("n")
    )
    return trainings.annotate(
        institute_training_count=Coalesce(
            Subquery(approved, output_field=IntegerField()), 0
        )
    )


def annotate_institute_public_counts(institutes):
    """Annotate institutes with `training_count` (their `APPROVED` trainings) and `category_count`
    (the distinct top-level categories those trainings fall in; a sub-category counts as its
    parent). One query, however many rows. Pass approved institutes only (the public list)."""
    approved = Training.objects.filter(
        institute=OuterRef("pk"), status=TrainingStatus.APPROVED
    ).order_by()

    def scalar(aggregate):
        return Coalesce(
            Subquery(
                approved.values(group=Value(1)).annotate(n=aggregate).values("n"),
                output_field=IntegerField(),
            ),
            0,
        )

    top_level = Coalesce(
        "category__parent_id", "category_id", output_field=IntegerField()
    )
    return institutes.annotate(
        training_count=scalar(Count("pk")),
        category_count=scalar(Count(top_level, distinct=True)),
    )


def start_date_range(bucket, today=None):
    """(first day, last day) a StartBucket covers; the last day is None when it has no end.
    The windows are rolling and cumulative, counted from today. A training that has already
    started is in no bucket."""
    today = today or timezone.localdate()
    if bucket == StartBucket.LATER:
        return today + timedelta(days=max(StartBucket.DAYS.values()) + 1), None
    return today, today + timedelta(days=StartBucket.DAYS[bucket])
