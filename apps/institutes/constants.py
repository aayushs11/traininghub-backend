class InstituteStatus:
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    INFO_REQUESTED = "INFO_REQUESTED"
    SUSPENDED = "SUSPENDED"

    CHOICES = (
        (PENDING, "Pending"),
        (APPROVED, "Approved"),
        (REJECTED, "Rejected"),
        (INFO_REQUESTED, "Info requested"),
        (SUSPENDED, "Suspended"),
    )
    TRANSITIONS = {
        PENDING: {APPROVED, REJECTED, INFO_REQUESTED},
        INFO_REQUESTED: {PENDING},
        REJECTED: {PENDING},  # a rejected institute can re-apply
        APPROVED: {SUSPENDED},
        SUSPENDED: {APPROVED},
    }
    REASON_REQUIRED = {REJECTED, INFO_REQUESTED, SUSPENDED}


class InstituteType:
    PRIVATE_TRAINING_AFFILIATED = "PRIVATE_TRAINING_AFFILIATED"
    CTEVT_AFFILIATED = "CTEVT_AFFILIATED"
    VOCATIONAL_TRAINING = "VOCATIONAL_TRAINING"
    LANGUAGE_SCHOOL = "LANGUAGE_SCHOOL"
    COMPANY = "COMPANY"
    NGO_INGO = "NGO_INGO"
    GOVERNMENT = "GOVERNMENT"

    CHOICES = (
        (PRIVATE_TRAINING_AFFILIATED, "Private Training Affiliated"),
        (CTEVT_AFFILIATED, "CTEVT Affiliated"),
        (VOCATIONAL_TRAINING, "Vocational Training"),
        (LANGUAGE_SCHOOL, "Language School"),
        (COMPANY, "Company"),
        (NGO_INGO, "NGO/INGO"),
        (GOVERNMENT, "Government"),
    )


class MemberRole:
    OWNER = "OWNER"
    STAFF = "STAFF"
    CHOICES = ((OWNER, "Owner"), (STAFF, "Staff"))


class DocumentStatus:
    PENDING, VERIFIED, REJECTED = "PENDING", "VERIFIED", "REJECTED"
    RE_UPLOADED = "RE_UPLOADED"  # a rejected document the institute has uploaded again
    CHOICES = (
        (PENDING, "Pending"),
        (VERIFIED, "Verified"),
        (REJECTED, "Rejected"),
        (RE_UPLOADED, "Re-uploaded"),
    )

AWAITING_REVIEW = "AWAITING_REVIEW"


class InvitationStatus:
    PENDING, ACCEPTED, REVOKED = "PENDING", "ACCEPTED", "REVOKED"
    CHOICES = ((PENDING, "Pending"), (ACCEPTED, "Accepted"), (REVOKED, "Revoked"))


# what `institute/action-required/` counts as a finished profile section
DESCRIPTION_MIN_LENGTH = 150  # characters
GALLERY_MIN_PHOTOS = 4
SOCIAL_LINKS_MIN = 2
