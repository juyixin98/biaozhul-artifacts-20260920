from rest_framework import serializers

from common.audit import record_audit

from .models import AdNetwork, App, Placement


class AppSerializer(serializers.ModelSerializer):
    class Meta:
        model = App
        fields = [
            "id",
            "name",
            "bundle_id",
            "platform",
            "api_key",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["api_key", "created_at", "updated_at"]

    def validate(self, attrs):
        qs = App.objects.filter(
            developer=self.context["request"].user,
            bundle_id=attrs.get("bundle_id"),
        )
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                {"bundle_id": "you already have an app with this bundle_id"}
            )
        return attrs

    def create(self, validated_data):
        app = App.objects.create(
            developer=self.context["request"].user, **validated_data
        )
        record_audit(
            developer=self.context["request"].user,
            action="create",
            resource_type="app",
            resource_id=app.id,
            resource_repr=app.name,
            diff={
                "name": app.name,
                "bundle_id": app.bundle_id,
                "platform": app.platform,
            },
        )
        return app

    def update(self, instance, validated_data):
        changed = {}
        for field, value in validated_data.items():
            if getattr(instance, field) != value:
                changed[field] = {"old": getattr(instance, field), "new": value}
                setattr(instance, field, value)
        instance.save()
        if changed:
            record_audit(
                developer=self.context["request"].user,
                action="update",
                resource_type="app",
                resource_id=instance.id,
                resource_repr=instance.name,
                diff=changed,
            )
        return instance


class AdNetworkSerializer(serializers.ModelSerializer):
    class Meta:
        model = AdNetwork
        fields = ["id", "name", "code", "is_active", "created_at", "updated_at"]
        read_only_fields = ["created_at", "updated_at"]

    def validate(self, attrs):
        qs = AdNetwork.objects.filter(
            developer=self.context["request"].user, code=attrs.get("code")
        )
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(
                {"code": "you already have a network with this code"}
            )
        return attrs

    def create(self, validated_data):
        network = AdNetwork.objects.create(
            developer=self.context["request"].user, **validated_data
        )
        record_audit(
            developer=self.context["request"].user,
            action="create",
            resource_type="ad_network",
            resource_id=network.id,
            resource_repr=network.name,
            diff={"name": network.name, "code": network.code},
        )
        return network

    def update(self, instance, validated_data):
        changed = {}
        for field, value in validated_data.items():
            if getattr(instance, field) != value:
                changed[field] = {"old": getattr(instance, field), "new": value}
                setattr(instance, field, value)
        instance.save()
        if changed:
            record_audit(
                developer=self.context["request"].user,
                action="update",
                resource_type="ad_network",
                resource_id=instance.id,
                resource_repr=instance.name,
                diff=changed,
            )
        return instance


class PlacementSerializer(serializers.ModelSerializer):
    app_id = serializers.PrimaryKeyRelatedField(
        queryset=App.objects.all(), source="app", write_only=True
    )

    class Meta:
        model = Placement
        fields = [
            "id",
            "app_id",
            "name",
            "placement_key",
            "ad_type",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]

    def validate_app_id(self, app):
        request = self.context["request"]
        if app.developer_id != request.user.id:
            raise serializers.ValidationError(
                "you cannot create placements under another developer's app"
            )
        return app

    def validate(self, attrs):
        app = attrs.get("app") or getattr(self.instance, "app", None)
        key = attrs.get("placement_key")
        if app is not None and key is not None:
            qs = Placement.objects.filter(app=app, placement_key=key)
            if self.instance is not None:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError(
                    {"placement_key": "this key already exists for the app"}
                )
        return attrs

    def create(self, validated_data):
        placement = Placement.objects.create(**validated_data)
        record_audit(
            developer=self.context["request"].user,
            action="create",
            resource_type="placement",
            resource_id=placement.id,
            resource_repr=placement.placement_key,
            diff={
                "app_id": placement.app_id,
                "placement_key": placement.placement_key,
                "ad_type": placement.ad_type,
            },
        )
        return placement

    def update(self, instance, validated_data):
        changed = {}
        for field, value in validated_data.items():
            if getattr(instance, field) != value:
                changed[field] = {"old": getattr(instance, field), "new": value}
                setattr(instance, field, value)
        instance.save()
        if changed:
            record_audit(
                developer=self.context["request"].user,
                action="update",
                resource_type="placement",
                resource_id=instance.id,
                resource_repr=instance.placement_key,
                diff=changed,
            )
        return instance
