from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import generics, permissions

from .models import CrewMembership, ProjectAssignment
from .permissions import IsAdmin
from .serializers import (
    CrewMembershipSerializer,
    LoginSerializer,
    ProjectAssignmentSerializer,
    UserSerializer,
)


class LoginView(APIView):
    """Exchange username/password for a token used on every other request.

    POST /api/auth/login/  {"username": ..., "password": ...}
    """

    authentication_classes = ()
    permission_classes = (permissions.AllowAny,)

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        token, _ = Token.objects.get_or_create(user=user)
        data = UserSerializer(user).data
        data["token"] = token.key
        return Response(data)


class MeView(APIView):
    def get(self, request):
        return Response(UserSerializer(request.user).data)


class ProjectAssignmentList(generics.ListCreateAPIView):
    queryset = ProjectAssignment.objects.all()
    serializer_class = ProjectAssignmentSerializer
    permission_classes = (IsAdmin,)


class CrewMembershipList(generics.ListCreateAPIView):
    queryset = CrewMembership.objects.all()
    serializer_class = CrewMembershipSerializer
    permission_classes = (IsAdmin,)
