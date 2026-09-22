"""跨课程越权访问：教师只能读取自己课程的文本与结果。"""

from rest_framework.test import APITestCase

from .factories import make_course, make_submission, make_teacher


class AuthorizationTests(APITestCase):
    def setUp(self):
        self.t1 = make_teacher("teacher1")
        self.t2 = make_teacher("teacher2")
        self.c1 = make_course(self.t1, "C1")
        self.c2 = make_course(self.t2, "C2")
        self.submission = make_submission(self.c1, text="Secret essay for course one.")

    def test_other_teacher_gets_404_on_submission(self):
        self.client.force_authenticate(self.t2)
        resp = self.client.get(
            f"/api/courses/{self.c1.id}/submissions/{self.submission.id}/"
        )
        self.assertEqual(resp.status_code, 404)

    def test_other_teacher_cannot_submit_into_course(self):
        from rest_framework.test import APIRequestFactory
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.force_authenticate(self.t2)
        f = SimpleUploadedFile("a.txt", b"intrusion attempt body text here")
        resp = self.client.post(
            f"/api/courses/{self.c1.id}/submit-batch/",
            {"files": [f]},
            format="multipart",
        )
        self.assertEqual(resp.status_code, 404)

    def test_other_teacher_cannot_read_samples_or_results(self):
        self.client.force_authenticate(self.t2)
        self.assertEqual(
            self.client.get(f"/api/courses/{self.c1.id}/samples/").status_code, 404
        )
        self.assertEqual(
            self.client.get(
                f"/api/courses/{self.c1.id}/submissions/{self.submission.id}/results/1/"
            ).status_code,
            404,
        )

    def test_owner_can_access(self):
        self.client.force_authenticate(self.t1)
        resp = self.client.get(
            f"/api/courses/{self.c1.id}/submissions/{self.submission.id}/"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["id"], self.submission.id)

    def test_anonymous_rejected(self):
        resp = self.client.get(
            f"/api/courses/{self.c1.id}/submissions/{self.submission.id}/"
        )
        self.assertEqual(resp.status_code, 401)

    def test_course_list_is_scoped_per_teacher(self):
        self.client.force_authenticate(self.t1)
        resp = self.client.get("/api/courses/")
        ids = {c["id"] for c in resp.data}
        self.assertEqual(ids, {self.c1.id})
