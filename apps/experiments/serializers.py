from rest_framework import serializers

from .models import Experiment, ExperimentAssignment


class ExperimentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Experiment
        fields = (
            "id",
            "placement",
            "name",
            "variant_a_version",
            "variant_b_version",
            "status",
            "created_by",
            "created_at",
            "stopped_at",
        )
        read_only_fields = ("id", "status", "created_by", "created_at", "stopped_at")

    def _placement(self, attrs):
        from apps.catalog.models import Placement

        placement_id = attrs.get("placement")
        if self.instance is not None:
            return self.instance.placement
        if placement_id is None:
            raise serializers.ValidationError({"placement": "required"})
        if isinstance(placement_id, Placement):
            placement = placement_id
        else:
            try:
                placement = Placement.objects.select_related("app").get(
                    id=placement_id
                )
            except (Placement.DoesNotExist, ValueError, TypeError):
                raise serializers.ValidationError(
                    {"placement": "Placement not found."}
                )
        if placement.app.owner_id != self.context["request"].user.id:
            raise serializers.ValidationError(
                {"placement": "Placement not found or not owned by you."}
            )
        return placement

    def validate(self, attrs):
        if self.instance is not None:
            raise serializers.ValidationError("Experiments cannot be edited.")
        placement = self._placement(attrs)
        va = attrs.get("variant_a_version")
        vb = attrs.get("variant_b_version")
        if va is None or vb is None:
            raise serializers.ValidationError(
                "variant_a_version and variant_b_version are required."
            )
        valid_ids = set(placement.versions.values_list("id", flat=True))
        if va.id not in valid_ids or vb.id not in valid_ids:
            raise serializers.ValidationError(
                "Both variant versions must belong to the experiment's placement."
            )
        if va.id == vb.id:
            raise serializers.ValidationError(
                "variant_a_version and variant_b_version must differ."
            )
        # Hand the resolved, ownership-checked object to the service.
        attrs["placement"] = placement
        return attrs


class AssignmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExperimentAssignment
        fields = ("id", "experiment", "user_key_hash", "variant", "assigned_at")
        read_only_fields = fields
