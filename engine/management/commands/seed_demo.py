"""初始化演示数据：教师账号、课程、固定样本。"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from engine.analysis import sample_data
from engine.analysis.extract import content_hash_of
from engine.analysis.pipeline import feature_vector_for_sample
from engine.models import Course, StyleSample


class Command(BaseCommand):
    help = "创建演示教师（demo_teacher / demo1234）、演示课程与固定风格样本。"

    def handle(self, *args, **opts):
        User = get_user_model()
        user, created = User.objects.get_or_create(username="demo_teacher")
        if created:
            user.set_password("demo1234")
            user.save()
        course, course_created = Course.objects.get_or_create(
            name="演示课程：写作风格线索", teacher=user
        )
        n = 0
        for item in sample_data.STUDENT_SAMPLES:
            n += self._add(course, StyleSample.Label.STUDENT, item)
        for item in sample_data.REFERENCE_SAMPLES:
            n += self._add(course, StyleSample.Label.REFERENCE, item)
        self.stdout.write(self.style.SUCCESS(
            f"演示数据就绪（新增样本 {n}）：登录 demo_teacher/demo1234，课程 id={course.id}"
        ))

    def _add(self, course, label, item):
        h = content_hash_of(item["text"])
        if StyleSample.objects.filter(course=course, content_hash=h, label=label).exists():
            return 0
        vec = feature_vector_for_sample(item["text"])
        StyleSample.objects.create(
            course=course, label=label, name=item["name"], content_hash=h,
            text=item["text"], feature_version=vec["version"], feature_vector=vec,
        )
        return 1
