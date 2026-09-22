"""Shared fixtures."""
import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from analysis.models import Course

User = get_user_model()


@pytest.fixture
def teacher_a(db):
    return User.objects.create_user(username="teacher_a", password="pw12345!")


@pytest.fixture
def teacher_b(db):
    return User.objects.create_user(username="teacher_b", password="pw12345!")


@pytest.fixture
def course_a(teacher_a):
    course = Course.objects.create(name="Course A", owner=teacher_a)
    course.teachers.add(teacher_a)
    return course


@pytest.fixture
def course_b(teacher_b):
    course = Course.objects.create(name="Course B", owner=teacher_b)
    course.teachers.add(teacher_b)
    return course


@pytest.fixture
def api_a(teacher_a):
    client = APIClient()
    client.force_authenticate(user=teacher_a)
    return client


@pytest.fixture
def api_b(teacher_b):
    client = APIClient()
    client.force_authenticate(user=teacher_b)
    return client


@pytest.fixture
def api_anon():
    return APIClient()


def upload(client, course, files, note=""):
    """POST to the batch upload endpoint; files = [(name, bytes), ...]."""
    data = {"course": str(course.id), "note": note}
    data["files"] = [
        SimpleUploadedFile(name, content) for name, content in files
    ]
    return client.post("/api/batches/upload/", data, format="multipart")
