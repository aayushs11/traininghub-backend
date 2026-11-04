"""Read-only queries for the control panel that join several apps."""

from apps.institutes.constants import InstituteStatus
from apps.training.constants import TrainingStatus
from apps.training.models import Training, TrainingStatusLog

_STATUS_LABELS = dict(InstituteStatus.CHOICES)
_TRAINING_LABELS = dict(TrainingStatus.CHOICES)


def _training_log_title(log):
    """A readable title for one training status change, e.g. 'Submitted for review'."""
    if log.to_status == TrainingStatus.SUBMITTED:
        return "Submitted for review"
    if log.to_status == TrainingStatus.APPROVED:
        return "Republished" if log.from_status == TrainingStatus.UNPUBLISHED else "Approved"
    if log.to_status == TrainingStatus.DRAFT:
        return "Withdrawn to draft"
    return _TRAINING_LABELS.get(log.to_status, log.to_status)


def _actor(log):
    if not log.changed_by_id:
        return None
    return log.changed_by.full_name or log.changed_by.email


def _event(type_, title, body, at, *, by=None, **extra):
    return {
        "type": type_,
        "title": title,
        "body": body,
        "at": at,
        "by": by,
        "from_status": None,
        "to_status": None,
        "reason": "",
        "training": None,
        **extra,
    }


def institute_activity(institute):
    """The review page timeline, newest first: when the institute registered, every status change
    (who, why), and each training it submitted. Training approvals and rejections will join once
    trainings keep a status log of their own."""
    documents = institute.documents.count()
    events = [
        _event(
            "REGISTERED",
            "Registered",
            f"Submitted institute details and {documents} document"
            f"{'' if documents == 1 else 's'}",
            institute.created_at,
        )
    ]

    logs = institute.status_logs.select_related("changed_by").order_by("-created_at", "pk")
    for log in logs:
        by = None
        if log.changed_by_id:
            by = log.changed_by.full_name or log.changed_by.email
        events.append(
            _event(
                "STATUS_CHANGED",
                f"Status changed to {_STATUS_LABELS.get(log.to_status, log.to_status)}",
                log.reason or (f"By {by}" if by else ""),
                log.created_at,
                by=by,
                from_status=log.from_status,
                to_status=log.to_status,
                reason=log.reason,
            )
        )

    logs_by_training = set()
    training_logs = TrainingStatusLog.objects.filter(
        training__institute=institute
    ).select_related("training", "changed_by")
    for log in training_logs:
        logs_by_training.add(log.training_id)
        by = _actor(log)
        title = _training_log_title(log)
        body = log.training.title + (f" \u2014 {log.reason}" if log.reason else "")
        events.append(
            _event(
                "TRAINING_SUBMITTED"
                if log.to_status == TrainingStatus.SUBMITTED
                else "TRAINING_STATUS_CHANGED",
                f"Training: {title.lower()}" if log.to_status != TrainingStatus.SUBMITTED else "Training submitted",
                body,
                log.created_at,
                by=by,
                from_status=log.from_status,
                to_status=log.to_status,
                reason=log.reason,
                training={
                    "slug": log.training.slug,
                    "title": log.training.title,
                    "status": log.training.status,
                },
            )
        )

    # trainings from before the log existed: show them once, at their creation date
    legacy = (
        Training.objects.filter(institute=institute)
        .exclude(status=TrainingStatus.DRAFT)
        .exclude(pk__in=logs_by_training)
        .only("slug", "title", "status", "created_at")
    )
    for training in legacy:
        events.append(
            _event(
                "TRAINING_SUBMITTED",
                "Training submitted",
                f"{training.title} \u2014 {training.get_status_display()}",
                training.created_at,
                training={
                    "slug": training.slug,
                    "title": training.title,
                    "status": training.status,
                },
            )
        )

    events.sort(key=lambda event: event["at"], reverse=True)
    return events


def training_activity(training):
    """The training's history for the review page, newest first: when it was created and every
    status change (who, why). A change made by the system (expiry) has no `by`."""
    events = [
        _event(
            "CREATED",
            "Draft created",
            training.title,
            training.created_at,
        )
    ]
    logs = training.status_logs.select_related("changed_by").order_by("created_at", "pk")
    for log in logs:
        by = _actor(log)
        events.append(
            _event(
                "STATUS_CHANGED",
                _training_log_title(log),
                log.reason or (f"By {by}" if by else ""),
                log.created_at,
                by=by,
                from_status=log.from_status,
                to_status=log.to_status,
                reason=log.reason,
            )
        )
    events.sort(key=lambda event: event["at"], reverse=True)
    return events
