from django.contrib.auth import get_user_model
from rest_framework import generics, permissions, status
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Balance
from apps.accounts.serializers import (
    BalanceSerializer,
    RegisterSerializer,
    TokenSerializer,
)

User = get_user_model()


class RegisterView(APIView):
    """Create a simulated trading account.  No email / KYC needed."""

    authentication_classes = ()
    permission_classes = (permissions.AllowAny,)

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        token = Token.objects.create(user=user)
        return Response(
            {"username": user.username, "token": token.key},
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    """Exchange username/password (HTTP Basic on this endpoint) for a token."""

    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        token, _ = Token.objects.get_or_create(user=request.user)
        return Response(TokenSerializer(token).data)


class BalanceListView(generics.ListAPIView):
    """List the caller's own balances.  Other users are never visible."""

    serializer_class = BalanceSerializer

    def get_queryset(self):
        return (
            Balance.objects.filter(user=self.request.user)
            .select_related("asset")
            .order_by("asset__code")
        )
