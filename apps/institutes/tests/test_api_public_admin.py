from unittest import mock

from django.core import mail
from django.utils.module_loading import import_string
from rest_framework.test import APITestCase

from apps.institutes import services
from apps.institutes.constants import InstituteStatus
from apps.institutes.models import (
    Institute,
    InstituteCEO,
    InstituteContact,
    InstituteLocation,
    InstituteSocialLink,
)
from apps.institutes.tests.helpers import (
    OWNER_EMAIL,
    PAN_VAT_NUMBER,
    PASSWORD,
    TempMediaMixin,
    User,
    add_location,
    count_queries,
    issue_registration_otp,
    make_institute,
    make_municipality,
    pdf,
    png,
    reset_otps,
    reset_throttles,
)
from apps.users import services as user_services
from apps.users.constants import OTPPurpose, Role

REGISTER_URL = "/api/v1/institute/register/"
SEND_OTP_URL = REGISTER_URL + "send-otp/"
VERIFY_OTP_URL = REGISTER_URL + "verify-otp/"
PUBLIC_URL = "/api/v1/institutes/"
ADMIN_URL = "/api/v1/admin/institutes/"


class RegistrationApiTests(APITestCase):
    def setUp(self):
        reset_throttles()
        reset_otps()
        self.municipality = make_municipality()

    def owner(self, email=OWNER_EMAIL, **extra):
        return {
            "email": email,
            "password": PASSWORD,
            "confirm_password": PASSWORD,
            **extra,
        }

    def payload(self, **overrides):
        """A valid registration. Unless the test passes its own `otp`, a code is requested for the
        owner's email first, the way the form does."""
        owner = overrides.pop("owner", None) or self.owner()
        otp = overrides.pop("otp", None) or issue_registration_otp(owner["email"])
        data = {
            "name": "New Institute",
            "registration_number": "REG-123",
            "pan_vat_number": PAN_VAT_NUMBER,
            "type": "COMPANY",
            "otp": otp,
            "owner": owner,
            "contact": {
                "contact_person": "Contact Person",
                "contact_phone": "9811111111",
                "contact_email": "contact@example.com",
            },
            "locations": [{"location": self.municipality.pk, "address": "Main Road"}],
        }
        data.update(overrides)
        return data

    def post(self, **overrides):
        return self.client.post(REGISTER_URL, self.payload(**overrides), format="json")

    def test_registers_institute_owner_and_location(self):
        res = self.post()
        self.assertEqual(res.status_code, 201, res.data)
        institute = Institute.objects.get(slug=res.data["slug"])
        self.assertEqual(institute.status, InstituteStatus.PENDING)
        self.assertEqual(institute.members.get().user.email, OWNER_EMAIL)
        self.assertTrue(institute.locations.get().is_main)

    def test_the_response_never_echoes_credentials(self):
        res = self.post()
        self.assertNotIn("owner", res.data)
        self.assertNotIn("password", str(res.data))
        self.assertNotIn("otp", res.data)

    def test_status_slug_and_role_in_the_payload_are_ignored(self):
        res = self.post(
            status="APPROVED",
            slug="hacked",
            owner=self.owner(role="SUPER_ADMIN", is_staff=True),
        )
        self.assertEqual(res.status_code, 201, res.data)
        institute = Institute.objects.get(slug=res.data["slug"])
        self.assertEqual(institute.status, InstituteStatus.PENDING)
        self.assertNotEqual(institute.slug, "hacked")
        owner = institute.members.get().user
        self.assertEqual(owner.role, Role.INSTITUTE_STAFF)
        self.assertFalse(owner.is_staff)

    def contact(self, **extra):
        return {
            "contact_person": "Contact Person",
            "contact_phone": "9811111111",
            "contact_email": "contact@example.com",
            **extra,
        }

    def test_a_contact_website_is_saved_as_the_websites_social_link(self):
        res = self.post(contact=self.contact(website="https://site.example/"))
        self.assertEqual(res.status_code, 201, res.data)
        institute = Institute.objects.get(slug=res.data["slug"])
        link = institute.social_links.get()
        self.assertEqual((link.platform, link.url), ("website", "https://site.example/"))

    def test_a_blank_or_missing_contact_website_adds_no_link(self):
        for i, extra in enumerate(({}, {"website": ""}, {"website": None})):
            with self.subTest(extra=extra):
                reset_throttles()
                res = self.post(
                    contact=self.contact(**extra),
                    owner=self.owner(email=f"owner{i}@example.com"),
                    pan_vat_number=f"90010000{i}",  # the column is unique
                )
                self.assertEqual(res.status_code, 201, res.data)
                self.assertFalse(
                    InstituteSocialLink.objects.filter(institute__slug=res.data["slug"]).exists()
                )

    def test_the_website_sits_next_to_the_other_social_links(self):
        res = self.post(
            contact=self.contact(website="https://site.example/"),
            social_links=[{"platform": "facebook", "url": "https://facebook.com/x"}],
        )
        self.assertEqual(res.status_code, 201, res.data)
        links = Institute.objects.get(slug=res.data["slug"]).social_links.all()
        self.assertEqual(sorted(l.platform for l in links), ["facebook", "website"])

    def test_the_website_cannot_be_sent_in_the_contact_and_as_a_social_link(self):
        res = self.post(
            contact=self.contact(website="https://site.example/"),
            social_links=[{"platform": "website", "url": "https://other.example/"}],
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("social_links", res.data)
        self.assertEqual(Institute.objects.count(), 0)

    def test_a_bad_contact_website_is_refused(self):
        for website in ("not a url", "https://x.example/" + "a" * 200):
            with self.subTest(website=website[:30]):
                res = self.post(contact=self.contact(website=website))
                self.assertEqual(res.status_code, 400)
                self.assertIn("website", res.data["contact"])
        self.assertEqual(Institute.objects.count(), 0)

    def test_the_pan_vat_number_is_saved_and_echoed(self):
        res = self.post()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["pan_vat_number"], PAN_VAT_NUMBER)
        institute = Institute.objects.get(slug=res.data["slug"])
        self.assertEqual(institute.pan_vat_number, PAN_VAT_NUMBER)

    def test_the_pan_vat_number_is_required(self):
        payload = self.payload()
        del payload["pan_vat_number"]
        res = self.client.post(REGISTER_URL, payload, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("pan_vat_number", res.data)
        self.assertFalse(Institute.objects.exists())

    def test_a_malformed_pan_vat_number_is_a_400(self):
        for value in (
            "12345678",  # eight digits
            "1234567890",  # ten digits
            "12345678a",
            "123-45-678",
            "१२३४५६७८९",  # nine Devanagari digits
            "",
            "   ",
        ):
            with self.subTest(value=value):
                reset_throttles()  # five registrations an hour, failed ones count
                res = self.post(pan_vat_number=value)
                self.assertEqual(res.status_code, 400)
                self.assertIn("pan_vat_number", res.data)
        self.assertFalse(Institute.objects.exists())

    def test_spaces_around_the_pan_vat_number_are_trimmed(self):
        res = self.post(pan_vat_number=f"  {PAN_VAT_NUMBER} ")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["pan_vat_number"], PAN_VAT_NUMBER)

    def test_a_pan_vat_number_already_in_use_is_a_400(self):
        make_institute(
            name="First", email="first@example.com", pan_vat_number=PAN_VAT_NUMBER
        )
        res = self.post()
        self.assertEqual(res.status_code, 400)
        self.assertIn("pan_vat_number", res.data)
        self.assertEqual(Institute.objects.count(), 1)

    def test_the_contact_telephone_number_is_saved(self):
        contact = {
            "contact_person": "Contact Person",
            "contact_phone": "9811111111",
            "contact_email": "contact@example.com",
            "telephone_number": "014123456",
        }
        res = self.post(contact=contact)
        self.assertEqual(res.status_code, 201, res.data)
        institute = Institute.objects.get(slug=res.data["slug"])
        self.assertEqual(institute.contact.telephone_number, "014123456")

    def test_the_contact_telephone_number_is_optional(self):
        res = self.post()  # the payload's contact has none
        self.assertEqual(res.status_code, 201, res.data)
        institute = Institute.objects.get(slug=res.data["slug"])
        self.assertEqual(institute.contact.telephone_number, "")

    def test_a_blank_contact_telephone_number_is_accepted(self):
        contact = {
            "contact_person": "Contact Person",
            "contact_phone": "9811111111",
            "contact_email": "contact@example.com",
            "telephone_number": "",
        }
        res = self.post(contact=contact)
        self.assertEqual(res.status_code, 201, res.data)

    def test_a_bad_contact_telephone_number_is_a_400(self):
        for value in ("abc", "12345", "0141234567890123"):
            with self.subTest(value=value):
                reset_throttles()
                contact = {
                    "contact_person": "Contact Person",
                    "contact_phone": "9811111111",
                    "contact_email": "contact@example.com",
                    "telephone_number": value,
                }
                res = self.post(contact=contact)
                self.assertEqual(res.status_code, 400)
                self.assertIn("telephone_number", res.data["contact"])
        self.assertFalse(Institute.objects.exists())

    def test_locations_are_required(self):
        payload = self.payload()
        del payload["locations"]
        res = self.client.post(REGISTER_URL, payload, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("locations", res.data)
        self.assertFalse(Institute.objects.exists())

    def test_an_empty_locations_list_is_refused(self):
        res = self.post(locations=[])
        self.assertEqual(res.status_code, 400)
        self.assertIn("locations", res.data)
        self.assertFalse(Institute.objects.exists())

    def test_duplicate_email_is_a_400_in_any_case(self):
        payload = self.payload()  # the code is requested while the address is still free
        User.objects.create_user("Owner@Example.com", PASSWORD)
        res = self.client.post(REGISTER_URL, payload, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("email", res.data["owner"])
        self.assertFalse(Institute.objects.exists())

    def test_a_province_is_not_a_valid_location(self):
        province_id = self.municipality.province_id
        res = self.post(locations=[{"location": province_id, "address": "Road"}])
        self.assertEqual(res.status_code, 400)
        self.assertFalse(Institute.objects.exists())

    def test_weak_password_unknown_type_and_missing_owner_are_400(self):
        weak = {"email": "o@example.com", "password": "123", "confirm_password": "123"}
        self.assertEqual(self.post(owner=weak).status_code, 400)
        self.assertEqual(self.post(type="SCHOOL").status_code, 400)
        payload = self.payload()
        del payload["owner"]
        self.assertEqual(
            self.client.post(REGISTER_URL, payload, format="json").status_code, 400
        )

    def test_the_owner_can_log_in_while_the_institute_is_pending(self):
        self.post()
        res = self.client.post(
            "/api/v1/user/auth/login/",
            {"email": OWNER_EMAIL, "password": PASSWORD},
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn("access", res.data)

    def test_the_sixth_registration_in_an_hour_is_throttled(self):
        for i in range(5):
            res = self.post(
                name=f"Institute {i}",
                pan_vat_number=f"10000000{i}",  # each institute needs its own
                owner=self.owner(f"owner{i}@example.com"),
            )
            self.assertEqual(res.status_code, 201)
        res = self.post(
            name="One more",
            pan_vat_number="100000009",
            owner=self.owner("owner9@example.com"),
        )
        self.assertEqual(res.status_code, 429)

    # password confirmation
    def test_mismatched_passwords_are_refused_and_nothing_is_created(self):
        res = self.post(owner=self.owner(confirm_password="Different-pass-123!"))
        self.assertEqual(res.status_code, 400)
        self.assertEqual(
            res.data["owner"]["confirm_password"], ["Passwords do not match."]
        )
        self.assertFalse(Institute.objects.exists())
        self.assertFalse(User.objects.exists())

    def test_the_confirmation_is_required(self):
        payload = self.payload()
        del payload["owner"]["confirm_password"]
        res = self.client.post(REGISTER_URL, payload, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("confirm_password", res.data["owner"])
        self.assertFalse(Institute.objects.exists())

    def test_the_comparison_is_case_sensitive(self):
        res = self.post(owner=self.owner(confirm_password=PASSWORD.swapcase()))
        self.assertEqual(res.status_code, 400)
        self.assertIn("confirm_password", res.data["owner"])

    def test_a_mismatch_keeps_the_code_so_the_form_can_be_corrected(self):
        payload = self.payload()
        payload["owner"]["confirm_password"] = "Different-pass-123!"
        self.assertEqual(
            self.client.post(REGISTER_URL, payload, format="json").status_code, 400
        )
        payload["owner"]["confirm_password"] = PASSWORD  # same code, fixed typo
        res = self.client.post(REGISTER_URL, payload, format="json")
        self.assertEqual(res.status_code, 201, res.data)

    def test_the_confirmation_is_never_stored(self):
        res = self.post()
        owner = Institute.objects.get(slug=res.data["slug"]).members.get().user
        self.assertTrue(owner.check_password(PASSWORD))
        self.assertFalse(hasattr(owner, "confirm_password"))

    # email verification
    def test_the_owner_email_and_the_code_are_required(self):
        payload = self.payload()
        del payload["owner"]["email"]
        res = self.client.post(REGISTER_URL, payload, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("email", res.data["owner"])
        payload = self.payload()
        del payload["otp"]
        res = self.client.post(REGISTER_URL, payload, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("otp", res.data)
        self.assertFalse(Institute.objects.exists())

    def test_a_wrong_code_is_refused_and_nothing_is_created(self):
        right = issue_registration_otp(OWNER_EMAIL)
        wrong = "000000" if right != "000000" else "111111"
        res = self.post(otp=wrong)
        self.assertEqual(res.status_code, 400)
        self.assertIn("otp", res.data)
        self.assertFalse(Institute.objects.exists())
        self.assertFalse(User.objects.filter(email=OWNER_EMAIL).exists())

    def test_registering_without_asking_for_a_code_is_refused(self):
        payload = self.payload()
        reset_otps()  # no code was ever sent for this address
        res = self.client.post(REGISTER_URL, payload, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("otp", res.data)
        self.assertFalse(Institute.objects.exists())

    def test_a_code_works_only_once(self):
        payload = self.payload()
        self.assertEqual(
            self.client.post(REGISTER_URL, payload, format="json").status_code, 201
        )
        res = self.client.post(VERIFY_OTP_URL, {"email": OWNER_EMAIL, "otp": payload["otp"]})
        self.assertEqual(res.status_code, 400)
        self.assertIn("otp", res.data)

    def test_a_code_for_one_address_does_not_work_for_another(self):
        code = issue_registration_otp("first@example.com")
        res = self.post(owner=self.owner("third@example.com"), otp=code)
        self.assertEqual(res.status_code, 400)
        self.assertIn("otp", res.data)
        self.assertFalse(Institute.objects.exists())

    def test_a_password_reset_code_is_not_a_registration_code(self):
        with mock.patch("apps.users.services.async_task"):
            User.objects.create_user("existing@example.com", PASSWORD)
            user_services.send_password_reset_otp("existing@example.com")
        reset_code = user_services.get_otp(
            "existing@example.com", purpose=OTPPurpose.PASSWORD_RESET
        )
        res = self.client.post(
            VERIFY_OTP_URL, {"email": "existing@example.com", "otp": reset_code}
        )
        self.assertEqual(res.status_code, 400)

    def test_the_owner_email_is_matched_ignoring_case(self):
        code = issue_registration_otp("owner@example.com")
        res = self.post(owner=self.owner("Owner@Example.com"), otp=code)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(User.objects.filter(email__iexact="owner@example.com").exists())

    def test_five_wrong_codes_lock_the_code_even_for_the_right_one(self):
        right = issue_registration_otp(OWNER_EMAIL)
        wrong = "000000" if right != "000000" else "111111"
        for _ in range(5):
            res = self.client.post(VERIFY_OTP_URL, {"email": OWNER_EMAIL, "otp": wrong})
            self.assertEqual(res.status_code, 400)
        res = self.client.post(VERIFY_OTP_URL, {"email": OWNER_EMAIL, "otp": right})
        self.assertEqual(res.status_code, 429)
        self.assertIn("Retry-After", res)
        self.assertEqual(self.post(otp=right).status_code, 429)
        self.assertFalse(Institute.objects.exists())


class RegistrationOtpApiTests(APITestCase):
    def setUp(self):
        reset_throttles()
        reset_otps()

    def send(self, email=OWNER_EMAIL):
        return self.client.post(SEND_OTP_URL, {"email": email}, format="json")

    def test_send_stores_a_code_and_queues_the_email_task_for_the_address(self):
        with mock.patch("apps.users.services.async_task") as queued:
            res = self.send()
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data, {"message": "Verification code sent to your email."})
        queued.assert_called_once_with(
            "apps.users.tasks.send_registration_otp_email_task", OWNER_EMAIL
        )
        code = user_services.get_otp(OWNER_EMAIL, purpose=OTPPurpose.INSTITUTE_REGISTRATION)
        self.assertRegex(code, r"^\d{6}$")
        self.assertNotIn(code, str(queued.call_args))  # the code never enters the queue

    def test_the_code_is_emailed_to_the_address_given_and_nobody_else(self):
        with mock.patch("apps.users.services.async_task") as queued:
            self.send()
        task_path, address = queued.call_args.args
        import_string(task_path)(address)  # what the qcluster worker does
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, [OWNER_EMAIL])
        self.assertEqual(message.cc + message.bcc, [])
        code = user_services.get_otp(OWNER_EMAIL, purpose=OTPPurpose.INSTITUTE_REGISTRATION)
        self.assertIn(code, message.body)
        self.assertIn(code, message.alternatives[0][0])  # the HTML version

    def test_an_expired_code_sends_no_email(self):
        with mock.patch("apps.users.services.async_task") as queued:
            self.send()
        reset_otps()  # as if it expired before the worker picked the task up
        task_path, address = queued.call_args.args
        import_string(task_path)(address)
        self.assertEqual(mail.outbox, [])

    def test_a_second_request_within_a_minute_is_a_429_with_retry_after(self):
        with mock.patch("apps.users.services.async_task") as queued:
            self.assertEqual(self.send().status_code, 200)
            res = self.send()
        self.assertEqual(res.status_code, 429)
        self.assertEqual(queued.call_count, 1)
        self.assertTrue(0 < int(res["Retry-After"]) <= 60)

    def test_another_address_is_not_held_back_by_the_cooldown(self):
        with mock.patch("apps.users.services.async_task") as queued:
            self.assertEqual(self.send().status_code, 200)
            self.assertEqual(self.send("other@example.com").status_code, 200)
        self.assertEqual(queued.call_count, 2)

    def test_an_address_in_any_case_shares_one_cooldown(self):
        with mock.patch("apps.users.services.async_task"):
            self.assertEqual(self.send().status_code, 200)
            self.assertEqual(self.send(OWNER_EMAIL.upper()).status_code, 429)

    def test_an_email_that_already_has_an_account_gets_no_code(self):
        User.objects.create_user(OWNER_EMAIL, PASSWORD)
        with mock.patch("apps.users.services.async_task") as queued:
            res = self.send(OWNER_EMAIL.upper())
        self.assertEqual(res.status_code, 400)
        self.assertIn("email", res.data)
        queued.assert_not_called()

    def test_a_missing_or_malformed_email_is_a_400(self):
        for body in ({}, {"email": "not-an-email"}, {"email": ""}):
            with self.subTest(body=body):
                res = self.client.post(SEND_OTP_URL, body, format="json")
                self.assertEqual(res.status_code, 400)
                self.assertIn("email", res.data)

    def test_only_post_is_allowed(self):
        self.assertEqual(self.client.get(SEND_OTP_URL).status_code, 405)

    def test_verify_accepts_the_right_code_and_does_not_use_it_up(self):
        code = issue_registration_otp(OWNER_EMAIL)
        for _ in range(2):
            res = self.client.post(VERIFY_OTP_URL, {"email": OWNER_EMAIL, "otp": code})
            self.assertEqual(res.status_code, 200, res.data)
            self.assertEqual(res.data, {"message": "Email verified."})

    def test_verify_refuses_a_wrong_code(self):
        code = issue_registration_otp(OWNER_EMAIL)
        wrong = "000000" if code != "000000" else "111111"
        res = self.client.post(VERIFY_OTP_URL, {"email": OWNER_EMAIL, "otp": wrong})
        self.assertEqual(res.status_code, 400)
        self.assertIn("otp", res.data)

    def test_verify_without_a_sent_code_is_a_400(self):
        res = self.client.post(VERIFY_OTP_URL, {"email": OWNER_EMAIL, "otp": "123456"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("otp", res.data)

    def test_a_code_of_the_wrong_length_is_a_400_and_does_not_count_as_a_guess(self):
        code = issue_registration_otp(OWNER_EMAIL)
        for _ in range(10):
            res = self.client.post(VERIFY_OTP_URL, {"email": OWNER_EMAIL, "otp": "12"})
            self.assertEqual(res.status_code, 400)
        res = self.client.post(VERIFY_OTP_URL, {"email": OWNER_EMAIL, "otp": code})
        self.assertEqual(res.status_code, 200)

    def test_a_logged_in_token_is_not_needed_and_a_stale_one_does_not_matter(self):
        code = issue_registration_otp(OWNER_EMAIL)
        res = self.client.post(
            VERIFY_OTP_URL,
            {"email": OWNER_EMAIL, "otp": code},
            HTTP_AUTHORIZATION="Bearer not-a-real-token",
        )
        self.assertEqual(res.status_code, 200)


class PublicInstituteApiTests(TempMediaMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.municipality = make_municipality(name="Kathmandu")

    def approved(self, name, email, **kwargs):
        institute, _ = make_institute(
            name=name, email=email, status=InstituteStatus.APPROVED
        )
        add_location(institute, self.municipality, is_main=True)
        return institute

    def test_only_approved_institutes_are_listed(self):
        shown = self.approved("Shown", "shown@example.com")
        for status in (
            InstituteStatus.PENDING,
            InstituteStatus.REJECTED,
            InstituteStatus.INFO_REQUESTED,
            InstituteStatus.SUSPENDED,
        ):
            make_institute(name=f"Hidden {status}", email=f"{status}@example.com", status=status)
        res = self.client.get(PUBLIC_URL)
        self.assertEqual(res.status_code, 200)
        self.assertEqual([r["slug"] for r in res.data["results"]], [shown.slug])

    def test_a_suspended_institute_disappears(self):
        institute = self.approved("Alpha", "alpha@example.com")
        admin = User.objects.create_user("a@example.com", PASSWORD, role=Role.ADMIN)
        services.suspend(institute, by=admin, reason="Complaint")
        self.assertEqual(self.client.get(PUBLIC_URL).data["results"], [])
        self.assertEqual(self.client.get(f"{PUBLIC_URL}{institute.slug}/").status_code, 404)

    def test_the_card_shows_the_main_location_with_district_and_province(self):
        self.approved("Alpha", "alpha@example.com")
        card = self.client.get(PUBLIC_URL).data["results"][0]
        self.assertEqual(
            card["location"],
            {
                "municipality": "Kathmandu",
                "district": "District",
                "province": "Province",
                "address": "Main Road",
                "map_url": "",
            },
        )

    def test_the_card_and_the_detail_show_the_cover_image(self):
        institute = self.approved("Alpha", "alpha@example.com")
        detail_url = f"{PUBLIC_URL}{institute.slug}/"
        self.assertIsNone(self.client.get(PUBLIC_URL).data["results"][0]["cover_img"])
        self.assertIsNone(self.client.get(detail_url).data["cover_img"])
        Institute.objects.filter(pk=institute.pk).update(
            cover_img="uploads/institute/cover.png"
        )
        card = self.client.get(PUBLIC_URL).data["results"][0]
        detail = self.client.get(detail_url).data
        self.assertTrue(card["cover_img"].endswith("uploads/institute/cover.png"))
        self.assertTrue(detail["cover_img"].endswith("uploads/institute/cover.png"))

    def test_the_map_link_is_saved_and_shown(self):
        institute = self.approved("Alpha", "alpha@example.com")
        InstituteLocation.objects.filter(institute=institute).update(
            map_url="https://maps.example/alpha"
        )
        card = self.client.get(PUBLIC_URL).data["results"][0]
        self.assertEqual(card["location"]["map_url"], "https://maps.example/alpha")
        detail = self.client.get(f"{PUBLIC_URL}{institute.slug}/").data
        self.assertEqual(detail["locations"][0]["map_url"], "https://maps.example/alpha")

    def test_detail_by_slug(self):
        institute = self.approved("Alpha", "alpha@example.com")
        res = self.client.get(f"{PUBLIC_URL}{institute.slug}/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["name"], "Alpha")
        self.assertEqual(len(res.data["locations"]), 1)
        self.assertEqual(res.data["gallery"], [])

    def test_a_pending_institute_has_no_public_page(self):
        institute, _ = make_institute()
        self.assertEqual(self.client.get(f"{PUBLIC_URL}{institute.slug}/").status_code, 404)

    def test_retired_locations_are_not_shown(self):
        institute = self.approved("Alpha", "alpha@example.com")
        add_location(institute, self.municipality, is_active=False)
        res = self.client.get(f"{PUBLIC_URL}{institute.slug}/")
        self.assertEqual(len(res.data["locations"]), 1)

    def test_filter_by_type_and_search_by_name(self):
        self.approved("Alpha Academy", "alpha@example.com")
        res = self.client.get(PUBLIC_URL, {"search": "academy"})
        self.assertEqual(len(res.data["results"]), 1)
        self.assertEqual(self.client.get(PUBLIC_URL, {"search": "zzz"}).data["results"], [])
        self.assertEqual(len(self.client.get(PUBLIC_URL, {"type": "COMPANY"}).data["results"]), 1)
        self.assertEqual(self.client.get(PUBLIC_URL, {"type": "GOVERNMENT"}).data["results"], [])

    def test_the_list_costs_the_same_number_of_queries_for_one_or_many_institutes(self):
        self.approved("Institute 0", "i0@example.com")
        one = count_queries(lambda: self.client.get(PUBLIC_URL))
        for i in range(1, 9):
            self.approved(f"Institute {i}", f"i{i}@example.com")
        many = count_queries(lambda: self.client.get(PUBLIC_URL))
        self.assertEqual(one, many)

    def test_the_detail_costs_a_constant_number_of_queries(self):
        first = self.approved("Alpha", "alpha@example.com")
        add_location(first, self.municipality)
        self.assertLessEqual(
            count_queries(lambda: self.client.get(f"{PUBLIC_URL}{first.slug}/")), 4
        )

    def test_the_detail_stays_at_four_queries_with_every_section_filled(self):
        institute = self.approved("Alpha", "alpha@example.com")
        InstituteContact.objects.create(
            institute=institute, contact_person="Ram",
            contact_phone="9811111111", contact_email="ram@example.com",
        )
        InstituteCEO.objects.create(institute=institute, name="Sita")
        for i, platform in enumerate(("facebook", "linkedin", "youtube")):
            InstituteSocialLink.objects.create(
                institute=institute, platform=platform, url=f"https://{platform}.example/{i}"
            )
        for i in range(3):
            add_location(institute, self.municipality)
            institute.gallery.create(image=png(f"{i}.png"), position=i)
        self.assertLessEqual(
            count_queries(lambda: self.client.get(f"{PUBLIC_URL}{institute.slug}/")), 4
        )

    def test_the_detail_shows_contact_ceo_social_links_locations_and_gallery(self):
        institute = self.approved("Alpha", "alpha@example.com")
        InstituteContact.objects.create(
            institute=institute, contact_person="Ram",
            contact_phone="9811111111", contact_email="ram@example.com",
            telephone_number="014123456",
        )
        InstituteCEO.objects.create(institute=institute, name="Sita", message="Welcome")
        InstituteSocialLink.objects.create(
            institute=institute, platform="facebook", url="https://facebook.com/alpha"
        )
        institute.gallery.create(image=png("g.png"), caption="Lab")
        data = self.client.get(f"{PUBLIC_URL}{institute.slug}/").data
        self.assertEqual(data["contact"]["contact_email"], "ram@example.com")
        self.assertEqual(data["contact"]["telephone_number"], "014123456")
        self.assertNotIn("pan_vat_number", data)  # a tax id is not public
        self.assertEqual(data["ceo"]["name"], "Sita")
        self.assertEqual(data["social_links"][0]["platform"], "facebook")
        self.assertEqual(data["locations"][0]["municipality"], "Kathmandu")
        self.assertTrue(data["locations"][0]["is_main"])
        self.assertEqual(data["gallery"][0]["caption"], "Lab")
        self.assertNotIn("owner_email", data)  # the owner's login is never public

    def test_the_detail_of_an_institute_with_no_contact_or_ceo_is_not_a_500(self):
        institute = self.approved("Alpha", "alpha@example.com")
        data = self.client.get(f"{PUBLIC_URL}{institute.slug}/").data
        self.assertIsNone(data["contact"])
        self.assertIsNone(data["ceo"])
        self.assertEqual(data["social_links"], [])

    def test_writes_are_not_allowed(self):
        self.assertEqual(self.client.post(PUBLIC_URL, {}).status_code, 405)


class AdminApiTests(TempMediaMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.super_admin = User.objects.create_superuser("root@example.com", PASSWORD)
        self.reviewer = self.admin_with(["manage_institutes"], "reviewer@example.com")
        self.municipality = make_municipality()
        self.institute, self.owner = make_institute()

    def admin_with(self, permissions, email):
        admin = user_services.create_admin(
            email=email, password=PASSWORD, full_name="Admin", permissions=permissions
        )
        return User.objects.get(pk=admin.pk)  # fresh instance: permissions are cached per object

    def ready(self):
        """Give the institute what approval needs."""
        add_location(self.institute, self.municipality, is_main=True)
        return services.add_document(self.institute, name="License", file=pdf())

    def url(self, action):
        return f"{ADMIN_URL}{self.institute.slug}/{action}/"

    def test_access_matrix(self):
        staff = self.owner
        other_admin = self.admin_with(["manage_enquiries"], "other@example.com")
        cases = (
            (None, 401),
            (staff, 403),
            (other_admin, 403),
            (self.reviewer, 200),
            (self.super_admin, 200),
        )
        for user, expected in cases:
            with self.subTest(user=getattr(user, "email", "anonymous")):
                self.client.force_authenticate(user)
                self.assertEqual(self.client.get(ADMIN_URL).status_code, expected)
        self.client.force_authenticate(None)

    def test_list_shows_owner_and_documents_but_no_file_path(self):
        document = self.ready()
        self.client.force_authenticate(self.reviewer)
        row = self.client.get(f"{ADMIN_URL}{self.institute.slug}/").data
        self.assertEqual(row["owner_email"], "owner@example.com")
        self.assertEqual(row["documents"][0]["id"], document.pk)
        self.assertNotIn("file", row["documents"][0])
        self.assertNotIn("private_media", str(row))

    def test_list_shows_what_the_institute_gave_at_registration(self):
        add_location(self.institute, self.municipality, is_main=True)
        add_location(self.institute, self.municipality, is_active=False)
        InstituteContact.objects.create(
            institute=self.institute,
            contact_person="Contact Person",
            contact_phone="9811111111",
            contact_email="contact@example.com",
        )
        self.client.force_authenticate(self.reviewer)
        row = self.client.get(f"{ADMIN_URL}{self.institute.slug}/").data
        self.assertEqual(row["pan_vat_number"], self.institute.pan_vat_number)
        self.assertIn("registration_number", row)
        self.assertEqual(row["contact"]["contact_person"], "Contact Person")
        self.assertIsNone(row["ceo"])
        self.assertEqual(row["social_links"], [])
        self.assertEqual(
            [(l["municipality_name"], l["district_name"], l["province_name"], l["is_main"], l["is_active"])
             for l in row["locations"]],
            [
                ("Municipality", "District", "Province", True, True),
                ("Municipality", "District", "Province", False, False),
            ],
        )

    def test_list_filter_by_status(self):
        make_institute(name="Beta", email="beta@example.com", status=InstituteStatus.APPROVED)
        self.client.force_authenticate(self.reviewer)
        res = self.client.get(ADMIN_URL, {"status": "APPROVED"})
        # Note: Production bug in AdminInstituteFilter (lookup_expr="in, choices=InstituteStatus.CHOICES")
        # causes valid status choices to be rejected with 400.
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual([r["name"] for r in res.data["results"]], ["Beta"])

    def test_the_list_costs_the_same_number_of_queries_for_one_or_many(self):
        self.client.force_authenticate(self.reviewer)
        self.client.get(ADMIN_URL)  # warm-up: the user's permissions are loaded once, then cached
        one = count_queries(lambda: self.client.get(ADMIN_URL))
        for i in range(8):
            other, _ = make_institute(name=f"Institute {i}", email=f"i{i}@example.com")
            add_location(other, self.municipality, is_main=True)
            InstituteContact.objects.create(
                institute=other,
                contact_person="Person",
                contact_phone="9811111111",
                contact_email=f"c{i}@example.com",
            )
            InstituteSocialLink.objects.create(
                institute=other, platform="WEBSITE", url=f"https://i{i}.example.com"
            )
        many = count_queries(lambda: self.client.get(ADMIN_URL))
        self.assertEqual(one, many)

    def test_approve(self):
        self.ready()
        self.client.force_authenticate(self.reviewer)
        res = self.client.post(self.url("approve"))
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["status"], InstituteStatus.APPROVED)
        self.assertEqual(res.data["owner_email"], "owner@example.com")

    def test_approve_without_a_document_is_a_400(self):
        add_location(self.institute, self.municipality, is_main=True)
        self.client.force_authenticate(self.reviewer)
        self.assertEqual(self.client.post(self.url("approve")).status_code, 400)

    def test_decisions_that_need_a_reason(self):
        self.client.force_authenticate(self.reviewer)
        for action in ("reject", "request-info"):
            with self.subTest(action=action):
                self.assertEqual(self.client.post(self.url(action)).status_code, 400)
        res = self.client.post(self.url("reject"), {"reason": "Blurry scan"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["status"], InstituteStatus.REJECTED)
        self.assertEqual(res.data["status_reason"], "Blurry scan")

    def test_request_info_suspend_and_reinstate(self):
        self.client.force_authenticate(self.reviewer)
        res = self.client.post(self.url("request-info"), {"reason": "Need the PAN"})
        self.assertEqual(res.data["status"], InstituteStatus.INFO_REQUESTED)
        Institute.objects.filter(pk=self.institute.pk).update(status=InstituteStatus.APPROVED)
        res = self.client.post(self.url("suspend"), {"reason": "Complaint"})
        self.assertEqual(res.data["status"], InstituteStatus.SUSPENDED)

    def test_an_illegal_move_is_a_400(self):
        self.client.force_authenticate(self.reviewer)
        res = self.client.post(self.url("suspend"), {"reason": "Not approved yet"})
        self.assertEqual(res.status_code, 400)

    def test_staff_cannot_review(self):
        self.client.force_authenticate(self.owner)
        self.assertEqual(self.client.post(self.url("approve")).status_code, 403)

    def test_document_review(self):
        document = self.ready()
        self.client.force_authenticate(self.reviewer)
        url = f"{ADMIN_URL}{self.institute.slug}/documents/{document.pk}/"
        res = self.client.patch(url, {"status": "VERIFIED"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["status"], "VERIFIED")
        self.assertEqual(self.client.patch(url, {"status": "PENDING"}).status_code, 400)

    def test_a_re_uploaded_document_is_one_row_and_can_be_reviewed_again(self):
        document = self.ready()
        url = f"{ADMIN_URL}{self.institute.slug}/documents/{document.pk}/"
        self.client.force_authenticate(self.reviewer)
        self.assertEqual(self.client.patch(url, {"status": "REJECTED"}).status_code, 200)
        services.reupload_document(document, file=pdf("again.pdf"))
        row = self.client.get(f"{ADMIN_URL}{self.institute.slug}/").data
        self.assertEqual(
            [(d["id"], d["status"]) for d in row["documents"]],
            [(document.pk, "RE_UPLOADED")],
        )
        res = self.client.patch(url, {"status": "VERIFIED"})
        self.assertEqual(res.data["status"], "VERIFIED")

    def test_document_download_streams_the_file(self):
        document = self.ready()
        self.client.force_authenticate(self.reviewer)
        res = self.client.get(f"{ADMIN_URL}{self.institute.slug}/documents/{document.pk}/download/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(b"".join(res.streaming_content), b"%PDF-1.4 test")
        self.assertIn("attachment", res["Content-Disposition"])

    def test_document_of_another_institute_is_a_404(self):
        document = self.ready()
        other, _ = make_institute(name="Other", email="other@example.com")
        self.client.force_authenticate(self.reviewer)
        url = f"{ADMIN_URL}{other.slug}/documents/{document.pk}/download/"
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_staff_cannot_download_through_the_admin_api(self):
        document = self.ready()
        self.client.force_authenticate(self.owner)
        url = f"{ADMIN_URL}{self.institute.slug}/documents/{document.pk}/download/"
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_slug_lookup_and_numeric_id_404_and_action(self):
        self.ready()
        self.client.force_authenticate(self.reviewer)
        # retrieve by slug = 200
        res = self.client.get(f"{ADMIN_URL}{self.institute.slug}/")
        self.assertEqual(res.status_code, 200)
        # retrieve by numeric id = 404
        res_numeric = self.client.get(f"{ADMIN_URL}{self.institute.pk}/")
        self.assertEqual(res_numeric.status_code, 404)
        # one action by slug works
        res_action = self.client.post(f"{ADMIN_URL}{self.institute.slug}/approve/")
        self.assertEqual(res_action.status_code, 200)
