from django.contrib.auth import get_user_model
from rest_framework import serializers
from rest_framework.authtoken.models import Token

from apps.accounts.models import Asset, Balance

User = get_user_model()


class RegisterSerializer(serializers.Serializer):
    username = serializers.CharField(min_length=3, max_length=150)
    password = serializers.CharField(min_length=6, max_length=128, write_only=True)

    def validate_username(self, value):
        if User.objects.filter(username=value).exists():
            raise serializers.ValidationError("username already taken")
        return value

    def create(self, validated):
        user = User.objects.create_user(
            username=validated["username"], password=validated["password"]
        )
        return user


class TokenSerializer(serializers.ModelSerializer):
    class Meta:
        model = Token
        fields = ("key",)


class AssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asset
        fields = ("code", "name", "is_active")


class BalanceSerializer(serializers.ModelSerializer):
    asset = serializers.CharField(source="asset.code")

    class Meta:
        model = Balance
        fields = ("asset", "available", "frozen", "updated_at")
