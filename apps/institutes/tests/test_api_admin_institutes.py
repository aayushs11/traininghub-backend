from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.common.models.category import Category
from apps.institutes import services as institute_services
from apps.institutes.constants import DocumentStatus, InstituteStatus, InstituteType
from apps.institutes.models import (
    Institute,
    InstituteCEO,
    InstituteContact,
    InstituteDocument,
    InstituteSocialLink,
    InstituteStatusLog,
)
from apps.institutes.tests.helpers import (
    PASSWORD,
    TempMediaMixin,
    add_location,
    make_institute,
    make_municipality,
    next_pan_vat_number,
    pdf,
)
from apps.training.constants import TrainingStatus
from apps.training.models import Training
from apps.users import services as user_services

User = get_user_model()
ADMIN_URL = "/api/v1/admin/institutes/"


class AdminInstitutesApiTests(TempMediaMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.super_admin = User.objects.create_superuser("root@example.com", PASSWORD)
        self.reviewer = self.admin_with(["manage_institutes"], "reviewer@example.com")
        self.municipality = make_municipality()
        self.institute, self.owner = make_institute(name="Alpha Institute", email="alpha.owner@example.com")
        add_location(self.institute, self.municipality, is_main=True)

    def admin_with(self, permissions, email):
        admin = user_services.create_admin(
            email=email, password=PASSWORD, full_name="Admin Reviewer", permissions=permissions
        )
        return User.objects.get(pk=admin.pk)

    def ready_institute(self, name="Ready Institute", email="ready@example.com", status=InstituteStatus.PENDING):
        inst, owner = make_institute(name=name, email=email, status=status)
        add_location(inst, self.municipality, is_main=True)
        doc = institute_services.add_document(inst, name="License", file=pdf())
        return inst, owner, doc

    # ==========================================
    # A. Institutes list (GET institutes/)
    # ==========================================

    def test_list_row_has_exact_keys_and_no_unwanted_keys(self):
        InstituteContact.objects.create(
            institute=self.institute,
            contact_person="Contact Person",
            contact_phone="9811111111",
            contact_email="contact@example.com",
        )
        InstituteCEO.objects.create(
            institute=self.institute,
            name="CEO Person",
            message="Bio message",
        )
        InstituteSocialLink.objects.create(
            institute=self.institute, platform="WEBSITE", url="https://alpha.example.com"
        )
        institute_services.add_document(self.institute, name="License", file=pdf())

        self.client.force_authenticate(self.reviewer)
        res = self.client.get(ADMIN_URL)
        self.assertEqual(res.status_code, 200)
        row = res.data["results"][0]

        expected_keys = {
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
        }
        self.assertEqual(set(row.keys()), expected_keys)

        for unwanted in (
            "documents",
            "ceo",
            "social_links",
            "locations",
            "owner_email",
            "pan_vat_number",
        ):
            self.assertNotIn(unwanted, row)

    def test_list_default_order_pending_then_info_requested_then_others_newest_first(self):
        now = timezone.now()
        # Set self.institute timestamp
        Institute.objects.filter(pk=self.institute.pk).update(created_at=now - timedelta(minutes=50))

        p_old, _ = make_institute("Pending Old", status=InstituteStatus.PENDING, email="p_old@example.com")
        Institute.objects.filter(pk=p_old.pk).update(created_at=now - timedelta(minutes=30))

        p_new, _ = make_institute("Pending New", status=InstituteStatus.PENDING, email="p_new@example.com")
        Institute.objects.filter(pk=p_new.pk).update(created_at=now - timedelta(minutes=10))

        info, _ = make_institute("Info Req", status=InstituteStatus.INFO_REQUESTED, email="info@example.com")
        Institute.objects.filter(pk=info.pk).update(created_at=now - timedelta(minutes=20))

        a_old, _ = make_institute("App Old", status=InstituteStatus.APPROVED, email="a_old@example.com")
        Institute.objects.filter(pk=a_old.pk).update(created_at=now - timedelta(minutes=40))

        a_new, _ = make_institute("App New", status=InstituteStatus.APPROVED, email="a_new@example.com")
        Institute.objects.filter(pk=a_new.pk).update(created_at=now - timedelta(minutes=5))

        self.client.force_authenticate(self.reviewer)
        res = self.client.get(ADMIN_URL)
        self.assertEqual(res.status_code, 200)

        slugs = [r["slug"] for r in res.data["results"]]
        expected = [p_new.slug, p_old.slug, self.institute.slug, info.slug, a_new.slug, a_old.slug]
        self.assertEqual(slugs, expected)

    def test_list_trainings_count_counts_any_status(self):
        cat = Category.objects.create(name="Tech Category")
        Training.objects.create(
            institute=self.institute,
            category=cat,
            title="Training 1",
            slug="training-1",
            status=TrainingStatus.DRAFT,
            fee_npr=1000,
        )
        Training.objects.create(
            institute=self.institute,
            category=cat,
            title="Training 2",
            slug="training-2",
            status=TrainingStatus.SUBMITTED,
            fee_npr=1000,
        )
        Training.objects.create(
            institute=self.institute,
            category=cat,
            title="Training 3",
            slug="training-3",
            status=TrainingStatus.APPROVED,
            fee_npr=1000,
        )

        other, _ = make_institute("Other Institute", email="other@example.com")

        self.client.force_authenticate(self.reviewer)
        res = self.client.get(ADMIN_URL)
        self.assertEqual(res.status_code, 200)

        rows = {r["slug"]: r for r in res.data["results"]}
        self.assertEqual(rows[self.institute.slug]["trainings_count"], 3)
        self.assertEqual(rows[other.slug]["trainings_count"], 0)

    def test_list_pending_documents_count_pending_and_reuploaded_only(self):
        InstituteDocument.objects.create(
            institute=self.institute, name="D1", status=DocumentStatus.PENDING, file=pdf("d1.pdf")
        )
        InstituteDocument.objects.create(
            institute=self.institute, name="D2", status=DocumentStatus.RE_UPLOADED, file=pdf("d2.pdf")
        )
        InstituteDocument.objects.create(
            institute=self.institute, name="D3", status=DocumentStatus.VERIFIED, file=pdf("d3.pdf")
        )
        InstituteDocument.objects.create(
            institute=self.institute, name="D4", status=DocumentStatus.REJECTED, file=pdf("d4.pdf")
        )

        self.client.force_authenticate(self.reviewer)
        res = self.client.get(ADMIN_URL)
        self.assertEqual(res.status_code, 200)

        row = next(r for r in res.data["results"] if r["slug"] == self.institute.slug)
        # Note: Production bug in apps.institutes.constants: AWAITING_REVIEW is defined as
        # string "AWAITING_REVIEW" instead of tuple (PENDING, RE_UPLOADED), causing count to be 0.
        self.assertEqual(row["pending_documents_count"], 2)

    def test_list_filter_by_status_comma_separated(self):
        inst_p, _ = make_institute("Pending Inst", status=InstituteStatus.PENDING, email="ip@example.com")
        inst_i, _ = make_institute("Info Inst", status=InstituteStatus.INFO_REQUESTED, email="ii@example.com")
        inst_a, _ = make_institute("App Inst", status=InstituteStatus.APPROVED, email="ia@example.com")
        inst_r, _ = make_institute("Rej Inst", status=InstituteStatus.REJECTED, email="ir@example.com")

        self.client.force_authenticate(self.reviewer)
        res_multi = self.client.get(ADMIN_URL, {"status": "PENDING,INFO_REQUESTED"})
        # Note: Production bug in apps.control_panel.api.v1.filters: AdminInstituteFilter.status
        # defines lookup_expr="in, choices=InstituteStatus.CHOICES" which rejects valid choices with 400.
        self.assertEqual(res_multi.status_code, 200, res_multi.data)
        slugs_multi = {r["slug"] for r in res_multi.data["results"]}
        self.assertIn(inst_p.slug, slugs_multi)
        self.assertIn(inst_i.slug, slugs_multi)
        self.assertNotIn(inst_a.slug, slugs_multi)
        self.assertNotIn(inst_r.slug, slugs_multi)

        res_single = self.client.get(ADMIN_URL, {"status": "APPROVED"})
        self.assertEqual(res_single.status_code, 200, res_single.data)
        slugs_single = {r["slug"] for r in res_single.data["results"]}
        self.assertEqual(slugs_single, {inst_a.slug})

    def test_list_filter_by_status_invalid_or_lowercase_returns_400(self):
        self.client.force_authenticate(self.reviewer)
        for bad_status in ("pending", "BOGUS", "PENDING,BOGUS"):
            with self.subTest(status=bad_status):
                res = self.client.get(ADMIN_URL, {"status": bad_status})
                self.assertEqual(res.status_code, 400)

    def test_list_filter_by_type_and_search(self):
        inst1, _ = make_institute(
            name="Alpha Tech Institute",
            email="tech1@example.com",
            pan_vat_number="900000011",
        )
        Institute.objects.filter(pk=inst1.pk).update(
            type=InstituteType.COMPANY, registration_number="REG-ALPHA-99"
        )

        inst2, _ = make_institute(
            name="Beta Vocational Center",
            email="tech2@example.com",
            pan_vat_number="900000022",
        )
        Institute.objects.filter(pk=inst2.pk).update(
            type=InstituteType.GOVERNMENT, registration_number="REG-BETA-88"
        )

        self.client.force_authenticate(self.reviewer)

        # ?type=
        res_type = self.client.get(ADMIN_URL, {"type": InstituteType.COMPANY})
        self.assertEqual(res_type.status_code, 200)
        slugs_type = {r["slug"] for r in res_type.data["results"]}
        self.assertIn(inst1.slug, slugs_type)
        self.assertNotIn(inst2.slug, slugs_type)

        # ?search= name
        res_name = self.client.get(ADMIN_URL, {"search": "Alpha Tech"})
        self.assertEqual([r["slug"] for r in res_name.data["results"]], [inst1.slug])

        # ?search= registration_number
        res_reg = self.client.get(ADMIN_URL, {"search": "REG-BETA-88"})
        self.assertEqual([r["slug"] for r in res_reg.data["results"]], [inst2.slug])

        # ?search= pan_vat_number
        res_pan = self.client.get(ADMIN_URL, {"search": "900000011"})
        self.assertEqual([r["slug"] for r in res_pan.data["results"]], [inst1.slug])

    def test_list_filter_has_pending_documents(self):
        inst_pending, _ = make_institute("Inst Pending", email="ipend@example.com")
        InstituteDocument.objects.create(
            institute=inst_pending, name="D_Pend", status=DocumentStatus.PENDING, file=pdf("p.pdf")
        )

        inst_reup, _ = make_institute("Inst Reup", email="ireup@example.com")
        InstituteDocument.objects.create(
            institute=inst_reup, name="D_Reup", status=DocumentStatus.RE_UPLOADED, file=pdf("r.pdf")
        )

        inst_ver, _ = make_institute("Inst Verified", email="iver@example.com")
        InstituteDocument.objects.create(
            institute=inst_ver, name="D_Ver", status=DocumentStatus.VERIFIED, file=pdf("v.pdf")
        )

        inst_rej, _ = make_institute("Inst Rejected", email="irej@example.com")
        InstituteDocument.objects.create(
            institute=inst_rej, name="D_Rej", status=DocumentStatus.REJECTED, file=pdf("j.pdf")
        )

        inst_none, _ = make_institute("Inst None", email="inone@example.com")

        self.client.force_authenticate(self.reviewer)

        res_true = self.client.get(ADMIN_URL, {"has_pending_documents": "true"})
        self.assertEqual(res_true.status_code, 200)
        slugs_true = {r["slug"] for r in res_true.data["results"]}
        # Note: Production bug: AWAITING_REVIEW string causes has_pending_documents to return empty.
        self.assertIn(inst_pending.slug, slugs_true)
        self.assertIn(inst_reup.slug, slugs_true)
        self.assertNotIn(inst_ver.slug, slugs_true)
        self.assertNotIn(inst_rej.slug, slugs_true)
        self.assertNotIn(inst_none.slug, slugs_true)

        res_false = self.client.get(ADMIN_URL, {"has_pending_documents": "false"})
        self.assertEqual(res_false.status_code, 200)
        slugs_false = {r["slug"] for r in res_false.data["results"]}
        self.assertNotIn(inst_pending.slug, slugs_false)
        self.assertNotIn(inst_reup.slug, slugs_false)
        self.assertIn(inst_ver.slug, slugs_false)
        self.assertIn(inst_rej.slug, slugs_false)
        self.assertIn(inst_none.slug, slugs_false)

    # ==========================================
    # B. Summary (GET institutes/summary/)
    # ==========================================

    def test_summary_tabs_and_by_status(self):
        Institute.objects.all().delete()
        make_institute("P1", status=InstituteStatus.PENDING, email="p1@example.com")
        make_institute("P2", status=InstituteStatus.PENDING, email="p2@example.com")
        make_institute("I1", status=InstituteStatus.INFO_REQUESTED, email="i1@example.com")
        make_institute("A1", status=InstituteStatus.APPROVED, email="a1@example.com")
        make_institute("A2", status=InstituteStatus.APPROVED, email="a2@example.com")
        make_institute("A3", status=InstituteStatus.APPROVED, email="a3@example.com")
        make_institute("R1", status=InstituteStatus.REJECTED, email="r1@example.com")
        make_institute("S1", status=InstituteStatus.SUSPENDED, email="s1@example.com")

        self.client.force_authenticate(self.reviewer)
        res = self.client.get(f"{ADMIN_URL}summary/")
        self.assertEqual(res.status_code, 200)

        tabs = res.data["tabs"]
        self.assertEqual(tabs["all"], 8)
        self.assertEqual(tabs["pending"], 3)  # PENDING + INFO_REQUESTED
        self.assertEqual(tabs["approved"], 3)
        self.assertEqual(tabs["rejected"], 1)
        self.assertEqual(tabs["suspended"], 1)

        by_status = res.data["by_status"]
        for code, _ in InstituteStatus.CHOICES:
            self.assertIn(code, by_status)
        self.assertEqual(by_status[InstituteStatus.PENDING], 2)
        self.assertEqual(by_status[InstituteStatus.INFO_REQUESTED], 1)
        self.assertEqual(by_status[InstituteStatus.APPROVED], 3)
        self.assertEqual(by_status[InstituteStatus.REJECTED], 1)
        self.assertEqual(by_status[InstituteStatus.SUSPENDED], 1)

    def test_summary_documents_to_verify_counts_approved_institutes_with_pending_or_reuploaded_docs(self):
        # 1. Approved with PENDING and RE_UPLOADED -> counts once
        i1, _ = make_institute("I1", status=InstituteStatus.APPROVED, email="i1@example.com")
        InstituteDocument.objects.create(institute=i1, name="Doc1", status=DocumentStatus.PENDING, file=pdf("1.pdf"))
        InstituteDocument.objects.create(institute=i1, name="Doc2", status=DocumentStatus.RE_UPLOADED, file=pdf("2.pdf"))

        # 2. Approved with RE_UPLOADED -> counts once
        i2, _ = make_institute("I2", status=InstituteStatus.APPROVED, email="i2@example.com")
        InstituteDocument.objects.create(institute=i2, name="Doc3", status=DocumentStatus.RE_UPLOADED, file=pdf("3.pdf"))

        # 3. Approved with only VERIFIED and REJECTED -> not counted
        i3, _ = make_institute("I3", status=InstituteStatus.APPROVED, email="i3@example.com")
        InstituteDocument.objects.create(institute=i3, name="Doc4", status=DocumentStatus.VERIFIED, file=pdf("4.pdf"))
        InstituteDocument.objects.create(institute=i3, name="Doc5", status=DocumentStatus.REJECTED, file=pdf("5.pdf"))

        # 4. PENDING institute with PENDING doc -> not counted (not approved)
        i4, _ = make_institute("I4", status=InstituteStatus.PENDING, email="i4@example.com")
        InstituteDocument.objects.create(institute=i4, name="Doc6", status=DocumentStatus.PENDING, file=pdf("6.pdf"))

        # 5. SUSPENDED institute with RE_UPLOADED doc -> not counted (not approved)
        i5, _ = make_institute("I5", status=InstituteStatus.SUSPENDED, email="i5@example.com")
        InstituteDocument.objects.create(institute=i5, name="Doc7", status=DocumentStatus.RE_UPLOADED, file=pdf("7.pdf"))

        self.client.force_authenticate(self.reviewer)
        res = self.client.get(f"{ADMIN_URL}summary/")
        self.assertEqual(res.status_code, 200)
        # Note: Production bug: AWAITING_REVIEW string causes documents_to_verify to return 0.
        self.assertEqual(res.data["documents_to_verify"], 2)

    # ==========================================
    # C. Detail (GET institutes/{slug}/)
    # ==========================================

    def test_detail_returns_documents_contact_ceo_social_links_locations_owner_email(self):
        doc = institute_services.add_document(self.institute, name="Official License", file=pdf())
        InstituteContact.objects.create(
            institute=self.institute,
            contact_person="Ramesh Sharma",
            contact_phone="9841000000",
            contact_email="ramesh@example.com",
        )
        InstituteCEO.objects.create(
            institute=self.institute,
            name="Sita Thapa",
            message="Founding CEO",
        )
        InstituteSocialLink.objects.create(
            institute=self.institute, platform="LINKEDIN", url="https://linkedin.com/company/alpha"
        )

        self.client.force_authenticate(self.reviewer)
        res = self.client.get(f"{ADMIN_URL}{self.institute.slug}/")
        self.assertEqual(res.status_code, 200)
        data = res.data

        self.assertEqual(data["owner_email"], self.owner.email)
        self.assertEqual(data["contact"]["contact_person"], "Ramesh Sharma")
        self.assertEqual(data["ceo"]["name"], "Sita Thapa")
        self.assertEqual(len(data["social_links"]), 1)
        self.assertEqual(len(data["locations"]), 1)

        docs = data["documents"]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["id"], doc.pk)
        self.assertEqual(docs[0]["name"], "Official License")
        self.assertEqual(docs[0]["status"], DocumentStatus.PENDING)
        self.assertIn("created_at", docs[0])
        self.assertNotIn("file", docs[0])
        self.assertNotIn("private_media", str(data))

    def test_detail_available_actions_by_status(self):
        self.client.force_authenticate(self.reviewer)
        cases = (
            (InstituteStatus.PENDING, ["approve", "reject", "request_info"]),
            (InstituteStatus.APPROVED, ["suspend"]),
            (InstituteStatus.SUSPENDED, ["reinstate"]),
            (InstituteStatus.REJECTED, []),
            (InstituteStatus.INFO_REQUESTED, []),
        )
        for status, expected in cases:
            with self.subTest(status=status):
                Institute.objects.filter(pk=self.institute.pk).update(status=status)
                res = self.client.get(f"{ADMIN_URL}{self.institute.slug}/")
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.data["available_actions"], expected)

    # ==========================================
    # D. Actions and InstituteStatusLog
    # ==========================================

    def test_valid_transitions_return_200_and_write_one_status_log(self):
        transitions = (
            ("approve", InstituteStatus.PENDING, InstituteStatus.APPROVED, {}, ""),
            ("reject", InstituteStatus.PENDING, InstituteStatus.REJECTED, {"reason": "Fake docs"}, "Fake docs"),
            ("request-info", InstituteStatus.PENDING, InstituteStatus.INFO_REQUESTED, {"reason": "Clearer scan"}, "Clearer scan"),
            ("suspend", InstituteStatus.APPROVED, InstituteStatus.SUSPENDED, {"reason": "Complaint"}, "Complaint"),
            ("reinstate", InstituteStatus.SUSPENDED, InstituteStatus.APPROVED, {}, ""),
        )

        self.client.force_authenticate(self.reviewer)

        for action, from_st, to_st, payload, expected_reason in transitions:
            with self.subTest(action=action):
                inst, _, _ = self.ready_institute(name=f"Inst {action}", email=f"{action}@example.com", status=from_st)
                res = self.client.post(f"{ADMIN_URL}{inst.slug}/{action}/", payload)
                self.assertEqual(res.status_code, 200, res.data)
                self.assertEqual(res.data["status"], to_st)

                logs = InstituteStatusLog.objects.filter(institute=inst)
                self.assertEqual(logs.count(), 1)
                log = logs.first()
                self.assertEqual(log.from_status, from_st)
                self.assertEqual(log.to_status, to_st)
                self.assertEqual(log.reason, expected_reason)
                self.assertEqual(log.changed_by, self.reviewer)

    def test_invalid_transitions_return_400_and_write_no_log(self):
        invalid_cases = (
            ("approve", InstituteStatus.APPROVED, {}),
            ("approve", InstituteStatus.REJECTED, {}),
            ("approve", InstituteStatus.INFO_REQUESTED, {}),
            ("approve", InstituteStatus.SUSPENDED, {}),
            ("reinstate", InstituteStatus.PENDING, {}),
            ("reinstate", InstituteStatus.APPROVED, {}),
            ("suspend", InstituteStatus.PENDING, {"reason": "Pre-approval suspend"}),
        )

        self.client.force_authenticate(self.reviewer)

        for action, from_st, payload in invalid_cases:
            with self.subTest(action=action, from_status=from_st):
                inst, _, _ = self.ready_institute(
                    name=f"Bad {action} {from_st}", email=f"bad_{action}_{from_st}@example.com", status=from_st
                )
                InstituteStatusLog.objects.filter(institute=inst).delete()
                res = self.client.post(f"{ADMIN_URL}{inst.slug}/{action}/", payload)
                self.assertEqual(res.status_code, 400)
                self.assertEqual(InstituteStatusLog.objects.filter(institute=inst).count(), 0)

    def test_actions_with_missing_or_blank_reason_return_400_and_write_no_log(self):
        self.client.force_authenticate(self.reviewer)

        # reject and request-info (from PENDING)
        for action in ("reject", "request-info"):
            for bad_payload in ({}, {"reason": ""}, {"reason": "   "}):
                with self.subTest(action=action, payload=bad_payload):
                    inst, _, _ = self.ready_institute(
                        name=f"Inst {action} blank", email=f"blank_{action}_{next_pan_vat_number()}@example.com"
                    )
                    res = self.client.post(f"{ADMIN_URL}{inst.slug}/{action}/", bad_payload)
                    self.assertEqual(res.status_code, 400)
                    self.assertEqual(InstituteStatusLog.objects.filter(institute=inst).count(), 0)

        # suspend (from APPROVED)
        inst_app, _, _ = self.ready_institute(
            name="Inst Suspend Blank", email="app_blank@example.com", status=InstituteStatus.APPROVED
        )
        for bad_payload in ({}, {"reason": ""}, {"reason": "   "}):
            with self.subTest(action="suspend", payload=bad_payload):
                res = self.client.post(f"{ADMIN_URL}{inst_app.slug}/suspend/", bad_payload)
                self.assertEqual(res.status_code, 400)
                self.assertEqual(InstituteStatusLog.objects.filter(institute=inst_app).count(), 0)

    def test_approve_without_active_location_or_without_document_returns_400_and_writes_no_log(self):
        self.client.force_authenticate(self.reviewer)

        # No active location
        inst_no_loc, _ = make_institute("No Loc", email="noloc@example.com")
        add_location(inst_no_loc, self.municipality, is_active=False)
        institute_services.add_document(inst_no_loc, name="Doc", file=pdf())
        res_loc = self.client.post(f"{ADMIN_URL}{inst_no_loc.slug}/approve/")
        self.assertEqual(res_loc.status_code, 400)
        self.assertEqual(InstituteStatusLog.objects.filter(institute=inst_no_loc).count(), 0)

        # No document
        inst_no_doc, _ = make_institute("No Doc", email="nodoc@example.com")
        add_location(inst_no_doc, self.municipality, is_main=True)
        res_doc = self.client.post(f"{ADMIN_URL}{inst_no_doc.slug}/approve/")
        self.assertEqual(res_doc.status_code, 400)
        self.assertEqual(InstituteStatusLog.objects.filter(institute=inst_no_doc).count(), 0)

    def test_resubmit_writes_pending_log_row(self):
        # From REJECTED without actor
        inst_rej, owner_rej, _ = self.ready_institute(
            name="Rej Resubmit", email="rej_resub@example.com", status=InstituteStatus.REJECTED
        )
        InstituteStatusLog.objects.filter(institute=inst_rej).delete()
        inst_rej = institute_services.resubmit(inst_rej)
        self.assertEqual(inst_rej.status, InstituteStatus.PENDING)
        log1 = InstituteStatusLog.objects.get(institute=inst_rej)
        self.assertEqual(log1.from_status, InstituteStatus.REJECTED)
        self.assertEqual(log1.to_status, InstituteStatus.PENDING)
        self.assertIsNone(log1.changed_by)

        # From INFO_REQUESTED with actor
        inst_info, owner_info, _ = self.ready_institute(
            name="Info Resubmit", email="info_resub@example.com", status=InstituteStatus.INFO_REQUESTED
        )
        InstituteStatusLog.objects.filter(institute=inst_info).delete()
        inst_info = institute_services.resubmit(inst_info, by=owner_info)
        self.assertEqual(inst_info.status, InstituteStatus.PENDING)
        log2 = InstituteStatusLog.objects.get(institute=inst_info)
        self.assertEqual(log2.from_status, InstituteStatus.INFO_REQUESTED)
        self.assertEqual(log2.to_status, InstituteStatus.PENDING)
        self.assertEqual(log2.changed_by, owner_info)

    # ==========================================
    # E. Approve side effect on documents
    # ==========================================

    def test_approve_sets_every_non_verified_document_to_verified(self):
        inst, _, _ = self.ready_institute("Multi Doc Inst", email="multidoc@example.com")
        doc1 = InstituteDocument.objects.create(
            institute=inst, name="Doc PENDING", status=DocumentStatus.PENDING, file=pdf("1.pdf")
        )
        doc2 = InstituteDocument.objects.create(
            institute=inst, name="Doc REJECTED", status=DocumentStatus.REJECTED, file=pdf("2.pdf")
        )
        doc3 = InstituteDocument.objects.create(
            institute=inst, name="Doc RE_UPLOADED", status=DocumentStatus.RE_UPLOADED, file=pdf("3.pdf")
        )
        doc4 = InstituteDocument.objects.create(
            institute=inst, name="Doc VERIFIED", status=DocumentStatus.VERIFIED, file=pdf("4.pdf")
        )

        self.client.force_authenticate(self.reviewer)
        res = self.client.post(f"{ADMIN_URL}{inst.slug}/approve/")
        self.assertEqual(res.status_code, 200)

        for doc in (doc1, doc2, doc3, doc4):
            doc.refresh_from_db()
            self.assertEqual(doc.status, DocumentStatus.VERIFIED)

    def test_reject_request_info_suspend_reinstate_leave_documents_unchanged(self):
        self.client.force_authenticate(self.reviewer)

        # Reject leaves documents unchanged
        inst_rej, _, _ = self.ready_institute("Rej Doc Inst", email="rejdoc@example.com")
        d_rej = InstituteDocument.objects.create(
            institute=inst_rej, name="Doc P", status=DocumentStatus.PENDING, file=pdf("p.pdf")
        )
        self.client.post(f"{ADMIN_URL}{inst_rej.slug}/reject/", {"reason": "Reason"})
        d_rej.refresh_from_db()
        self.assertEqual(d_rej.status, DocumentStatus.PENDING)

        # Request info leaves documents unchanged
        inst_req, _, _ = self.ready_institute("Req Doc Inst", email="reqdoc@example.com")
        d_req = InstituteDocument.objects.create(
            institute=inst_req, name="Doc P2", status=DocumentStatus.PENDING, file=pdf("p2.pdf")
        )
        self.client.post(f"{ADMIN_URL}{inst_req.slug}/request-info/", {"reason": "Reason"})
        d_req.refresh_from_db()
        self.assertEqual(d_req.status, DocumentStatus.PENDING)

        # Suspend leaves documents unchanged
        inst_susp, _, _ = self.ready_institute("Susp Doc Inst", email="suspdoc@example.com", status=InstituteStatus.APPROVED)
        d_susp = InstituteDocument.objects.create(
            institute=inst_susp, name="Doc V", status=DocumentStatus.VERIFIED, file=pdf("v.pdf")
        )
        self.client.post(f"{ADMIN_URL}{inst_susp.slug}/suspend/", {"reason": "Reason"})
        d_susp.refresh_from_db()
        self.assertEqual(d_susp.status, DocumentStatus.VERIFIED)

        # Reinstate leaves documents unchanged
        inst_reinst, _, _ = self.ready_institute("Reinst Doc Inst", email="reinstdoc@example.com", status=InstituteStatus.SUSPENDED)
        d_reinst = InstituteDocument.objects.create(
            institute=inst_reinst, name="Doc Pend", status=DocumentStatus.PENDING, file=pdf("pend.pdf")
        )
        self.client.post(f"{ADMIN_URL}{inst_reinst.slug}/reinstate/")
        d_reinst.refresh_from_db()
        self.assertEqual(d_reinst.status, DocumentStatus.PENDING)

    def test_document_uploaded_after_approval_starts_as_pending_and_reinstate_does_not_verify_it(self):
        inst, _, _ = self.ready_institute("Post Approve Inst", email="postapp@example.com", status=InstituteStatus.APPROVED)
        new_doc = institute_services.add_document(inst, name="Post Approve Doc", file=pdf("post.pdf"))
        self.assertEqual(new_doc.status, DocumentStatus.PENDING)

        self.client.force_authenticate(self.reviewer)

        # Appears under ?has_pending_documents=true
        res_list = self.client.get(ADMIN_URL, {"has_pending_documents": "true"})
        # Note: Production bug: AWAITING_REVIEW string causes has_pending_documents to return empty.
        self.assertIn(inst.slug, [r["slug"] for r in res_list.data["results"]])

        # Appears in summary documents_to_verify
        res_summary = self.client.get(f"{ADMIN_URL}summary/")
        # Note: Production bug: AWAITING_REVIEW string causes documents_to_verify to be 0.
        self.assertGreaterEqual(res_summary.data["documents_to_verify"], 1)

        # Suspend then Reinstate
        self.client.post(f"{ADMIN_URL}{inst.slug}/suspend/", {"reason": "Temporary hold"})
        self.client.post(f"{ADMIN_URL}{inst.slug}/reinstate/")

        new_doc.refresh_from_db()
        self.assertEqual(new_doc.status, DocumentStatus.PENDING)

    # ==========================================
    # F. Documents
    # ==========================================

    def test_document_review_accepts_verified_and_rejected_refuses_pending(self):
        doc1 = institute_services.add_document(self.institute, name="License Doc 1", file=pdf())
        doc2 = institute_services.add_document(self.institute, name="License Doc 2", file=pdf())
        url1 = f"{ADMIN_URL}{self.institute.slug}/documents/{doc1.pk}/"
        url2 = f"{ADMIN_URL}{self.institute.slug}/documents/{doc2.pk}/"

        self.client.force_authenticate(self.reviewer)

        # VERIFIED accepted
        res_v = self.client.patch(url1, {"status": "VERIFIED"})
        self.assertEqual(res_v.status_code, 200)
        self.assertEqual(res_v.data["status"], "VERIFIED")

        # REJECTED accepted
        res_r = self.client.patch(url2, {"status": "REJECTED"})
        self.assertEqual(res_r.status_code, 200)
        self.assertEqual(res_r.data["status"], "REJECTED")

        # PENDING returns 400
        res_p = self.client.patch(url2, {"status": "PENDING"})
        self.assertEqual(res_p.status_code, 400)

    def test_document_id_belonging_to_another_institute_returns_404_for_patch_and_download(self):
        doc = institute_services.add_document(self.institute, name="Doc A", file=pdf())
        other, _ = make_institute("Other Inst", email="other_doc@example.com")

        self.client.force_authenticate(self.reviewer)

        # PATCH with other institute's slug returns 404
        patch_url = f"{ADMIN_URL}{other.slug}/documents/{doc.pk}/"
        res_patch = self.client.patch(patch_url, {"status": "VERIFIED"})
        self.assertEqual(res_patch.status_code, 404)

        # Download with other institute's slug returns 404
        dl_url = f"{ADMIN_URL}{other.slug}/documents/{doc.pk}/download/"
        res_dl = self.client.get(dl_url)
        self.assertEqual(res_dl.status_code, 404)

    # ==========================================
    # G. Activity (GET institutes/{slug}/activity/)
    # ==========================================

    def test_activity_response_format_unpaginated_and_newest_first(self):
        self.client.force_authenticate(self.reviewer)
        res = self.client.get(f"{ADMIN_URL}{self.institute.slug}/activity/")
        self.assertEqual(res.status_code, 200)

        # Unpaginated: exactly {"results": [...]}
        self.assertEqual(set(res.data.keys()), {"results"})
        for paginated_key in ("count", "next", "previous"):
            self.assertNotIn(paginated_key, res.data)

        results = res.data["results"]
        self.assertTrue(len(results) >= 1)
        # Newest first
        timestamps = [r["at"] for r in results]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))

    def test_activity_oldest_event_is_registered_with_fixed_body(self):
        self.client.force_authenticate(self.reviewer)
        res = self.client.get(f"{ADMIN_URL}{self.institute.slug}/activity/")
        self.assertEqual(res.status_code, 200)

        oldest = res.data["results"][-1]
        self.assertEqual(oldest["type"], "REGISTERED")
        self.assertEqual(oldest["body"], "Submitted the registration details")

    def test_activity_status_changed_events_show_actor_and_reason(self):
        reviewer1 = self.reviewer
        reviewer2 = user_services.create_admin(
            email="rev2@example.com", password=PASSWORD, full_name="", permissions=["manage_institutes"]
        )

        inst, _, _ = self.ready_institute("Activity Log Inst", email="actlog@example.com")

        # Approve by reviewer1 (full_name="Admin Reviewer")
        self.client.force_authenticate(reviewer1)
        res_app = self.client.post(f"{ADMIN_URL}{inst.slug}/approve/")
        self.assertEqual(res_app.status_code, 200)

        # Suspend by reviewer2 (full_name="", fallback to email)
        self.client.force_authenticate(reviewer2)
        res_susp = self.client.post(f"{ADMIN_URL}{inst.slug}/suspend/", {"reason": "Late compliance"})
        self.assertEqual(res_susp.status_code, 200)

        res_act = self.client.get(f"{ADMIN_URL}{inst.slug}/activity/")
        self.assertEqual(res_act.status_code, 200)
        events = res_act.data["results"]

        status_events = [e for e in events if e["type"] == "STATUS_CHANGED"]
        self.assertEqual(len(status_events), 2)

        # Newest first: suspend event
        suspend_event = status_events[0]
        self.assertEqual(suspend_event["from_status"], InstituteStatus.APPROVED)
        self.assertEqual(suspend_event["to_status"], InstituteStatus.SUSPENDED)
        self.assertEqual(suspend_event["reason"], "Late compliance")
        self.assertEqual(suspend_event["by"], "rev2@example.com")

        # Approve event
        approve_event = status_events[1]
        self.assertEqual(approve_event["from_status"], InstituteStatus.PENDING)
        self.assertEqual(approve_event["to_status"], InstituteStatus.APPROVED)
        self.assertEqual(approve_event["by"], "Admin Reviewer")

    def test_activity_institute_with_no_log_rows_and_legacy_training_created_event(self):
        inst, _ = make_institute("Legacy Inst", email="legacy@example.com")
        InstituteStatusLog.objects.filter(institute=inst).delete()

        cat = Category.objects.create(name="Legacy Category")
        Training.objects.create(
            institute=inst,
            category=cat,
            title="Legacy Python Course",
            slug="legacy-python-course",
            status=TrainingStatus.SUBMITTED,
            fee_npr=5000,
        )

        self.client.force_authenticate(self.reviewer)
        res = self.client.get(f"{ADMIN_URL}{inst.slug}/activity/")
        self.assertEqual(res.status_code, 200)

        events = res.data["results"]
        types = [e["type"] for e in events]
        self.assertEqual(set(types), {"REGISTERED", "TRAINING_CREATED"})

        training_event = next(e for e in events if e["type"] == "TRAINING_CREATED")
        # Note: Production bug in apps.control_panel.api.v1.selectors.institute_activity:
        # sets title to training.title instead of "Training created" and body to
        # f"{training.title} — {status}" instead of training title only.
        self.assertEqual(training_event["title"], "Training created")
        self.assertEqual(training_event["body"], "Legacy Python Course")

    def test_activity_training_review_events_appear_when_training_has_logs(self):
        inst, _, _ = self.ready_institute("Training Log Inst", email="trlog@example.com")
        cat = Category.objects.create(name="Web Category")
        training = Training.objects.create(
            institute=inst,
            category=cat,
            title="Web Development",
            slug="web-development",
            status=TrainingStatus.APPROVED,
            fee_npr=8000,
        )

        # Note: Production bug: TrainingStatusLog model is not implemented on this branch,
        # and institute_activity does not query training status logs.
        from apps.training import models as training_models
        self.assertTrue(
            hasattr(training_models, "TrainingStatusLog"),
            "TrainingStatusLog model does not exist in apps.training.models"
        )

        self.client.force_authenticate(self.reviewer)
        res = self.client.get(f"{ADMIN_URL}{inst.slug}/activity/")
        self.assertEqual(res.status_code, 200)
        review_events = [e for e in res.data["results"] if e["type"] in ("TRAINING_SUBMITTED", "TRAINING_APPROVED")]
        self.assertTrue(len(review_events) > 0)

    def test_activity_unknown_slug_404_and_auth_matrix(self):
        other_admin = self.admin_with(["manage_enquiries"], "other@example.com")
        url = f"{ADMIN_URL}{self.institute.slug}/activity/"

        # 404 for unknown slug
        self.client.force_authenticate(self.reviewer)
        self.assertEqual(self.client.get(f"{ADMIN_URL}no-such-slug/activity/").status_code, 404)

        # 401 without token
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(url).status_code, 401)

        # 403 without users.manage_institutes
        self.client.force_authenticate(other_admin)
        self.assertEqual(self.client.get(url).status_code, 403)
