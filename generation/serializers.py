from rest_framework import serializers

from .models import GeneratedImage, GenerationJob, ReferenceImage


class GenerationJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = GenerationJob
        fields = [
            "id", "deck", "status", "total_cards", "completed_cards", "failed_cards",
            "error_message", "started_at", "finished_at", "updated_at",
        ]
        read_only_fields = fields


class GeneratedImageSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = GeneratedImage
        fields = [
            "id", "card", "theme", "visual_prompt", "prompt_json", "image_url",
            "provider", "prompt_version", "created_at",
        ]
        read_only_fields = fields

    def get_image_url(self, obj):
        request = self.context.get("request")
        return request.build_absolute_uri(obj.image.url) if request else obj.image.url


class GeneratedImageSummarySerializer(GeneratedImageSerializer):
    concept_source = serializers.SerializerMethodField()
    themed_name = serializers.SerializerMethodField()
    themed_flavor = serializers.SerializerMethodField()
    frame_style = serializers.SerializerMethodField()

    class Meta:
        model = GeneratedImage
        fields = ["id", "image_url", "theme", "concept_source", "themed_name",
                  "themed_flavor", "frame_style", "created_at"]

    def get_concept_source(self, obj):
        return obj.prompt_json.get("concept_source", "")

    def get_themed_name(self, obj):
        return obj.prompt_json.get("themed_name", "")

    def get_themed_flavor(self, obj):
        return obj.prompt_json.get("themed_flavor", "")

    def get_frame_style(self, obj):
        return obj.prompt_json.get("frame_style")


class TextField(serializers.CharField):
    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class ThemeAssignmentInputSerializer(serializers.Serializer):
    element = TextField(max_length=200)
    category = TextField(max_length=60, allow_blank=True)
    visual_brief = TextField()


class ReferenceImageSerializer(serializers.ModelSerializer):
    image_url = serializers.SerializerMethodField()

    class Meta:
        model = ReferenceImage
        fields = ["id", "image_url", "source_url"]

    def get_image_url(self, obj):
        request = self.context.get("request")
        return request.build_absolute_uri(obj.image.url) if request else obj.image.url
