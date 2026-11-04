import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.db.models.functions import Length
from django.db.models.lookups import GreaterThanOrEqual
from django.utils import timezone
from django_q.tasks import async_task

from apps.institutes.constants import (
    DESCRIPTION_MIN_LENGTH,
    GALLERY_MIN_PHOTOS,
    SOCIAL_LINKS_MIN,
    DocumentStatus,
    InstituteStatus,
    InvitationStatus,
    MemberRole,
)
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
    InstituteStatusLog,
)
from apps.institutes.validators import validate_institute_location
from apps.users import services as user_services
from apps.users.constants import OTPPurpose, Role, SocialPlatform

User = get_user_model()


# status workflow
def _transition(institute, to, *, reason="", check=None, by=None):
    """Move an institute to a new status and log it. Caller must be inside transaction.atomic()."""
    institute = Institute.objects.select_for_update().get(pk=institute.pk)
    previous = institute.status
    if to not in InstituteStatus.TRANSITIONS[previous]:
        raise ValidationError(f"Cannot move from {previous} to {to}.")
    if to in InstituteStatus.REASON_REQUIRED and not reason.strip():
        raise ValidationError({"reason": "A reason is required."})
    if check:
        check(institute)
    institute.status = to
    institute.status_reason = reason
    institute.save(update_fields=["status", "status_reason", "modified_at"])
    InstituteStatusLog.objects.create(
        institute=institute,
        from_status=previous,
        to_status=to,
        reason=reason.strip(),
        changed_by=by,
    )
    return institute


def _ready_for_approval(institute):
    if not institute.locations.filter(is_active=True).exists():
        raise ValidationError("Add at least one active location before approval.")
    if not institute.documents.exists():
        raise ValidationError(
            "Upload at least one verification document before approval."
        )


@transaction.atomic
def approve(institute, *, by):
    # TODO(notify): the institute owner
    institute = _transition(
        institute, InstituteStatus.APPROVED, check=_ready_for_approval, by=by
    )
    # approving the institute accepts everything it uploaded so far; a document added
    # later starts as PENDING (the model default) and is verified by an admin on its own
    institute.documents.exclude(status=DocumentStatus.VERIFIED).update(
        status=DocumentStatus.VERIFIED, modified_at=timezone.now()
    )
    return institute


@transaction.atomic
def reject(institute, *, by, reason):
    return _transition(institute, InstituteStatus.REJECTED, reason=reason, by=by)


@transaction.atomic
def request_info(institute, *, by, reason):
    return _transition(institute, InstituteStatus.INFO_REQUESTED, reason=reason, by=by)


@transaction.atomic
def suspend(institute, *, by, reason):
    # Staff keep their accounts (they can still log in); the public queries hide the institute.
    return _transition(institute, InstituteStatus.SUSPENDED, reason=reason, by=by)


@transaction.atomic
def reinstate(institute, *, by):
    return _transition(institute, InstituteStatus.APPROVED, by=by)


@transaction.atomic
def resubmit(institute, *, by=None):
    """The owner answers an info request, or re-applies after a rejection."""
    return _transition(institute, InstituteStatus.PENDING, by=by)


@transaction.atomic
def update_profile(institute, **fields):
    """Change the institute's own columns. The contact person, the CEO and the social links have
    their own endpoints and services (`save_ceo`, `add_social_link`, ...)."""
    institute = Institute.objects.select_for_update().get(pk=institute.pk)
    renamed = "name" in fields and fields["name"] != institute.name
    for name, value in fields.items():
        setattr(institute, name, value)
    institute.save(update_fields=[*fields, "modified_at"])
    if renamed:
        # the institute name is part of every training's search vector
        transaction.on_commit(
            lambda: async_task(
                "apps.training.tasks.refresh_institute_trainings",
                institute.pk,
                save=False,
            )
        )
    return institute


@transaction.atomic
def set_cover_img(institute, image):
    """Replace the institute's cover image. The old file is deleted once the change is committed."""
    institute = Institute.objects.select_for_update().get(pk=institute.pk)
    old_name, storage = institute.cover_img.name, institute.cover_img.storage
    institute.cover_img = image
    institute.save(update_fields=["cover_img", "modified_at"])
    if old_name:
        transaction.on_commit(lambda: storage.delete(old_name))
    return institute


# registration and locations
def register(
    *, institute_data, owner, contact, otp, locations=(), ceo=None, social_links=()
):
    """Create an institute with its owner, contact, locations and links.

    The owner's email is the institute's login, and `otp` is the code emailed to it by
    users.services.send_registration_otp. The code is checked before anything is written and used up at the end,
    inside the transaction, so a failed registration rolls back and a code works only once.
    """
    email = user_services.normalize_email(owner["email"])
    user_services.verify_otp(email, otp, purpose=OTPPurpose.INSTITUTE_REGISTRATION)
    with transaction.atomic():
        institute = _create_registration(
            institute_data=institute_data,
            owner=owner,
            contact=contact,
            locations=locations,
            ceo=ceo,
            social_links=social_links,
        )
        # Last, so that of two parallel requests with the same code only one gets through; the
        # other raises here and its writes are rolled back.
        user_services.consume_otp(email, purpose=OTPPurpose.INSTITUTE_REGISTRATION)
    return institute


def _create_registration(*, institute_data, owner, contact, locations, ceo, social_links):
    for item in locations:
        validate_institute_location(
            item["location"]
        )  # bulk_create skips model validation
    institute = Institute.objects.create(**institute_data)
    owner_user = User.objects.create_user(role=Role.INSTITUTE_STAFF, **owner)
    InstituteMember.objects.create(
        user=owner_user, institute=institute, role=MemberRole.OWNER
    )
    InstituteLocation.objects.bulk_create(
        [
            InstituteLocation(institute=institute, is_main=(i == 0), **item)
            for i, item in enumerate(locations)
        ]
    )
    contact = dict(contact)
    website = contact.pop("website", None)  # not a column: it is the "website" social link
    InstituteContact.objects.create(institute=institute, **contact)
    if ceo:
        InstituteCEO.objects.create(institute=institute, **ceo)
    links = [InstituteSocialLink(institute=institute, **link) for link in social_links]
    if website:
        links.append(
            InstituteSocialLink(
                institute=institute, platform=SocialPlatform.WEBSITE, url=website
            )
        )
    InstituteSocialLink.objects.bulk_create(links)
    return institute


@transaction.atomic
def add_location(institute, *, location, address, contact_phone="", map_url=""):
    validate_institute_location(location)
    Institute.objects.select_for_update().get(
        pk=institute.pk
    )  # serialise per institute
    first = not institute.locations.filter(is_active=True).exists()
    return InstituteLocation.objects.create(
        institute=institute,
        location=location,
        address=address,
        contact_phone=contact_phone,
        map_url=map_url,
        is_main=first,
    )


@transaction.atomic
def update_location(instance, **fields):
    if "location" in fields:
        validate_institute_location(fields["location"])
    instance = InstituteLocation.objects.select_for_update().get(pk=instance.pk)
    for name, value in fields.items():
        setattr(instance, name, value)
    instance.save(update_fields=[*fields, "modified_at"])
    return instance


@transaction.atomic
def set_main_location(location):
    Institute.objects.select_for_update().get(pk=location.institute_id)
    location = InstituteLocation.objects.get(pk=location.pk)
    if not location.is_active:
        raise ValidationError("An inactive location cannot be the main office.")
    InstituteLocation.objects.filter(
        institute_id=location.institute_id, is_main=True
    ).exclude(pk=location.pk).update(
        is_main=False
    )  # clear first, then set (partial unique index)
    location.is_main = True
    location.save(update_fields=["is_main", "modified_at"])
    return location


@transaction.atomic
def deactivate_location(location):
    Institute.objects.select_for_update().get(pk=location.institute_id)
    location = InstituteLocation.objects.get(pk=location.pk)
    if location.is_main:
        raise ValidationError("Choose another main office first.")
    location.is_active = False
    location.save(update_fields=["is_active", "modified_at"])
    return location


# documents and gallery
@transaction.atomic
def add_document(institute, *, name, file):
    """Upload a new verification document. An institute has one row per document name (any case),
    so a name it already used is refused; a rejected document is uploaded again with
    `reupload_document`."""
    name = name.strip()
    # one upload per institute at a time, so two requests cannot both find "no such name"
    Institute.objects.select_for_update().get(pk=institute.pk)
    if InstituteDocument.objects.filter(institute=institute, name__iexact=name).exists():
        raise ValidationError(
            {
                "name": "A document with this name already exists. If it was rejected, "
                "upload the file again to that document instead of adding a new one."
            }
        )
    return InstituteDocument.objects.create(institute=institute, name=name, file=file)


@transaction.atomic
def reupload_document(document, *, file):
    """Replace the file of a `REJECTED` document and send it back to review as `RE_UPLOADED`. The
    row, and so its id, stays. Any other status is refused, so a file an admin may be reading, or
    has verified, is never swapped underneath them."""
    document = InstituteDocument.objects.select_for_update().get(pk=document.pk)
    if document.status != DocumentStatus.REJECTED:
        raise ValidationError("Only a rejected document can be uploaded again.")
    old_name, storage = document.file.name, document.file.storage
    document.file = file
    document.status = DocumentStatus.RE_UPLOADED
    document.save(update_fields=["file", "status", "modified_at"])
    transaction.on_commit(lambda: storage.delete(old_name))  # the rejected file is not kept
    return document


@transaction.atomic
def review_document(document, *, status, by):
    if status not in (DocumentStatus.VERIFIED, DocumentStatus.REJECTED):
        raise ValidationError({"status": "Choose VERIFIED or REJECTED."})
    document = InstituteDocument.objects.select_for_update().get(pk=document.pk)
    document.status = status
    document.save(update_fields=["status", "modified_at"])
    return document


@transaction.atomic
def add_gallery_image(institute, *, image, caption=""):
    Institute.objects.select_for_update().get(pk=institute.pk)
    return InstituteGalleryImage.objects.create(
        institute=institute,
        image=image,
        caption=caption,
        position=institute.gallery.count(),  # appended at the end
    )


@transaction.atomic
def reorder_gallery(institute, ids):
    images = {
        i.pk: i
        for i in InstituteGalleryImage.objects.select_for_update().filter(
            institute=institute
        )
    }
    if sorted(images) != sorted(ids):
        raise ValidationError("Send every gallery image id exactly once.")
    for position, pk in enumerate(ids):
        images[pk].position = position
    InstituteGalleryImage.objects.bulk_update(images.values(), ["position"])


# staff and invitations
def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


@transaction.atomic
def invite(*, institute, email, by):
    """Invite a new staff member. The raw token exists only in the email and in memory:
    the database keeps a hash, and the queued task is not saved in django-q's results table."""
    email = email.strip()
    Institute.objects.select_for_update().get(pk=institute.pk)
    institute.invitations.filter(  # an expired invite must not block a new one
        email__iexact=email,
        status=InvitationStatus.PENDING,
        expires_at__lt=timezone.now(),
    ).update(status=InvitationStatus.REVOKED)
    if User.objects.filter(email__iexact=email).exists():
        raise ValidationError(
            {"email": "This email already has an account."}
        )  # one institute per user
    if institute.invitations.filter(
        email__iexact=email, status=InvitationStatus.PENDING
    ).exists():
        raise ValidationError(
            {"email": "An invitation is already pending for this email."}
        )
    token = secrets.token_urlsafe(32)
    invitation = InstituteInvitation.objects.create(
        institute=institute,
        email=email,
        role=MemberRole.STAFF,
        invited_by=by,
        token_hash=_hash(token),
        expires_at=timezone.now()
        + timedelta(days=settings.INSTITUTE_INVITATION_TTL_DAYS),
    )
    transaction.on_commit(
        lambda: async_task(
            "apps.institutes.tasks.send_invitation_email",
            invitation.pk,
            token,
            save=False,
        )
    )
    return invitation


@transaction.atomic
def accept_invitation(*, token, full_name, password):
    invitation = (
        InstituteInvitation.objects.select_for_update(of=("self",))
        .select_related("institute")
        .filter(token_hash=_hash(token), status=InvitationStatus.PENDING)
        .first()
    )
    if invitation is None or invitation.expires_at < timezone.now():
        raise ValidationError("This invitation is invalid or has expired.")
    if User.objects.filter(email__iexact=invitation.email).exists():
        raise ValidationError("This email already has an account.")
    user = User.objects.create_user(
        email=invitation.email,
        password=password,
        full_name=full_name,
        role=Role.INSTITUTE_STAFF,
    )
    InstituteMember.objects.create(
        user=user, institute=invitation.institute, role=invitation.role
    )
    invitation.status = InvitationStatus.ACCEPTED
    invitation.save(update_fields=["status", "modified_at"])
    return user


@transaction.atomic
def revoke_invitation(invitation, *, by):
    invitation = InstituteInvitation.objects.select_for_update().get(pk=invitation.pk)
    if invitation.status != InvitationStatus.PENDING:
        raise ValidationError("Only a pending invitation can be revoked.")
    invitation.status = InvitationStatus.REVOKED
    invitation.save(update_fields=["status", "modified_at"])
    return invitation


@transaction.atomic
def remove_staff(member, *, by):
    member = (
        InstituteMember.objects.select_for_update()
        .select_related("user")
        .get(pk=member.pk)
    )
    if member.role == MemberRole.OWNER:
        raise ValidationError("The owner cannot be removed.")
    user = member.user
    member.delete()
    # deactivates the account and revokes its refresh tokens
    user_services.set_status(user, active=False, by=by)


@transaction.atomic
def save_ceo(institute, **fields):
    """Create or update the CEO. Only the keys sent are changed, so an omitted photo stays."""
    Institute.objects.select_for_update().get(
        pk=institute.pk
    )  # serialise per institute
    ceo = InstituteCEO.objects.filter(institute=institute).first() or InstituteCEO(
        institute=institute
    )
    for name, value in fields.items():
        setattr(ceo, name, value)
    ceo.save()
    return ceo


def _check_platform_free(institute_id, platform, *, exclude_pk=None):
    if platform == SocialPlatform.OTHER:
        return
    taken = InstituteSocialLink.objects.filter(
        institute_id=institute_id, platform=platform
    )
    if exclude_pk:
        taken = taken.exclude(pk=exclude_pk)
    if taken.exists():
        raise ValidationError(
            {"platform": "This platform is already added."}
        )  # 400, not a 409


@transaction.atomic
def add_social_link(institute, **fields):
    Institute.objects.select_for_update().get(pk=institute.pk)
    _check_platform_free(institute.pk, fields["platform"])
    return InstituteSocialLink.objects.create(institute=institute, **fields)


@transaction.atomic
def update_social_link(link, **fields):
    Institute.objects.select_for_update().get(pk=link.institute_id)
    if "platform" in fields:
        _check_platform_free(link.institute_id, fields["platform"], exclude_pk=link.pk)
    for name, value in fields.items():
        setattr(link, name, value)
    link.save()
    return link


@transaction.atomic
def update_contact(contact, **fields):
    """Change the contact row. `website` is not one of its columns: it is the institute's
    "website" social link, set when given and removed when blank. Only the keys sent are changed."""
    Institute.objects.select_for_update().get(
        pk=contact.institute_id
    )  # serialise per institute
    sets_website = "website" in fields
    website = fields.pop("website", None)
    for name, value in fields.items():
        setattr(contact, name, value)
    if fields:
        contact.save(update_fields=[*fields, "modified_at"])
    if sets_website:
        _set_website(contact.institute_id, website)
    return contact


def _set_website(institute_id, url):
    """One website link per institute (the unique index allows one per platform but `other`)."""
    links = InstituteSocialLink.objects.filter(
        institute_id=institute_id, platform=SocialPlatform.WEBSITE
    )
    if url:
        InstituteSocialLink.objects.update_or_create(
            institute_id=institute_id,
            platform=SocialPlatform.WEBSITE,
            defaults={"url": url},
        )
    else:
        links.delete()


# profile completion (read only)
def _at_least(queryset, count):
    """EXISTS that is true only when the subquery has `count` rows or more
    (`LIMIT 1 OFFSET count-1`), so a count threshold needs no join or GROUP BY."""
    return Exists(queryset.values("pk")[count - 1 : count])


def annotate_profile_completion(queryset):
    """Annotate institutes with one boolean per profile section: `True` when the section is
    filled in, `False` when the institute still has to fill it. One query for all sections.

    The text columns are NOT NULL, so "empty" is `""`. The API strips surrounding whitespace
    on write, so a stored description's length is its trimmed length.
    """
    mine = {"institute": OuterRef("pk")}
    return queryset.annotate(
        has_description=GreaterThanOrEqual(
            Length("description"), DESCRIPTION_MIN_LENGTH
        ),
        # the row can exist without a message or photo: both are blank=True
        has_ceo=Exists(
            InstituteCEO.objects.filter(**mine).exclude(Q(message="") | Q(photo=""))
        ),
        has_gallery=_at_least(
            InstituteGalleryImage.objects.filter(**mine), GALLERY_MIN_PHOTOS
        ),
        # registration fills the first three; only the landline is optional
        has_contact=Exists(
            InstituteContact.objects.filter(**mine).exclude(
                Q(contact_person="")
                | Q(contact_phone="")
                | Q(contact_email="")
                | Q(telephone_number="")
            )
        ),
        has_social_links=_at_least(
            InstituteSocialLink.objects.filter(**mine), SOCIAL_LINKS_MIN
        ),
    )
