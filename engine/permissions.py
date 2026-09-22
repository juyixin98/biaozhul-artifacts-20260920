from rest_framework import permissions


class IsCourseTeacher(permissions.BasePermission):
    """对象级：只允许课程所属教师访问其课程下的文本、样本与结果。"""

    def has_object_permission(self, request, view, obj):
        course = getattr(obj, "course", obj)
        return course.teacher_id == request.user.id
