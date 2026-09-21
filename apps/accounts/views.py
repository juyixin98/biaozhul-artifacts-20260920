from django.contrib.auth import authenticate
from rest_framework import status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .serializers import DeveloperSerializer, LoginSerializer


class DeveloperViewSet(viewsets.GenericViewSet):
    """Register developers and issue API tokens.

    POST /api/auth/register  -> create account, returns token
    POST /api/auth/login     -> exchange credentials for a token
    POST /api/auth/logout    -> delete the current token (auth required)
    """

    serializer_class = DeveloperSerializer

    @action(detail=False, methods=["post"], permission_classes=[AllowAny])
    def register(self, request):
        serializer = DeveloperSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        token, _ = Token.objects.get_or_create(user=user)
        return Response(
            {
                "developer": {
                    "id": user.id,
                    "username": user.username,
                    "email": user.email,
                },
                "token": token.key,
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["post"], permission_classes=[AllowAny])
    def login(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = authenticate(
            request,
            username=serializer.validated_data["username"],
            password=serializer.validated_data["password"],
        )
        if user is None:
            return Response(
                {"message": "Invalid credentials", "errors": None},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key})

    @action(detail=False, methods=["post"], url_path="logout")
    def logout(self, request):
        Token.objects.filter(user=request.user).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
