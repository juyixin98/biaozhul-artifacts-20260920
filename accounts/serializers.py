from django.contrib.auth import authenticate
from rest_framework import serializers

from .models import CrewMembership, Profile, ProjectAssignment, Role


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(trim_whitespace=False)

    def validate(self, attrs):
        user = authenticate(
            username=attrs["username"], password=attrs["password"]
        )
        if user is None or not user.is_active:
            raise serializers.ValidationError("invalid_credentials")
        attrs["user"] = user
        return attrs


class UserSerializer(serializers.Serializer):
    id = serializers.IntegerField(read_only=True)
    username = serializers.CharField(read_only=True)
    role = serializers.SerializerMethodField()
    display_name = serializers.SerializerMethodField()
    token = serializers.SerializerMethodField()
    project_ids = serializers.SerializerMethodField()
    crew_ids = serializers.SerializerMethodField()

    def get_role(self, obj):
        return obj.profile.role if hasattr(obj, "profile") else Role.WORKER

    def get_display_name(self, obj):
        return getattr(getattr(obj, "profile", None), "display_name", "") or obj.username

    def get_token(self, obj):
        return getattr(getattr(obj, "auth_token", None), "key", None)

    def get_project_ids(self, obj):
        return list(obj.project_assignments.values_list("project_id", flat=True))

    def get_crew_ids(self, obj):
        return list(obj.crew_memberships.values_list("crew_id", flat=True))


class ProjectAssignmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectAssignment
        fields = ("id", "user", "project")


class CrewMembershipSerializer(serializers.ModelSerializer):
    class Meta:
        model = CrewMembership
        fields = ("id", "user", "crew")
