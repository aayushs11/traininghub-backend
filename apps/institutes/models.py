from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import OneToOneField
from django.db.models.functions import Lower

from apps.common.models.base import BaseModel, SlugModel
from apps.common.utils.helpers import get_upload_path
from apps.common.validators import validate_phone_number
from apps.institutes.constants import (
    DocumentStatus,
    InstituteStatus,
    InstituteType,
    InvitationStatus,
    MemberRole,
)
from apps.institutes.storage import PrivateStorage
from apps.institutes.validators import validate_document_file, validate_image_file
from apps.users.constants import SocialPlatform


class Institute(BaseModel, SlugModel):
    name = models.CharField(max_length=255)
    registration_number = models.CharField(max_length=50)
    pan_vat_number = models.CharField(max_length=9, unique=True)
    type = models.CharField(max_length=30, choices=InstituteType.CHOICES)
    established_year = models.PositiveSmallIntegerField(null=True, blank=True)
    description = models.TextField(blank=True)
    logo = models.ImageField(
        upload_to=get_upload_path, blank=True, validators=[validate_image_file]
    )
    cover_img = models.ImageField(
        upload_to=get_upload_path, blank=True, validators=[validate_image_file]
    )
    status = models.CharField(
        max_length=20, choices=InstituteStatus.CHOICES, default=InstituteStatus.PENDING
    )
    status_reason = models.TextField(blank=True)

    class Meta:
        indexes = [models.Index(fields=["status"], name="institutes_status_idx")]

    def __str__(self):
        return self.name


class InstituteMember(BaseModel):
    # OneToOne: a staff user belongs to ONE institute only, enforced by the database.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="membership"
    )
    institute = models.ForeignKey(
        Institute, on_delete=models.CASCADE, related_name="members"
    )
    role = models.CharField(max_length=10, choices=MemberRole.CHOICES)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["institute"],
                condition=models.Q(role=MemberRole.OWNER),
                name="institutes_one_owner_per_institute",
            ),
        ]


class InstituteInvitation(BaseModel):
    institute = models.ForeignKey(
        Institute, on_delete=models.CASCADE, related_name="invitations"
    )
    email = models.EmailField()
    role = models.CharField(
        max_length=10, choices=MemberRole.CHOICES, default=MemberRole.STAFF
    )
    token_hash = models.CharField(
        max_length=64, unique=True
    )  # sha256 of the emailed token
    status = models.CharField(
        max_length=10,
        choices=InvitationStatus.CHOICES,
        default=InvitationStatus.PENDING,
    )
    expires_at = models.DateTimeField()
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(  # one pending invite per institute and email
                Lower("email"),
                "institute",
                condition=models.Q(status=InvitationStatus.PENDING),
                name="institutes_one_pending_invite",
            ),
        ]
        indexes = [
            models.Index(fields=["expires_at"], name="institutes_invite_expiry_idx")
        ]


class InstituteDocument(BaseModel):
    institute = models.ForeignKey(
        Institute, on_delete=models.CASCADE, related_name="documents"
    )
    name = models.CharField(max_length=150)
    file = models.FileField(
        upload_to=get_upload_path,
        storage=PrivateStorage,
        validators=[validate_document_file],
    )
    status = models.CharField(
        max_length=20, choices=DocumentStatus.CHOICES, default=DocumentStatus.PENDING
    )

    class Meta:
        indexes = [
            models.Index(
                fields=["institute", "status"], name="institutes_doc_status_idx"
            )
        ]
        constraints = [
            # one row per document: a rejected one is replaced in place (`services.reupload_document`)
            models.UniqueConstraint(
                "institute",
                Lower("name"),
                name="institutes_document_unique_name",
                violation_error_message="A document with this name already exists.",
            ),
        ]


class InstituteLocation(BaseModel):
    institute = models.ForeignKey(
        Institute, on_delete=models.CASCADE, related_name="locations"
    )
    # must be an active MUNICIPALITY-level Location; checked by validate_institute_location
    location = models.ForeignKey(
        "common.Location", on_delete=models.PROTECT, related_name="+"
    )
    address = models.CharField(max_length=255)
    map_url = models.URLField(blank=True)  # "Google Map location" link
    contact_phone = models.CharField(
        max_length=25, blank=True, validators=[validate_phone_number]
    )
    is_main = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["institute"],
                condition=models.Q(is_main=True, is_active=True),
                name="institutes_one_main_location",
            ),
            models.CheckConstraint(
                condition=~models.Q(is_main=True, is_active=False),
                name="institutes_main_location_is_active",
            ),
        ]
        indexes = [
            models.Index(
                fields=["institute", "is_active"], name="institutes_loc_active_idx"
            )
        ]


class InstituteStatusLog(BaseModel):
    """One row per status change (approve, reject, request info, suspend, resubmit ...), so the
    review history survives: `Institute.status_reason` only keeps the latest reason."""

    institute = models.ForeignKey(
        Institute, on_delete=models.CASCADE, related_name="status_logs"
    )
    from_status = models.CharField(max_length=20, choices=InstituteStatus.CHOICES)
    to_status = models.CharField(max_length=20, choices=InstituteStatus.CHOICES)
    reason = models.TextField(blank=True)
    # the admin who reviewed, or the owner who resubmitted; null if unknown or the user is deleted
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        indexes = [
            models.Index(
                fields=["institute", "created_at"], name="institutes_statuslog_idx"
            )
        ]


class InstituteGalleryImage(BaseModel):
    institute = models.ForeignKey(
        Institute, on_delete=models.CASCADE, related_name="gallery"
    )
    image = models.ImageField(
        upload_to=get_upload_path, validators=[validate_image_file]
    )
    caption = models.CharField(max_length=200, blank=True)
    position = models.PositiveSmallIntegerField(
        default=0
    )  # no unique: a reorder is one bulk_update

    class Meta:
        indexes = [
            models.Index(
                fields=["institute", "position"], name="institutes_gallery_pos_idx"
            )
        ]


class InstituteContact(BaseModel):
    """Required contact person for institute registration."""

    institute = models.OneToOneField(
        Institute, on_delete=models.CASCADE, related_name="contact"
    )
    contact_person = models.CharField(max_length=150)
    contact_phone = models.CharField(max_length=25, validators=[validate_phone_number])
    contact_email = models.EmailField()
    telephone_number = models.CharField(
        max_length=25,
        blank=True,
        validators=[validate_phone_number],
    )

    def __str__(self):
        return f"{self.contact_person} - {self.institute.name}"


class InstituteCEO(BaseModel):
    """Optional CEO/leadership profile."""

    institute = models.OneToOneField(
        Institute, on_delete=models.CASCADE, related_name="ceo"
    )
    name = models.CharField(max_length=150)
    message = models.TextField(blank=True)
    photo = models.ImageField(
        upload_to=get_upload_path, blank=True, validators=[validate_image_file]
    )
    linkedin_url = models.URLField(blank=True)

    def __str__(self):
        return f"CEO: {self.name} - {self.institute.name}"


class InstituteSocialLink(BaseModel):
    """Extensible social media links for institute."""

    institute = models.ForeignKey(
        Institute, on_delete=models.CASCADE, related_name="social_links"
    )
    platform = models.CharField(max_length=20, choices=SocialPlatform.CHOICES)
    url = models.URLField()
    label = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["institute", "platform"],
                condition=~models.Q(
                    platform=SocialPlatform.OTHER
                ),  # any number of "other" links
                name="institutes_unique_social_platform",
            )
        ]

    def __str__(self):
        return f"{self.get_platform_display()} - {self.institute.name}"
