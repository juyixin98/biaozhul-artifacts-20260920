"""Projects, crews and versioned form templates.

A :class:`FormTemplateVersion` row is an *immutable snapshot*. Once a version
is published its JSON never changes; every historical submission keeps its
foreign key to the exact version it was validated against, so a reader can
always reconstruct the form as it existed at collection time.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

FIELD_TYPES = ("text", "number", "enum", "date")
ALLOWED_RULE_OPS = ("not_blank", "==", "!=", "in", "not_in")


def validate_field_schema(fields):
    """Validate the ``fields`` array of a template version.

    Raises ``django.core.exceptions.ValidationError`` on the first structural
    problem so that bad versions can never be published.
    """
    from django.conf import settings as dj_settings

    max_fields = dj_settings.MAX_FIELDS_PER_TEMPLATE

    if not isinstance(fields, list) or not fields:
        raise ValidationError("fields must be a non-empty array")
    if len(fields) > max_fields:
        raise ValidationError(f"a template may contain at most {max_fields} fields")

    keys = set()
    for index, field in enumerate(fields):
        where = f"fields[{index}]"
        if not isinstance(field, dict):
            raise ValidationError(f"{where} must be an object")

        key = field.get("key")
        if not isinstance(key, str) or not key:
            raise ValidationError(f"{where}.key is required")
        if not key.replace("_", "").isalnum():
            raise ValidationError(
                f"{where}.key must contain only letters, numbers and underscores"
            )
        if key in keys:
            raise ValidationError(f"duplicate field key: {key}")
        keys.add(key)

        label = field.get("label")
        if not isinstance(label, str) or not label.strip():
            raise ValidationError(f"{where}.label is required")

        ftype = field.get("type")
        if ftype not in FIELD_TYPES:
            raise ValidationError(
                f"{where}.type must be one of {', '.join(FIELD_TYPES)}"
            )

        options = field.get("options")
        if ftype == "enum":
            if not isinstance(options, list) or not options or not all(
                isinstance(o, str) and o for o in options
            ):
                raise ValidationError(
                    f"{where}: enum fields require a non-empty options array of strings"
                )
            if len(set(options)) != len(options):
                raise ValidationError(f"{where}: enum options must be unique")
        elif options is not None:
            raise ValidationError(f"{where}: options are only valid for enum fields")

        required_if = field.get("required_if")
        if required_if is not None:
            _validate_required_if(required_if, keys | {key}, where)

    return fields


def _validate_required_if(rule, known_keys, where):
    """A conditional-required rule references another field.

    Example::

        {"field": "surface", "op": "==", "value": "concrete"}
    """
    if not isinstance(rule, dict):
        raise ValidationError(f"{where}.required_if must be an object")
    ref = rule.get("field")
    op = rule.get("op")
    if not isinstance(ref, str) or not ref:
        raise ValidationError(f"{where}.required_if.field is required")
    if ref not in known_keys:
        raise ValidationError(
            f"{where}.required_if references unknown field {ref!r}"
        )
    if op not in ALLOWED_RULE_OPS:
        raise ValidationError(f"{where}.required_if.op must be one of {ALLOWED_RULE_OPS}")
    if op in ("in", "not_in"):
        if not isinstance(rule.get("value"), list):
            raise ValidationError(
                f"{where}.required_if.value must be a list for operator {op!r}"
            )


class Project(models.Model):
    name = models.CharField(max_length=128, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("name",)

    def __str__(self):
        return self.name


class Crew(models.Model):
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="crews"
    )
    name = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("project", "name")
        ordering = ("project", "name")

    def __str__(self):
        return f"{self.project.name}/{self.name}"


class FormTemplate(models.Model):
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="templates"
    )
    code = models.SlugField(max_length=64)
    name = models.CharField(max_length=128)
    created_at = models.DateTimeField(auto_now_add=True)
    current_version = models.ForeignKey(
        "FormTemplateVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        unique_together = ("project", "code")
        ordering = ("project", "code")

    def __str__(self):
        return f"{self.code}@v{self.current_version_number}"

    @property
    def current_version_number(self):
        return self.current_version.version if self.current_id else None

    def publish_version(self, fields, *, published_by, min_supported_version=None):
        """Freeze a new immutable snapshot and make it current.

        Version numbers are allocated per template inside a row lock so that
        two concurrent publishes can never receive the same number.
        """
        validate_field_schema(fields)
        locked = type(self).objects.select_for_update().get(pk=self.pk)
        last = (
            FormTemplateVersion.objects.filter(template=locked)
            .order_by("-version")
            .first()
        )
        next_version = (last.version + 1) if last else 1
        if min_supported_version is None:
            # By default every previously published version stays acceptable;
            # admins can tighten this explicitly when a field is deleted.
            min_supported_version = 1
        version = FormTemplateVersion.objects.create(
            template=locked,
            version=next_version,
            fields=fields,
            min_supported_version=min_supported_version,
            published_by=published_by,
        )
        locked.current_version = version
        locked.save(update_fields=["current_version"])
        return version


class FormTemplateVersion(models.Model):
    template = models.ForeignKey(
        FormTemplate, on_delete=models.PROTECT, related_name="versions"
    )
    version = models.PositiveIntegerField()
    fields = models.JSONField()
    min_supported_version = models.PositiveIntegerField(default=1)
    published_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="published_template_versions",
    )
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("template", "version")
        ordering = ("template", "-version")

    def __str__(self):
        return f"{self.template.code}@v{self.version}"

    @property
    def field_map(self):
        return {f["key"]: f for f in self.fields}

    def save(self, *args, **kwargs):
        # Immutability: a published snapshot can never be edited. Creation is
        # the only write path (``publish_version`` validates the schema first).
        if self.pk is not None:
            raise ValidationError("published template versions are immutable")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("published template versions are immutable")
