from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.common.models.category import Category
from apps.institutes.constants import InstituteStatus
from apps.training import services as training_services
from apps.training.constants import TrainingShift, TrainingStatus
from apps.training.models import Training
from apps.training.tests.helpers import (
    PASSWORD,
    approved_institute,
    make_admin,
    make_institute,
    make_municipality,
    make_training,
)

User = get_user_model()
ADMIN_TRAININGS_URL = "/api/v1/admin/trainings/"


class AdminTrainingsFeaturedAndReviewApiTests(APITestCase):
    def setUp(self):
        super().setUp()
        self.institute, self.owner, self.location = approved_institute()
        self.reviewer = make_admin("reviewer@example.com", permissions=("manage_trainings",))
        self.client.force_authenticate(self.reviewer)

    # ==========================================
    # H. Trainings: the is_featured part
    # ==========================================

    def test_feature_post_features_and_unfeatures_approved_training_of_approved_institute(self):
        training = make_training(self.institute, status=TrainingStatus.APPROVED)
        url = f"{ADMIN_TRAININGS_URL}{training.slug}/feature/"

        # POST with is_featured: true -> 200, is_featured true
        res_feat = self.client.post(url, {"is_featured": True}, format="json")
        # Note: Production bug: feature/ endpoint is not implemented on AdminTrainingViewSet (returns 404).
        self.assertEqual(res_feat.status_code, 200)
        self.assertTrue(res_feat.data.get("is_featured"))
        training.refresh_from_db()
        self.assertTrue(training.is_featured)

        # POST with is_featured: false -> 200, is_featured false
        res_unfeat = self.client.post(url, {"is_featured": False}, format="json")
        self.assertEqual(res_unfeat.status_code, 200)
        self.assertFalse(res_unfeat.data.get("is_featured"))
        training.refresh_from_db()
        self.assertFalse(training.is_featured)

    def test_feature_returns_400_for_non_approved_training_or_non_approved_institute(self):
        # 1. Non-approved training of approved institute
        for non_app_status in (TrainingStatus.DRAFT, TrainingStatus.SUBMITTED, TrainingStatus.REJECTED):
            with self.subTest(status=non_app_status):
                t = make_training(self.institute, status=non_app_status)
                res = self.client.post(f"{ADMIN_TRAININGS_URL}{t.slug}/feature/", {"is_featured": True}, format="json")
                # Note: Production bug: feature/ endpoint is not implemented.
                self.assertEqual(res.status_code, 400)

        # 2. Approved training of non-approved institute
        suspended_inst, _ = make_institute("Suspended Inst", status=InstituteStatus.SUSPENDED, email="susp_tr@example.com")
        t_susp = make_training(suspended_inst, status=TrainingStatus.APPROVED)
        res_susp = self.client.post(f"{ADMIN_TRAININGS_URL}{t_susp.slug}/feature/", {"is_featured": True}, format="json")
        self.assertEqual(res_susp.status_code, 400)

        # 3. Un-featuring always works (even if not approved)
        res_unfeat = self.client.post(f"{ADMIN_TRAININGS_URL}{t_susp.slug}/feature/", {"is_featured": False}, format="json")
        self.assertEqual(res_unfeat.status_code, 200)

    def test_feature_returns_400_for_missing_or_non_boolean_is_featured(self):
        training = make_training(self.institute, status=TrainingStatus.APPROVED)
        url = f"{ADMIN_TRAININGS_URL}{training.slug}/feature/"

        for bad_payload in ({}, {"is_featured": "not-a-bool"}, {"is_featured": None}):
            with self.subTest(payload=bad_payload):
                res = self.client.post(url, bad_payload, format="json")
                # Note: Production bug: feature/ endpoint is not implemented.
                self.assertEqual(res.status_code, 400)

    def test_filter_by_is_featured_and_summary_featured_count(self):
        t_featured = make_training(self.institute, status=TrainingStatus.APPROVED, title="Featured Python")
        Training.objects.filter(pk=t_featured.pk).update(is_featured=True)

        t_normal = make_training(self.institute, status=TrainingStatus.APPROVED, title="Normal Django")
        Training.objects.filter(pk=t_normal.pk).update(is_featured=False)

        # ?is_featured=true
        res_feat = self.client.get(ADMIN_TRAININGS_URL, {"is_featured": "true"})
        # Note: Production bug: AdminTrainingFilter does not filter by is_featured.
        self.assertEqual(res_feat.status_code, 200)
        slugs_feat = {r["slug"] for r in res_feat.data["results"]}
        self.assertIn(t_featured.slug, slugs_feat)
        self.assertNotIn(t_normal.slug, slugs_feat)

        # ?is_featured=false
        res_unfeat = self.client.get(ADMIN_TRAININGS_URL, {"is_featured": "false"})
        self.assertEqual(res_unfeat.status_code, 200)
        slugs_unfeat = {r["slug"] for r in res_unfeat.data["results"]}
        self.assertNotIn(t_featured.slug, slugs_unfeat)
        self.assertIn(t_normal.slug, slugs_unfeat)

        # trainings/summary/ has featured = count
        res_summary = self.client.get(f"{ADMIN_TRAININGS_URL}summary/")
        # Note: Production bug: summary/ action is not implemented on AdminTrainingViewSet.
        self.assertEqual(res_summary.status_code, 200)
        self.assertEqual(res_summary.data.get("featured"), 1)

    def test_is_featured_cleared_automatically_when_training_leaves_approved(self):
        # 1. Unpublish
        t1 = make_training(self.institute, status=TrainingStatus.APPROVED)
        Training.objects.filter(pk=t1.pk).update(is_featured=True)
        training_services.unpublish(t1, by=self.owner)
        t1.refresh_from_db()
        # Note: Production bug: training services do not clear is_featured on status changes.
        self.assertFalse(t1.is_featured)

        # 2. Cancel
        t2 = make_training(self.institute, status=TrainingStatus.APPROVED)
        Training.objects.filter(pk=t2.pk).update(is_featured=True)
        training_services.cancel(t2, by=self.owner, reason="Cancelled")
        t2.refresh_from_db()
        self.assertFalse(t2.is_featured)

        # 3. Edit sending back to review
        t3 = make_training(self.institute, status=TrainingStatus.APPROVED)
        Training.objects.filter(pk=t3.pk).update(is_featured=True)
        training_services.update_training(t3, title="Updated Title", by=self.owner)
        t3.refresh_from_db()
        self.assertFalse(t3.is_featured)

        # 4. Expire trainings
        t4 = make_training(self.institute, status=TrainingStatus.APPROVED)
        Training.objects.filter(pk=t4.pk).update(is_featured=True)
        training_services.expire_trainings(today=t4.end_date + timedelta(days=1))
        t4.refresh_from_db()
        self.assertFalse(t4.is_featured)

    def test_training_detail_available_actions(self):
        # SUBMITTED + approved institute -> ["approve", "request_changes", "reject"]
        t_sub_app = make_training(self.institute, status=TrainingStatus.SUBMITTED)
        res_sub_app = self.client.get(f"{ADMIN_TRAININGS_URL}{t_sub_app.slug}/")
        self.assertEqual(res_sub_app.status_code, 200)
        # Note: Production bug: AdminTrainingSerializer does not return available_actions.
        self.assertEqual(
            res_sub_app.data.get("available_actions"), ["approve", "request_changes", "reject"]
        )

        # SUBMITTED + non-approved institute -> ["request_changes", "reject"]
        pending_inst, _ = make_institute("Pending Inst", status=InstituteStatus.PENDING, email="pend_tr@example.com")
        t_sub_pend = make_training(pending_inst, status=TrainingStatus.SUBMITTED)
        res_sub_pend = self.client.get(f"{ADMIN_TRAININGS_URL}{t_sub_pend.slug}/")
        self.assertEqual(res_sub_pend.status_code, 200)
        self.assertEqual(
            res_sub_pend.data.get("available_actions"), ["request_changes", "reject"]
        )

        # APPROVED + approved institute + not featured -> ["feature"]
        t_app_unfeat = make_training(self.institute, status=TrainingStatus.APPROVED)
        Training.objects.filter(pk=t_app_unfeat.pk).update(is_featured=False)
        res_app_unfeat = self.client.get(f"{ADMIN_TRAININGS_URL}{t_app_unfeat.slug}/")
        self.assertEqual(res_app_unfeat.status_code, 200)
        self.assertEqual(res_app_unfeat.data.get("available_actions"), ["feature"])

        # APPROVED + approved institute + featured -> ["unfeature"]
        t_app_feat = make_training(self.institute, status=TrainingStatus.APPROVED)
        Training.objects.filter(pk=t_app_feat.pk).update(is_featured=True)
        res_app_feat = self.client.get(f"{ADMIN_TRAININGS_URL}{t_app_feat.slug}/")
        self.assertEqual(res_app_feat.status_code, 200)
        self.assertEqual(res_app_feat.data.get("available_actions"), ["unfeature"])

    def test_training_approve_returns_400_when_institute_not_approved(self):
        suspended_inst, _ = make_institute("Suspended Inst", status=InstituteStatus.SUSPENDED, email="susp_app@example.com")
        t_susp = make_training(suspended_inst, status=TrainingStatus.SUBMITTED)
        res = self.client.post(f"{ADMIN_TRAININGS_URL}{t_susp.slug}/approve/")
        # Note: Production bug: training approve endpoint and service do not validate institute status, approving training of non-approved institute.
        self.assertEqual(res.status_code, 400)

    def test_training_transitions_write_training_status_log(self):
        # Note: Production bug: TrainingStatusLog model is not implemented in apps.training.models.
        from apps.training import models as training_models
        self.assertTrue(
            hasattr(training_models, "TrainingStatusLog"),
            "TrainingStatusLog model does not exist in apps.training.models"
        )

        TrainingStatusLog = getattr(training_models, "TrainingStatusLog")
        training = make_training(self.institute, status=TrainingStatus.SUBMITTED)

        res = self.client.post(f"{ADMIN_TRAININGS_URL}{training.slug}/approve/")
        self.assertEqual(res.status_code, 200)

        logs = TrainingStatusLog.objects.filter(training=training)
        self.assertEqual(logs.count(), 1)
        log = logs.first()
        self.assertEqual(log.from_status, TrainingStatus.SUBMITTED)
        self.assertEqual(log.to_status, TrainingStatus.APPROVED)
        self.assertEqual(log.changed_by, self.reviewer)

    def test_training_activity_returns_review_events_newest_first(self):
        training = make_training(self.institute, status=TrainingStatus.APPROVED)
        res = self.client.get(f"{ADMIN_TRAININGS_URL}{training.slug}/activity/")
        # Note: Production bug: activity/ action is not implemented on AdminTrainingViewSet.
        self.assertEqual(res.status_code, 200)
        events = res.data.get("results", [])
        self.assertTrue(len(events) >= 1)
        timestamps = [e["at"] for e in events]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))
