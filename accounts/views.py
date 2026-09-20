"""账户相关视图。普通用户只能看到自己的资产。"""
from django.contrib.auth.models import User
from rest_framework import status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Account
from .serializers import (
    AccountSerializer,
    LoginSerializer,
    RegisterSerializer,
)


class RegisterView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        ser = RegisterSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = ser.save()
        token, _ = Token.objects.get_or_create(user=user)
        return Response(
            {"username": user.username, "token": token.key},
            status=status.HTTP_201_CREATED,
        )


class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        ser = LoginSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = ser.validated_data["user"]
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"username": user.username, "token": token.key})


class MyAccountsView(APIView):
    """当前用户自己的资产，任何用户不能查看他人账户。"""

    def get(self, request):
        qs = (
            Account.objects.filter(user=request.user, account_type="USER")
            .select_related("asset")
            .order_by("asset__symbol")
        )
        return Response(AccountSerializer(qs, many=True).data)


class MyLedgerView(APIView):
    """当前用户自己的账本流水（追加式，只暴露本人账户的分录）。"""

    def get(self, request):
        from .models import LedgerEntry

        qs = (
            LedgerEntry.objects.filter(account__user=request.user)
            .select_related("asset")
            .order_by("-id")[:200]
        )
        data = [
            {
                "id": e.pk,
                "tx_group": str(e.tx_group),
                "asset": e.asset.symbol,
                "amount": str(e.amount),
                "memo": e.memo,
                "ref_type": e.ref_type,
                "ref_id": e.ref_id,
                "created_at": e.created_at,
            }
            for e in qs
        ]
        return Response(data)
