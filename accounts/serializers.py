from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from rest_framework import serializers
from rest_framework.authtoken.models import Token

from .models import Account, Asset


class RegisterSerializer(serializers.Serializer):
    username = serializers.CharField(min_length=2, max_length=150)
    password = serializers.CharField(min_length=6, max_length=128, write_only=True)

    def create(self, validated):
        return User.objects.create_user(
            username=validated["username"], password=validated["password"]
        )


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        user = authenticate(
            username=attrs["username"], password=attrs["password"]
        )
        if not user:
            raise serializers.ValidationError("用户名或密码错误")
        attrs["user"] = user
        return attrs


class AssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asset
        fields = ["symbol", "name", "is_active"]


class AccountSerializer(serializers.ModelSerializer):
    asset = serializers.CharField(source="asset.symbol")
    total = serializers.DecimalField(max_digits=36, decimal_places=8, read_only=True)

    class Meta:
        model = Account
        fields = ["asset", "available", "frozen", "total"]
