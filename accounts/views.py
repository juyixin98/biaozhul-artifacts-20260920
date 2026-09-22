"""Authentication endpoints: self-serve registration and token retrieval."""
from rest_framework import serializers
from rest_framework.authtoken.models import Token
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Developer


class RegisterSerializer(serializers.Serializer):
    username = serializers.CharField(min_length=3, max_length=150)
    email = serializers.EmailField(required=False, allow_blank=True, default="")
    password = serializers.CharField(min_length=8, max_length=128, write_only=True)
    company_name = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate_username(self, value):
        if Developer.objects.filter(username=value).exists():
            raise serializers.ValidationError("username already taken")
        return value

    def create(self, validated_data):
        developer = Developer.objects.create_user(
            username=validated_data["username"],
            email=validated_data.get("email", ""),
            password=validated_data["password"],
            company_name=validated_data.get("company_name", ""),
        )
        return developer


class RegisterView(APIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        developer = serializer.save()
        token, _ = Token.objects.get_or_create(user=developer)
        return Response(
            {
                "developer_id": developer.id,
                "username": developer.username,
                "token": token.key,
            },
            status=201,
        )


class DeveloperObtainTokenView(ObtainAuthToken):
    """Standard DRF token endpoint; returns the token plus username."""

    def post(self, request, *args, **kwargs):
        serializer = self.serializer_class(
            data=request.data, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key, "username": user.username})
